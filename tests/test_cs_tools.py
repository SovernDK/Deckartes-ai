"""The text the Counter-Strike tools give the model, from fake PandaScore and GitHub responses."""

import unittest
from unittest import mock

import httpx

from core.config import config
from frozen_clock import freeze
from http_fake import HttpTest
from tools import tools

NAVI = {"id": 1, "name": "Natus Vincere"}
MOUZ = {"id": 2, "name": "MOUZ"}
MINOR = {"id": 3, "name": "Minor Five"}
OTHER = {"id": 4, "name": "Other Five"}

DETAILS = "BO3, ESL Pro League 2026, Grand final"
STREAM = "https://twitch.tv/esl"

STANDINGS_FILES = "/repos/ValveSoftware/counter-strike_regional_standings/contents/live"
STANDINGS_RAW = "/ValveSoftware/counter-strike_regional_standings/main/live"
STANDINGS = ("| Standing | Points | Team Name | Roster |\n| --- | --- | --- | --- |\n"
             "| 1 | 2010 | Vitality | apEX, ZywOo |\n| 2 | 1900 | MOUZ | Brollan, torzsi |\n")

def standings(year: int = 2026) -> dict:
    return {f"{STANDINGS_FILES}/{year}": [{"name": f"standings_global_{year}_10_06.md"}],
            f"{STANDINGS_RAW}/{year}/standings_global_{year}_10_06.md": STANDINGS}

def match(status: str, first: dict, second: dict, *, scores: tuple[int, int] | None = None, winner: dict | None = None,
          tier: str = "s", **fields) -> dict:
    """A match as PandaScore returns it."""
    payload = {
        "name": "Grand final: match", "status": status, "number_of_games": 3,
        "opponents": [{"opponent": first}, {"opponent": second}],
        "league": {"name": "ESL"}, "serie": {"full_name": "Pro League 2026"}, "tournament": {"tier": tier},
        "winner_id": winner["id"] if winner else None,
        "results": [{"team_id": first["id"], "score": scores[0]}, {"team_id": second["id"], "score": scores[1]}]
                   if scores else [],
    }
    return payload | fields

def finished(first: dict, second: dict, scores: tuple[int, int], winner: dict | None, **fields) -> dict:
    return match("finished", first, second, scores=scores, winner=winner, **{"end_at": "2026-11-10T17:30:00Z"} | fields)

def not_started(first: dict, second: dict, **fields) -> dict:
    return match("not_started", first, second, **{"scheduled_at": "2026-11-13T17:00:00Z"} | fields)

def running(first: dict, second: dict, **fields) -> dict:
    return match("running", first, second, scores=(1, 0),
                 streams_list=[{"main": True, "raw_url": STREAM}], **fields)

TEAM = {"id": 1, "name": "Natus Vincere", "acronym": "NAVI", "location": "UA",
        "players": [{"id": 9, "name": "b1t", "first_name": "Valerii", "last_name": "Vakhovskyi",
                     "nationality": "UA", "age": 23}]}

class CsToolTest(HttpTest):
    def setUp(self):
        freeze(self, "2026-11-11 12:00")
        for name, value in (("pandascore_token", "token"), ("cs_min_tier", "b")):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def serve_results(self, *matches: dict):
        return self.serve({"/csgo/teams": [TEAM], "/csgo/matches/past": list(matches)})

