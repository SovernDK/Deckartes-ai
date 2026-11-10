"""Validation runs: how well the loaded model picks and calls tools, to guide fine-tuning.

validation_cases.json holds the questions and what each should do. A run plays every case through
the real Conversation and tool_router, with fake tools, a frozen clock and the Assistant preset, so
two models get identical inputs. The scores count only what tuning the chat model can fix. How the
router did goes to a file of its own, for tuning the router. No UI here: Chat starts a run and shows report().

The cases are a held-out test set. Don't train on them word for word.
"""

import datetime as dt
import inspect
import json
import re
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import aclosing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from agent import tool_router
from agent.conversation import MAX_TOOL_ROUNDS, Conversation, Failed, Text, ToolsDone, ToolStarted
from agent.prompts import current_time_line
from core import clock
from core.config import PROJECT_ROOT, config
from tools.tools import TOOL_GROUPS, TOOLS, TOOLS_BY_NAME

CASES_FILE = PROJECT_ROOT / "validation_cases.json"
RESULTS_FOLDER = PROJECT_ROOT / "validation_results"
PRESET = "Assistant"
CATEGORIES = ("explicit", "paraphrased", "obfuscated", "follow-up", "no-tool", "chained", "after-result")
WEAKEST_TOOLS = 10

CASE_FIELDS = {"id", "category", "question", "expect", "ordered", "forbid", "arguments", "carry", "results",
               "history", "tempt", "claim_patterns"}
ADD_EVENT = "add_calendar_event"
# Wording that says something was done; used on replies where it wasn't, or was declined
CLAIM_WORDS = [r"\b(added|scheduled|saved|booked|created)\b", r"\b(on|in|to) your calendar\b"]
NEGATION = re.compile(r"n['’]t\b|\b(not|never|nothing|unable|cannot)\b", re.IGNORECASE)
CLAIM_NEGATION_REACH = 40

@dataclass
class Case:
    id: str
    category: str
    question: str
    # One entry per required step; each lists the tools that would satisfy it
    expect: list[list[str]]
    ordered: bool = False
    forbid: list[str] = field(default_factory=list)
    # Tool -> parameter -> text the value must contain, or a list of acceptable texts
    arguments: dict[str, dict] = field(default_factory=dict)
    # The same shape, for values that must come from an earlier tool's result
    carry: dict[str, dict] = field(default_factory=dict)
    # Fake results replacing the defaults for this case
    results: dict[str, str] = field(default_factory=dict)
    # Scripted earlier turns: {"question", "reply"}
    history: list[dict] = field(default_factory=list)
    # Groups to send on a no-tool case, so the model has something to resist
    tempt: list[str] = field(default_factory=list)
    # Regexes that count as claiming success; replaces the default wording
    claim_patterns: list[str] = field(default_factory=list)

    @property
    def must_be_in_order(self) -> bool:
        return self.ordered or self.category == "chained"

    @property
    def expected_groups(self) -> list[str]:
        """The groups of the expected tools, without the tempting ones."""
        tools = {tool for step in self.expect for tool in step}
        return [name for name, group in TOOL_GROUPS.items() if tools & set(group["tools"])]

@dataclass
class Suite:
    now: str
    timezone: str
    default_results: dict[str, str]
    cases: list[Case]

    def moment(self) -> dt.datetime:
        """The time a run is frozen at."""
        return dt.datetime.fromisoformat(self.now).replace(tzinfo=ZoneInfo(self.timezone))

    def category_counts(self) -> dict[str, int]:
        counts = Counter(case.category for case in self.cases)
        return {category: counts[category] for category in CATEGORIES if counts[category]}

@dataclass
class Call:
    name: str
    # None when the model's arguments weren't valid JSON
    arguments: dict | None

@dataclass
class Outcome:
    calls: list[Call]
    reply: str
    looped: bool = False

@dataclass
class Score:
    tool_pass: bool
    full_pass: bool
    # Why it failed; None when it passed
    label: str | None

@dataclass
class CaseResult:
    case: Case
    calls: list[Call]
    reply: str
    narration: bool
    router_missed: bool
    # None when the case errored
    score: Score | None
    error: str | None = None

# Checking the case file

def _require(condition: bool, case_id: str, problem: str) -> None:
    if not condition:
        raise ValueError(f"validation case {case_id!r}: {problem}")

def _check_tools(names, case_id: str) -> None:
    for name in names:
        _require(name in TOOLS_BY_NAME, case_id, f"unknown tool {name!r}")

