---
name: writing-tests
description: Use when writing or updating tests in tests/, adding coverage for new code, or fixing a bug that needs a regression test. Covers this repo's end-to-end test style, shared fakes and naming.
---

# Writing tests

Test end to end: enter through the highest public entry point and fake only the system boundaries. A test proves a behaviour the user or the model sees, not how the code gets there.

## Entry points, highest first

1. **The app**: Textual pilot, `app.run_test()` (recipe in [docs/ui.md](../../../docs/ui.md)). For anything visible in the UI.
2. **A whole turn**: `Conversation.stream_reply` with only the model faked (`tests/test_conversation.py`). For routing, the tool loop, compaction, events. The Telegram bot drives the same turn (`tests/test_telegram_bot.py`).
3. **A tool**: call `tools.<tool>(...)` and `assertEqual` the exact text the model gets ([docs/adding-a-tool.md](../../../docs/adding-a-tool.md)).
4. **Live**: `test_live_*.py`, real Rapid-MLX. Start `asyncSetUp` with `require_live(self)` (`tests/live.py`): it runs only with `LIVE=1` and the chat server up. Only for what fakes can't show (server flags, streaming format, real routing scores); judging the model's answers belongs in `/validate`.

Go one level lower only when the level above can't reach the case cheaply. Private helpers (`_name`) and backend internals are covered through these, never tested directly.

## Fakes: boundaries only

| Boundary | Fake |
| --- | --- |
| HTTP | subclass `HttpTest`, `self.serve({path: response})` (`tests/http_fake.py`) |
| Clock | `freeze(self, "2026-11-11 12:00")` (`tests/frozen_clock.py`) |
| Model | `FakeClient` (`tests/test_conversation.py`) over `mlx_client.stream_chat`/`complete`; patch `mlx_server.ensure_chat` before mounting `Chat` |
| Embeddings | `FakeEmbeddings` (`tests/test_tool_router.py`) over `mlx_client.embed` |
| Calendar | [docs/calendar-tests.md](../../../docs/calendar-tests.md) |
| Files | real `tempfile.TemporaryDirectory()` |
| Settings | `mock.patch.object(config, ...)` |

Undo every patch with `addCleanup`. Wait with `await pilot.pause()`, never `sleep`.

## Keep the suite lean

- Before writing, read the area's existing test file. Extend a test or add a case to it rather than adding a near-copy.
- One test per distinct behaviour. Cover the happy path, each error the user or model sees, and the boundary that actually broke or could break; skip permutations that run the same code path.
- A bug fix gets one regression test, written first and seen failing.
- Name tests as behaviour sentences: `test_score_is_from_the_teams_side_when_listed_second`.
## Run

```bash
PYTHONPATH=src venv/bin/python -m unittest discover tests
PYTHONPATH=src:tests venv/bin/python -m unittest test_cs_tools -k results   # one file, filtered
LIVE=1 PYTHONPATH=src:tests venv/bin/python -m unittest discover tests -p 'test_live_*.py'   # live only, server up
venv/bin/ruff check .
```

Done when the new test fails without the change, the full suite passes and Ruff is clean.
