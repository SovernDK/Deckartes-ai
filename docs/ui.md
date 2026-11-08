# UI

## Verifying UI changes

Verify UI changes headlessly with Textual's pilot instead of launching the app:

```python
app = DecartesApp()
async with app.run_test(size=(100, 30)) as pilot:
    app.query_one(Input).focus(); app.query_one(Input).value = "hi"
    await pilot.press("enter"); await app.workers.wait_for_complete(); await pilot.pause()
    app.save_screenshot("shot.svg")
```

`Input` has to be focused before `pilot.press("enter")` or nothing is submitted. To fake model output, put a `FakeClient` (`tests/test_conversation.py`) in `ollama_client._clients[("async", config.host)]` and give `config.model` a name no earlier run used, since tool support is remembered per model. History is `app.query_one(Chat).conversation.history`.

## Chat flow (`chat.py`)

On submit: `/`-prefixed input goes to `Chat.COMMANDS` (name → method name + description; the help screen reads the descriptions); otherwise `set_busy(True)` swaps the `Input` for the `Spinner` and starts the `fetch_stream` worker. The worker's `finally` always calls `set_busy(False)`. Interrupting (`ctrl+c`, a priority app binding enabled through `check_action` only while `Chat.busy` and no text is selected, so otherwise `ctrl+c` falls through to Textual's copy-selection; `copy_to_clipboard` also pipes to `pbcopy` because some terminals ignore OSC 52) cancels the worker; the worker then removes the unfinished reply bubble.

## File explorer (`file_explorer.py`, key `e`)

A `DirectoryTree` rooted at the home folder whose `filter_paths` keeps only what Finder shows (`disk_scan.is_user_facing`: no dot-files, no `UF_HIDDEN` entries like `~/Library`). The highlighted entry's size is scanned in a thread worker after `SCAN_DELAY`, capped at `SCAN_BUDGET` entries (the details line then says "at least"). Selecting a file dismisses the screen with its `Path`, and `DecartesApp.file_chosen` passes it to `Chat.load_file`.

## Config popup

The config popup (`config_screen.py`, key `c`) edits `system_prompt`, `temperature` and `context_length`; it checks every field before applying any. `context_length` is picked from `config.CONTEXT_LENGTHS` (2K-32K; add values there), and a value set to something else in `config.py` is kept as an extra option. Options under `RECOMMENDED_MIN_CONTEXT` are labelled "little room with tools". The context also decides whether a loaded file is sent whole (`rag.WHOLE_FILE_SHARE`), but only when the file is loaded.

## Token usage

`Conversation.stream_reply` adds each chat call's `prompt_eval_count` / `eval_count` to the `Usage` in the turn's `TurnInfo`, summed over tool rounds, since each round re-sends the conversation. It also sums `eval_duration` for `tokens_per_second`, which is generation time only (no model loading or prompt evaluation); the usage line shows it as "tok/s". `TurnInfo` is one turn's tokens plus what it was sent with (tool groups, loaded file), and `TurnInfo.summary()` writes the usage line, so a change to the line belongs there and is tested without Textual. `Conversation` keeps `last_turn` and `total_usage` (tokens only), counting failed turns too because they still used tokens, and `/clear` resets both. After each reply `Chat.add_usage` mounts a `.usage` line, always. `t` toggles `config.show_token_usage`, which sets the `show-usage` class on `Chat` (CSS shows or hides every line at once) and adds the total to the Output border title (`Chat.update_usage`).

## UI details

The help screen is built from `DecartesApp.BINDINGS` (it uses a binding's `tooltip` when set) plus `Chat.COMMANDS`, so new ones appear without editing the screen. Rich renderables can't use Textual CSS variables like `$accent`; resolve them with `app.get_css_variables()`. Styles are in `chat.tcss`, loaded via `CSS_PATH` relative to `app.py`.
