---
name: glpi-ticket-triage
description: "GLPI ticket triage: reply and auto-resolve from KB when confident, else internal note; capitalize resolved tickets into the KB."
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
     check): call `mcp__glpi__escalate_ticket(ticket_id=<item.id>,
     reason=<your diagnosis and a suggested next step for the
     technician>)` — this one call both assigns the ticket to a human
     **and** posts your diagnosis as the private note explaining why, so
     it is never left unassigned with nobody noticing. **Do not also call
     `mcp__glpi__add_followup` here** — `escalate_ticket` already posts
     the private note itself; calling both would post the same diagnosis
     twice. Do not guess a public reply in this case — escalation is
     always the safe default. **Never call `mcp__glpi__add_solution`
     here** — only a confident public reply (above) or a human ever
     resolves a ticket.

### `event: "update"` where the ticket's status just changed to "Solved"

The payload's `item.status.name` is `"Solved"` (or the equivalent in GLPI's
configured language) when this applies. Skip this branch entirely for any
other status value.

1. Call `mcp__glpi__get_ticket_solution(ticket_id=<item.id>)` first — this
   is the ticket's actual, formal resolution (a distinct record from any
   followup), which may have been written by a human technician and may
   say something different from any earlier diagnosis you or anyone else
   left as a followup along the way. If it returns a result, treat its
   `content` as the authoritative description of what actually fixed the
   issue — use it, not your own or anyone else's earlier guesses, as the
   basis for step 4 below. If it returns `None` (no formal solution
   recorded — unusual but possible), fall back to
   `mcp__glpi__get_ticket_followups` and use the most recent entry instead.
2. Call `mcp__glpi__search_kb` using the ticket's title/content, same as above.
3. If a clearly-matching article already exists: do nothing (avoid duplicate
   KB entries).
4. If none exists: call `mcp__glpi__create_kb_article` with:
   - `name`: a short, reusable title for the underlying issue — generalize
     it rather than copying the ticket's own title verbatim if it is too
     specific or personal (e.g. "Ticket #123: mon imprimante ne marche pas"
     → "Résoudre un problème d'impression réseau").
   - `content`: the general problem plus the **actual** solution from
     step 1, written to stand alone without needing the original ticket
     for context. Do not substitute a generic troubleshooting checklist
     here if the real solution names a specific cause (a driver, an
     update, a setting, a part) — say what it actually was.
   - `is_faq`: `False` — leave end-user-facing FAQ visibility to a human
     reviewer; this only populates the internal KB.

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

## Guardrails

- The only write actions that are ever appropriate here are `add_followup`
  (only for a confident public reply, or the confirms-fixed close-out in
  the update branch — never to post a private diagnosis, see below),
  `add_solution` (only alongside a confident public `add_followup`, never
  alone and never for a private/internal note), `create_kb_article`,
  `assign_self` (only alongside a confident, resolving `add_followup`), and
  `escalate_ticket` (only when explicitly deciding to hand off to a human --
  never as a substitute for a real diagnosis) (plus
  `search_kb`/`search_tickets`/`get_ticket`/`get_ticket_images`/
  `get_ticket_followups`/`get_ticket_solution` for reads). Never attempt to delete a ticket, or
  touch user/rights data — the `glpi` MCP server does not expose those
  actions, and the underlying GLPI account does not have the rights for
  them either. `escalate_ticket` only ever assigns the one pre-configured
  escalation user -- there is no tool for assigning an arbitrary person or
  group.
- `escalate_ticket` always posts its own private followup (the `reason`
  argument) — never call `add_followup(is_private=True, ...)` right before
  or after it with the same or similar content, or the diagnosis is posted
  twice on the ticket.
- When unsure whether a match is confident enough for a public reply,
  default to a private/internal followup instead. A wrong technician-facing
  note is easy to correct; a wrong public reply — or a ticket auto-resolved
  on a wrong answer — reaches the requester directly.
