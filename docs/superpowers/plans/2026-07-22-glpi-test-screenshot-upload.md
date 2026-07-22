# GLPI Test-Screenshot Upload Script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A script that creates a fresh GLPI test ticket and attaches a real, synthetically-generated screenshot to it, so `get_ticket_images` can be re-verified end-to-end without a human manually pasting an image into the browser.

**Architecture:** GLPI's OAuth REST API cannot upload file content (confirmed by reading GLPI 11.0.8's HL API source — `ResourceAccessor::getInputParamsBySchema()` drops any field not in the public OpenAPI schema, so an internal field like `_filename` never reaches `Document::add()`). The script therefore mixes two auth mechanisms: the existing OAuth password grant for ticket operations, and a classic session-cookie login for the one operation OAuth can't do — driving GLPI's own `ajax/fileupload.php` + `front/document.form.php` the same way the browser's rich-text editor does.

**Tech Stack:** Python 3, `requests`, `Pillow` (both already present on the host — confirmed via `python3 -c "import PIL, requests"`), GLPI REST API v2.3 (OAuth2 password grant) + GLPI's classic session-based web forms.

## Global Constraints

- Single file: `glpi-integration/scripts/attach_test_screenshot.py`, run directly on the host (not in a container) — reads `GLPI_API_URL`, `GLPI_OAUTH_CLIENT_ID`, `GLPI_OAUTH_CLIENT_SECRET`, `GLPI_USER`, `GLPI_PASSWORD` from the environment (same vars `mcp-server/server.py` uses).
- `BASE_URL` (e.g. `http://localhost:8080`) is derived from `GLPI_API_URL` (e.g. `http://localhost:8080/api.php/v2.3`) by stripping the `/api.php/...` suffix — there is no separate env var for it.
- GLPI's AJAX endpoints (`isXmlHttpRequest() === true`, i.e. requests sent with an `X-Requested-With: XMLHttpRequest` header) require the CSRF token as an `X-Glpi-Csrf-Token` **header**. Classic (non-AJAX) form POSTs like `front/document.form.php` require it as a `_glpi_csrf_token` **body field** instead. Mixing these up produces a silent `403 Access denied` — confirmed live during design (see the spec's "Validation performed during design" section).
- `front/document.form.php`'s `add` action auto-links the new `Document` to any item passed via `itemtype`/`items_id` in the same POST (`Document::post_addItem()`) — no separate link call needed.
- No unit tests with mocked HTTP calls anywhere in this plan except for the pure image-generation function (Task 3) — per the design spec's Testing section, this exact gap was already deliberately deferred to live verification once before; mocking these specific HTTP calls would only test our own assumptions about GLPI's internals, not anything real. Every other task's "test" is a live run against the dev GLPI instance (`http://localhost:8080`, already running via `docker-compose.yml` in the repo root).
- Every live-check step below will create a real disposable ticket (and sometimes a document) in the running dev GLPI instance. This is expected and matches the design's explicit "no cleanup" scope — leftover test tickets/documents are fine to ignore or clean up manually later, `hermes-bot` cannot delete tickets anyway.
- Full background and every validated fact this plan relies on: `docs/superpowers/specs/2026-07-22-glpi-test-screenshot-upload-design.md`.

---

## Task 1: OAuth ticket creation

**Files:**
- Create: `glpi-integration/scripts/attach_test_screenshot.py`

**Interfaces:**
- Produces: `get_oauth_token() -> str`; `create_ticket(token: str, name: str, content: str) -> int`; module-level `API_URL: str`, `BASE_URL: str` (both derived from env at import time).

- [ ] **Step 1: Create the file with env loading and the OAuth helpers**

