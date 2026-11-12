"""Validation runs (validation.py): the case file's checks, scoring and whole runs.

Only the chat model and the embeddings are faked, so a run goes through the real Conversation
and tool_router. Nothing here calls a real model or a real tool.
"""

import asyncio
import datetime as dt
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import tool_router
from core import clock
from core.config import config
from model import mlx_client
from model.tool_schema import model_signature
from test_conversation import FakeClient, call, chunk
from test_tool_router import FakeEmbeddings
from tools import tools
from tools.tools import TOOLS_BY_NAME
from validation import validation


def suite_data(*cases: dict) -> dict:
    return {"now": "2026-11-11 19:30", "timezone": "Europe/Warsaw",
            "default_results": {name: f"default {name}" for name in TOOLS_BY_NAME}, "cases": list(cases)}

def case_data(**fields) -> dict:
    return {"id": "c", "category": "explicit", "question": "q", "expect": ["get_cpu_usage"], **fields}

def case(**fields) -> validation.Case:
    return validation.parse_suite(suite_data(case_data(**fields))).cases[0]

def Tool(tool_name: str, /, **arguments) -> validation.Call:
    return validation.Call(tool_name, arguments)

def outcome(*calls, reply="ok", looped=False) -> validation.Outcome:
    return validation.Outcome(calls=list(calls), reply=reply, looped=looped)

ADD = {"title": "x", "date": "tomorrow", "start_time": "10:00"}