def _check_value_checks(checks: dict, case: Case) -> None:
    expected = {tool for step in case.expect for tool in step}
    _check_tools(checks, case.id)
    for tool, parameters in checks.items():
        _require(tool in expected, case.id, f"checks arguments of {tool}, which it doesn't expect")
        known = inspect.signature(TOOLS_BY_NAME[tool]).parameters
        for parameter in parameters:
            _require(parameter in known, case.id, f"{tool} has no parameter {parameter!r}")

def _result_for(case: Case, tool: str, defaults: dict[str, str]) -> str:
    return case.results.get(tool, defaults[tool])

def _check_carried(case: Case, defaults: dict[str, str]) -> None:
    """A carried value has to be found in an earlier step's result, or the model couldn't know it."""
    for tool, parameters in case.carry.items():
        earlier = [_result_for(case, name, defaults) for step in case.expect if tool not in step for name in step]
        for value in parameters.values():
            options = value if isinstance(value, list) else [value]
            found = any(option.lower() in result.lower() for option in options for result in earlier)
            _require(found, case.id, f"carried value {value!r} is in no earlier tool's result")

def _parse_case(data: dict, defaults: dict[str, str]) -> Case:
    case_id = str(data.get("id"))
    for name in ("id", "category", "question", "expect"):
        _require(name in data, case_id, f"missing field {name!r}")
    unknown = set(data) - CASE_FIELDS
    _require(not unknown, case_id, f"unknown fields {sorted(unknown)}")
    _require(data["category"] in CATEGORIES, case_id, f"unknown category {data['category']!r}")
    steps = [step if isinstance(step, list) else [step] for step in data["expect"]]
    case = Case(**{**data, "expect": steps})

    _check_tools([tool for step in case.expect for tool in step], case.id)
    _check_tools(case.forbid, case.id)
    _check_tools(case.results, case.id)
    _check_value_checks(case.arguments, case)
    _check_value_checks(case.carry, case)
    for group in case.tempt:
        _require(group in TOOL_GROUPS, case.id, f"unknown tempting group {group!r}")
    for pattern in case.claim_patterns:
        try:
            re.compile(pattern)
        except re.error as e:
            _require(False, case.id, f"broken claim pattern {pattern!r}: {e}")
    for turn in case.history:
        _require(set(turn) == {"question", "reply"}, case.id, "each history turn needs a question and a reply")

    if case.category == "no-tool":
        _require(not case.expect, case.id, "a no-tool case can't expect a tool")
        _require(bool(case.tempt), case.id, "a no-tool case needs tempt groups to send")
    else:
        _require(bool(case.expect), case.id, "expect can't be empty")
    if case.category == "follow-up":
        _require(bool(case.history), case.id, "a follow-up needs history")
    if case.category == "chained":
        _require(len(case.expect) >= 2, case.id, "a chain needs at least two steps")
    if case.category == "after-result":
        expected = {tool for step in case.expect for tool in step}
        _require(bool(expected & set(case.results)), case.id, "an after-result case needs its own results")
    _check_carried(case, defaults)
    return case

def parse_suite(data: dict) -> Suite:
    """A Suite from the case file's data. Raises ValueError naming the first problem found."""
    for name in ("now", "timezone", "default_results", "cases"):
        if name not in data:
            raise ValueError(f"validation cases: missing {name!r}")
    defaults = data["default_results"]
    missing = sorted(set(TOOLS_BY_NAME) - set(defaults))
    if missing:
        raise ValueError(f"validation cases: no default result for {', '.join(missing)}")
    cases = [_parse_case(case, defaults) for case in data["cases"]]
    repeated = [name for name, count in Counter(case.id for case in cases).items() if count > 1]
    if repeated:
        raise ValueError(f"validation cases: id used twice: {', '.join(repeated)}")
    suite = Suite(data["now"], data["timezone"], defaults, cases)
    try:
        suite.moment()
    except (KeyError, ValueError) as e:
        raise ValueError(f"validation cases: can't read the time {suite.now!r} in {suite.timezone!r}: {e}") from e
    return suite

def load_suite(path: Path = CASES_FILE) -> Suite:
    """Read and check the case file. Raises ValueError if it can't be used."""
    try:
        return parse_suite(json.loads(path.read_text()))
    except OSError as e:
        raise ValueError(f"could not read {path.name}: {e}") from e

# Scoring

def _value_matches(actual, expected) -> bool:
    options = expected if isinstance(expected, list) else [expected]
    return any(option.lower() in str(actual).lower() for option in options)

def _arguments_usable(call: Call) -> bool:
    if call.arguments is None:
        return False
    try:
        inspect.signature(TOOLS_BY_NAME[call.name]).bind(**call.arguments)
    except (TypeError, KeyError):
        return False
    return True

