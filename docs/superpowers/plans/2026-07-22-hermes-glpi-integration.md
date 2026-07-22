# Hermes ↔ GLPI Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the `hermes-glpi` Hermes agent to the local GLPI instance so it triages new tickets (reply from the KB when confident, otherwise leave an internal note) and capitalizes resolved tickets back into GLPI's Knowledge Base.

**Architecture:** GLPI's native outbound webhooks (Ticket New/Update) fire into a small internal relay (`glpi-webhook-relay`) that re-signs the payload in the HMAC format Hermes expects and forwards it to Hermes's webhook platform. Hermes runs the `glpi-ticket-triage` skill, which calls a purpose-built MCP server (`glpi`) exposing exactly six GLPI actions (search/read tickets, reply, resolve, search/create KB articles).

**Tech Stack:** Python 3 (stdlib `http.server`/`hmac` for the relay; `mcp` SDK's `FastMCP` + `requests` for the MCP server, both already present in the `hermes-glpi` image's venv), Docker Compose, GLPI REST API v2.3 (OAuth2 password grant).

## Global Constraints

- GLPI REST API base: `http://localhost:8080/api.php/v2.3` (reachable from `hermes-glpi` because it runs with `network_mode: host` and GLPI publishes port 8080).
- GLPI API v2.3 auth is OAuth2 password grant only — `client_id`/`client_secret`/`username`/`password`, token endpoint `POST /token`, `expires_in: 3600`, refresh via `grant_type=refresh_token`.
- The `hermes-bot` GLPI account has no delete rights (verified) — the MCP server must never expose a delete action, matching that boundary.
- GLPI signs webhooks with `X-GLPI-signature`/`X-GLPI-timestamp`, which Hermes's webhook adapter does not recognize (it only accepts GitHub/GitLab/Svix/its own Generic V1-V2 header names) — every GLPI webhook must go through the `glpi-webhook-relay`, never straight to Hermes.
- Hermes's webhook `--events` filter matches `payload["event_type"]` or `payload["type"]` — GLPI's payload uses the key `"event"`, which those checks never match. **Do not pass `--events` when creating the subscription** — it would cause every delivery to be silently ignored. Branch on the `event` field inside the skill instead.
- `mcp_servers.<name>.env` values support `${VAR}` interpolation from the process environment (confirmed in `/opt/hermes/tools/mcp_tool.py`) — reference the existing `docker-compose.yml` env vars (`GLPI_API_URL`, `GLPI_OAUTH_CLIENT_ID`, `GLPI_OAUTH_CLIENT_SECRET`, `GLPI_USER`, `GLPI_PASSWORD`) this way; never duplicate the literal secret values into `config.yaml`.
- `hermes-bot`'s Technician profile must have the `KNOWBASEADMIN` right (bit `1024` on the `knowbase` row in `glpi_profilerights`) or every KB article it creates via the API is invisible to every future read, including its own `search_kb` calls — the API never sets `users_id` or any visibility target when creating a `KnowbaseItem`, and GLPI's visibility rule excludes untargeted articles from everyone except the `KNOWBASEADMIN` bypass. See the design spec's Environment section for the full explanation; discovered and fixed during Task 6.
- Full background and every validated fact this plan relies on: `docs/superpowers/specs/2026-07-22-hermes-glpi-integration-design.md`.

---

## Task 1: Signing relay — pure function and HTTP handler

**Files:**
- Create: `glpi-integration/relay/relay.py`
- Test: `glpi-integration/relay/test_relay.py`

**Interfaces:**
- Produces: `relay.compute_v2_signature(secret: bytes, timestamp: str, body: bytes) -> str`; `relay.RelayHandler` (an `http.server.BaseHTTPRequestHandler` subclass); `relay.run_server()`; module globals `relay.TARGET_URL: str`, `relay.SECRET: bytes`, `relay.LISTEN_PORT: int` (read from `RELAY_TARGET_URL`/`RELAY_SECRET`/`RELAY_LISTEN_PORT` env vars, defaulting to `""`/`b""`/`8080` so the module is importable without those env vars set — required for testing).

- [ ] **Step 1: Write the failing tests**

Create `glpi-integration/relay/test_relay.py`:

```python
import hashlib
import hmac
import json
import threading
import unittest
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

import relay


class ComputeV2SignatureTests(unittest.TestCase):
    def test_known_vector(self):
        secret = b"test-secret"
        timestamp = "1700000000"
        body = b'{"hello":"world"}'
        expected = hmac.new(
            secret, timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        self.assertEqual(relay.compute_v2_signature(secret, timestamp, body), expected)

    def test_different_bodies_produce_different_signatures(self):
        secret = b"test-secret"
        timestamp = "1700000000"
        sig_a = relay.compute_v2_signature(secret, timestamp, b"a")
        sig_b = relay.compute_v2_signature(secret, timestamp, b"b")
        self.assertNotEqual(sig_a, sig_b)


class _CapturingTargetHandler(BaseHTTPRequestHandler):
    """Fake 'Hermes' endpoint that records the last request it received."""

    captured = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _CapturingTargetHandler.captured = {
            "body": body,
            "headers": dict(self.headers.items()),
        }
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


class RelayEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.target_server = HTTPServer(("127.0.0.1", 0), _CapturingTargetHandler)
        self.target_port = self.target_server.server_port
        threading.Thread(target=self.target_server.serve_forever, daemon=True).start()

        relay.TARGET_URL = f"http://127.0.0.1:{self.target_port}/webhooks/glpi-ticket"
        relay.SECRET = b"shared-secret"

        self.relay_server = HTTPServer(("127.0.0.1", 0), relay.RelayHandler)
        self.relay_port = self.relay_server.server_port
        threading.Thread(target=self.relay_server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.relay_server.shutdown()
        self.target_server.shutdown()

    def test_forwards_with_correct_signature(self):
        payload = json.dumps({"item": {"id": 1}, "event": "new"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.relay_port}/relay",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)

        captured = _CapturingTargetHandler.captured
        self.assertEqual(captured["body"], payload)
        timestamp = captured["headers"]["X-Webhook-Timestamp"]
        expected_sig = relay.compute_v2_signature(b"shared-secret", timestamp, payload)
        self.assertEqual(captured["headers"]["X-Webhook-Signature-V2"], expected_sig)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd glpi-integration/relay && python3 -m unittest test_relay -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'relay'` (the module doesn't exist yet).

- [ ] **Step 3: Write `relay.py`**

Create `glpi-integration/relay/relay.py`:

```python
#!/usr/bin/env python3
"""Relay GLPI's outbound webhooks to Hermes, re-signed in Hermes's
Generic V2 HMAC scheme.

GLPI signs its webhooks with X-GLPI-signature/X-GLPI-timestamp, a format
Hermes's webhook adapter does not recognize (it only matches GitHub,
GitLab, Svix, or its own X-Webhook-Signature[-V2] header names). This
relay sits between the two: it trusts the request because it is only
reachable from the `glpi` container on the private compose network (no
port is published to the host), and re-signs the untouched body so
Hermes accepts it.
"""

import hashlib
import hmac
import http.server
import os
import sys
import time
import urllib.error
import urllib.request

LISTEN_PORT = int(os.environ.get("RELAY_LISTEN_PORT", "8080"))
TARGET_URL = os.environ.get("RELAY_TARGET_URL", "")
SECRET = os.environ.get("RELAY_SECRET", "").encode()


def compute_v2_signature(secret: bytes, timestamp: str, body: bytes) -> str:
    """Hermes Generic V2 signature: hex HMAC-SHA256 of "<timestamp>.<body>"."""
    signed_content = timestamp.encode() + b"." + body
    return hmac.new(secret, signed_content, hashlib.sha256).hexdigest()


class RelayHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)

        timestamp = str(int(time.time()))
        signature = compute_v2_signature(SECRET, timestamp, body)

        req = urllib.request.Request(
            TARGET_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature-V2": signature,
                "X-Webhook-Timestamp": timestamp,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except urllib.error.URLError as exc:
            print(f"[relay] forward failed: {exc}", file=sys.stderr)
            self.send_response(502)
            self.end_headers()
            return

        self.send_response(status)
        self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[relay] {self.address_string()} - {fmt % args}", file=sys.stderr)


def run_server():
    if not TARGET_URL or not SECRET:
        raise SystemExit("RELAY_TARGET_URL and RELAY_SECRET must be set")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), RelayHandler)
    print(f"[relay] listening on 0.0.0.0:{LISTEN_PORT}, forwarding to {TARGET_URL}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd glpi-integration/relay && python3 -m unittest test_relay -v`
Expected:
```
test_different_bodies_produce_different_signatures ... ok
test_known_vector ... ok
test_forwards_with_correct_signature ... ok

OK
```

---

## Task 2: Deploy the relay via docker-compose

**Files:**
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `glpi-integration/relay/relay.py` (Task 1) — run as `python3 /app/relay.py` inside the new service.
- Produces: a running `glpi-webhook-relay` service reachable from the `glpi` container at `http://glpi-webhook-relay/relay` (port 80 — see the note in Step 2 on why), and a `$RELAY_SECRET` value that Task 4 must reuse verbatim when creating the Hermes webhook subscription.

- [ ] **Step 1: Generate the shared HMAC secret**

Run: `openssl rand -hex 32`
Expected: a 64-character hex string, e.g. `a3f1...` — copy it, it is used twice below (as `RELAY_SECRET` here, and as `--secret` in Task 4).

- [ ] **Step 2: Edit `docker-compose.yml`**

Move `extra_hosts` off the `glpi` service (it no longer needs to reach the host directly — it will call the relay by its compose service name) and add the new `glpi-webhook-relay` service, which does need to reach the host:

```yaml
  glpi:
    image: glpi/glpi:latest
    restart: "no"
    volumes:
      # Using a named volume avoids permission issues on host (automatically managed by Docker)
      - glpi_data:/var/glpi
    environment:
      - GLPI_DB_HOST=db
      - GLPI_DB_NAME=glpi
      - GLPI_DB_USER=glpi
      - GLPI_DB_PASSWORD=glpi
      - GLPI_DB_PORT=3306
      - GLPI_INSTALL_MODE=DOCKER
    ports:
      - "8080:80"
    depends_on:
      - db

  glpi-webhook-relay:
    image: python:3.13-alpine
    container_name: glpi-webhook-relay
    restart: unless-stopped
    command: python3 /app/relay.py
    volumes:
      - ./glpi-integration/relay:/app:ro
    environment:
      # Port 80, not 8080: GLPI's outbound-webhook SSRF allowlist regex
      # (Toolbox::isUrlSafe, checked against GLPI_SERVERSIDE_URL_ALLOWLIST)
      # has no port-matching group at all — any webhook target URL with an
      # explicit :port is unconditionally rejected as "unsafe" before GLPI
      # even attempts the request, regardless of hostname. Using the
      # default HTTP port keeps the target URL port-free so GLPI accepts it.
      - RELAY_LISTEN_PORT=80
      - RELAY_TARGET_URL=http://host.docker.internal:8644/webhooks/glpi-ticket
      - RELAY_SECRET=<paste the secret generated in Step 1>
    extra_hosts:
      - "host.docker.internal:host-gateway"   # so the relay can reach Hermes on the host
```

(i.e.: delete the 2-line `extra_hosts:` block that was under `glpi`, and insert the whole `glpi-webhook-relay` service block — placed between `glpi` and `db` or anywhere at the same indentation level under `services:`.)

- [ ] **Step 3: Start the relay**

Run: `docker compose up -d glpi glpi-webhook-relay`
Expected: `Container glpi-webhook-relay Started`, `Container glpi_docker_dev-glpi-1 Recreated` (recreated because `extra_hosts` changed).

- [ ] **Step 4: Verify the relay is listening and reachable from `glpi`**

Run: `docker exec glpi_docker_dev-glpi-1 curl -s -o /dev/null -w "HTTP %{http_code}\n" http://glpi-webhook-relay/relay`
Expected: `HTTP 501` (Not Implemented — `relay.py` only defines `do_POST`, so Python's stdlib `BaseHTTPRequestHandler` falls back to its default 501 response for any other verb. This confirms DNS resolution and the port both work; the exact non-2xx code is incidental, what matters is that the request reached the relay's handler at all).


---

## Task 3: GLPI-side webhook definitions

**Files:** none (GLPI UI configuration only).

**Interfaces:**
- Consumes: the relay URL from Task 2 (`http://glpi-webhook-relay/relay` — port 80, no explicit port in the URL; see Task 2 Step 2's note on GLPI's SSRF allowlist rejecting any URL with a `:port`).
- Produces: two active GLPI webhook definitions whose queued deliveries Task 6's end-to-end test depends on.

- [ ] **Step 1: Delete the throwaway test webhook**

In GLPI: Setup → Webhooks → open `hermes-capture-test` → delete it (it was created only to capture a sample payload during design and points nowhere useful now).

- [ ] **Step 2: Create the "New" webhook**

Setup → Webhooks → **+**:
- Name: `hermes-ticket-new`
- Active: Yes
- Itemtype: `Ticket`
- Event: `New`
- URL: `http://glpi-webhook-relay/relay` (no `:80` needed — it's the default HTTP port)
- HTTP method: POST
- Number of retries: `3` (GLPI's own retry covers transient relay/Hermes downtime, per the design's error-handling section)

Save.

- [ ] **Step 3: Create the "Update" webhook**

Same as Step 2, but:
- Name: `hermes-ticket-update`
- Event: `Update`

Save.

- [ ] **Step 4: Verify dispatch end-to-end (payload only — signature is checked in Task 6)**

Create a throwaway test ticket in GLPI (any title/content). Wait up to 90 seconds (GLPI's `QueuedWebhook` cron runs every 60s).

Run: `docker exec glpi_docker_dev-db-1 mariadb -uglpi -pglpi glpi -e "SELECT id,itemtype,event,url,sent_try,sent_time,last_status_code FROM glpi_queuedwebhooks ORDER BY id DESC LIMIT 3;"`
Expected: a new row with `event=new`, `url=http://glpi-webhook-relay/relay`, `sent_try=1`, `sent_time` populated (non-NULL) — confirms GLPI successfully reached the relay (a connection failure, or GLPI's SSRF check rejecting the URL, would leave `sent_time` NULL and `sent_try` incrementing on each cron pass — check `/var/glpi/logs/webhook.log` inside the `glpi` container if that happens). `last_status_code` may be `404` at this stage — that's expected and correct, not a bug: Task 4 hasn't created Hermes's `/webhooks/glpi-ticket` route yet, so Hermes has nothing to answer with until then. The full 2xx path is verified in Task 6, after Task 4.

Leave this test ticket for now — Task 6 covers full cleanup.

---

## Task 4: Hermes webhook subscription and triage skill

**Files:**
- Create: `glpi-integration/skills/glpi-ticket-triage/SKILL.md`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: the `RELAY_SECRET` value from Task 2 Step 1 (must be passed as `--secret` below — the relay and the Hermes subscription must share the exact same secret).
- Produces: an active `glpi-ticket` Hermes webhook subscription at `http://localhost:8644/webhooks/glpi-ticket`, which Task 6's end-to-end test triggers.

- [ ] **Step 1: Write the skill**

Create `glpi-integration/skills/glpi-ticket-triage/SKILL.md`:

```markdown
---
name: glpi-ticket-triage
description: "GLPI ticket triage: reply from KB when confident, else internal note; capitalize resolved tickets into the KB."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [glpi, helpdesk, itsm, knowledge-base]
---

# GLPI Ticket Triage

Loaded for every run triggered by the `glpi-ticket` webhook subscription. The
webhook payload is a GLPI Ticket event: `{"item": {...ticket...}, "event":
"new"|"update"}`. Use the `mcp__glpi__*` tools (from the `glpi` MCP server) for
every action on GLPI — never call the GLPI REST API directly.

## Decision policy

### `event: "new"`

1. Read the ticket's `name` (title) and `content` (description) from the
   payload.
2. Call `mcp__glpi__search_kb` with an RSQL filter built from the ticket's key
   terms, e.g. `name=like="*<keyword>*"`. If that returns nothing, also try
   `content=like="*<keyword>*"`.
3. Decide:
   - **Confident match** — the KB article clearly answers this exact
     request, not just a loosely related topic: reply directly with
     `mcp__glpi__add_followup(ticket_id=<item.id>, content=<answer drawn from
     the KB article>, is_private=False)`.
   - **No confident match, or the request needs ticket-specific info** (asset
     details, account access, something only a technician can check):
     `mcp__glpi__add_followup(ticket_id=<item.id>, content=<your diagnosis and
     a suggested next step for the technician>, is_private=True)`. Do not
     guess a public reply in this case — an internal note is always the safe
     default.
4. Never call `mcp__glpi__add_solution` or otherwise resolve/close the ticket
   in this step — only a human, or the resolution step below, does that.

### `event: "update"` where the ticket's status just changed to "Solved"

The payload's `item.status.name` is `"Solved"` (or the equivalent in GLPI's
configured language) when this applies. Skip this branch entirely for any
other status value.

1. Call `mcp__glpi__search_kb` using the ticket's title/content, same as above.
2. If a clearly-matching article already exists: do nothing (avoid duplicate
   KB entries).
3. If none exists: call `mcp__glpi__create_kb_article` with:
   - `name`: a short, reusable title for the underlying issue — generalize
     it rather than copying the ticket's own title verbatim if it is too
     specific or personal (e.g. "Ticket #123: mon imprimante ne marche pas"
     → "Résoudre un problème d'impression réseau").
   - `content`: the general problem plus the solution, written to stand
     alone without needing the original ticket for context.
   - `is_faq`: `False` — leave end-user-facing FAQ visibility to a human
     reviewer; this only populates the internal KB.

## Guardrails

- The only write actions that are ever appropriate here are `add_followup`
  and `create_kb_article` (plus `search_kb`/`search_tickets`/`get_ticket`
  for reads). Never attempt to delete a ticket, reassign it, or touch
  user/rights data — the `glpi` MCP server does not expose those actions,
  and the underlying GLPI account does not have the rights for them either.
- When unsure whether a match is confident enough for a public reply,
  default to a private/internal followup instead. A wrong technician-facing
  note is easy to correct; a wrong public reply reaches the requester
  directly.
```

- [ ] **Step 2: Mount the skill into the Hermes container**

In `docker-compose.yml`, under the `hermes-glpi` service's `volumes:`, add a line so the skill directory lands exactly where Hermes scans for skills (nested inside the existing `.hermes-work` mount — Docker supports mounting a more specific path on top of a broader one):

```yaml
    volumes:
      - ./.hermes-work:/opt/data
      - ./glpi-integration/skills/glpi-ticket-triage:/opt/data/skills/devops/glpi-ticket-triage:ro
```

- [ ] **Step 3: Restart Hermes and verify the skill is discovered**

Run: `docker compose up -d hermes-glpi`
Run: `docker exec hermes-glpi cat /opt/data/skills/devops/glpi-ticket-triage/SKILL.md | head -5`
Expected: the file's YAML frontmatter (`---`, `name: glpi-ticket-triage`, ...) — confirms the mount landed correctly.

- [ ] **Step 4: Remove the throwaway capture subscription**

Run: `docker exec hermes-glpi hermes webhook remove glpi-capture`
Expected: confirmation that the subscription was removed.

Run: `docker exec hermes-glpi rm /opt/data/scripts/capture_glpi.py /opt/data/glpi_webhook_capture.log`
(the second file may not exist if no capture ever landed — ignore "No such file" for it specifically.)

- [ ] **Step 5: Create the real subscription**

Run (replace `<RELAY_SECRET>` with the exact value generated in Task 2 Step 1 — it must match `docker-compose.yml`'s `RELAY_SECRET` for `glpi-webhook-relay` exactly):

```bash
docker exec hermes-glpi hermes webhook subscribe glpi-ticket \
  --secret "<RELAY_SECRET>" \
  --skills glpi-ticket-triage \
  --description "GLPI ticket triage and KB capitalization" \
  --deliver log \
  --prompt "Événement GLPI: {event} sur le ticket #{item.id} \"{item.name}\".

Contenu: {item.content}

Statut actuel: {item.status.name}

Applique la politique de décision du skill glpi-ticket-triage à ce ticket (ticket_id={item.id})."
```

Expected output includes:
```
Created webhook subscription: glpi-ticket
URL:    http://localhost:8644/webhooks/glpi-ticket
```

**Do not add `--events`** — see the Global Constraints note: Hermes's `--events` filter only matches `payload["event_type"]`/`payload["type"]`, and GLPI's payload uses the key `"event"`, so any `--events` value here would cause every delivery to be silently ignored.

---

## Task 5: GLPI MCP server

**Files:**
- Create: `glpi-integration/mcp-server/server.py`
- Test: `glpi-integration/mcp-server/test_server.py`
- Modify: `docker-compose.yml`
- Modify (inside container, via `docker exec`): `/opt/data/config.yaml`

**Interfaces:**
- Produces: `server.GLPIClient` (methods: `_password_grant`, `_refresh_grant`, `_store_token`, `_ensure_token`, `request(method, path, **kwargs)`), and six `@mcp.tool()`-decorated functions: `search_tickets(query: str, limit: int) -> list`, `get_ticket(ticket_id: int) -> dict`, `add_followup(ticket_id: int, content: str, is_private: bool) -> dict`, `add_solution(ticket_id: int, content: str) -> dict`, `search_kb(query: str) -> list`, `create_kb_article(name: str, content: str, is_faq: bool) -> dict`.
- Consumes: env vars `GLPI_API_URL`, `GLPI_OAUTH_CLIENT_ID`, `GLPI_OAUTH_CLIENT_SECRET`, `GLPI_USER`, `GLPI_PASSWORD` (already set in `docker-compose.yml`'s `hermes-glpi` service).

- [ ] **Step 1: Write the failing tests**

Create `glpi-integration/mcp-server/test_server.py`:

```python
import os
import unittest
from unittest.mock import patch, MagicMock

# Force-override rather than setdefault: these tests run inside the same
# hermes-glpi container that has the *real* GLPI credentials set as env
# vars, so setdefault would be a no-op there and silently assert against
# production values instead of these fixtures.
os.environ["GLPI_API_URL"] = "http://testserver/api.php/v2.3"
os.environ["GLPI_OAUTH_CLIENT_ID"] = "test-client-id"
os.environ["GLPI_OAUTH_CLIENT_SECRET"] = "test-client-secret"
os.environ["GLPI_USER"] = "test-user"
os.environ["GLPI_PASSWORD"] = "test-password"

import server


def _fake_response(json_body, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    resp.content = b"1" if json_body is not None else b""
    resp.raise_for_status.return_value = None
    return resp


class GLPIClientTokenTests(unittest.TestCase):
    def setUp(self):
        self.client = server.GLPIClient()

    @patch("server.requests.post")
    def test_password_grant_stores_token(self, mock_post):
        mock_post.return_value = _fake_response(
            {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 3600}
        )
        self.client._password_grant()

        self.assertEqual(self.client._access_token, "tok-1")
        self.assertEqual(self.client._refresh_token, "ref-1")
        sent_data = mock_post.call_args.kwargs["data"]
        self.assertEqual(sent_data["grant_type"], "password")
        self.assertEqual(sent_data["username"], "test-user")

    @patch("server.requests.post")
    def test_ensure_token_reuses_valid_token(self, mock_post):
        self.client._access_token = "tok-cached"
        self.client._expires_at = server.time.time() + 3600
        self.client._ensure_token()
        mock_post.assert_not_called()

    @patch("server.requests.post")
    def test_ensure_token_refreshes_when_expired_with_refresh_token(self, mock_post):
        self.client._access_token = "tok-old"
        self.client._refresh_token = "ref-1"
        self.client._expires_at = server.time.time() - 10
        mock_post.return_value = _fake_response(
            {"access_token": "tok-new", "refresh_token": "ref-2", "expires_in": 3600}
        )

        self.client._ensure_token()

        self.assertEqual(self.client._access_token, "tok-new")
        sent_data = mock_post.call_args.kwargs["data"]
        self.assertEqual(sent_data["grant_type"], "refresh_token")

    @patch("server.requests.post")
    def test_ensure_token_falls_back_to_password_grant_when_refresh_fails(self, mock_post):
        self.client._access_token = "tok-old"
        self.client._refresh_token = "ref-1"
        self.client._expires_at = server.time.time() - 10

        refresh_fail = MagicMock()
        refresh_fail.raise_for_status.side_effect = server.requests.HTTPError("refresh failed")
        password_ok = _fake_response(
            {"access_token": "tok-fresh", "refresh_token": "ref-3", "expires_in": 3600}
        )
        mock_post.side_effect = [refresh_fail, password_ok]

        self.client._ensure_token()

        self.assertEqual(self.client._access_token, "tok-fresh")
        self.assertEqual(mock_post.call_count, 2)


class GLPIClientRequestTests(unittest.TestCase):
    def setUp(self):
        self.client = server.GLPIClient()
        self.client._access_token = "tok-1"
        self.client._expires_at = server.time.time() + 3600

    @patch("server.requests.request")
    def test_request_adds_bearer_header(self, mock_request):
        mock_request.return_value = _fake_response({"id": 1})
        result = self.client.request("GET", "/Assistance/Ticket/1")

        self.assertEqual(result, {"id": 1})
        headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer tok-1")
        self.assertEqual(
            mock_request.call_args.args[1], "http://testserver/api.php/v2.3/Assistance/Ticket/1"
        )


class ToolTests(unittest.TestCase):
    @patch.object(server.glpi, "request")
    def test_add_followup_calls_correct_endpoint(self, mock_request):
        mock_request.return_value = {"id": 5}
        result = server.add_followup(42, "hello", False)

        self.assertEqual(result, {"id": 5})
        mock_request.assert_called_once_with(
            "POST",
            "/Assistance/Ticket/42/Timeline/Followup",
            json={"content": "hello", "is_private": False},
        )

    @patch.object(server.glpi, "request")
    def test_create_kb_article_calls_correct_endpoint(self, mock_request):
        mock_request.return_value = {"id": 9}
        result = server.create_kb_article("Title", "Body", True)

        self.assertEqual(result, {"id": 9})
        mock_request.assert_called_once_with(
            "POST",
            "/Knowledgebase/Article",
            json={"name": "Title", "content": "Body", "is_faq": True},
        )

    @patch.object(server.glpi, "request")
    def test_search_kb_uses_filter_param(self, mock_request):
        mock_request.return_value = []
        server.search_kb('name=like="*wifi*"')
        mock_request.assert_called_once_with(
            "GET",
            "/Knowledgebase/Article",
            params={"filter": 'name=like="*wifi*"'},
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Mount the mcp-server directory into the Hermes container**

The `mcp`/`requests` packages only exist inside `hermes-glpi`'s venv, not on the host, so all test runs for this component happen via `docker exec` against a live bind mount (edits to the host file are visible immediately, no copying needed).

In `docker-compose.yml`, under the `hermes-glpi` service's `volumes:`, add:

```yaml
      - ./glpi-integration/mcp-server:/opt/glpi-mcp-server:ro
```

Run: `docker compose up -d hermes-glpi`
Expected: `Container hermes-glpi Recreated`, `Container hermes-glpi Started`.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `docker exec -w /opt/glpi-mcp-server hermes-glpi python3 -B -m unittest test_server -v`

(`-B` disables bytecode caching — the mount is read-only, so Python can't write `__pycache__` there.)

Expected: FAIL — `ModuleNotFoundError: No module named 'server'` (`server.py` doesn't exist yet).

- [ ] **Step 4: Write `server.py`**

Create `glpi-integration/mcp-server/server.py`:

```python
#!/usr/bin/env python3
"""Minimal MCP server exposing a deliberately small set of GLPI actions:
search/read tickets, reply to a ticket, propose a solution, and
search/create Knowledge Base articles. No delete or admin actions are
exposed, mirroring the hermes-bot GLPI account's own restrictions.

See docs/superpowers/specs/2026-07-22-hermes-glpi-integration-design.md.
"""

import os
import time

import requests
from mcp.server.fastmcp import FastMCP

API_URL = os.environ["GLPI_API_URL"].rstrip("/")
CLIENT_ID = os.environ["GLPI_OAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["GLPI_OAUTH_CLIENT_SECRET"]
GLPI_USER = os.environ["GLPI_USER"]
GLPI_PASSWORD = os.environ["GLPI_PASSWORD"]

mcp = FastMCP("glpi")


class GLPIClient:
    def __init__(self):
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0

    def _password_grant(self):
        resp = requests.post(
            f"{API_URL}/token",
            data={
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "username": GLPI_USER,
                "password": GLPI_PASSWORD,
                "scope": "api",
            },
            timeout=15,
        )
        resp.raise_for_status()
        self._store_token(resp.json())

    def _refresh_grant(self):
        resp = requests.post(
            f"{API_URL}/token",
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": self._refresh_token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        self._store_token(resp.json())

    def _store_token(self, payload):
        self._access_token = payload["access_token"]
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        self._expires_at = time.time() + payload["expires_in"] - 60

    def _ensure_token(self):
        if self._access_token and time.time() < self._expires_at:
            return
        if self._refresh_token:
            try:
                self._refresh_grant()
                return
            except requests.HTTPError:
                pass
        self._password_grant()

    def request(self, method, path, **kwargs):
        self._ensure_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._access_token}"
        resp = requests.request(
            method, f"{API_URL}{path}", headers=headers, timeout=15, **kwargs
        )
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return None


glpi = GLPIClient()


@mcp.tool()
def search_tickets(query: str = "", limit: int = 10) -> list:
    """Search/list tickets. `query` is a GLPI RSQL filter, e.g.
    name=like="*wifi*" (empty returns the most recent tickets)."""
    params = {"limit": limit}
    if query:
        params["filter"] = query
    return glpi.request("GET", "/Assistance/Ticket", params=params)


@mcp.tool()
def get_ticket(ticket_id: int) -> dict:
    """Fetch full details for one ticket by id."""
    return glpi.request("GET", f"/Assistance/Ticket/{ticket_id}")


@mcp.tool()
def add_followup(ticket_id: int, content: str, is_private: bool) -> dict:
    """Add a followup to a ticket. is_private=True posts an internal note
    (technicians only); False replies to the requester directly."""
    return glpi.request(
        "POST",
        f"/Assistance/Ticket/{ticket_id}/Timeline/Followup",
        json={"content": content, "is_private": is_private},
    )


@mcp.tool()
def add_solution(ticket_id: int, content: str) -> dict:
    """Propose a solution for a ticket."""
    return glpi.request(
        "POST",
        f"/Assistance/Ticket/{ticket_id}/Timeline/Solution",
        json={"content": content},
    )


@mcp.tool()
def search_kb(query: str) -> list:
    """Search Knowledge Base articles. `query` is a GLPI RSQL filter, e.g.
    name=like="*wifi*" or content=like="*wifi*"."""
    return glpi.request("GET", "/Knowledgebase/Article", params={"filter": query})


@mcp.tool()
def create_kb_article(name: str, content: str, is_faq: bool = False) -> dict:
    """Create a new Knowledge Base article."""
    return glpi.request(
        "POST",
        "/Knowledgebase/Article",
        json={"name": name, "content": content, "is_faq": is_faq},
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `docker exec -w /opt/glpi-mcp-server hermes-glpi python3 -B -m unittest test_server -v`
Expected: all 8 tests `ok`, ending in `OK`.

- [ ] **Step 6: Register the MCP server in `config.yaml`**

Run this to append the `mcp_servers` block (uses `${VAR}` interpolation — no literal secrets in the file):

```bash
docker exec hermes-glpi python3 -c "
import yaml
path = '/opt/data/config.yaml'
with open(path) as f:
    cfg = yaml.safe_load(f)
cfg.setdefault('mcp_servers', {})['glpi'] = {
    'command': 'python3',
    'args': ['/opt/glpi-mcp-server/server.py'],
    'env': {
        'GLPI_API_URL': '\${GLPI_API_URL}',
        'GLPI_OAUTH_CLIENT_ID': '\${GLPI_OAUTH_CLIENT_ID}',
        'GLPI_OAUTH_CLIENT_SECRET': '\${GLPI_OAUTH_CLIENT_SECRET}',
        'GLPI_USER': '\${GLPI_USER}',
        'GLPI_PASSWORD': '\${GLPI_PASSWORD}',
    },
}
with open(path, 'w') as f:
    yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
print('done')
"
```
Expected: `done`.

- [ ] **Step 7: Restart Hermes and confirm the tools are registered**

Run: `docker compose restart hermes-glpi` (note: `docker compose up -d` is a no-op here — only the container's bind-mounted `config.yaml` changed in Step 6, not `docker-compose.yml`, so nothing tells Compose to recreate the container; `restart` is what actually reloads the process).

Hermes routes these logs to files under `/opt/data/logs/`, not to the container's captured stdout/stderr — `docker logs` won't show them.

Run: `sleep 6 && docker exec hermes-glpi tail -n 5 /opt/data/logs/agent.log`
Expected: a line like `MCP server 'glpi' (stdio): registered N tool(s): mcp__glpi__search_tickets, mcp__glpi__get_ticket, mcp__glpi__add_followup, mcp__glpi__add_solution, mcp__glpi__search_kb, mcp__glpi__create_kb_article, ...`. Note the tool names use **double** underscores (`mcp__glpi__<tool>`), not single (`mcp_glpi_<tool>`) — the native-mcp skill's own docs describe the convention with single underscores, but this is what Hermes actually registers; the `glpi-ticket-triage` skill (Task 4) has already been corrected to use the double-underscore names. `N` may be higher than 6 — FastMCP automatically adds 4 generic protocol tools (`list_resources`, `read_resource`, `list_prompts`, `get_prompt`) to every server; that's expected, not a sign `server.py` defined extra GLPI actions. If nothing appears, check `/opt/data/logs/mcp-stderr.log` for a connection error and fix before continuing.


---

## Task 6: End-to-end verification and cleanup

**Files:** none (verification only).

**Interfaces:**
- Consumes: everything from Tasks 1–5.

- [ ] **Step 1: Trigger a real "new ticket" event**

In GLPI, create a ticket whose content matches something you expect the KB to already know about, or — for a clean first test — anything at all, to observe the "no confident match → private note" path. Note the ticket's id.

- [ ] **Step 2: Confirm the relay forwarded it**

Run: `docker logs glpi-webhook-relay --tail 20`
Expected: a log line for the incoming POST (no Python traceback).

- [ ] **Step 3: Confirm Hermes ran the triage skill**

Hermes routes these logs to files under `/opt/data/logs/`, not to `docker logs` (see the note in Task 5 Step 7).

Run: `docker exec hermes-glpi grep -i "glpi-ticket" /opt/data/logs/gateway.log | tail -10`
Expected: a line showing the `glpi-ticket` webhook route fired and triggered an agent run.

- [ ] **Step 4: Confirm the followup landed on the ticket**

In GLPI, open the test ticket from Step 1 and check its timeline for a new followup (public or private, per the skill's decision).

If nothing appears after ~2 minutes, check in order: `docker exec hermes-glpi grep -iE "error|glpi-ticket|mcp__glpi" /opt/data/logs/agent.log /opt/data/logs/gateway.log /opt/data/logs/errors.log` (agent/tool errors), then `docker exec glpi_docker_dev-db-1 mariadb -uglpi -pglpi glpi -e "SELECT id,sent_try,sent_time FROM glpi_queuedwebhooks ORDER BY id DESC LIMIT 3;"` (did GLPI even attempt delivery).

- [ ] **Step 5: Verify the resolution → KB path**

Mark the same test ticket "Solved" in GLPI (this fires the `Update` webhook). Wait ~2 minutes, then check GLPI's Knowledge Base (Tools → Knowledge base) for a new article about it.

- [ ] **Step 6: Confirm `hermes-bot` still cannot delete anything**

Run (reuse the OAuth password grant to get a fresh token, then attempt a delete that must fail):

```bash
BASE="http://localhost:8080/api.php/v2.3"
TOKEN=$(curl -s -X POST "$BASE/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "grant_type=password" \
  --data-urlencode "client_id=<GLPI_OAUTH_CLIENT_ID>" \
  --data-urlencode "client_secret=<GLPI_OAUTH_CLIENT_SECRET>" \
  --data-urlencode "username=hermes-bot" \
  --data-urlencode "password=<GLPI_PASSWORD>" \
  --data-urlencode "scope=api" | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
curl -s -w "\nHTTP %{http_code}\n" -X DELETE "$BASE/Assistance/Ticket/<test-ticket-id>" -H "Authorization: Bearer $TOKEN"
```
Expected: `HTTP 403`.

- [ ] **Step 7: Clean up design-time test artifacts**

In GLPI, delete or close the design-time test tickets: `#1` (`[TEST hermes-bot] Vérification API`) and `#2` (`Je suis fatigué!`), plus any test ticket created in Step 1 above, once you're satisfied with the result.

- [ ] **Step 8: Final review**

Read through the final `docker-compose.yml` end to end and confirm it matches every change made across Tasks 2, 4, and 5 (the `glpi-webhook-relay` service, the `hermes-glpi` volume mounts for the skill and MCP server directories, `extra_hosts` on `glpi-webhook-relay` rather than `glpi`). This project has no git repository (by the user's choice — see the design spec), so there is no commit checkpoint here; this read-through is the closing verification instead.

---

## Task 7: Ticket image attachments (post-delivery addition)

User-reported gap after Task 6's verification: Hermes picks up tickets correctly but never looks at attached screenshots/documents. Added via a follow-up brainstorming round — see the design spec's Components section 4 ("Image attachments") for the full rationale, including the live-confirmed fact that Hermes's native-mcp client (`tools/mcp_tool.py`) converts any MCP tool's `ImageContent` result into a vision-model message automatically, not just from a dedicated vision tool.

**Files:**
- Modify: `glpi-integration/mcp-server/server.py`
- Modify: `glpi-integration/mcp-server/test_server.py`
- Modify: `glpi-integration/skills/glpi-ticket-triage/SKILL.md`

**Interfaces:**
- Consumes: `GLPIClient.request(method, path, **kwargs)` (existing, Task 5) — gains a new `raw: bool = False` keyword.
- Produces: `get_ticket_images(ticket_id: int) -> list` (a new `@mcp.tool()`, registered by Hermes as `mcp__glpi__get_ticket_images`), returning `mcp.server.fastmcp.Image` objects for up to 5 `image/*`-mime-type documents linked to the ticket.

- [ ] **Step 1: Write the failing tests**

Add to `glpi-integration/mcp-server/test_server.py` (append these classes — do not remove or modify the existing ones):

```python
class GLPIClientRawModeTests(unittest.TestCase):
    def setUp(self):
        self.client = server.GLPIClient()
        self.client._access_token = "tok-1"
        self.client._expires_at = server.time.time() + 3600

    @patch("server.requests.request")
    def test_raw_mode_returns_bytes_without_parsing_json(self, mock_request):
        resp = MagicMock()
        resp.content = b"\x89PNG raw bytes, not json"
        resp.raise_for_status.return_value = None
        mock_request.return_value = resp

        result = self.client.request(
            "GET", "/Management/Document/1/Download", raw=True
        )

        self.assertEqual(result, b"\x89PNG raw bytes, not json")
        resp.json.assert_not_called()


class GetTicketImagesTests(unittest.TestCase):
    @patch.object(server.glpi, "request")
    def test_filters_to_images_and_downloads_each(self, mock_request):
        mock_request.side_effect = [
            [
                {"id": 10, "mime": "image/png", "filename": "screenshot.png"},
                {"id": 11, "mime": "application/pdf", "filename": "manual.pdf"},
                {"id": 12, "mime": "image/jpeg", "filename": "photo.jpg"},
            ],
            b"PNGDATA",
            b"JPGDATA",
        ]

        images = server.get_ticket_images(42)

        self.assertEqual(len(images), 2)
        self.assertEqual(images[0].data, b"PNGDATA")
        self.assertEqual(images[1].data, b"JPGDATA")
        mock_request.assert_any_call(
            "GET", "/Assistance/Ticket/42/Timeline/Document"
        )
        mock_request.assert_any_call(
            "GET", "/Management/Document/10/Download", raw=True
        )
        mock_request.assert_any_call(
            "GET", "/Management/Document/12/Download", raw=True
        )

    @patch.object(server.glpi, "request")
    def test_caps_at_five_images(self, mock_request):
        docs = [
            {"id": i, "mime": "image/png", "filename": f"img{i}.png"}
            for i in range(8)
        ]
        mock_request.side_effect = [docs] + [b"DATA"] * 8

        images = server.get_ticket_images(42)

        self.assertEqual(len(images), 5)

    @patch.object(server.glpi, "request")
    def test_no_documents_returns_empty_list(self, mock_request):
        mock_request.return_value = []

        images = server.get_ticket_images(42)

        self.assertEqual(images, [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker exec -w /opt/glpi-mcp-server hermes-glpi python3 -B -m unittest test_server -v`
Expected: FAIL — `TypeError: request() got an unexpected keyword argument 'raw'` (first new test) and `AttributeError: module 'server' has no attribute 'get_ticket_images'` (the rest), alongside the 8 pre-existing tests still passing.

- [ ] **Step 3: Add `raw` mode to `GLPIClient.request`**

In `glpi-integration/mcp-server/server.py`, replace the `request` method:

```python
    def request(self, method, path, raw=False, **kwargs):
        self._ensure_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._access_token}"
        resp = requests.request(
            method, f"{API_URL}{path}", headers=headers, timeout=15, **kwargs
        )
        resp.raise_for_status()
        if raw:
            return resp.content
        if resp.content:
            return resp.json()
        return None
```

- [ ] **Step 4: Add the `get_ticket_images` tool**

In `glpi-integration/mcp-server/server.py`, change the import line and add the new tool after `create_kb_article`:

```python
from mcp.server.fastmcp import FastMCP, Image
```

```python
MAX_IMAGES_PER_TICKET = 5


@mcp.tool()
def get_ticket_images(ticket_id: int) -> list:
    """Fetch image attachments (screenshots/photos) linked to a ticket, as
    viewable images. Non-image documents (PDF, Word, etc.) are not
    supported yet and are skipped. Capped at the first 5 image
    attachments found on the ticket."""
    docs = glpi.request("GET", f"/Assistance/Ticket/{ticket_id}/Timeline/Document")
    images = []
    for doc in docs:
        mime = doc.get("mime") or ""
        if not mime.startswith("image/"):
            continue
        data = glpi.request(
            "GET", f"/Management/Document/{doc['id']}/Download", raw=True
        )
        images.append(Image(data=data, format=mime.split("/")[-1]))
        if len(images) >= MAX_IMAGES_PER_TICKET:
            break
    return images
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `docker exec -w /opt/glpi-mcp-server hermes-glpi python3 -B -m unittest test_server -v`
Expected: all 12 tests `ok` (the 8 from Task 5 plus these 4), ending in `OK`.

- [ ] **Step 6: Restart Hermes and confirm the new tool is registered**

Run: `docker compose restart hermes-glpi` (same reason as Task 5 Step 7 — no `docker-compose.yml` change here, so `up -d` would be a no-op).
Run: `sleep 6 && docker exec hermes-glpi tail -n 5 /opt/data/logs/agent.log`
Expected: `MCP server 'glpi' (stdio): registered 11 tool(s): ...mcp__glpi__get_ticket_images...` (was 10 before this task — the 6 original GLPI tools plus 4 FastMCP protocol tools; now 7 + 4).

- [ ] **Step 7: Update the skill**

In `glpi-integration/skills/glpi-ticket-triage/SKILL.md`, replace the `### event: "new"` section's numbered list:

```markdown
### `event: "new"`

1. Read the ticket's `name` (title) and `content` (description) from the
   payload.
2. Call `mcp__glpi__get_ticket_images(ticket_id=<item.id>)` — always, even
   if nothing in the ticket text mentions an attachment. If it returns any
   images, look at them before continuing: an error message, a code, or
   other visual context in a screenshot often matters more than the
   ticket's own written description. Non-image attachments (PDF, Word,
   etc.) are not supported yet and won't appear here.
3. Call `mcp__glpi__search_kb` with an RSQL filter built from the ticket's
   key terms, e.g. `name=like="*<keyword>*"`. If that returns nothing, also
   try `content=like="*<keyword>*"`.
4. Decide:
   - **Confident match** — the KB article clearly answers this exact
     request, not just a loosely related topic: reply directly with
     `mcp__glpi__add_followup(ticket_id=<item.id>, content=<answer drawn
     from the KB article>, is_private=False)`.
   - **No confident match, or the request needs ticket-specific info**
     (asset details, account access, something only a technician can
     check): `mcp__glpi__add_followup(ticket_id=<item.id>, content=<your
     diagnosis and a suggested next step for the technician>,
     is_private=True)`. Do not guess a public reply in this case — an
     internal note is always the safe default.
5. Never call `mcp__glpi__add_solution` or otherwise resolve/close the
   ticket in this step — only a human, or the resolution step below, does
   that.
```

(This only renumbers and inserts step 2; steps 1 and what was 3-4 keep their existing wording, now as steps 1, 3, 4, 5.)

- [ ] **Step 8: Verify the skill file update landed and re-read cleanly**

Run: `docker exec hermes-glpi grep -c "get_ticket_images" /opt/data/skills/devops/glpi-ticket-triage/SKILL.md`
Expected: `1` or more (confirms the bind-mounted file picked up the edit — no restart needed for skill content, but Step 6's restart already happened).

- [ ] **Step 9: Live end-to-end verification (controller-run, not part of automated tests)**

This step needs a ticket with a real image attached, which requires GLPI's document-upload flow (not yet exercised anywhere in this project) — hand this step to the controller rather than scripting it blind here. At minimum: create or reuse a ticket, attach an image to it (via the GLPI UI, or by working out the API upload flow live), trigger a "new" or "update" event, and confirm via `/opt/data/logs/agent.log` that `mcp__glpi__get_ticket_images` was called and returned at least one image, and that the resulting followup reflects something only visible in the image (not just the ticket's text).

---

## Task 8: Self-assignment and escalation (post-delivery addition)

User-reported requirement: "j'ai également besoin que si le bot répond par lui-même au ticket, qui se l'assigne, j'ai besoin qu'il soit en capacité d'escalader si ça devient nécessaire." Added via a follow-up brainstorming round — see the design spec's Components section 4 (new tool rows + the "Assignment / escalation" note) and Security/guardrails section (the `Ticket::ASSIGN` right) for the full rationale.

**Already verified live before writing this task (do not re-derive):**
- `hermes-bot` lacked `Ticket::ASSIGN` (bit `8192` on the `ticket` row in `glpi_profilerights`) — confirmed via a 403 on `POST /Assistance/Ticket/{id}/TeamMember`, even for self-assignment. **Already granted**: `UPDATE glpi_profilerights SET rights = rights | 8192 WHERE profiles_id=6 AND name='ticket'` — re-verify with `SELECT rights FROM glpi_profilerights WHERE profiles_id=6 AND name='ticket';` should show `437255`, not `429063`, if you need to confirm this task's prerequisite is still in place.
- `POST /Assistance/Ticket/{id}/TeamMember` body shape: `{"type": "User", "id": <numeric user id>, "role": "assigned"}` (confirmed working live: both self-assignment and assigning a second user succeed, both appear as separate entries in the ticket's `team` array).
- `GET /Assistance/Ticket/{id}/Timeline/Followup` uses the same timeline-wrapper shape already discovered for `Timeline/Document` in Task 7's bug fix: `[{"type": "Followup", "item": {"content": ..., "is_private": ..., "date": ..., "user": {"id": ..., "name": ...}, ...}}]` — **not** a flat array of Followup objects.
- `GET /Administration/User` (no filter, full list) returns entries with a `username` field (e.g. `{"id": 7, "username": "hermes-bot", ...}`) — the RSQL `filter` param does not reliably narrow this endpoint by username (confirmed during Task 5's design), so username-to-id resolution must filter the full list client-side.

**Files:**
- Modify: `glpi-integration/mcp-server/server.py`
- Modify: `glpi-integration/mcp-server/test_server.py`
- Modify: `glpi-integration/skills/glpi-ticket-triage/SKILL.md`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `GLPIClient.request(method, path, **kwargs)` (existing).
- Produces: `GLPIClient.resolve_user_id(username: str) -> int` (new method, cached); three new `@mcp.tool()` functions registered by Hermes as `mcp__glpi__get_ticket_followups`, `mcp__glpi__assign_self`, `mcp__glpi__escalate_ticket`.

- [ ] **Step 1: Write the failing tests**

Add to `glpi-integration/mcp-server/test_server.py` (append — do not remove or modify any existing test class):

```python
class ResolveUserIdTests(unittest.TestCase):
    @patch.object(server.glpi, "request")
    def test_resolves_and_caches(self, mock_request):
        mock_request.return_value = [
            {"id": 4, "username": "tech"},
            {"id": 7, "username": "hermes-bot"},
        ]
        client = server.GLPIClient()

        user_id = client.resolve_user_id("hermes-bot")
        user_id_again = client.resolve_user_id("hermes-bot")

        self.assertEqual(user_id, 7)
        self.assertEqual(user_id_again, 7)
        mock_request.assert_called_once_with("GET", "/Administration/User")

    @patch.object(server.glpi, "request")
    def test_raises_for_unknown_username(self, mock_request):
        mock_request.return_value = [{"id": 4, "username": "tech"}]
        client = server.GLPIClient()

        with self.assertRaises(ValueError):
            client.resolve_user_id("does-not-exist")


def _timeline_followup(content, is_private, author_name):
    """Shape of one entry from GET .../Timeline/Followup -- the same
    timeline-wrapper pattern already confirmed live for Timeline/Document
    in Task 7 (see that fix's commit message), not a flat object."""
    return {
        "type": "Followup",
        "item": {
            "content": content,
            "is_private": is_private,
            "date": "2026-07-22T12:00:00+00:00",
            "user": {"id": 1, "name": author_name},
        },
    }


class GetTicketFollowupsTests(unittest.TestCase):
    @patch.object(server.glpi, "request")
    def test_simplifies_timeline_wrapper_shape(self, mock_request):
        mock_request.return_value = [
            _timeline_followup("Bonjour", False, "hermes-bot"),
            _timeline_followup("Merci !", False, "ivan"),
        ]

        followups = server.get_ticket_followups(9)

        self.assertEqual(len(followups), 2)
        self.assertEqual(
            followups[0],
            {
                "content": "Bonjour",
                "is_private": False,
                "date": "2026-07-22T12:00:00+00:00",
                "author_name": "hermes-bot",
            },
        )
        self.assertEqual(followups[1]["author_name"], "ivan")

    @patch.object(server.glpi, "request")
    def test_no_followups_returns_empty_list(self, mock_request):
        mock_request.return_value = None
        self.assertEqual(server.get_ticket_followups(9), [])


class AssignSelfTests(unittest.TestCase):
    @patch.object(server.glpi, "resolve_user_id")
    @patch.object(server.glpi, "request")
    def test_assigns_resolved_self_id(self, mock_request, mock_resolve):
        mock_resolve.return_value = 7
        mock_request.return_value = {"id": 1, "href": "/Assistance/Ticket/9/TeamMember/1"}

        result = server.assign_self(9)

        mock_resolve.assert_called_once_with(server.GLPI_USER)
        mock_request.assert_called_once_with(
            "POST",
            "/Assistance/Ticket/9/TeamMember",
            json={"type": "User", "id": 7, "role": "assigned"},
        )
        self.assertEqual(result, {"id": 1, "href": "/Assistance/Ticket/9/TeamMember/1"})


class EscalateTicketTests(unittest.TestCase):
    @patch.object(server.glpi, "resolve_user_id")
    @patch.object(server.glpi, "request")
    def test_assigns_escalation_user_and_posts_private_reason(
        self, mock_request, mock_resolve
    ):
        mock_resolve.return_value = 4
        mock_request.side_effect = [
            {"id": 1, "href": "/Assistance/Ticket/9/TeamMember/1"},
            {"id": 2, "href": "/Assistance/Ticket/9/Timeline/Followup/2"},
        ]

        result = server.escalate_ticket(
            9, "Le client dit que ca ne marche toujours pas."
        )

        mock_resolve.assert_called_once_with(server.ESCALATION_USER)
        mock_request.assert_any_call(
            "POST",
            "/Assistance/Ticket/9/TeamMember",
            json={"type": "User", "id": 4, "role": "assigned"},
        )
        mock_request.assert_any_call(
            "POST",
            "/Assistance/Ticket/9/Timeline/Followup",
            json={
                "content": "Le client dit que ca ne marche toujours pas.",
                "is_private": True,
            },
        )
        self.assertEqual(result, {"id": 1, "href": "/Assistance/Ticket/9/TeamMember/1"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker exec -w /opt/glpi-mcp-server hermes-glpi python3 -B -m unittest test_server -v`
Expected: FAIL — `AttributeError: 'GLPIClient' object has no attribute 'resolve_user_id'` (and similar for the three new tool functions not existing yet), alongside the 14 pre-existing tests still passing.

- [ ] **Step 3: Add `resolve_user_id` to `GLPIClient`**

In `glpi-integration/mcp-server/server.py`, add a cache dict to `__init__` and a new method (place the method anywhere inside the `GLPIClient` class, e.g. right after `request`):

```python
    def __init__(self):
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        self._user_id_cache = {}
```

```python
    def resolve_user_id(self, username):
        if username in self._user_id_cache:
            return self._user_id_cache[username]
        users = self.request("GET", "/Administration/User") or []
        for user in users:
            if user.get("username") == username:
                self._user_id_cache[username] = user["id"]
                return user["id"]
        raise ValueError(f"No GLPI user found with username {username!r}")
```

- [ ] **Step 4: Add the escalation-target env var and the three new tools**

In `glpi-integration/mcp-server/server.py`, add near the other `os.environ[...]` reads at the top:

```python
ESCALATION_USER = os.environ["GLPI_ESCALATION_USER"]
```

Then add these three tools after `get_ticket_images`:

```python
@mcp.tool()
def get_ticket_followups(ticket_id: int) -> list:
    """List the followups (replies/notes) on a ticket, in the order GLPI
    returns them, as {content, is_private, date, author_name} -- use this
    to see who wrote the most recent message on a ticket and what they
    said."""
    entries = glpi.request(
        "GET", f"/Assistance/Ticket/{ticket_id}/Timeline/Followup"
    ) or []
    return [
        {
            "content": e["item"]["content"],
            "is_private": e["item"]["is_private"],
            "date": e["item"]["date"],
            "author_name": (e["item"].get("user") or {}).get("name"),
        }
        for e in entries
    ]


@mcp.tool()
def assign_self(ticket_id: int) -> dict:
    """Assign hermes-bot itself to a ticket, taking visible ownership of
    it. Use this when replying with a confident, resolving answer."""
    user_id = glpi.resolve_user_id(GLPI_USER)
    return glpi.request(
        "POST",
        f"/Assistance/Ticket/{ticket_id}/TeamMember",
        json={"type": "User", "id": user_id, "role": "assigned"},
    )


@mcp.tool()
def escalate_ticket(ticket_id: int, reason: str) -> dict:
    """Hand a ticket off to a human technician: assign the configured
    escalation user and leave an internal note explaining why. Use this
    whenever the ticket needs human judgment instead of another guess."""
    user_id = glpi.resolve_user_id(ESCALATION_USER)
    assignment = glpi.request(
        "POST",
        f"/Assistance/Ticket/{ticket_id}/TeamMember",
        json={"type": "User", "id": user_id, "role": "assigned"},
    )
    glpi.request(
        "POST",
        f"/Assistance/Ticket/{ticket_id}/Timeline/Followup",
        json={"content": reason, "is_private": True},
    )
    return assignment
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `docker exec -w /opt/glpi-mcp-server hermes-glpi python3 -B -m unittest test_server -v`
Expected: all 20 tests `ok` (the 14 from before plus these 6), ending in `OK`.

- [ ] **Step 6: Add `GLPI_ESCALATION_USER` to `docker-compose.yml`**

Under the `hermes-glpi` service's `environment:` list (not `.env` — this is a plain username, not a secret), add:

```yaml
      - GLPI_ESCALATION_USER=tech
```

Run: `docker compose restart hermes-glpi` (no `docker-compose.yml`-triggered recreate needed for an `environment:` value that's already templated the same way as the others — but restart regardless since this env var is new; if `docker compose restart` doesn't pick up the new variable, use `docker compose up -d hermes-glpi` instead, which will recreate the container).
Run: `docker exec hermes-glpi sh -c 'echo $GLPI_ESCALATION_USER'`
Expected: `tech`.

- [ ] **Step 7: Confirm the three new tools are registered**

Run: `sleep 6 && docker exec hermes-glpi tail -n 5 /opt/data/logs/agent.log`
Expected: `MCP server 'glpi' (stdio): registered N tool(s): ...mcp__glpi__get_ticket_followups...mcp__glpi__assign_self...mcp__glpi__escalate_ticket...` (was 11 before this task — 7 GLPI tools + 4 FastMCP protocol tools; now 10 + 4 = 14).

- [ ] **Step 8: Update the skill**

In `glpi-integration/skills/glpi-ticket-triage/SKILL.md`, make three edits.

First, in the `### event: "new"` section's step 4, replace the two bullet points:

```markdown
   - **Confident match** — the KB article clearly answers this exact
     request, not just a loosely related topic: reply directly with
     `mcp__glpi__add_followup(ticket_id=<item.id>, content=<answer drawn
     from the KB article>, is_private=False)`, **and then also** call
     `mcp__glpi__add_solution(ticket_id=<item.id>, content=<the same
     answer>)` — the same confidence bar that justifies a public reply
     also justifies auto-resolving the ticket. `add_solution` moves the
     ticket to GLPI's "Solved" status, not an irreversible "Closed" one —
     the requester can still reopen it if the answer didn't actually fix
     things, so this is a safe default, not a one-way door. Calling it
     will itself queue a fresh `update` event for a later run — that's
     expected, see the branch below. **Finally, call
     `mcp__glpi__assign_self(ticket_id=<item.id>)`** — if you're
     confident enough to answer and resolve on your own, take visible
     ownership of the ticket too.
   - **No confident match, or the request needs ticket-specific info**
     (asset details, account access, something only a technician can
     check): `mcp__glpi__add_followup(ticket_id=<item.id>, content=<your
     diagnosis and a suggested next step for the technician>,
     is_private=True)`, **and then** call
     `mcp__glpi__escalate_ticket(ticket_id=<item.id>, reason=<the same
     diagnosis>)` — hand the ticket to a human instead of leaving an
     unassigned note nobody might notice. Do not guess a public reply in
     this case — an internal note plus escalation is always the safe
     default. **Never call `mcp__glpi__add_solution` here** — only a
     confident public reply (above) or a human ever resolves a ticket.
```

Second, immediately after the existing `### event: "update"` section for the Solved-status branch (i.e. after its numbered list, before the `## Guardrails` heading), insert a whole new section:

```markdown
### `event: "update"` where the status is anything else, and the bot is already assigned

Check `item.team` in the payload for an entry with `role: "assigned"` and
`name: "hermes-bot"`. If there is no such entry, this update doesn't
concern you at all -- do nothing, regardless of what else changed.

If there is one, call `mcp__glpi__get_ticket_followups(ticket_id=<item.id>)`
and look at the single most recent entry (the last one in the list):

1. Authored by `hermes-bot` itself -- do nothing. This update was only a
   side effect of your own prior action (posting a followup or solution
   touches the ticket); reacting to it would create a loop.
2. Authored by anyone else who is not this ticket's original requester
   (`item.team`'s `role: "requester"` entry) -- e.g. a technician like
   `tech` already working an escalation -- do nothing. Never interfere
   with a human already engaged on a ticket.
3. Authored by the original requester -- read what they wrote:
   - Confirms the problem is fixed, or a simple thank-you -- call
     `mcp__glpi__add_solution(ticket_id=<item.id>, content=<a short
     close-out message>)`. This cascades into the KB-capitalization
     branch above on a later run, same as any other resolution.
   - Says it's still broken, got worse, or raises something new -- call
     `mcp__glpi__escalate_ticket(ticket_id=<item.id>, reason=<quote what
     the requester said>)`.
   - Ambiguous -- escalate. Same "default to human involvement when
     uncertain" principle as the initial triage decision.
```

Third, in the `## Guardrails` section, replace the existing bullet list with:

```markdown
- The only write actions that are ever appropriate here are `add_followup`,
  `add_solution` (only alongside a confident public `add_followup`, never
  alone and never for a private/internal note), `create_kb_article`,
  `assign_self` (only alongside a confident, resolving `add_followup`), and
  `escalate_ticket` (only when explicitly deciding to hand off to a human --
  never as a substitute for a real diagnosis) (plus
  `search_kb`/`search_tickets`/`get_ticket`/`get_ticket_images`/
  `get_ticket_followups` for reads). Never attempt to delete a ticket, or
  touch user/rights data — the `glpi` MCP server does not expose those
  actions, and the underlying GLPI account does not have the rights for
  them either. `escalate_ticket` only ever assigns the one pre-configured
  escalation user -- there is no tool for assigning an arbitrary person or
  group.
- When unsure whether a match is confident enough for a public reply,
  default to a private/internal followup instead. A wrong technician-facing
  note is easy to correct; a wrong public reply — or a ticket auto-resolved
  on a wrong answer — reaches the requester directly.
```

- [ ] **Step 9: Verify the skill file update landed**

Run: `docker exec hermes-glpi grep -c "escalate_ticket" /opt/data/skills/devops/glpi-ticket-triage/SKILL.md`
Expected: `1` or more (confirms the bind-mounted file picked up the edit).

- [ ] **Step 10: Live end-to-end verification (controller-run, not part of automated tests)**

Three scenarios need a live check, each requiring simulating multi-step ticket conversations that don't fit a scripted assertion here -- hand this to the controller:

1. **Confident match still self-assigns.** Create a ticket that confidently matches an existing KB article (as already done for Task 6/the auto-resolve feature). Confirm via `agent.log` that `mcp__glpi__assign_self` was called, and via the GLPI API/DB that the ticket's `team` now includes `{"role": "assigned", "name": "hermes-bot"}`.
2. **No match escalates immediately.** Create a ticket with content that has no matching KB article. Confirm `mcp__glpi__escalate_ticket` was called (not just a private followup), and that the ticket's `team` includes `{"role": "assigned", "name": "tech"}` plus a private followup explaining why.
3. **Follow-up from the requester triggers the new update branch.** On a ticket the bot already resolved (self-assigned + Solved) or escalated (assigned to `tech`), post a followup as if from the original requester (via the API, using `hermes-bot`'s credentials is fine for the test -- the *content* is what the skill reasons about, not who technically posted it) saying the problem is NOT fixed. Confirm the resulting `update` event triggers `escalate_ticket` rather than being ignored, and that it is NOT re-triggered by the escalation's own follow-up actions (no infinite loop -- check `agent.log` for repeated firings on the same ticket beyond the expected one).