class ScoreTest(unittest.TestCase):
    def check(self, score, tool: bool, full: bool, label: str | None):
        self.assertEqual((score.tool_pass, score.full_pass, score.label), (tool, full, label))

    def test_the_expected_tool_passes(self):
        self.check(validation.score(case(), outcome(Tool("get_cpu_usage"))), True, True, None)

    def test_any_listed_alternative_passes(self):
        fields = {"expect": [["get_server_overview", "get_server_status"]]}
        self.check(validation.score(case(**fields), outcome(Tool("get_server_status"))), True, True, None)

    def test_extra_tools_are_allowed(self):
        self.check(validation.score(case(), outcome(Tool("get_memory_usage"), Tool("get_cpu_usage"))), True, True, None)

    def test_a_forbidden_tool_is_a_wrong_tool(self):
        result = validation.score(case(forbid=["get_memory_usage"]), outcome(Tool("get_cpu_usage"), Tool("get_memory_usage")))
        self.check(result, False, False, "wrong_tool")

    def test_a_different_tool_is_a_wrong_tool(self):
        self.check(validation.score(case(), outcome(Tool("get_memory_usage"))), False, False, "wrong_tool")

    def test_no_call_is_a_missed_call(self):
        self.check(validation.score(case(), outcome()), False, False, "missed_call")

    def follow_up(self, **fields):
        return case(category="follow-up", history=[{"question": "a", "reply": "b"}], **fields)

    def test_a_follow_up_asking_instead_of_calling_is_asked_for_value(self):
        self.check(validation.score(self.follow_up(), outcome(reply="Which server?")), False, False, "asked_for_value")

    def test_a_follow_up_not_asking_is_a_missed_call(self):
        self.check(validation.score(self.follow_up(), outcome(reply="It is fine.")), False, False, "missed_call")

    def test_a_question_mark_outside_a_follow_up_is_a_missed_call(self):
        self.check(validation.score(case(), outcome(reply="Which server?")), False, False, "missed_call")

    def no_tool(self):
        return case(category="no-tool", expect=[], tempt=["server"])

    def test_no_tool_case_passes_without_a_call(self):
        self.check(validation.score(self.no_tool(), outcome()), True, True, None)

    def test_no_tool_case_with_a_call_is_an_over_call(self):
        self.check(validation.score(self.no_tool(), outcome(Tool("get_cpu_usage"))), False, False, "over_call")

    def team_case(self):
        return case(expect=["get_cs_team"], arguments={"get_cs_team": {"name": "vitality"}})

    def test_an_argument_check_ignores_case_and_matches_part_of_the_value(self):
        self.check(validation.score(self.team_case(), outcome(Tool("get_cs_team", name="Team VITALITY"))), True, True, None)

    def test_a_wrong_argument_passes_the_tool_score_only(self):
        self.check(validation.score(self.team_case(), outcome(Tool("get_cs_team", name="NAVI"))), True, False, "bad_arguments")

    def test_an_argument_may_list_acceptable_values(self):
        fields = {"expect": ["get_calendar_events"], "arguments": {"get_calendar_events": {"date": ["tomorrow", "2026-11-12"]}}}
        self.check(validation.score(case(**fields), outcome(Tool("get_calendar_events", date="2026-11-12"))), True, True, None)

    def test_arguments_only_need_to_be_right_on_one_call(self):
        result = validation.score(self.team_case(), outcome(Tool("get_cs_team", name="NAVI"), Tool("get_cs_team", name="Vitality")))
        self.check(result, True, True, None)

    def test_invalid_json_is_bad_arguments(self):
        self.check(validation.score(case(), outcome(validation.Call("get_cpu_usage", None))), True, False, "bad_arguments")

    def test_arguments_the_tool_cannot_take_are_bad_arguments(self):
        fields = {"expect": ["get_cs_team"]}
        self.check(validation.score(case(**fields), outcome(Tool("get_cs_team", team="NAVI"))), True, False, "bad_arguments")

    def chain(self, **fields):
        return case(category="chained", expect=["get_next_episode", "add_calendar_event"],
                    results={"get_next_episode": "Airs 2026-11-14."}, **fields)

    def test_a_chain_in_order_passes(self):
        self.check(validation.score(self.chain(), outcome(Tool("get_next_episode", name="x"), Tool("add_calendar_event", **ADD))), True, True, None)

    def test_a_chain_out_of_order_is_broken(self):
        self.check(validation.score(self.chain(), outcome(Tool("add_calendar_event", **ADD), Tool("get_next_episode", name="x"))), False, False, "broken_chain")

    def test_a_chain_stopping_early_is_broken(self):
        self.check(validation.score(self.chain(), outcome(Tool("get_next_episode", name="x"), reply="It airs Saturday.")), False, False, "broken_chain")

    def test_a_chain_with_none_of_its_tools_is_a_wrong_tool(self):
        self.check(validation.score(self.chain(), outcome(Tool("get_cpu_usage"))), False, False, "wrong_tool")

    def test_a_value_not_carried_through_a_chain_passes_the_tool_score_only(self):
        fields = {"carry": {"add_calendar_event": {"date": "2026-11-14"}}}
        made = outcome(Tool("get_next_episode", name="x"), Tool("add_calendar_event", **ADD))
        self.check(validation.score(self.chain(**fields), made), True, False, "broken_chain")

    def test_a_carried_value_passes(self):
        fields = {"carry": {"add_calendar_event": {"date": "2026-11-14"}}}
        made = outcome(Tool("get_next_episode", name="x"), Tool("add_calendar_event", **{**ADD, "date": "2026-11-14"}))
        self.check(validation.score(self.chain(**fields), made), True, True, None)

    def test_ordered_outside_a_chain_is_a_wrong_tool_when_reversed(self):
        fields = {"expect": ["get_cpu_usage", "get_memory_usage"], "ordered": True}
        self.check(validation.score(case(**fields), outcome(Tool("get_memory_usage"), Tool("get_cpu_usage"))), False, False, "wrong_tool")

    def test_order_does_not_matter_by_default(self):
        fields = {"expect": ["get_cpu_usage", "get_memory_usage"]}
        self.check(validation.score(case(**fields), outcome(Tool("get_memory_usage"), Tool("get_cpu_usage"))), True, True, None)

    def add_event(self):
        return case(expect=["add_calendar_event"])

    def test_claiming_an_event_was_added_without_the_tool_is_a_false_claim(self):
        self.check(validation.score(self.add_event(), outcome(reply="I've added it to your calendar.")), False, False, "false_claim")

    def test_saying_it_was_not_added_is_not_a_claim(self):
        self.check(validation.score(self.add_event(), outcome(reply="I haven't added it yet.")), False, False, "missed_call")

    def test_saying_nothing_was_added_is_not_a_claim(self):
        self.check(validation.score(self.add_event(), outcome(reply="Nothing was added to your calendar.")), False, False, "missed_call")

    def after_result(self, **fields):
        return case(category="after-result", expect=["add_calendar_event"],
                    results={"add_calendar_event": "The user declined."}, **fields)

    def test_claiming_success_after_a_decline_fails_only_the_full_score(self):
        self.check(validation.score(self.after_result(), outcome(Tool("add_calendar_event", **ADD), reply="Done, I added it.")), True, False, "false_claim")

    def test_reporting_the_decline_passes(self):
        self.check(validation.score(self.after_result(), outcome(Tool("add_calendar_event", **ADD), reply="It was not added, you declined.")), True, True, None)

    def test_a_case_can_set_what_counts_as_a_claim(self):
        fields = {"category": "after-result", "results": {"get_cpu_usage": "Tool failed."}, "claim_patterns": [r"\d+\s*%"]}
        self.check(validation.score(case(**fields), outcome(Tool("get_cpu_usage"), reply="CPU is at 40 %.")), True, False, "false_claim")

    def test_a_case_claim_pattern_replaces_the_default_words(self):
        fields = {"category": "after-result", "results": {"get_cpu_usage": "Tool failed."}, "claim_patterns": [r"\d+\s*%"]}
        self.check(validation.score(case(**fields), outcome(Tool("get_cpu_usage"), reply="I saved nothing.")), True, True, None)

    def test_hitting_the_round_limit_fails_both_scores(self):
        self.check(validation.score(case(), outcome(Tool("get_cpu_usage"), looped=True)), False, False, "looped")

