# Hermes ↔ GLPI Integration — Design

Date: 2026-07-22
Status: Approved, ready for implementation plan

## Goal

Connect the Hermes agent (`hermes-glpi` container) to the local GLPI instance so it can:

1. Triage incoming tickets, semi-autonomously — respond directly to the requester when confident an existing knowledge-base answer applies, otherwise leave an internal note for a technician.
2. Automatically capitalize resolved tickets into GLPI's Knowledge Base, so future tickets can be answered from accumulated knowledge.

Scope: all ticket categories, decided per-ticket by the agent (no category allowlist for v1).

## Environment (validated facts)

- GLPI version: **11.0.8**, REST API is **v2.3** (`/api.php/v2.3`). The legacy `apirest.php` API is disabled on this instance and cannot be used.
- GLPI API v2.3 auth is **OAuth2-only**. Supported grants on this instance: `authorization_code` (interactive, unusable for a bot) and `password` (username/password exchanged for a JWT). No `client_credentials` grant is configured.
- A dedicated technician account `hermes-bot` exists, with an OAuth client (`grants: ["password"]`, `scopes: ["api"]`) already created in GLPI (Setup → General → API → OAuth clients).
- Verified empirically against the running instance:
  - `POST /api.php/v2.3/token` (password grant) returns a JWT `access_token` (`expires_in: 3600`) and a `refresh_token`.
  - `hermes-bot` can read/write tickets, followups, solutions, and KB articles: `GET/POST /Assistance/Ticket`, `POST /Assistance/Ticket/{id}/Timeline/Followup`, `POST /Assistance/Ticket/{id}/Timeline/Solution`, `GET/POST/DELETE /Knowledgebase/Article`.
  - `hermes-bot` **cannot** delete tickets (`DELETE /Assistance/Ticket/{id}` → 403) — confirmed as a deliberate safety boundary, not a bug to fix.
  - **KB articles created via the API are invisible by default, even to their own creator.** `POST /Knowledgebase/Article` never sets `users_id` (it silently ignores a client-supplied `users_id` field — confirmed by direct DB inspection after posting one), and the API exposes no endpoint to create the profile/group/entity visibility-target rows (`glpi_knowbaseitems_profiles`, `_users`, entity targets) that the classic web UI form sets automatically. GLPI's `KnowbaseItem::getVisibilityCriteria()` excludes an article from every read (list, search, and direct-by-ID GET all return nothing) unless the viewer matches its author, a target, or holds the `KNOWBASEADMIN` right (`KnowbaseItem::KNOWBASEADMIN = 1024`), which bypasses all target checks. Fix applied: granted `KNOWBASEADMIN` to `hermes-bot`'s Technician profile (`UPDATE glpi_profilerights SET rights = rights | 1024 WHERE profiles_id=<technician> AND name='knowbase'`) — this is the intentionally-correct fix (hermes-bot's role is KB management, not a workaround), not `is_faq`, which does not affect this at all: `getVisibilityCriteria()` picks its criteria based on the *viewer's* interface/rights, never the *article's* `is_faq` field. Without this right, `search_kb` can never find articles it (or any prior run) already created, silently defeating the "avoid duplicate KB entries" requirement.
- GLPI has **native outbound webhooks** (Setup → Webhooks — a distinct page from Setup → Notifications, which only covers email/browser). Per-`Ticket` events available: `New`, `Update`, `Delete` (single-select, so triage requires two separate webhook definitions: `Ticket`+`New` and `Ticket`+`Update`).
- GLPI queues outbound webhooks (`glpi_queuedwebhooks` table) and flushes them via an existing cron task (`QueuedWebhook`, external mode, 60s frequency) — already active on this instance, no extra setup needed.
- Confirmed webhook payload shape (captured from `glpi_queuedwebhooks.body`):
  ```json
  {
    "item": { "...full Ticket object, same shape as the REST API...": "" },
    "event": "new"
  }
  ```
- GLPI signs its webhook requests with `X-GLPI-signature` (hex) and `X-GLPI-timestamp` (unix seconds) headers. Hermes's webhook adapter (`gateway/platforms/webhook.py`) only recognizes GitHub (`X-Hub-Signature-256`), GitLab (`X-Gitlab-Token`), Svix, and its own generic V1/V2 schemes (`X-Webhook-Signature[-V2]` / `X-Webhook-Timestamp`) — matched by exact header name. GLPI's headers match none of these, so a direct GLPI → Hermes webhook call is **always rejected with 401**. This requires a small relay (see Components).
- `hermes-glpi` runs with `network_mode: host` (needed for the dashboard, see prior work). This means:
  - Hermes can reach GLPI directly at `http://localhost:8080` (GLPI's port is published to the host).
  - Anything in the `glpi`/default compose network that needs to reach Hermes must go through `host.docker.internal` (requires `extra_hosts: ["host.docker.internal:host-gateway"]` on the calling service).

## Architecture

```
GLPI (Ticket New/Update)
   │  queued, dispatched by GLPI's own cron (~60s)
   ▼
glpi-webhook-relay  (new, internal-only compose service)
   │  re-signs body as Hermes's Generic V2 HMAC scheme
   ▼
Hermes webhook route  http://host.docker.internal:8644/webhooks/glpi-ticket
   │  triggers an agent run (skill: glpi-ticket-triage)
   ▼
Agent  ──(MCP tools mcp__glpi__*)──▶  GLPI REST API v2.3 (as hermes-bot)
```

## Components

### 1. GLPI-side configuration (already done)

- `hermes-bot` technician account (no delete rights).
- OAuth client for `hermes-bot` (password grant, `api` scope) — `client_id`/`client_secret` in `docker-compose.yml` under the `hermes-glpi` service.
- Two webhook definitions (Setup → Webhooks): `Ticket`/`New` and `Ticket`/`Update`, both targeting the relay (see below), HTTP method POST.

### 2. `glpi-webhook-relay` (new compose service)

Minimal internal HTTP relay, not published to the host, reachable only from the `glpi` container over the compose bridge network.

- Responsibility: accept GLPI's raw webhook POST, discard GLPI's own (incompatible) signature headers, re-sign the untouched body using Hermes's Generic V2 scheme (`X-Webhook-Signature-V2 = hex(HMAC-SHA256(secret, "{timestamp}.{body}"))`, `X-Webhook-Timestamp = <unix seconds>`), and forward it unmodified otherwise to `http://host.docker.internal:8644/webhooks/glpi-ticket`.
- Trust model: relies on network isolation (only `glpi` can reach it) rather than re-validating GLPI's own signature — acceptable since it sits on a private compose network with no host port published.
- Implementation: single Python stdlib script (`http.server` + `hmac`/`hashlib` + `urllib.request`), run via the `python:3.13-alpine` image with the script bind-mounted — no custom image build needed.
- Needs `extra_hosts: ["host.docker.internal:host-gateway"]` to reach Hermes.

### 3. Hermes: webhook platform + subscription

- `platforms.webhook` enabled in `config.yaml` (`host: 0.0.0.0`, `port: 8644`) — already done.
- One dynamic subscription, `glpi-ticket`, created via `hermes webhook subscribe`, with its own HMAC secret (shared with the relay via an env var), routed to the triage skill.

### 4. MCP server `glpi` (new, custom, minimal)

Registered under `mcp_servers.glpi` in Hermes's `config.yaml`. Holds GLPI credentials via `env:` (never exposed to the general shell environment, per Hermes's MCP env-filtering). Handles the OAuth2 password-grant flow itself, including token refresh before the 1h expiry.

Tools exposed (deliberately minimal — no delete, no rights/user management):

| Tool | GLPI API call |
|---|---|
| `search_tickets` | `GET /Assistance/Ticket` |
| `get_ticket` | `GET /Assistance/Ticket/{id}` |
| `add_followup` | `POST /Assistance/Ticket/{id}/Timeline/Followup` — `content`, `is_private` |
| `add_solution` | `POST /Assistance/Ticket/{id}/Timeline/Solution` — `content` |
| `search_kb` | `GET /Knowledgebase/Article` |
| `create_kb_article` | `POST /Knowledgebase/Article` — `name`, `content`, `is_faq` |

### 5. Skill `glpi-ticket-triage`

Loaded by the `glpi-ticket` webhook subscription. Encodes the decision policy:

- **`event: "new"`** — search the KB for a matching answer.
  - Confident match → `add_followup(is_private=false)`: a direct reply to the requester.
  - No confident match → `add_followup(is_private=true)`: an internal note (draft diagnosis / suggested next step) for a technician to review. No public-facing reply is sent in this case.
- **`event: "update"` where the ticket's status just became "Solved"** — check `search_kb` for an existing article covering the same issue.
  - None found → draft and `create_kb_article` from the ticket's content + solution.
  - One found → do nothing (avoid duplicate KB entries).

## Security / guardrails

- `hermes-bot` has no delete rights on tickets (verified) and no rights/user management.
- The MCP tool surface has no delete/admin actions on any itemtype — this is the actual boundary the agent operates within regardless of the underlying account's raw GLPI rights, so even a prompt-injected or misbehaving agent run can't remove data via this path.
- One narrower exception to "mirrors the account's own restriction," found and accepted during Task 6: `hermes-bot`'s Technician profile was granted `KNOWBASEADMIN` (see Environment section) so its own `search_kb` calls can see articles it previously created. This specific right also makes `canUpdateItem()`/`canDeleteItem()` return true unconditionally for *any* KB article (bypassing the normal ownership/visibility checks) — i.e., the account itself can now edit or delete KB content it doesn't own, at the GLPI level. This does not widen the agent's actual capability, since the `glpi` MCP server still exposes no update/delete tool for `KnowbaseItem` — only `search_kb` and `create_kb_article` — but it is a real change to the account's raw rights, scoped to the Knowledge Base only (Tickets are unaffected).
- The relay is unreachable from outside the compose network (no published port).
- Public-facing replies (`is_private=false`) are gated entirely by the skill's confidence check — default is to stay private/internal when uncertain, matching the "semi-autonomous" approval from brainstorming.

## Error handling

- OAuth token expiry (1h): the MCP server refreshes proactively before expiry using the stored `refresh_token`; a hard failure (e.g. revoked token) surfaces as a tool error rather than crashing the server.
- Relay unreachable / Hermes gateway down: GLPI's own retry (`sent_try`, `Number of retries` field on the webhook definition) covers transient failures; no additional retry logic needed in the relay itself.
- Malformed or partial ticket payloads: the triage skill should skip gracefully (leave an internal note flagging the issue) rather than fail the whole run.
- Duplicate KB articles: always `search_kb` before `create_kb_article`.

## Testing plan

1. Unit-level: `hermes webhook test glpi-ticket --payload '<captured sample payload>'` to exercise the triage skill without needing a live GLPI event.
2. End-to-end: create a real test ticket in GLPI → confirm the relay receives and forwards it → confirm Hermes runs the triage skill → confirm the expected followup (public or private) appears on the ticket.
3. Resolution path: mark a test ticket "Solved" → confirm a KB article is created (or correctly skipped if one already exists).
4. Confirm `hermes-bot` still cannot delete tickets/KB articles after any profile changes made during implementation.

## Out of scope for v1

- Category/queue allowlisting (may be added later if the "Hermes decides" approach proves too permissive).
- Multi-entity / multi-language GLPI setups.
- Automatic ticket status transitions beyond adding a solution (e.g., auto-closing tickets).

## Known follow-ups from design-time testing

- Two test artifacts remain in GLPI from verification and should be cleaned up manually: ticket `#1` (`[TEST hermes-bot] Vérification API`) and ticket `#2` (`Je suis fatigué!`, created to trigger the webhook capture).
- The temporary `hermes-capture-test` GLPI webhook and the `glpi-capture` Hermes subscription/script are throwaway verification artifacts and should be removed once the real `glpi-ticket` subscription is in place.