def _call_passes(call: Call, checks: dict) -> bool:
    return _arguments_usable(call) and all(
        name in call.arguments and _value_matches(call.arguments[name], expected) for name, expected in checks.items())

def _arguments_ok(case: Case, calls: list[Call]) -> bool:
    """Each step has a call with usable arguments that pass its checks."""
    return all(any(_call_passes(call, case.arguments.get(call.name, {})) for call in calls if call.name in step)
               for step in case.expect)

def _carried_ok(case: Case, calls: list[Call]) -> bool:
    return all(any(_call_passes(call, checks) for call in calls if call.name == tool)
               for tool, checks in case.carry.items())

def _steps_met(case: Case, names: list[str]) -> bool:
    if not case.must_be_in_order:
        return all(any(name in step for name in names) for step in case.expect)
    position = 0
    for step in case.expect:
        while position < len(names) and names[position] not in step:
            position += 1
        if position == len(names):
            return False
        position += 1
    return True

def _says_done(reply: str) -> bool:
    """Whether the reply says it was done, skipping "I haven't added it" and the like."""
    for pattern in CLAIM_WORDS:
        for match in re.finditer(pattern, reply, re.IGNORECASE):
            before = reply[max(0, match.start() - CLAIM_NEGATION_REACH):match.start()]
            if not NEGATION.search(re.split(r"[.!?\n]", before)[-1]):
                return True
    return False

def _false_claim(case: Case, outcome: Outcome, names: list[str]) -> bool:
    adds_event = any(ADD_EVENT in step for step in case.expect)
    if case.category != "after-result" and not (adds_event and ADD_EVENT not in names):
        return False
    if case.claim_patterns:
        return any(re.search(pattern, outcome.reply, re.IGNORECASE) for pattern in case.claim_patterns)
    return _says_done(outcome.reply)

def score(case: Case, outcome: Outcome) -> Score:
    """Whether the right tools ran (tool_pass), whether everything else was right too (full_pass),
    and the first label that fits a failure."""
    names = [call.name for call in outcome.calls]
    forbidden = any(name in case.forbid for name in names)
    over_called = case.category == "no-tool" and bool(names)
    steps_met = _steps_met(case, names)
    tool_pass = steps_met and not forbidden and not over_called and not outcome.looped

    claimed = _false_claim(case, outcome, names)
    if claimed:
        label = "false_claim"
    elif case.category == "follow-up" and not names and "?" in outcome.reply:
        label = "asked_for_value"
    elif case.expect and not names:
        label = "missed_call"
    elif over_called:
        label = "over_call"
    elif forbidden:
        label = "wrong_tool"
    elif not steps_met:
        started = any(name in step for step in case.expect for name in names)
        label = "broken_chain" if case.category == "chained" and started else "wrong_tool"
    elif not _arguments_ok(case, outcome.calls):
        label = "bad_arguments"
    elif not _carried_ok(case, outcome.calls):
        label = "broken_chain"
    elif outcome.looped:
        label = "looped"
    else:
        label = None
    return Score(tool_pass, label is None, label)

# Running

def _router_record(case: Case, selection: tool_router.Selection, question: str, previous: str | None) -> dict:
    expected = case.expected_groups
    chosen = selection.groups
    extra = [group for group in chosen or [] if group not in expected]
    if chosen is None:
        status = "error"
    elif any(not any(tool in TOOL_GROUPS[group]["tools"] for group in chosen for tool in step) for step in case.expect):
        status = "missed"
    else:
        status = "extra" if extra else "correct"
    return {"id": case.id, "category": case.category, "question": question, "previous": previous,
            "expected_groups": expected, "chosen_groups": chosen, "extra_groups": extra,
            "scores": selection.scores, "status": status}

def _with_case_groups(case: Case, selection: tool_router.Selection) -> tool_router.Selection:
    """The router's selection plus the groups the case is testing, so every case tests the model."""
    if selection.groups is None:
        return selection
    keep = {tool.__name__ for tool in selection.tools or []}
    for group in [*case.expected_groups, *case.tempt]:
        keep.update(TOOL_GROUPS[group]["tools"])
    return tool_router.Selection([tool for tool in TOOLS if tool.__name__ in keep], selection.groups, selection.scores)

@contextmanager
def _run_settings(suite: Suite) -> Iterator[None]:
    """The fixed preset, routing and time of a run; the user's come back afterwards."""
    saved = config.system_prompt, config.tool_routing, config.timezone
    config.system_prompt, config.tool_routing, config.timezone = PRESET, True, suite.timezone
    try:
        with clock.frozen_at(suite.moment()):
            yield
    finally:
        config.system_prompt, config.tool_routing, config.timezone = saved

