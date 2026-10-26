+++
id = "014"
title = "Bash approval card in the chat doesn't close after the answer"
tier = "standard"
+++

## Goal
After the user clicks Allow once / Always allow / Deny on a command approval card in the chat, the card disappears,
and the command runs (on Allow) or is refused (on Deny) without a second prompt.

## What's wrong today
Reported: a Bash command-run approval popup in the chat stays on screen after the answer.

Where it goes through (read-only check, not yet reproduced):
- `agentdesk/web/index.html` ~line 340-353: `pollPending` re-renders the cards only when the set of ids changes;
  `answer` posts the choice, then clears `pendingKey` and polls again. The card can only go away once the server
  stops listing its id.
- `agentdesk/server.py` ~line 768-772: the endpoint returns 404 "that request isn't waiting any more" when
  `Approvals.resolve` finds no entry; the page shows that as a note, but the card stays until the next poll.
- `agentdesk/server.py` ~line 176-220: `Approvals.ask` awaits a Future on the SDK loop; `resolve` sets it through
  `loop.call_soon_threadsafe`; `ask` removes the item in `finally`. If the Future is never resolved (for example the
  loop given to `Approvals` is not the one running the SDK, or the loop is blocked), the id stays listed and the card
  stays.
- Check also whether a chat reset or mode switch (`Chat.reset`) replaces `Approvals` while a card is open, leaving
  an old id in the page.

## Where
- `agentdesk/server.py`: `Approvals`, `Chat.permission`, `Chat` construction (which loop is passed), the pending
  endpoints.
- `agentdesk/web/index.html`: `pollPending` / `answer`.
- Tests in `tests/` for the approval queue (allow once, always allow, deny, timeout) — extend them.

## Constraints
- All work happens in an isolated container with a cloned test app, never on the live app or its `data/`.
- Don't weaken the approval rules: nothing runs before Allow; deny and timeout still deny.
- Don't change the guard or the allow-list format.
- Tests: `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider` in `tests/`; temp `AGENTDESK_HOME`.

## Done when
- Reproduced in the clone: a Bash approval card, then Allow once → the card disappears and the command runs once;
  Deny → the card disappears and the command doesn't run; Always allow → it disappears and the same prefix no longer asks.
- A card whose request has already ended (timeout, reset) disappears on the next poll.
- A test covers the case that caused the stuck card; the full test run passes.
- Report the root cause found, the files changed, and what was tested.