class TeamResultsTest(CsToolTest):
    async def line(self, result: dict) -> str:
        self.serve_results(result)
        return (await tools.get_cs_match_results("Natus Vincere")).split("\n")[-1]

    async def test_header_and_day(self):
        self.serve_results(finished(NAVI, MOUZ, (2, 1), NAVI))
        self.assertEqual(await tools.get_cs_match_results("Natus Vincere"),
                         "Recent results of Natus Vincere, newest first (local time):\n"
                         "Tuesday 2026-11-10:\n"
                         f"- 18:30 won 2-1 vs MOUZ ({DETAILS})")

    async def test_score_is_from_the_teams_side_when_listed_second(self):
        self.assertEqual(await self.line(finished(MOUZ, NAVI, (1, 2), NAVI)), f"- 18:30 won 2-1 vs MOUZ ({DETAILS})")

    async def test_lost(self):
        self.assertEqual(await self.line(finished(NAVI, MOUZ, (0, 2), MOUZ)), f"- 18:30 lost 0-2 vs MOUZ ({DETAILS})")

    async def test_drew(self):
        self.assertEqual(await self.line(finished(NAVI, MOUZ, (1, 1), None)), f"- 18:30 drew 1-1 vs MOUZ ({DETAILS})")

    async def test_forfeit(self):
        self.assertEqual(await self.line(finished(NAVI, MOUZ, (1, 0), NAVI, forfeit=True)),
                         f"- 18:30 won 1-0 vs MOUZ ({DETAILS}, forfeit)")

    async def test_no_results(self):
        self.serve_results()
        self.assertEqual(await tools.get_cs_match_results("Natus Vincere"),
                         "Recent results of Natus Vincere: No finished matches found")

    async def test_no_such_team(self):
        self.serve({"/csgo/teams": []})
        self.assertEqual(await tools.get_cs_match_results("Nope"), "Nope: No team found with that name.")

class TeamUpcomingTest(CsToolTest):
    async def test_a_started_match_is_listed_first(self):
        self.serve({"/csgo/teams": [TEAM], "/csgo/matches/upcoming": [not_started(NAVI, MOUZ)],
                    "/csgo/matches/running": [running(NAVI, MOUZ)]})
        self.assertEqual(await tools.get_cs_upcoming_matches("Natus Vincere"),
                         "Upcoming matches of Natus Vincere, soonest first (local time):\n"
                         f"- LIVE NOW vs MOUZ, score 1-0 ({DETAILS}, stream {STREAM})\n"
                         "Friday 2026-11-13:\n"
                         f"- 18:00 vs MOUZ ({DETAILS})")

class AllMatchesTest(CsToolTest):
    async def test_results_name_the_winner_and_leave_out_small_or_stale_matches(self):
        self.serve({**standings(), "/csgo/matches/past": [
            finished(NAVI, MINOR, (0, 2), MINOR),
            finished(MINOR, OTHER, (2, 0), MINOR, tier="c"),
            finished(MINOR, MOUZ, (0, 2), MOUZ, tier="c", end_at="2026-11-09T17:30:00Z"),
            finished(NAVI, OTHER, (2, 0), NAVI, end_at="2026-09-01T17:30:00Z"),
        ]})
        self.assertEqual(await tools.get_cs_match_results("all"),
                         "Recent results in major CS events, newest first (local time):\n"
                         "Tuesday 2026-11-10:\n"
                         f"- 18:30 Minor Five beat Natus Vincere 2-0 ({DETAILS})\n"
                         "Monday 2026-11-09:\n"
                         f"- 18:30 MOUZ beat Minor Five 2-0 ({DETAILS})")

    SMALL_PAGE = [finished(MINOR, OTHER, (2, 0), MINOR, tier="c")] * 100

    def pages_requested(self, server) -> list[str]:
        return [r.url.params["page"] for r in server.requests if r.url.path == "/csgo/matches/past"]

    async def test_keeps_paging_past_small_matches_until_a_short_page(self):
        major = finished(NAVI, MOUZ, (2, 1), NAVI)
        server = self.serve({**standings(), "/csgo/matches/past":
                             lambda request: self.SMALL_PAGE if request.url.params["page"] == "1" else [major]})
        self.assertIn("Natus Vincere beat MOUZ 2-1", await tools.get_cs_match_results("all"))
        self.assertEqual(self.pages_requested(server), ["1", "2"])

    async def test_stops_paging_once_it_has_enough(self):
        server = self.serve({**standings(), "/csgo/matches/past": [finished(NAVI, MOUZ, (2, 1), NAVI)] * 100})
        await tools.get_cs_match_results("all")
        self.assertEqual(self.pages_requested(server), ["1"])

    async def test_stops_paging_at_the_cap(self):
        server = self.serve({**standings(), "/csgo/matches/past": self.SMALL_PAGE})
        await tools.get_cs_match_results("all")
        self.assertEqual(self.pages_requested(server), ["1", "2", "3", "4"])

    async def test_nothing_major(self):
        self.serve({**standings(), "/csgo/matches/upcoming": [not_started(MINOR, OTHER, tier="c")]})
        self.assertEqual(await tools.get_cs_upcoming_matches("all"),
                         "Upcoming matches in major CS events: No upcoming matches scheduled in major events")

