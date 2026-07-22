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