```python
#!/usr/bin/env python3
"""Attach a real screenshot to a fresh GLPI test ticket, end-to-end.

GLPI's OAuth REST API cannot upload file content on this instance (see
docs/superpowers/specs/2026-07-22-glpi-test-screenshot-upload-design.md
for why) so this script mixes OAuth (for ticket operations) with a
classic session-cookie login (for the upload GLPI's own rich-text
editor uses).

Requires: pip install requests pillow
"""
import io
import os
import re
import sys

import requests
from PIL import Image, ImageDraw

API_URL = os.environ["GLPI_API_URL"].rstrip("/")
BASE_URL = API_URL.split("/api.php")[0]
CLIENT_ID = os.environ["GLPI_OAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["GLPI_OAUTH_CLIENT_SECRET"]
GLPI_USER = os.environ["GLPI_USER"]
GLPI_PASSWORD = os.environ["GLPI_PASSWORD"]


def get_oauth_token() -> str:
    """Password-grant OAuth token, same flow as mcp-server/server.py."""
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
    return resp.json()["access_token"]


def create_ticket(token: str, name: str, content: str) -> int:
    resp = requests.post(
        f"{API_URL}/Assistance/Ticket",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": name, "content": content},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["id"]


if __name__ == "__main__":
    pass
```

- [ ] **Step 2: Get real credentials from the running containers and export them**

Run: `for v in GLPI_API_URL GLPI_OAUTH_CLIENT_ID GLPI_OAUTH_CLIENT_SECRET GLPI_USER GLPI_PASSWORD; do echo "export $v=$(docker exec hermes-glpi printenv $v)"; done > /tmp/glpi_env.sh && source /tmp/glpi_env.sh`

Expected: no output from `source` (it just sets the 5 variables in your shell).

- [ ] **Step 3: Live-check ticket creation**

Run: `cd glpi-integration/scripts && python3 -c "
import attach_test_screenshot as a
token = a.get_oauth_token()
print('token ok, len', len(token))
ticket_id = a.create_ticket(token, '[plan check] Task 1', 'temporary, safe to delete')
print('created ticket', ticket_id)
"`

Expected: prints `token ok, len <some number>` then `created ticket <integer>` with no traceback.

- [ ] **Step 4: Commit**

```bash
git add glpi-integration/scripts/attach_test_screenshot.py
git commit -m "feat: OAuth ticket creation for GLPI test-screenshot script"
```

---

## Task 2: Classic session login

**Files:**
- Modify: `glpi-integration/scripts/attach_test_screenshot.py`

**Interfaces:**
- Consumes: `BASE_URL`, `GLPI_USER`, `GLPI_PASSWORD` (Task 1).
- Produces: `classic_login() -> tuple[requests.Session, str]` — returns `(session, csrf_token)`; the session's cookie jar carries the authenticated PHP session for later steps, `csrf_token` is a fresh token scraped from the post-login page (required by Tasks 4 and 5).

- [ ] **Step 1: Add the login helper**

Add to `glpi-integration/scripts/attach_test_screenshot.py`, after `create_ticket`:

```python
def classic_login() -> tuple[requests.Session, str]:
    """Log in via GLPI's classic (non-OAuth) form, for the one operation
    (file upload) the OAuth REST API cannot do."""
    session = requests.Session()
    login_page = session.get(f"{BASE_URL}/index.php", timeout=15)
    login_page.raise_for_status()
    csrf_match = re.search(r'_glpi_csrf_token"\s*value="([^"]*)"', login_page.text)
    if not csrf_match:
        raise RuntimeError("Could not find CSRF token on GLPI login page")

    resp = session.post(
        f"{BASE_URL}/front/login.php",
        data={
            "_glpi_csrf_token": csrf_match.group(1),
            "login_name": GLPI_USER,
            "login_password": GLPI_PASSWORD,
        },
        timeout=15,
    )
    resp.raise_for_status()
    if "central.php" not in resp.url:
        raise RuntimeError(f"GLPI login failed, landed on {resp.url} instead of central.php")

    fresh_csrf_match = re.search(r'_glpi_csrf_token"\s*value="([^"]*)"', resp.text)
    if not fresh_csrf_match:
        raise RuntimeError("Could not find CSRF token on post-login page")
    return session, fresh_csrf_match.group(1)
```

- [ ] **Step 2: Live-check login**

Run: `cd glpi-integration/scripts && python3 -c "
import attach_test_screenshot as a
session, csrf = a.classic_login()
print('logged in, cookies:', bool(session.cookies))
print('csrf token len:', len(csrf))
"`

