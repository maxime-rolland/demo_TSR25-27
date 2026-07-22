# GLPI test-screenshot upload script — Design

Date: 2026-07-22
Status: Approved, ready for implementation plan

## Goal

Provide a repeatable way to attach a **real** image to a GLPI test ticket, so the
`get_ticket_images` MCP tool (see
`docs/superpowers/specs/2026-07-22-hermes-glpi-integration-design.md`, Components
section 4) can be re-verified end-to-end without relying on a human manually
pasting a screenshot into the browser each time.

## Background / bug that triggered this

Tickets `#7`, `#8`, `#9` ("Souci imprimante...", "Bourrage papier...") were
created directly via `POST /Assistance/Ticket` with only a `content` field
claiming a screenshot was attached ("j'ai mis une capture d'écran en pièce
jointe"). No file was ever uploaded. Confirmed via direct DB inspection
(`glpi_documents_items` has zero rows for these tickets) and via the API
(`GET /Assistance/Ticket/{id}/Timeline/Document` returns `[]` for all three).
`get_ticket_images` is not buggy here — it correctly returns nothing because
there is nothing to return.

The one ticket with a real attachment, `#5` ("Mon pc Portable Chauffe"), got it
because a real user pasted an image into GLPI's own rich-text ticket editor in
the browser — a session-cookie-authenticated code path, unrelated to the OAuth
REST API `hermes-bot` uses.

## Key finding: the OAuth REST API cannot upload file content

Verified by reading GLPI 11.0.8 source inside the running container
(`/var/www/glpi/src/Glpi/Api/HL/...`) and by live testing against the real API:

- `Glpi\Api\HL\Router::handleRequest()` only special-cases
  `Content-Type: application/json` bodies (decoding and merging into request
  parameters). Multipart bodies are never inspected; `$_FILES` is never read
  anywhere in the High-Level API controller stack.
- The only code in the whole codebase that reads `$_FILES`/`_filename` for
  document creation lives in `APIRest.php` — the **legacy** REST API, already
  confirmed disabled on this instance (see the main design spec's Environment
  section).
- `Glpi\Api\HL\ResourceAccessor::getInputParamsBySchema()` builds the `$input`
  array passed to `$item->add()` **strictly from the OpenAPI-declared schema
  properties** (`name`, `filename`, `mime`, `comment`, `link`, ...). An internal
  field like `_filename` is not a schema property, so even if it's included in
  a JSON body, it is silently dropped before `Document::add()` is ever called.
  Confirmed live: a JSON `POST /Management/Document` with `filename`/`mime` set
  creates a DB row with those text values, but `GET .../Download` 500s — there
  is no real file behind it. A multipart attempt with a legacy-style
  `uploadManifest` field created a document with all-null fields — the file
  was silently ignored.
- `downloadDocument()` (`ManagementController.php`) always reads from
  `GLPI_DOC_DIR/{filepath}` on local disk; it has no special handling for
  `link`-type (external URL) documents either, so that isn't a workaround.

**Conclusion:** real file upload for `Document` is only reachable through
GLPI's classic, session-authenticated, non-REST form flow. This script drives
that flow directly, the same way the browser does.

## Architecture

```
attach_test_screenshot.py (run on host: python3, has `requests` + `Pillow`)
   │
   ├─ 1. OAuth REST (existing hermes-bot client_id/secret + password grant)
   │      POST /Assistance/Ticket            → create the test ticket
   │
   ├─ 2. Classic session login (hermes-bot username/password, cookie jar)
   │      GET  /front/login.php               → scrape CSRF token
   │      POST /front/login.php                → establish PHP session cookie
   │
   ├─ 3. Generate image locally (Pillow)
   │      draws a plain PNG with a fabricated error code + device model
   │
   ├─ 4. Session-authenticated upload
   │      POST /ajax/fileupload.php  (multipart, field "filename")
   │        headers: X-Requested-With: XMLHttpRequest,
   │                 X-Glpi-Csrf-Token: <token scraped from the logged-in page>
   │        → { "filename": [ { "name": "<generated tmp filename>", ... } ] }
   │
   ├─ 5. Session-authenticated classic form
   │      POST /front/document.form.php
   │        add=1, name=..., _filename[0]=<tmp filename>,
   │        _tag_filename[0]=<placeholder>, _prefix_filename[0]='',
   │        itemtype=Ticket, items_id=<ticket id>, _glpi_csrf_token=...
   │      → GLPI's Document::post_addItem() auto-creates the Document_Item
   │        link to the ticket (same as the real "Documents" tab widget)
   │
   └─ 6. Verify (OAuth REST, existing pattern)
          GET /Assistance/Ticket/{id}/Timeline/Document  → confirms the link
          GET /Management/Document/{id}/Download          → confirms real
                                                             image bytes + mime
```

Two auth mechanisms are used deliberately, mirroring how GLPI itself splits
these concerns: OAuth for ticket data (the MCP server's normal path), session
cookie only for the one operation OAuth cannot do.

## Components

Single file: `glpi-integration/scripts/attach_test_screenshot.py`.

- Reuses the same env vars as `mcp-server/server.py`
  (`GLPI_API_URL`, `GLPI_OAUTH_CLIENT_ID`, `GLPI_OAUTH_CLIENT_SECRET`,
  `GLPI_USER`, `GLPI_PASSWORD`) — same credentials, read from the environment,
  not hardcoded. Runnable as: `GLPI_API_URL=... ... python3 attach_test_screenshot.py`.
- CLI surface: no arguments needed for the common case (always creates a fresh
  ticket); accepts an optional ticket name/content override for convenience.
- Prints each step's result (ticket id created, upload response, document id,
  verification result) so a human watching the output can follow along —
  this is a manual verification aid, not a silent batch tool.

## Error handling

Dev/test tooling, not production code: fail fast and loud. Any unexpected
HTTP status at any step raises immediately (`resp.raise_for_status()` /
explicit check + `sys.exit(1)` with the response body printed) rather than
retrying or falling back. No fallback path if session login fails — that's a
signal something about the environment changed and needs a human to look, not
something to paper over.

## Validation performed during design

Every step of the flow above was manually driven end-to-end against the live
dev GLPI instance while writing this spec (`docker exec`-scraped `hermes-bot`
credentials, plain `requests` calls, no script yet) to de-risk the plan before
committing to it:

1. `POST /Assistance/Ticket` (OAuth) → created ticket `#10`.
2. Classic login (`GET /index.php` for CSRF, `POST /front/login.php`) → 200,
   redirected to `central.php` logged in as `hermes-bot`.
3. `POST /ajax/fileupload.php` with `X-Requested-With` +
   `X-Glpi-Csrf-Token` headers → 200, returned tmp filename `dryrun.png`.
4. `POST /front/document.form.php` with `itemtype=Ticket&items_id=10` → 302,
   created `Document` id `5` auto-linked to ticket `#10`.
5. `GET /Assistance/Ticket/10/Timeline/Document` (OAuth) → confirmed the link.
6. `GET /Management/Document/5/Download` (OAuth) → 200, returned the exact
   216 bytes uploaded, `mime: image/png`.

**Leftover from this validation, needs manual cleanup**: ticket `#10`
("[DRY RUN] test attach script validation") and document `#5`. Same category
as the existing "Known follow-ups" note in the main design spec.

## Testing

This exact gap — real file upload through GLPI's API — was already explicitly
deferred to live/manual verification in the original implementation plan
(Task 7: "hand this step to the controller rather than scripting it blind
here"). Mocking the HTTP calls here would only test our own assumptions about
GLPI's internals, not anything real. Consistent with that precedent:

- No unit tests with mocked HTTP for this script.
- Verification = running it against the live dev GLPI instance and checking:
  1. The script's own step 6 output (Timeline/Document link + real
     Download bytes/mime).
  2. The ticket's "Documents" tab in the GLPI UI shows the attached image.
  3. After the ~60s webhook cron cycle, `docker exec hermes-glpi tail -n 20
     /opt/data/logs/agent.log` shows `get_ticket_images` returning a non-empty
     result for the new ticket, and the resulting followup reflects the
     fabricated detail from the image (not just the ticket's own text) — the
     same proof-of-path used for ticket `#5`.

## Out of scope

- No cleanup/deletion of test tickets or documents (matches the existing
  "Known follow-ups" precedent in the main design spec, where ticket
  cleanup is left as a manual note, not automated — `hermes-bot` cannot
  delete tickets by design anyway).
- No support for non-image attachments — mirrors `get_ticket_images`'
  own image-only scope.
- Not wired into the `test_server.py` / `test_relay.py` automated suites.