class Run:
    """One validation of the loaded model. Finished cases stay in results if it's stopped."""

    def __init__(self, suite: Suite, categories: list[str] | None = None) -> None:
        self.suite = suite
        self.cases = [case for case in suite.cases if categories is None or case.category in categories]
        self.model = config.model
        self.results: list[CaseResult] = []
        self.router_records: list[dict] = []
        self.stopped = False

    async def execute(self, progress: Callable[[int, int], None] | None = None) -> None:
        """Play the cases. Cancelling it stops the run; call report() and save() either way."""
        try:
            with _run_settings(self.suite):
                for done, case in enumerate(self.cases, start=1):
                    self.results.append(await self._play(case))
                    if progress is not None:
                        progress(done, len(self.cases))
        except BaseException:
            self.stopped = True
            raise

    async def _play(self, case: Case) -> CaseResult:
        calls: list[Call] = []
        record: dict = {}

        async def select(question: str, previous: str | None) -> tool_router.Selection:
            selection = await tool_router.select_tools(question, previous)
            record.update(_router_record(case, selection, question, previous))
            return _with_case_groups(case, selection)

        async def run_fake_tool(name: str, arguments: dict) -> str:
            # The ToolStarted event for this call has just added it
            calls[-1].arguments = arguments
            tool = TOOLS_BY_NAME.get(name)
            if tool is None:
                return f"Unknown tool: {name}"
            try:
                inspect.signature(tool).bind(**arguments)
            except TypeError as e:
                return f"Tool {name} failed: {e}"
            return _result_for(case, name, self.suite.default_results)

        conversation = Conversation(tool_runner=run_fake_tool, tool_selector=select)
        for turn in case.history:
            conversation.history += [{"role": "system", "content": current_time_line()},
                                     {"role": "user", "content": turn["question"]},
                                     {"role": "assistant", "content": turn["reply"]}]
        reply = before_call = ""
        narration = False
        rounds = 0
        failure = None
        async with aclosing(conversation.stream_reply(case.question)) as events:
            async for event in events:
                if isinstance(event, Text):
                    reply += event.text
                    before_call += event.text
                elif isinstance(event, ToolStarted):
                    calls.append(Call(event.name, None))
                    narration = narration or bool(before_call.strip())
                    before_call = ""
                elif isinstance(event, ToolsDone):
                    rounds += 1
                    before_call = ""
                elif isinstance(event, Failed):
                    failure = event.message

        reply = reply.strip()
        if record:
            self.router_records.append(record)
        missed = record.get("status") == "missed"
        looped = failure is not None and rounds >= MAX_TOOL_ROUNDS
        if failure is not None and not looped:
            return CaseResult(case, calls, reply, narration, missed, None, failure)
        return CaseResult(case, calls, reply, narration, missed, score(case, Outcome(calls, reply, looped)))

    # Results

    @property
    def scored(self) -> list[CaseResult]:
        return [result for result in self.results if result.score is not None]

    @property
    def errors(self) -> int:
        return sum(result.score is None for result in self.results)

    @property
    def status(self) -> str:
        if self.stopped or len(self.results) < len(self.suite.cases):
            return "partial"
        return "not comparable" if self.errors else "complete"

    def category_rates(self, attribute: str) -> dict[str, float]:
        """Percent of the scored cases passing, per category."""
        rates = {}
        for category in CATEGORIES:
            scores = [r.score for r in self.scored if r.case.category == category]
            if scores:
                rates[category] = 100 * sum(getattr(s, attribute) for s in scores) / len(scores)
        return rates

    def _headline(self, attribute: str) -> float | None:
        rates = self.category_rates(attribute)
        return sum(rates.values()) / len(rates) if rates else None

    @property
    def tool_score(self) -> float | None:
        """The right tools in the right order, each category counting equally."""
        return self._headline("tool_pass")

    @property
    def full_score(self) -> float | None:
        """The tool score plus arguments, carried values and no false claims."""
        return self._headline("full_pass")

    def failures(self) -> Counter:
        return Counter(r.score.label for r in self.scored if r.score.label)

    def weakest_tools(self) -> list[tuple[str, int, int]]:
        """(tool, passed, cases) of the tools with failures, worst first; a step counts for its first tool."""
        tally: dict[str, list[int]] = {}
        for result in self.scored:
            for step in result.case.expect:
                counts = tally.setdefault(step[0], [0, 0])
                counts[0] += result.score.full_pass
                counts[1] += 1
        failing = [(tool, passed, total) for tool, (passed, total) in tally.items() if passed < total]
        return sorted(failing, key=lambda item: (item[1] / item[2], item[0]))[:WEAKEST_TOOLS]

    def narration_rate(self) -> float | None:
        """Percent of the cases with a tool call that had text before one."""
        called = [result for result in self.scored if result.calls]
        return 100 * sum(result.narration for result in called) / len(called) if called else None

    def router_summary(self) -> dict | None:
        """Share of cases with no expected group missed, and how many extra groups were sent."""
        records = [record for record in self.router_records if record["status"] != "error"]
        if not records:
            return None
        missed = sum(record["status"] == "missed" for record in records)
        return {"cases": len(records), "missed": missed,
                "no_group_missed_percent": 100 * (len(records) - missed) / len(records),
                "average_extra_groups": sum(len(record["extra_groups"]) for record in records) / len(records)}

    def save(self, folder: Path = RESULTS_FOLDER) -> tuple[Path, Path]:
        """Write the model's results and the router's records; returns their paths."""
        folder.mkdir(parents=True, exist_ok=True)
        stamp = clock.now().strftime("%Y%m%d-%H%M%S")
        model_path = folder / f"{stamp}-{self.model.replace('/', '_')}.json"
        router_path = folder / f"{stamp}-router.json"
        model_path.write_text(json.dumps(self._model_data(), indent=1, ensure_ascii=False))
        router_path.write_text(json.dumps({
            "embed_model": config.embed_model, "chat_model": self.model, "status": self.status,
            "summary": self.router_summary(), "records": self.router_records,
        }, indent=1, ensure_ascii=False))
        return model_path, router_path

    def _model_data(self) -> dict:
        return {
            "model": self.model,
            "status": self.status,
            "tool_score": self.tool_score,
            "full_score": self.full_score,
            "tool_by_category": self.category_rates("tool_pass"),
            "full_by_category": self.category_rates("full_pass"),
            "failures": dict(self.failures()),
            "narration_percent": self.narration_rate(),
            "errors": self.errors,
            "settings": {"preset": PRESET, "temperature": config.temperature, "top_k": config.top_k,
                         "repetition_penalty": config.repetition_penalty, "max_tokens": config.max_tokens,
                         "enable_thinking": config.enable_thinking, "context_length": config.context_length,
                         "embed_model": config.embed_model},
            "cases": [self._case_data(result) for result in self.results],
        }

    @staticmethod
    def _case_data(result: CaseResult) -> dict:
        score_ = result.score
        return {"id": result.case.id, "category": result.case.category, "question": result.case.question,
                "tool_pass": score_.tool_pass if score_ else None, "full_pass": score_.full_pass if score_ else None,
                "label": score_.label if score_ else None, "error": result.error, "narration": result.narration,
                "router_missed": result.router_missed,
                "calls": [{"name": call.name, "arguments": call.arguments} for call in result.calls],
                "reply": result.reply}