Expected: `logged in, cookies: True` and `csrf token len: <some number>`, no traceback.

- [ ] **Step 3: Commit**

```bash
git add glpi-integration/scripts/attach_test_screenshot.py
git commit -m "feat: classic session login for GLPI test-screenshot script"
```

---

## Task 3: Synthetic test image

**Files:**
- Modify: `glpi-integration/scripts/attach_test_screenshot.py`
- Test: `glpi-integration/scripts/test_attach_test_screenshot.py`

**Interfaces:**
- Produces: `generate_test_image() -> bytes` (PNG bytes); module constants `FABRICATED_ERROR_CODE: str`, `FABRICATED_MODEL: str`.

This is the one function with no network calls, so it's the one place a real unit test adds value — same rationale as the design spec's Testing section.

- [ ] **Step 1: Write the failing test**

Create `glpi-integration/scripts/test_attach_test_screenshot.py`:

```python
import os
import unittest

os.environ.setdefault("GLPI_API_URL", "http://testserver/api.php/v2.3")
os.environ.setdefault("GLPI_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GLPI_OAUTH_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GLPI_USER", "test-user")
os.environ.setdefault("GLPI_PASSWORD", "test-password")

import attach_test_screenshot as a


class TestGenerateTestImage(unittest.TestCase):
    def test_returns_valid_png_bytes(self):
        data = a.generate_test_image()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")

    def test_image_is_reasonably_sized(self):
        data = a.generate_test_image()
        self.assertGreater(len(data), 100)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd glpi-integration/scripts && python3 -m unittest test_attach_test_screenshot -v`
Expected: `AttributeError: module 'attach_test_screenshot' has no attribute 'generate_test_image'`

- [ ] **Step 3: Implement `generate_test_image`**

Add to `glpi-integration/scripts/attach_test_screenshot.py`, after the `classic_login` import block (near the top, with the other constants):

```python
FABRICATED_ERROR_CODE = "J-9042"
FABRICATED_MODEL = "Brother HL-L2350DW"


def generate_test_image() -> bytes:
    """A plain synthetic 'screenshot' containing a fabricated detail that
    appears nowhere in the ticket's own text — the same verification
    method already proven on ticket #5 (see design spec)."""
    img = Image.new("RGB", (500, 220), color="white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 490, 210], outline="black", width=2)
    draw.text((30, 40), "PRINTER ERROR", fill="red")
    draw.text((30, 90), f"ERROR CODE: {FABRICATED_ERROR_CODE}", fill="black")
    draw.text((30, 130), f"MODEL: {FABRICATED_MODEL}", fill="black")
    draw.text((30, 170), "Fuser Unit Fault", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd glpi-integration/scripts && python3 -m unittest test_attach_test_screenshot -v`
Expected: `Ran 2 tests` / `OK`

- [ ] **Step 5: Commit**

```bash
git add glpi-integration/scripts/attach_test_screenshot.py glpi-integration/scripts/test_attach_test_screenshot.py
git commit -m "feat: synthetic screenshot generator for GLPI test-screenshot script"
```

---

## Task 4: Upload the file via `ajax/fileupload.php`

**Files:**
- Modify: `glpi-integration/scripts/attach_test_screenshot.py`

**Interfaces:**
- Consumes: `BASE_URL` (Task 1); `requests.Session`/`csrf_token` from `classic_login()` (Task 2).
- Produces: `upload_file(session: requests.Session, csrf_token: str, filename: str, file_bytes: bytes, mime: str) -> str` — returns the server-generated tmp filename GLPI reports back (needed by Task 5).

- [ ] **Step 1: Add the upload helper**

Add to `glpi-integration/scripts/attach_test_screenshot.py`, after `classic_login`:

```python
def upload_file(
    session: requests.Session, csrf_token: str, filename: str, file_bytes: bytes, mime: str
) -> str:
    """Upload bytes into GLPI's tmp storage via the same AJAX endpoint the
    rich-text editor's file widget uses. Returns the tmp filename GLPI
    assigned, to be referenced via `_filename` when creating the Document."""
    resp = session.post(
        f"{BASE_URL}/ajax/fileupload.php",
        files={"filename": (filename, io.BytesIO(file_bytes), mime)},
        data={"name": "filename"},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "X-Glpi-Csrf-Token": csrf_token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    entry = resp.json()["filename"][0]
    if "error" in entry:
        raise RuntimeError(f"GLPI upload rejected the file: {entry['error']}")
    return entry["name"]
```

- [ ] **Step 2: Live-check the upload**

Run: `cd glpi-integration/scripts && python3 -c "
import attach_test_screenshot as a
session, csrf = a.classic_login()
image_bytes = a.generate_test_image()
tmp_name = a.upload_file(session, csrf, 'plancheck.png', image_bytes, 'image/png')
print('uploaded as', tmp_name)
"`

Expected: `uploaded as plancheck.png` (or similar), no traceback.

- [ ] **Step 3: Commit**

```bash
git add glpi-integration/scripts/attach_test_screenshot.py
git commit -m "feat: GLPI tmp-storage upload for test-screenshot script"
```

---

## Task 5: Create and link the Document

**Files:**
- Modify: `glpi-integration/scripts/attach_test_screenshot.py`

**Interfaces:**
- Consumes: `BASE_URL` (Task 1); `requests.Session`/`csrf_token` (Task 2); tmp filename from `upload_file()` (Task 4); ticket id from `create_ticket()` (Task 1).
- Produces: `attach_document(session: requests.Session, csrf_token: str, tmp_filename: str, ticket_id: int, doc_name: str) -> None`.

- [ ] **Step 1: Add the attach helper**

Add to `glpi-integration/scripts/attach_test_screenshot.py`, after `upload_file`:

```python
def attach_document(
    session: requests.Session, csrf_token: str, tmp_filename: str, ticket_id: int, doc_name: str
) -> None:
    """Create the Document and link it to the ticket in one call — GLPI's
    Document::post_addItem() auto-creates the Document_Item link when
    itemtype/items_id are present, same as the real 'Documents' tab widget."""
    resp = session.post(
        f"{BASE_URL}/front/document.form.php",
        data={
            "_glpi_csrf_token": csrf_token,
            "add": "1",
            "name": doc_name,
            "_filename[0]": tmp_filename,
            "_tag_filename[0]": "attach-test-screenshot",
            "_prefix_filename[0]": "",
            "itemtype": "Ticket",
            "items_id": str(ticket_id),
        },
        timeout=15,
        allow_redirects=False,
    )
    if resp.status_code != 302:
        raise RuntimeError(
            f"document.form.php did not redirect as expected: "
            f"{resp.status_code} {resp.text[:300]}"
        )
```

- [ ] **Step 2: Live-check create + attach together**

Run: `cd glpi-integration/scripts && python3 -c "
import attach_test_screenshot as a
token = a.get_oauth_token()
ticket_id = a.create_ticket(token, '[plan check] Task 5', 'temporary, safe to delete')
session, csrf = a.classic_login()
image_bytes = a.generate_test_image()
tmp_name = a.upload_file(session, csrf, 'plancheck5.png', image_bytes, 'image/png')
a.attach_document(session, csrf, tmp_name, ticket_id, 'Plan check 5 attachment')
print('attached to ticket', ticket_id)

import requests
r = requests.get(f'{a.API_URL}/Assistance/Ticket/{ticket_id}/Timeline/Document', headers={'Authorization': f'Bearer {token}'})
print(r.status_code, r.json())
"`

Expected: `attached to ticket <id>`, then `200 [{...'documents_id': <some id>...}]` — a non-empty list.

- [ ] **Step 3: Commit**

```bash
git add glpi-integration/scripts/attach_test_screenshot.py
git commit -m "feat: create and link GLPI Document for test-screenshot script"
```

---

## Task 6: Verification, CLI wiring, and full end-to-end run

