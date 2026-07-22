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
from mcp.server.fastmcp import FastMCP, Image

API_URL = os.environ["GLPI_API_URL"].rstrip("/")
CLIENT_ID = os.environ["GLPI_OAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["GLPI_OAUTH_CLIENT_SECRET"]
GLPI_USER = os.environ["GLPI_USER"]
GLPI_PASSWORD = os.environ["GLPI_PASSWORD"]
ESCALATION_USER = os.environ["GLPI_ESCALATION_USER"]

mcp = FastMCP("glpi")


class GLPIClient:
    def __init__(self):
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        self._user_id_cache = {}

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

    def resolve_user_id(self, username):
        if username in self._user_id_cache:
            return self._user_id_cache[username]
        users = self.request("GET", "/Administration/User") or []
        for user in users:
            if user.get("username") == username:
                self._user_id_cache[username] = user["id"]
                return user["id"]
        raise ValueError(f"No GLPI user found with username {username!r}")


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
def search_kb(query: str = "", limit: int = 50) -> list:
    """List Knowledge Base articles for you to read and judge relevance
    yourself. Call with no `query` to get up to `limit` articles and
    review their name/content directly -- this is the recommended way to
    search: GLPI's `query` filter (an RSQL substring match, e.g.
    name=like="*wifi*") only catches an exact literal substring and
    silently misses any variant (accents, hyphenation like "wifi" vs
    "Wi-Fi", synonyms), so a guessed keyword is not a reliable way to
    rule an article out."""
    params = {"limit": limit}
    if query:
        params["filter"] = query
    return glpi.request("GET", "/Knowledgebase/Article", params=params)


@mcp.tool()
def create_kb_article(name: str, content: str, is_faq: bool = False) -> dict:
    """Create a new Knowledge Base article."""
    return glpi.request(
        "POST",
        "/Knowledgebase/Article",
        json={"name": name, "content": content, "is_faq": is_faq},
    )


MAX_IMAGES_PER_TICKET = 5


@mcp.tool()
def get_ticket_images(ticket_id: int) -> list:
    """Fetch image attachments (screenshots/photos) linked to a ticket, as
    viewable images. Non-image documents (PDF, Word, etc.) are not
    supported yet and are skipped. Capped at the first 5 image
    attachments found on the ticket."""
    links = glpi.request("GET", f"/Assistance/Ticket/{ticket_id}/Timeline/Document") or []
    images = []
    for link in links:
        document_id = link["item"]["documents_id"]
        meta = glpi.request("GET", f"/Management/Document/{document_id}") or {}
        mime = meta.get("mime") or ""
        if not mime.startswith("image/"):
            continue
        data = glpi.request(
            "GET", f"/Management/Document/{document_id}/Download", raw=True
        )
        images.append(Image(data=data, format=mime.split("/")[-1]))
        if len(images) >= MAX_IMAGES_PER_TICKET:
            break
    return images


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
def get_ticket_solution(ticket_id: int):
    """Fetch the ticket's formal solution (the record GLPI creates when a
    ticket is marked Solved -- distinct from ordinary followups), as
    {content, author_name, date}, or None if the ticket has no solution
    yet. When capitalizing a resolved ticket into the KB, this is the
    authoritative resolution -- prefer it over any earlier diagnosis
    notes (including your own) if they disagree, since a human may have
    solved it differently than you first guessed."""
    entries = glpi.request(
        "GET", f"/Assistance/Ticket/{ticket_id}/Timeline/Solution"
    ) or []
    if not entries:
        return None
    latest = entries[-1]["item"]
    return {
        "content": latest["content"],
        "author_name": (latest.get("user") or {}).get("name"),
        "date": latest["date_creation"],
    }


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
