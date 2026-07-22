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