# The report shown in the chat

STATUS_NOTES = {
    "partial": "**Status: partial.** Not every case ran, so these scores can't be compared with a full run.",
    "not comparable": "**Status: not comparable.** Some cases hit a server error and were left out.",
}

def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"

def report(run: Run, saved: list[Path]) -> str:
    """The results of a run, as Markdown."""
    lines = [f"## Validation: {run.model}", ""]
    if run.status in STATUS_NOTES:
        lines += [STATUS_NOTES[run.status], ""]
    lines += [(f"**Full score {_number(run.full_score)}** · Tool score {_number(run.tool_score)} "
              f"· {len(run.scored)} of {len(run.cases)} cases scored"), ""]
    tool, full = run.category_rates("tool_pass"), run.category_rates("full_pass")
    lines += ["| Category | Tool | Full |", "|---|---|---|"]
    lines += [f"| {category} | {tool[category]:.1f} | {full[category]:.1f} |" for category in tool]
    failures = run.failures()
    if failures:
        lines += ["", "| Failure | Cases |", "|---|---|"]
        lines += [f"| {label} | {count} |" for label, count in failures.most_common()]
    weakest = run.weakest_tools()
    if weakest:
        lines += ["", "**Weakest tools:** " + ", ".join(f"{name} {passed}/{total}" for name, passed, total in weakest)]
    lines += ["", f"**Narration before a tool call:** {_number(run.narration_rate())}%"]
    router = run.router_summary()
    if router:
        lines += ["", (f"**Router (not in the scores):** {router['no_group_missed_percent']:.1f}% of cases with no "
                      f"expected group missed, {router['average_extra_groups']:.1f} extra groups per case")]
    if saved:
        lines += ["", "**Saved:** " + ", ".join(str(path) for path in saved)]
    return "\n".join(lines)