class SuiteChecksTest(unittest.TestCase):
    def assert_rejected(self, mentioning: str, *cases: dict):
        with self.assertRaises(ValueError) as caught:
            validation.parse_suite(suite_data(*cases))
        self.assertIn(mentioning, str(caught.exception))

    def test_unknown_expected_tool_is_rejected(self):
        self.assert_rejected("nope", case_data(expect=["nope"]))

    def test_unknown_alternative_tool_is_rejected(self):
        self.assert_rejected("nope", case_data(expect=[["get_cpu_usage", "nope"]]))

    def test_unknown_forbidden_tool_is_rejected(self):
        self.assert_rejected("nope", case_data(forbid=["nope"]))

    def test_unknown_result_tool_is_rejected(self):
        self.assert_rejected("nope", case_data(results={"nope": "x"}))

    def test_argument_check_on_an_unknown_parameter(self):
        self.assert_rejected("team", case_data(expect=["get_cs_team"], arguments={"get_cs_team": {"team": "x"}}))

    def test_unknown_category_is_rejected(self):
        self.assert_rejected("hard", case_data(category="hard"))

    def test_duplicate_ids_are_rejected(self):
        self.assert_rejected("twice", case_data(), case_data())

    def test_unknown_field_is_rejected(self):
        self.assert_rejected("expects", case_data(expects=[]))

    def test_a_tool_case_needs_an_expected_tool(self):
        self.assert_rejected("expect", case_data(expect=[]))

    def test_a_no_tool_case_expects_nothing(self):
        self.assert_rejected("no-tool", case_data(category="no-tool", tempt=["server"]))

    def test_a_no_tool_case_needs_tempting_groups(self):
        self.assert_rejected("tempt", case_data(category="no-tool", expect=[]))

    def test_unknown_tempting_group_is_rejected(self):
        self.assert_rejected("nope", case_data(category="no-tool", expect=[], tempt=["nope"]))

    def test_a_follow_up_needs_earlier_turns(self):
        self.assert_rejected("history", case_data(category="follow-up"))

    def test_an_after_result_case_needs_its_own_result(self):
        self.assert_rejected("results", case_data(category="after-result"))

    def test_a_chain_needs_two_steps(self):
        self.assert_rejected("two", case_data(category="chained"))

    def test_a_carried_value_must_come_from_a_tool_result(self):
        self.assert_rejected("2026-11-14", case_data(
            category="chained", expect=["get_next_episode", "add_calendar_event"],
            carry={"add_calendar_event": {"date": "2026-11-14"}}))

    def test_broken_claim_pattern_is_rejected(self):
        self.assert_rejected("claim", case_data(claim_patterns=["("]))

    def test_every_tool_needs_a_default_result(self):
        data = suite_data(case_data())
        del data["default_results"]["get_cpu_usage"]
        with self.assertRaises(ValueError) as caught:
            validation.parse_suite(data)
        self.assertIn("get_cpu_usage", str(caught.exception))

    def test_a_time_that_cannot_be_read(self):
        with self.assertRaises(ValueError):
            validation.parse_suite({**suite_data(case_data()), "now": "soon"})

    def test_a_time_zone_that_does_not_exist(self):
        with self.assertRaises(ValueError):
            validation.parse_suite({**suite_data(case_data()), "timezone": "Mars/Base"})

    def test_broken_json_is_reported_as_a_value_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cases.json"
            path.write_text("{")
            with self.assertRaises(ValueError):
                validation.load_suite(path)

