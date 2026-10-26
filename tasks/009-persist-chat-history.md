+++
id = "009"
title = "Persist assistant chat history to disk"
tier = "mechanical"
+++

## Goal
The assistant chat history survives a server restart. Right now `Chat.history` (agentdesk/server.py, class `Chat`) is only an in-memory list, so every restart empties the chat for every project.

## What's wrong today
- `Chat.ask()` appends the user and assistant messages to `self.history` only.
- `Chat.reset()` clears that list.
- The only chat data written to disk is `chat_usage.json` (costs and tokens), written in `_ask()`.
- The dashboard rebuilds the chat from `GET /api/p/<slug>/chat`, which returns `chat(p).history`, so a saved history shows up with no frontend change.

## Where
- agentdesk/server.py: `Chat.__init__`, `Chat.ask`, `Chat.reset`, `chat()`.
- Storage file: `data/projects/<slug>/chat.json`, next to `chat_usage.json`. Use `self.p.path("chat.json")`.

## Do
1. Load: when `Chat` is created for a project (in `chat()` or `Chat.__init__`), read `chat.json` if it exists and set `self.history` from it. Load per project on first use, not for all projects at server startup. A missing or unreadable file means an empty history, not a crash.
2. Save: after each append in `ask()` (both the user message and the assistant reply), write the full list to `chat.json`. Write to a temp file and replace it, the same way `core.py` `save()` does, so a crash mid-write cannot leave a broken file.
3. `reset()`: also delete or empty `chat.json`.
4. Keep the existing `chat_usage.json` behaviour unchanged.

## Constraints
- Only the visible transcript is restored. The Claude session (`self.client`) starts fresh after a restart, so the model does not remember earlier turns. Do not try to restore the SDK session.
- Keep the same entry shape: `{"role", "text"}` plus `tools` and `cost` for assistant entries.
- Use UTF-8 with `ensure_ascii=False`, as `core.py` does.
- Do not change the frontend unless a test shows it needs to.
- Do not touch the permission rules in `can_use_tool`.

## Done when
- Send a message in the assistant chat, restart the server, reopen the project: the earlier messages and replies are shown.
- Reset clears the chat and the file, and it stays empty after a restart.
- A corrupted or missing `chat.json` gives an empty chat and the app still starts.
- Existing tests in `tests/test_chat.py` still pass, and a test covers save and reload (the test can use a temporary project directory).
