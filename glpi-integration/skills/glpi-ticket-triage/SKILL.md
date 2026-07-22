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