class CaseFileTest(unittest.TestCase):
    """The committed validation_cases.json."""

    @classmethod
    def setUpClass(cls):
        cls.suite = validation.load_suite()

    def test_it_passes_every_check(self):
        self.assertGreaterEqual(len(self.suite.cases), 150)

    def test_every_tool_has_a_case_at_each_single_tool_difficulty(self):
        for category in ("explicit", "paraphrased", "obfuscated"):
            covered = {step[0] for c in self.suite.cases if c.category == category for step in c.expect}
            self.assertEqual(covered, set(TOOLS_BY_NAME), category)

    def test_each_special_category_has_at_least_fifteen_cases(self):
        for category in ("follow-up", "no-tool", "chained", "after-result"):
            self.assertGreaterEqual(sum(c.category == category for c in self.suite.cases), 15, category)

    def test_a_perfect_model_passes_every_case(self):
        for c in self.suite.cases:
            calls = [self.perfect_call(c, step[0]) for step in c.expect]
            result = validation.score(c, outcome(*calls, reply="Here is what I found."))
            self.assertEqual((result.full_pass, result.label), (True, None), c.id)

    def perfect_call(self, c: validation.Case, tool: str) -> validation.Call:
        """A call passing the case's checks, with "x" for parameters nothing checks."""
        arguments = {name: "x" for name, parameter in model_signature(TOOLS_BY_NAME[tool]).parameters.items()
                     if parameter.default is inspect.Parameter.empty}
        for checks in (c.arguments.get(tool, {}), c.carry.get(tool, {})):
            arguments.update({name: value[0] if isinstance(value, list) else value for name, value in checks.items()})
        return validation.Call(tool, arguments)

    def test_a_model_that_never_calls_a_tool_passes_only_the_no_tool_cases(self):
        passed = {c.category for c in self.suite.cases if validation.score(c, outcome(reply="I can't help.")).full_pass}
        self.assertEqual(passed, {"no-tool"})

class RunTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tool_router._examples.clear()
        self.addCleanup(tool_router._examples.clear)
        self.patch(config, "model", "org/some-model")
        self.patch(config, "system_prompt", "Another preset")
        self.patch(config, "tool_routing", False)
        self.patch(config, "timezone", "Asia/Tokyo")

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, *rounds) -> FakeClient:
        client = FakeClient(*rounds)
        self.patch(mlx_client, "stream_chat", client.stream_chat)
        self.patch(mlx_client, "complete", client.complete)
        return client

    def route(self, scores: dict) -> None:
        self.patch(mlx_client, "embed", FakeEmbeddings(scores))

    async def run_cases(self, *cases: dict, categories=None, progress=None) -> validation.Run:
        run = validation.Run(validation.parse_suite(suite_data(*cases)), categories)
        await run.execute(progress)
        return run

    def tool_names(self, client: FakeClient, index=0) -> list[str]:
        return [tool.__name__ for tool in client.sent[index]["tools"] or []]

    async def test_a_passing_case_is_recorded_with_its_call_and_reply(self):
        self.route({"q": {"server": 0.9}})
        self.use([chunk(tool_calls=[call("get_cpu_usage")], done=True)], [chunk("CPU is fine.", done=True)])
        run = await self.run_cases(case_data())
        result = run.results[0]
        self.assertTrue(result.score.full_pass)
        self.assertEqual(result.calls, [validation.Call("get_cpu_usage", {})])
        self.assertEqual(result.reply, "CPU is fine.")

    async def test_the_model_gets_the_default_fake_result(self):
        self.route({"q": {"server": 0.9}})
        client = self.use([chunk(tool_calls=[call("get_cpu_usage")], done=True)], [chunk("ok", done=True)])
        await self.run_cases(case_data())
        self.assertEqual(client.sent[1]["messages"][-1]["content"], "default get_cpu_usage")

    async def test_a_case_can_override_the_fake_result(self):
        self.route({"q": {"server": 0.9}})
        client = self.use([chunk(tool_calls=[call("get_cpu_usage")], done=True)], [chunk("ok", done=True)])
        await self.run_cases(case_data(results={"get_cpu_usage": "special"}))
        self.assertEqual(client.sent[1]["messages"][-1]["content"], "special")

    async def test_a_made_up_tool_gets_the_same_answer_as_in_the_app(self):
        self.route({"q": {"server": 0.9}})
        client = self.use([chunk(tool_calls=[call("get_weather")], done=True)], [chunk("ok", done=True)])
        await self.run_cases(case_data())
        self.assertEqual(client.sent[1]["messages"][-1]["content"], "Unknown tool: get_weather")

    async def test_wrong_arguments_get_the_same_answer_as_in_the_app(self):
        self.route({"q": {"server": 0.9}})
        client = self.use([chunk(tool_calls=[call("get_cpu_usage", core=2)], done=True)], [chunk("ok", done=True)])
        await self.run_cases(case_data())
        self.assertEqual(client.sent[1]["messages"][-1]["content"],
                         await tools.run_tool("get_cpu_usage", {"core": 2}))

    async def test_scripted_turns_come_before_the_question(self):
        self.route({"q": {"server": 0.9}, "earlier": {"server": 0.9}})
        client = self.use([chunk("ok", done=True)])
        await self.run_cases(case_data(category="follow-up", history=[{"question": "earlier", "reply": "answer"}]))
        contents = [m["content"] for m in client.sent[0]["messages"]]
        self.assertLess(contents.index("earlier"), contents.index("answer"))
        self.assertLess(contents.index("answer"), contents.index("q"))

    async def test_each_case_starts_a_fresh_conversation(self):
        self.route({"one": {"server": 0.9}, "two": {"server": 0.9}})
        client = self.use([chunk("a", done=True)], [chunk("b", done=True)])
        await self.run_cases(case_data(id="1", question="one"), case_data(id="2", question="two"))
        self.assertNotIn("one", [m["content"] for m in client.sent[1]["messages"]])

    async def test_a_router_miss_is_recorded_and_the_expected_group_is_still_sent(self):
        self.route({"q": {"anime": 0.9, "server": 0.1}})
        client = self.use([chunk("ok", done=True)])
        run = await self.run_cases(case_data())
        self.assertIn("get_cpu_usage", self.tool_names(client))
        self.assertIn("get_next_episode", self.tool_names(client))
        record = run.router_records[0]
        self.assertEqual((record["status"], record["expected_groups"], record["chosen_groups"]), ("missed", ["server"], ["anime"]))
        self.assertTrue(run.results[0].router_missed)

    async def test_router_scores_and_previous_question_are_saved_in_the_record(self):
        self.route({"q": {"server": 0.8, None: 0.1}, "earlier": {"anime": 0.9}})
        self.use([chunk("ok", done=True)])
        run = await self.run_cases(case_data(category="follow-up", history=[{"question": "earlier", "reply": "a"}]))
        record = run.router_records[0]
        self.assertAlmostEqual(record["scores"]["server"], 0.8)
        self.assertEqual(record["previous"], "earlier")

    async def test_exactly_the_expected_groups_is_correct(self):
        self.route({"q": {"server": 0.9}})
        self.use([chunk("ok", done=True)])
        run = await self.run_cases(case_data())
        self.assertEqual(run.router_records[0]["status"], "correct")
        self.assertFalse(run.results[0].router_missed)

    async def test_a_tool_in_no_group_is_never_a_router_miss_since_it_is_always_sent(self):
        self.route({"q": {"anime": 0.9}})
        client = self.use([chunk("ok", done=True)])
        run = await self.run_cases(case_data(expect=["search_library"]))
        self.assertIn("search_library", self.tool_names(client))
        self.assertNotEqual(run.router_records[0]["status"], "missed")
        self.assertFalse(run.results[0].router_missed)

    async def test_more_than_the_expected_groups_is_extra(self):
        self.route({"q": {"server": 0.7, "anime": 0.6}})
        self.use([chunk("ok", done=True)])
        run = await self.run_cases(case_data())
        self.assertEqual(run.router_records[0]["status"], "extra")

    async def test_a_no_tool_case_is_sent_its_tempting_groups(self):
        self.route({"q": {None: 0.9}})
        client = self.use([chunk("It stands for central processing unit.", done=True)])
        run = await self.run_cases(case_data(category="no-tool", expect=[], tempt=["server"]))
        self.assertIn("get_cpu_usage", self.tool_names(client))
        self.assertEqual(run.router_records[0]["status"], "correct")

    async def test_a_no_tool_case_the_router_sent_tools_for_is_extra(self):
        self.route({"q": {"server": 0.9}})
        self.use([chunk("ok", done=True)])
        run = await self.run_cases(case_data(category="no-tool", expect=[], tempt=["server"]))
        self.assertEqual(run.router_records[0]["status"], "extra")

    async def test_a_router_that_could_not_run_is_recorded_and_all_expected_tools_still_sent(self):
        self.patch(mlx_client, "embed", mock.AsyncMock(side_effect=ConnectionError))
        client = self.use([chunk("ok", done=True)])
        run = await self.run_cases(case_data())
        self.assertEqual(run.router_records[0]["status"], "error")
        self.assertIn("get_cpu_usage", self.tool_names(client))

    async def test_invalid_json_from_the_model_is_scored_as_bad_arguments(self):
        self.route({"q": {"server": 0.9}})
        self.use([chunk(tool_calls=[mlx_client.ToolCall("id", "get_cpu_usage", None)], done=True)], [chunk("ok", done=True)])
        run = await self.run_cases(case_data())
        self.assertEqual(run.results[0].score.label, "bad_arguments")

    async def test_text_before_a_tool_call_is_narration_but_still_passes(self):
        self.route({"q": {"server": 0.9}})
        self.use([chunk("Let me check.", [call("get_cpu_usage")], done=True)], [chunk("ok", done=True)])
        run = await self.run_cases(case_data())
        self.assertTrue(run.results[0].narration)
        self.assertTrue(run.results[0].score.full_pass)

    async def test_text_only_after_the_tool_is_not_narration(self):
        self.route({"q": {"server": 0.9}})
        self.use([chunk(tool_calls=[call("get_cpu_usage")], done=True)], [chunk("CPU is fine.", done=True)])
        run = await self.run_cases(case_data())
        self.assertFalse(run.results[0].narration)

    async def test_a_model_that_never_stops_calling_tools_is_looped(self):
        self.route({"q": {"server": 0.9}})
        self.use(*[[chunk(tool_calls=[call("get_cpu_usage")], done=True)] for _ in range(10)])
        run = await self.run_cases(case_data())
        self.assertEqual(run.results[0].score.label, "looped")

    async def test_a_server_failure_is_an_error_excluded_from_the_score(self):
        self.route({"one": {"server": 0.9}, "two": {"server": 0.9}})
        self.use([ConnectionError("server down")], [chunk(tool_calls=[call("get_cpu_usage")], done=True)], [chunk("ok", done=True)])
        run = await self.run_cases(case_data(id="1", question="one"), case_data(id="2", question="two"))
        self.assertEqual(run.results[0].error, "server down")
        self.assertIsNone(run.results[0].score)
        self.assertEqual(run.full_score, 100.0)
        self.assertEqual(run.status, "not comparable")

    async def test_a_clean_full_run_is_complete(self):
        self.route({"q": {"server": 0.9}})
        self.use([chunk(tool_calls=[call("get_cpu_usage")], done=True)], [chunk("ok", done=True)])
        run = await self.run_cases(case_data())
        self.assertEqual(run.status, "complete")

    async def test_unticking_a_category_makes_the_run_partial_and_skips_it(self):
        self.route({"one": {"server": 0.9}})
        client = self.use([chunk("ok", done=True)])
        run = await self.run_cases(case_data(id="1", question="one"), case_data(id="2", question="two", category="paraphrased"),
                                   categories=["explicit"])
        self.assertEqual(run.status, "partial")
        self.assertEqual(len(client.sent), 1)

    async def test_ticking_every_category_in_the_file_is_not_partial(self):
        self.route({"q": {"server": 0.9}})
        self.use([chunk(tool_calls=[call("get_cpu_usage")], done=True)], [chunk("ok", done=True)])
        run = await self.run_cases(case_data(), categories=["explicit"])
        self.assertEqual(run.status, "complete")

    async def test_progress_is_reported_after_each_case(self):
        self.route({"one": {"server": 0.9}, "two": {"server": 0.9}})
        self.use([chunk("a", done=True)], [chunk("b", done=True)])
        seen = []
        await self.run_cases(case_data(id="1", question="one"), case_data(id="2", question="two"),
                             progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen, [(1, 2), (2, 2)])

    async def test_the_run_uses_the_assistant_preset_and_the_frozen_date(self):
        self.route({"q": {"server": 0.9}})
        client = self.use([chunk("ok", done=True)])
        await self.run_cases(case_data())
        system = client.sent[0]["messages"][0]["content"]
        self.assertIn("Wednesday 2026-11-11 (Europe/Warsaw)", system)
        self.assertIn("Current local time: 19:30.", [m["content"] for m in client.sent[0]["messages"]])
        self.assertIn("You are Deckartes", system, "the Assistant preset's prompt, not the user's preset")
        self.assertTrue(self.tool_names(client))

    async def test_settings_and_clock_are_restored_afterwards(self):
        self.route({"q": {"server": 0.9}})
        self.use([chunk("ok", done=True)])
        await self.run_cases(case_data())
        self.assert_restored()

    def assert_restored(self):
        self.assertEqual((config.system_prompt, config.tool_routing, config.timezone), ("Another preset", False, "Asia/Tokyo"))
        self.assertLess(abs(clock.now() - dt.datetime.now(dt.UTC)), dt.timedelta(minutes=1))

    async def stopped_run(self) -> validation.Run:
        self.route({"one": {"server": 0.9}, "two": {"server": 0.9}})
        started = asyncio.Event()
        hang = asyncio.Event()
        self.use([chunk("a", done=True)])
        client_stream = mlx_client.stream_chat
        async def stream(messages, tools=None):
            if any(m["content"] == "two" for m in messages):
                started.set()
                await hang.wait()
            async for delta in client_stream(messages, tools):
                yield delta
        self.patch(mlx_client, "stream_chat", stream)
        run = validation.Run(validation.parse_suite(suite_data(case_data(id="1", question="one"), case_data(id="2", question="two"))))
        task = asyncio.create_task(run.execute())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        return run

    async def test_a_stopped_run_keeps_finished_cases_and_is_partial(self):
        run = await self.stopped_run()
        self.assertEqual(run.status, "partial")
        self.assertEqual([r.case.id for r in run.results], ["1"])

    async def test_a_stopped_run_restores_settings_and_clock(self):
        await self.stopped_run()
        self.assert_restored()

    async def mixed_run(self) -> validation.Run:
        """explicit: 3 of 4 pass fully. no-tool: 0 of 1. paraphrased: right tool, wrong argument."""
        team = {"expect": ["get_cs_team"], "arguments": {"get_cs_team": {"name": "vitality"}}}
        questions = {f"e{n}": {"server": 0.9} for n in range(4)} | {"n": {None: 0.9}, "p": {"cs": 0.9}}
        self.route(questions)
        passing = [[chunk(tool_calls=[call("get_cpu_usage")], done=True)], [chunk("ok", done=True)]]
        self.use(*passing, *passing, *passing, [chunk("no tool", done=True)],
                 [chunk(tool_calls=[call("get_cpu_usage")], done=True)], [chunk("ok", done=True)],
                 [chunk(tool_calls=[call("get_cs_team", name="NAVI")], done=True)], [chunk("ok", done=True)])
        return await self.run_cases(*[case_data(id=f"e{n}", question=f"e{n}") for n in range(4)],
                                    case_data(id="n", question="n", category="no-tool", expect=[], tempt=["server"]),
                                    case_data(id="p", question="p", category="paraphrased", **team))

    async def test_scores_weight_each_category_equally(self):
        run = await self.mixed_run()
        # explicit 3/4, no-tool 0/1, paraphrased 1/1 for tools and 0/1 with arguments
        self.assertAlmostEqual(run.tool_score, (75 + 0 + 100) / 3)
        self.assertAlmostEqual(run.full_score, (75 + 0 + 0) / 3)

    async def test_the_report_gives_the_scores_labels_and_weakest_tools(self):
        run = await self.mixed_run()
        report = validation.report(run, [Path("a.json"), Path("b.json")])
        self.assertIn("58.3", report)
        self.assertIn("25.0", report)
        for expected in ("missed_call", "over_call", "bad_arguments", "get_cs_team", "a.json", "b.json", "org/some-model"):
            self.assertIn(expected, report)

    async def test_the_report_says_when_scores_cannot_be_compared(self):
        run = await self.stopped_run()
        self.assertIn("partial", validation.report(run, []))

    async def test_save_writes_results_and_router_files(self):
        self.route({"q": {"anime": 0.9, "server": 0.1}})
        self.use([chunk("Let me think.", [call("get_cpu_usage")], done=True)], [chunk("ok", done=True)])
        run = await self.run_cases(case_data())
        with tempfile.TemporaryDirectory() as folder:
            model_path, router_path = run.save(Path(folder))
            self.assertEqual(model_path.parent, Path(folder))
            self.assertIn("org_some-model", model_path.name)
            saved = json.loads(model_path.read_text())
            routed = json.loads(router_path.read_text())
        self.assertEqual(saved["model"], "org/some-model")
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["settings"]["temperature"], config.temperature)
        result = saved["cases"][0]
        self.assertEqual((result["id"], result["tool_pass"], result["narration"], result["router_missed"]), ("c", True, True, True))
        self.assertEqual(result["calls"], [{"name": "get_cpu_usage", "arguments": {}}])
        self.assertEqual(routed["embed_model"], config.embed_model)
        self.assertEqual(routed["records"][0]["status"], "missed")