class LiveTest(CsToolTest):
    async def test_major_matches_and_a_count_of_the_rest(self):
        self.serve({**standings(), "/csgo/matches/running": [running(NAVI, MOUZ), running(MINOR, OTHER, tier="c")]})
        self.assertEqual(await tools.get_cs_live_matches(),
                         "Live CS matches now:\n"
                         f"- LIVE NOW Natus Vincere 1-0 MOUZ ({DETAILS}, stream {STREAM})\n"
                         "1 smaller-league match is live but not listed.")

    async def test_several_smaller_matches_are_plural(self):
        self.serve({**standings(), "/csgo/matches/running": [running(NAVI, MOUZ), running(MINOR, OTHER, tier="c"),
                                                             running(OTHER, MINOR, tier="c")]})
        self.assertIn("\n2 smaller-league matches are live but not listed.", await tools.get_cs_live_matches())

    async def test_nothing_live(self):
        self.serve({**standings(), "/csgo/matches/running": []})
        self.assertEqual(await tools.get_cs_live_matches(), "No major matches are being played right now.")

class TeamTest(CsToolTest):
    def serve_team(self, *, past=(), upcoming=(), live=(), team=TEAM):
        self.serve({**standings(), "/csgo/teams": [team], "/csgo/matches/past": list(past),
                    "/csgo/matches/upcoming": list(upcoming), "/csgo/matches/running": list(live)})

    async def test_everything_known(self):
        self.serve_team(past=[finished(MOUZ, NAVI, (1, 2), NAVI)], upcoming=[not_started(NAVI, MOUZ)],
                        live=[running(NAVI, MOUZ)], team=TEAM | {"name": "MOUZ", "id": 2})
        self.assertEqual(await tools.get_cs_team("MOUZ"),
                         "MOUZ (NAVI, UA), Valve rank 2 (1900 points, as of 2026-10-06)\n"
                         "Players: b1t (Valerii Vakhovskyi, UA, 23)\n"
                         f"Playing now: LIVE NOW vs Natus Vincere, score 0-1 ({DETAILS}, stream {STREAM})\n"
                         f"Next match: Friday 2026-11-13 18:00 vs Natus Vincere ({DETAILS})\n"
                         "Recent results (newest first):\n"
                         f"- Tuesday 2026-11-10 18:30: lost 1-2 vs Natus Vincere ({DETAILS})")

    async def test_nothing_scheduled(self):
        self.serve_team(team=TEAM | {"players": []})
        self.assertEqual(await tools.get_cs_team("Natus Vincere"),
                         "Natus Vincere (NAVI, UA), not in Valve's global standings\n"
                         "Players: No roster listed\n"
                         "Playing now: Not playing right now\n"
                         "Next match: No upcoming match scheduled\n"
                         "Recent results: No finished matches found")

    async def test_next_match_without_a_time_says_so(self):
        self.serve_team(upcoming=[not_started(NAVI, MOUZ, scheduled_at=None)])
        self.assertIn(f"\nNext match: time not announced, vs MOUZ ({DETAILS})\n",
                      await tools.get_cs_team("Natus Vincere"))

    async def test_a_forfeit_without_an_end_time_is_dated_by_its_schedule(self):
        self.serve_team(past=[finished(NAVI, MOUZ, (1, 0), NAVI, forfeit=True, end_at=None,
                                       scheduled_at="2026-11-09T17:00:00Z")])
        self.assertIn(f"\n- Monday 2026-11-09 18:00: won 1-0 vs MOUZ ({DETAILS}, forfeit)",
                      await tools.get_cs_team("Natus Vincere"))

    async def test_a_result_with_no_time_at_all_says_so(self):
        self.serve_team(past=[finished(NAVI, MOUZ, (1, 0), NAVI, end_at=None)])
        self.assertIn(f"\n- date unknown: won 1-0 vs MOUZ ({DETAILS})", await tools.get_cs_team("Natus Vincere"))

    async def test_no_such_team(self):
        self.serve({"/csgo/teams": []})
        self.assertEqual(await tools.get_cs_team("Nope"),
                         "Nope: No team found with that name. Check the spelling or try the full name "
                         "(e.g. Natus Vincere).")

