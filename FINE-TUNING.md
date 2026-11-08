# Fine-tuning the model on Deckartes' tools

A plan for tuning the chat model (`LiquidAI/LFM3-1.7B-MLX-4bit`) on the app's 33 tools. Nothing here is built yet.

The goal is fewer tool mistakes: not calling a tool when one is needed, asking for values already in the conversation, and saying something was done before the tool has run.

**Not confirmed yet:** Liquid's fine-tuning docs cover LFM2 and LFM2.5, with nothing on LFM3 or MLX. Whether LFM3 trains smoothly with those tools has to be tested. The example counts by category below are estimates.

## Collecting data

Most of the data should be generated from the tools, with real logs correcting it. A survey after every tool use shouldn't be the main source, for three reasons:

- It only appears when a tool ran, so it misses the worst failure, where no tool ran at all. The "18 to 20" routing bug produced no tool call to rate.
- A "bad" rating isn't training data. Training needs the correct call, not just a verdict.
- Rating every tool call gets tedious quickly, and answers stop being reliable.

### What to add to the app

- **Silent logging of every turn:** messages, routed tools, tool calls and tool results, stored locally and kept out of git, because logs hold calendar and file data.
- **A `/bad` command, or one key, on the last reply.** It flags any turn, including ones where no tool ran. The correct call is then written by hand or by a stronger model.
- **Signals the app already has:** the user declining the calendar popup, tool errors, `ctrl+c`, the model asking for a value, and the user rephrasing a question.

### Sources, by amount

1. **Generated, most of the set.** A stronger model writes varied questions for each tool, in every language actually used, each with the correct call.
2. **Corrected failures from the logs.** A smaller set but the most valuable, because it shows real phrasing.
3. **Multi-turn follow-ups.** "Add this event", "18 to 20", "yes", "use defaults".

### How much

[Liquid suggests about 500 to 5,000 examples](https://docs.liquid.ai/lfm/fine-tuning/overview), and says quality and matching real inputs matter more than volume. Estimated split for this app:

| Category | Examples |
|---|---|
| Single tool calls, 20 to 30 per tool | about 800 |
| Follow-ups that reuse earlier values | 200 to 300 |
| No tool should be called | about 200 |
| Answers after a tool result, without claiming success early | 100 to 200 |
| **Training total** | **1,300 to 1,500** |
| Test set, never trained on, ideally real questions | 150 to 200 |

Real usage alone would take months at around 20 tool turns a day.

### Format rules

- Build examples through the app's real prompt: same system message, time line, routed tools and chat template.
- Store them as structured data and render them at training time, so changing a tool description doesn't invalidate the set.
- Make targets contain the tool call with no text before it, which should also reduce the narration before tool calls.
- Use LFM3's [XML tool format](https://huggingface.co/LiquidAI/LFM3-1.7B), `<tool_call><function=name><parameter=p>`. Liquid's general guide describes a Pythonic format from older models.

## Training and deployment

- Train a LoRA with [Unsloth](https://docs.liquid.ai/lfm/fine-tuning/unsloth) or [TRL](https://docs.liquid.ai/docs/fine-tuning/trl). [LEAP Finetune](https://leap.liquid.ai/docs/finetuning) is Liquid's own alternative.
- Use a rented GPU. Training on a 16 GB M1 isn't worth planning around.
- No adapter option was found in Rapid-MLX's CLI, so plan to merge the LoRA into the weights and convert back to MLX 4-bit.
- Point `config.model` at the result and add its parsers to `PARSER_FLAGS` in `mlx_server.py` if needed.

## Order of work

1. **Baseline.** Build the test set and score the current model. Without it there's no way to tell whether tuning helped, and some failures are cheaper to fix in prompts, tool descriptions or the router.
2. **Logging and `/bad`.** Useful even if the model is never tuned.
3. **Pilot.** About 300 examples aimed at follow-ups and premature claims, compared against the baseline.
4. **Scale up** only if the pilot clearly wins.
