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

`Input` has to be focused before `pilot.press("enter")` or nothing is submitted. Mounting `Chat` starts the real model server, so patch `mlx_server.ensure_chat` first (`tests/test_model_server_ui.py`). To fake model output, patch `mlx_client.stream_chat` and `complete` with a `FakeClient`'s methods (`tests/test_conversation.py`). History is `app.query_one(Chat).conversation.history`.

## Chat flow (`chat.py`)

On submit: `/`-prefixed input goes to `Chat.COMMANDS` (name → method name + description; the help screen reads the descriptions; those in `COMMANDS_WITH_ARGUMENT` get the rest of the line); otherwise `set_busy(True)` swaps the `Input` for the `Spinner` and starts the `fetch_stream` worker. The worker's `finally` always calls `set_busy(False)`. Interrupting (`ctrl+c`, a priority app binding enabled through `check_action` only while `Chat.busy` and no text is selected, so otherwise `ctrl+c` falls through to Textual's copy-selection; `copy_to_clipboard` also pipes to `pbcopy` because some terminals ignore OSC 52) cancels the worker; the worker then removes the unfinished reply bubble.

## File explorer (`file_explorer.py`, key `e`)

A `DirectoryTree` rooted at the home folder whose `filter_paths` keeps only what Finder shows (`disk_scan.is_user_facing`: no dot-files, no `UF_HIDDEN` entries like `~/Library`). The highlighted entry's size is scanned in a thread worker after `SCAN_DELAY`, capped at `SCAN_BUDGET` entries (the details line then says "at least"). Selecting a file, or pressing `l` on a file or folder, dismisses the screen with its `Path`, and `DecartesApp.file_chosen` passes it to `Chat.load_file`, which saves it to the library (`docs/library.md`).

## Config popup

The config popup (`config_screen.py`, key `c`) edits `temperature` and `context_length`; it checks every field before applying any. `context_length` is picked from `config.CONTEXT_LENGTHS` (2K-32K; add values there), and a value set to something else in `config.py` is kept as an extra option. Options under `RECOMMENDED_MIN_CONTEXT` are labelled "little room with tools".

## Default and Detailed mode (key `shift+t`)

`config.detailed` picks the mode; the app always starts in Default. Default shows questions and answers only. Detailed also shows the model's thinking (greyed `.thinking` Statics), the tools it called with their arguments, the usage line under each reply and the total tokens in the Output title. Thinking and usage lines are always mounted, and `DecartesApp.show_mode` sets the `detailed` class on `Chat` (CSS shows or hides them all, earlier replies too) and updates `#mode`, the label at the bottom right of the footer: yellow (`$warning`) for Default, blue (`$primary`) for Detailed. The footer is `#footer_bar`, a row of `Footer` and `#mode`. The binding is `T,shift+t`, since terminals report the key either way, with `show=False` because `#mode` already shows it. Like the other single-key shortcuts it only works while the input isn't focused.

Each streamed reply is a `Reply`: thinking, answer text and tool calls in the order they came, each change starting a new part (a `.thinking` Static, a `Markdown`, or a `.tool-calls` Static with one `called tool: name(key=value)` line per call from `tool_call_line`), and each tool round starting new parts (`Reply.next_round`). Tool calls, like thinking, show only in Detailed mode and are display-only: the history keeps neither. The spinner shows "Thinking..." until a round's first text, then "Answering...", then "Using tool: X" (held `MIN_TOOL_SECONDS`) and back to "Thinking..." for the next round. Tested in `tests/test_display_modes.py`.

## Token usage

`Conversation.stream_reply` adds each chat call's `TokenCount` to the `Usage` in the turn's `TurnInfo`, summed over tool rounds, since each round re-sends the conversation. It also sums `generation_ns` for `tokens_per_second`, which is generation time only (no model loading or prompt evaluation); the usage line shows it as "tok/s". `TurnInfo` is one turn's tokens plus the tool groups it was sent with, and `TurnInfo.summary()` writes the usage line, so a change to the line belongs there and is tested without Textual. `Conversation` keeps `last_turn` and `total_usage` (tokens only), counting failed turns too because they still used tokens, and `/clear` resets both. After each reply `Chat.add_usage` mounts a `.usage` line, always; it shows in Detailed mode, which also adds the total to the Output border title (`Chat.update_usage`).

## UI details

The help screen is built from `DecartesApp.BINDINGS` (it uses a binding's `tooltip` when set) plus `Chat.COMMANDS`, so new ones appear without editing the screen. Rich renderables can't use Textual CSS variables like `$accent`; resolve them with `app.get_css_variables()`. Styles are in `chat.tcss`, loaded via `CSS_PATH` relative to `app.py`.