class PlayerTest(CsToolTest):
    async def test_age_comes_from_the_roster_and_the_clock(self):
        # Born 2000-11-12: still 25 on the frozen 2026-11-11
        player = {"id": 5, "name": "ZywOo", "current_team": {"id": 7, "name": "Vitality"},
                  "current_videogame": {"slug": "cs-go"}}
        member = {"id": 5, "name": "ZywOo", "first_name": "Mathieu", "last_name": "Herbaut",
                  "nationality": "FR", "birthday": "2000-11-12"}
        self.serve({**standings(), "/csgo/players": [player], "/csgo/teams": [{"players": [member]}]})
        self.assertEqual(await tools.get_cs_player("ZywOo"),
                         "ZywOo (Mathieu Herbaut, FR, 25): plays for Vitality, "
                         "Valve rank 1 (2010 points, as of 2026-10-06)\n"
                         "Performance stats (rating, K/D, ADR) aren't available from this data source.")

class RankingTest(CsToolTest):
    EXPECTED = ("Valve Regional Standings (global), as of {year}-10-06:\n"
                "1. Vitality: 2010 points (apEX, ZywOo)")

    async def test_current_year(self):
        self.serve(standings())
        self.assertEqual(await tools.get_cs_ranking("world", 1), self.EXPECTED.format(year=2026))

    async def test_a_new_year_without_a_folder_uses_last_years_standings(self):
        freeze(self, "2027-01-02 12:00")
        self.serve({**standings(2026), f"{STANDINGS_FILES}/2027": httpx.Response(404, text="Not Found")})
        self.assertEqual(await tools.get_cs_ranking("global", 1), self.EXPECTED.format(year=2026))

    async def test_unreachable_github_is_reported(self):
        self.serve({f"{STANDINGS_FILES}/2026": httpx.ConnectError("no route"),
                    f"{STANDINGS_FILES}/2025": httpx.ConnectError("no route")})
        self.assertEqual(await tools.run_tool("get_cs_ranking", {"region": "global", "count": 1}),
                         "Tool get_cs_ranking failed: Could not reach GitHub: no route")

class TournamentsTest(CsToolTest):
    async def test_stages_are_grouped_by_event(self):
        event = {"serie_id": 1, "tier": "s", "league": {"name": "IEM"}, "serie": {"full_name": "Cologne 2026"},
                 "prizepool": "1000000 United States Dollar"}
        self.serve({"/csgo/tournaments/running": [
            event | {"name": "Groups", "begin_at": "2026-11-05T10:00:00Z", "end_at": "2026-11-08T10:00:00Z"},
            event | {"name": "Playoffs", "begin_at": "2026-11-10T10:00:00Z", "end_at": "2026-11-15T10:00:00Z"},
            {"serie_id": 2, "tier": "c", "name": "Small"},
        ]})
        self.assertEqual(await tools.get_cs_tournaments("ongoing"),
                         "Running major CS events (tier S is the biggest):\n"
                         "- IEM Cologne 2026 (tier S): 2026-11-05 to 2026-11-15, prize pool 1000000 USD, "
                         "stages: Groups, Playoffs")

if __name__ == "__main__":
    unittest.main()
