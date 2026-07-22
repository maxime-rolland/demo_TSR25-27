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
"new"|"update"}`. Use the `mcp_glpi_*` tools (from the `glpi` MCP server) for
every action on GLPI — never call the GLPI REST API directly.

## Decision policy

### `event: "new"`

1. Read the ticket's `name` (title) and `content` (description) from the
   payload.
2. Call `mcp_glpi_search_kb` with an RSQL filter built from the ticket's key
   terms, e.g. `name=like="*<keyword>*"`. If that returns nothing, also try
   `content=like="*<keyword>*"`.
3. Decide:
   - **Confident match** — the KB article clearly answers this exact
     request, not just a loosely related topic: reply directly with
     `mcp_glpi_add_followup(ticket_id=<item.id>, content=<answer drawn from
     the KB article>, is_private=False)`.
   - **No confident match, or the request needs ticket-specific info** (asset
     details, account access, something only a technician can check):
     `mcp_glpi_add_followup(ticket_id=<item.id>, content=<your diagnosis and
     a suggested next step for the technician>, is_private=True)`. Do not
     guess a public reply in this case — an internal note is always the safe
     default.
4. Never call `mcp_glpi_add_solution` or otherwise resolve/close the ticket
   in this step — only a human, or the resolution step below, does that.

### `event: "update"` where the ticket's status just changed to "Solved"

The payload's `item.status.name` is `"Solved"` (or the equivalent in GLPI's
configured language) when this applies. Skip this branch entirely for any
other status value.

1. Call `mcp_glpi_search_kb` using the ticket's title/content, same as above.
2. If a clearly-matching article already exists: do nothing (avoid duplicate
   KB entries).
3. If none exists: call `mcp_glpi_create_kb_article` with:
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

If nothing appears after ~2 minutes, check in order: `docker exec hermes-glpi grep -iE "error|glpi-ticket" /opt/data/logs/agent.log /opt/data/logs/gateway.log /opt/data/logs/errors.log` (agent/tool errors), then `docker exec glpi_docker_dev-db-1 mariadb -uglpi -pglpi glpi -e "SELECT id,sent_try,sent_time FROM glpi_queuedwebhooks ORDER BY id DESC LIMIT 3;"` (did GLPI even attempt delivery).

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
