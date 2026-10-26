+++
id = "010"
title = "Assistant chat remembers key points across restarts"
tier = "standard"
depends = ["009"]
+++

## Goal
After a restart the assistant knows the crucial points of earlier conversations, not only the visible transcript. Keep two separate stores per project:
- `chat.json`: the full transcript for display (from task 009). Not sent to the model.
- `chat_memory.md`: a short running memory of key facts, decisions and open threads. This is what the model gets back.

## What's wrong today
Task 009 restores the transcript only. The Claude session starts empty, so the model has no memory of earlier turns after a restart.

## Where
- agentdesk/server.py: `Chat` class (`_client`, `_ask`, `ask`, `reset`), `CHAT_PROMPT`.
- Storage: `data/projects/<slug>/chat_memory.md` via `self.p.path("chat_memory.md")`.

## Do
1. Memory file: `chat_memory.md` holds plain Markdown with sections such as "Decisions", "Facts about the project", "Open questions", "User preferences". Cap it at about 1500 words. When it grows past the cap, the summarizer must shorten it.
2. Inject memory: when `_client()` builds the system prompt, add the content of `chat_memory.md` (if present) under a heading like "What you remember from earlier conversations". Build it when the session is created, so each new session gets the latest memory.
3. Update memory: after each successful `ask()`, run one short, tool-free model call (use the chat model setting, or haiku) with the old memory plus the last user and assistant messages. It returns the full updated memory. Write it to `chat_memory.md` with a temp file and replace. Do this in the same lock, after the reply is saved to `chat.json`, so the user never waits for it to be shown. If it fails, keep the old memory and log it. Do not count its cost in `chat_usage.json` unless it is easy; if you skip it, say so in the report.
4. Reset: `reset()` clears `chat.json`, `chat_memory.md`, and the session.
5. Error path: if a session errors and is recreated, it gets the same memory on the next creation.

## Constraints
- Keep `chat.json` and `chat_memory.md` separate. The transcript is never sent to the model.
- The memory-update call must not use tools and must not change the permission rules in `can_use_tool`.
- Memory must not contain secrets or file contents. Store key facts only.
- Keep the updater's output format fixed (Markdown with the section headings above). Reject an empty or obviously broken reply and keep the old memory.

## Done when
- Tell the assistant a fact and a decision, restart the server, ask about them: the model answers correctly without being told again.
- `chat_memory.md` stays under the word cap after many turns.
- Reset empties both files and the model no longer knows the earlier facts.
- A failing memory update does not break the chat.
- Tests in `tests/test_chat.py` cover: memory injected into the prompt, memory written after an ask, reset clearing memory (mock the model call).