**Files:**
- Modify: `glpi-integration/scripts/attach_test_screenshot.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: `verify_attachment(token: str, ticket_id: int) -> dict` (keys: `document_id`, `mime`, `downloaded_bytes`); `main()` (script entry point, no return value).

- [ ] **Step 1: Add the verification helper and `main()`**

Add to `glpi-integration/scripts/attach_test_screenshot.py`, after `attach_document`, replacing the `if __name__ == "__main__": pass` placeholder from Task 1:

```python
def verify_attachment(token: str, ticket_id: int) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    links_resp = requests.get(
        f"{API_URL}/Assistance/Ticket/{ticket_id}/Timeline/Document",
        headers=headers,
        timeout=15,
    )
    links_resp.raise_for_status()
    entries = links_resp.json()
    if not entries:
        raise RuntimeError(f"Ticket {ticket_id} has no linked document after attach")
    document_id = entries[0]["item"]["documents_id"]

    meta_resp = requests.get(
        f"{API_URL}/Management/Document/{document_id}", headers=headers, timeout=15
    )
    meta_resp.raise_for_status()
    mime = meta_resp.json().get("mime")

    download_resp = requests.get(
        f"{API_URL}/Management/Document/{document_id}/Download", headers=headers, timeout=15
    )
    download_resp.raise_for_status()

    return {
        "document_id": document_id,
        "mime": mime,
        "downloaded_bytes": len(download_resp.content),
    }


def main() -> None:
    ticket_name = sys.argv[1] if len(sys.argv) > 1 else "[test] Souci imprimante, voir capture jointe"
    ticket_content = (
        sys.argv[2]
        if len(sys.argv) > 2
        else "Bonjour, j'ai un souci avec l'imprimante, j'ai mis une capture d'écran en pièce jointe."
    )

    token = get_oauth_token()
    ticket_id = create_ticket(token, ticket_name, ticket_content)
    print(f"Created ticket #{ticket_id}")

    session, csrf_token = classic_login()
    print(f"Logged in as {GLPI_USER}")

    image_bytes = generate_test_image()
    tmp_filename = upload_file(session, csrf_token, "test_screenshot.png", image_bytes, "image/png")
    print(f"Uploaded to GLPI tmp storage as {tmp_filename}")

    attach_document(session, csrf_token, tmp_filename, ticket_id, "Test screenshot")
    print(f"Linked document to ticket #{ticket_id}")

    result = verify_attachment(token, ticket_id)
    print(
        f"Verified: document #{result['document_id']}, mime={result['mime']}, "
        f"{result['downloaded_bytes']} bytes downloaded"
    )
    print(f"\nDone. Ticket: {BASE_URL}/front/ticket.form.php?id={ticket_id}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the full script for real**

Run: `cd glpi-integration/scripts && python3 attach_test_screenshot.py`

Expected output (ticket/document ids will differ):
```
Created ticket #11
Logged in as hermes-bot
Uploaded to GLPI tmp storage as test_screenshot.png
Linked document to ticket #11
Verified: document #6, mime=image/png, <N> bytes downloaded
Done. Ticket: http://localhost:8080/front/ticket.form.php?id=11
```

- [ ] **Step 3: Confirm in the GLPI UI**

Open the printed ticket URL in a browser, check the ticket's "Documents" tab (or the timeline) — the synthetic screenshot should be visible and downloadable, showing "PRINTER ERROR", "ERROR CODE: J-9042", "MODEL: Brother HL-L2350DW".

- [ ] **Step 4: Confirm the full triage path picks it up**

Wait for GLPI's webhook cron (~60s), then run:

`docker exec hermes-glpi grep -A 5 "Ticket #<the id from step 2>" /opt/data/logs/agent.log`

and

`docker exec hermes-glpi tail -n 20 /opt/data/logs/agent.log`

Expected: a `tool mcp__glpi__get_ticket_images completed` line with a non-trivial size (not the ~14-char empty-list baseline), and the resulting followup on the ticket (visible in the GLPI UI) references "J-9042" or "Brother HL-L2350DW" — proof the vision path used the real image, not just the ticket's text.

- [ ] **Step 5: Commit**

```bash
git add glpi-integration/scripts/attach_test_screenshot.py
git commit -m "feat: verification, CLI entry point, and end-to-end run for test-screenshot script"
```
