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


if __name__ == "__main__":
    pass
