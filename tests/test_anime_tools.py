"""The text the anime tools give the model, from fake AnimeSchedule responses."""

import datetime as dt
import unittest
from unittest import mock

from core.config import config
from frozen_clock import freeze
from http_fake import HttpTest
from tools import animeschedule_api, tools

ANIME = "/api/v3/anime"
TIMETABLE = "/api/v3/timetables/sub"
USER_LIST = "/api/v3/animelists/tester"


def show(
    route: str,
    title: str,
    *,
    english: str | None = None,
    episodes: int | None = None,
    score: int | None = None,
    tracked: int = 0,
) -> dict:
    """An anime as the API lists it; score is the API's 1-100, None for unrated."""
    return {
        "route": route,
        "title": title,
        "names": {"english": english} if english else {},
        "episodes": episodes,
        "stats": {"ratingCount": 100 if score else 0, "averageScore": score or 0, "trackedCount": tracked},
    }


def episode(route: str, title: str, airs: str, number: int | None, english: str | None = None) -> dict:
    return {"route": route, "title": title, "english": english, "episodeNumber": number, "episodeDate": airs}


def weekly(*episodes: dict):
    """The timetable endpoint: each requested ISO week gets only its own episodes, like the API."""

    def answer(request):
        wanted = (int(request.url.params["year"]), int(request.url.params["week"]))
        return [e for e in episodes if tuple(dt.datetime.fromisoformat(e["episodeDate"]).isocalendar())[:2] == wanted]

    return answer


def ongoing(*shows: dict) -> dict:
    return {"totalAmount": len(shows), "anime": list(shows)}


def listed(route: str, title: str, *, seen: int = 0, episodes: int = 12, score: int | None = None) -> dict:
    return {
        "route": route,
        "preferredTitle": title,
        "listStatus": "watching",
        "episodesSeen": seen,
        "episodes": episodes,
        "useAutoScores": False,
        "manualScore": score,
    }


def on_list(**statuses: list[str]):
    """The list endpoint: routes per status (to_watch for to-watch), filtered by ?list= like the API."""

    def answer(request):
        routes = statuses.get(request.url.params["list"].replace("-", "_"), [])
        return {"shows": {route: listed(route, route.title()) for route in routes}}

    return answer


class AnimeToolTest(HttpTest):
    def setUp(self):
        # A Wednesday in ISO week 46 of the Fall 2026 season
        freeze(self, "2026-11-11 12:00")
        for name, value in (
            ("animeschedule_token", "token"),
            ("animeschedule_user", "tester"),
            ("anime_air_type", "sub"),
            ("anime_min_rating", 6.0),
        ):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class SeasonTest(AnimeToolTest):
    async def test_current_season_is_named_with_todays_date(self):
        self.assertEqual(
            await tools.get_current_season(), "Today is Wednesday 2026-11-11. Current anime season: Fall 2026."
        )

    async def test_long_season_list_says_what_was_cut(self):
        shows = [show(f"s{i}", f"Show {i}", score=90) for i in range(41)]
        self.serve({ANIME: {"totalAmount": 1, "anime": shows}})
        text = await tools.list_season_anime("autumn", 2026)
        self.assertTrue(
            text.startswith(
                "Fall 2026 anime, best rated first, 41 shows (ratings out of 10; shows rated "
                "6 or lower left out; episode counts and premiere dates are given only "
                "when announced):\n"
            )
        )
        self.assertTrue(
            text.endswith(
                "\n...and 1 more lower-rated or unrated show not listed. "
                "To check a specific show, use get_next_episode."
            )
        )

    async def test_several_shows_cut_are_plural(self):
        shows = [show(f"s{i}", f"Show {i}", score=90) for i in range(42)]
        self.serve({ANIME: {"totalAmount": 1, "anime": shows}})
        self.assertIn(
            "\n...and 2 more lower-rated or unrated shows not listed.", await tools.list_season_anime("fall", 2026)
        )

    async def test_a_season_of_one_show_is_singular(self):
        self.serve({ANIME: ongoing(show("a", "Only Show", score=90))})
        self.assertIn(", 1 show (", await tools.list_season_anime("fall", 2026))

    def serve_pages(self, total: int):
        """The season list a page of 18 at a time, like the API."""
        shows = [show(f"s{i}", f"Show {i}", score=90) for i in range(total)]

        def answer(request):
            page = int(request.url.params["page"])
            return {"totalAmount": total, "anime": shows[(page - 1) * 18 : page * 18]}

        return self.serve({ANIME: answer})

    async def test_every_page_of_a_season_is_read(self):
        server = self.serve_pages(19)
        self.assertIn(", 19 shows (", await tools.list_season_anime("fall", 2026))
        self.assertEqual([r.url.params["page"] for r in server.requests], ["1", "2"])

    async def test_no_request_after_an_exactly_full_last_page(self):
        server = self.serve_pages(18)
        self.assertIn(", 18 shows (", await tools.list_season_anime("fall", 2026))
        self.assertEqual(len(server.requests), 1)

    async def test_top_of_next_season_fills_with_unrated_shows(self):
        server = self.serve(
            {
                ANIME: ongoing(
                    show("a", "Rated", score=80),
                    show("b", "Little Tracked", tracked=5),
                    show("c", "Much Tracked", tracked=500),
                )
            }
        )
        self.assertEqual(
            await tools.get_top_season_anime("next", 2),
            "Only 1 Winter 2027 show has a rating yet; the rest of the list are unrated shows ordered "
            "by how many users track them (anticipation, not quality). (episode counts and premiere "
            "dates are given only when announced)\n"
            "1. Rated: rated 8.0\n"
            "2. Much Tracked: unrated (tracked by 500 users)",
        )
        self.assertEqual(server.requests[0].url.params["seasons"], "winter")
        self.assertEqual(server.requests[0].url.params["years"], "2027")
        self.assertEqual(server.requests[0].headers["authorization"], "Bearer token")

    async def test_several_rated_shows_are_plural(self):
        self.serve({ANIME: ongoing(show("a", "One", score=80), show("b", "Two", score=70), show("c", "Three"))})
        self.assertIn("Only 2 Winter 2027 shows have a rating yet;", await tools.get_top_season_anime("next", 3))


class AiringTest(AnimeToolTest):
    async def test_name_first_by_time_without_low_rated_shows(self):
        server = self.serve(
            {
                TIMETABLE: [
                    episode("f", "Sousou no Frieren", "2026-11-13T16:30:00Z", 5, "Frieren"),
                    episode("x", "FX Fighter Kurumi-chan", "2026-11-13T13:31:00Z", 3),
                    episode("w", "Weak Show", "2026-11-13T12:00:00Z", 1),
                    episode("o", "Other Day", "2026-11-14T12:00:00Z", 2),
                ],
                ANIME: ongoing(show("f", "Sousou no Frieren", score=91), show("w", "Weak Show", score=40)),
            }
        )
        self.assertEqual(
            await tools.get_anime_airing_on("2026-11-13"),
            "Anime with a new subtitled episode on Friday 2026-11-13, by airing time (local time):\n"
            "FX Fighter Kurumi-chan: episode 3 at 14:31 (unrated)\n"
            "Frieren: episode 5 at 17:30 (rated 9.1)",
        )
        self.assertEqual(server.requests[0].url.params["week"], "46")

    async def test_day_with_nothing_airing_says_low_rated_shows_are_left_out(self):
        self.serve({TIMETABLE: [], ANIME: ongoing()})
        self.assertEqual(
            await tools.get_anime_airing_on("2026-11-12"),
            "No anime with a new subtitled episode on Thursday 2026-11-12 (shows rated 6 or lower are left out).",
        )

    async def test_a_day_name_is_refused_with_today_so_the_model_can_retry(self):
        self.assertEqual(
            await tools.run_tool("get_anime_airing_on", {"date": "friday"}),
            "Tool get_anime_airing_on failed: Date 'friday' must be YYYY-MM-DD; today is "
            "Wednesday 2026-11-11, so work out the date the user means from that",
        )


class DigestTest(AnimeToolTest):
    async def digest(self, timetable: list[dict], **statuses: list[str]) -> str | None:
        self.serve({USER_LIST: on_list(**statuses), TIMETABLE: weekly(*timetable)})
        return await animeschedule_api.airing_digest()

    async def test_lists_shows_by_airing_time(self):
        text = await self.digest(
            [
                episode("f", "Sousou no Frieren", "2026-11-11T16:30:00Z", 5, "Frieren"),
                episode("b", "Bocchi", "2026-11-11T13:00:00Z", 2),
            ],
            watching=["f", "b"],
        )
        self.assertEqual(
            text, "Airing today, Wednesday 11 November:\n- Bocchi: episode 2 at 14:00\n- Frieren: episode 5 at 17:30"
        )

    async def test_only_watching_and_to_watch_count(self):
        text = await self.digest(
            [
                episode("w", "Watching", "2026-11-11T13:00:00Z", 1),
                episode("t", "Planned", "2026-11-11T14:00:00Z", 1),
                episode("h", "Held", "2026-11-11T15:00:00Z", 1),
                episode("n", "Not Listed", "2026-11-11T16:00:00Z", 1),
            ],
            watching=["w"],
            to_watch=["t"],
            on_hold=["h"],
        )
        self.assertEqual(
            text, "Airing today, Wednesday 11 November:\n- Watching: episode 1 at 14:00\n- Planned: episode 1 at 15:00"
        )

    async def test_an_episode_that_already_aired_today_is_included(self):
        # Clock is frozen at 12:00 local
        text = await self.digest([episode("e", "Early", "2026-11-11T07:00:00Z", 3)], watching=["e"])
        assert text is not None
        self.assertIn("- Early: episode 3 at 08:00", text)

    async def test_low_rated_listed_show_is_kept(self):
        self.serve(
            {
                USER_LIST: on_list(watching=["w"]),
                ANIME: ongoing(show("w", "Weak", score=40)),
                TIMETABLE: weekly(episode("w", "Weak", "2026-11-11T13:00:00Z", 1)),
            }
        )
        text = await animeschedule_api.airing_digest()
        assert text is not None
        self.assertIn("- Weak: episode 1 at 14:00", text)

    async def test_today_means_the_local_date(self):
        text = await self.digest(
            [
                episode("a", "Past Midnight Here", "2026-11-10T23:30:00Z", 1),
                episode("b", "Tomorrow Here", "2026-11-11T23:30:00Z", 1),
            ],
            watching=["a", "b"],
        )
        self.assertEqual(text, "Airing today, Wednesday 11 November:\n- Past Midnight Here: episode 1 at 00:30")

    async def test_unknown_episode_number_is_left_out(self):
        text = await self.digest([episode("a", "Special", "2026-11-11T13:00:00Z", None)], watching=["a"])
        assert text is not None
        self.assertIn("- Special: at 14:00", text)

    async def test_nothing_airing_is_none(self):
        self.assertIsNone(await self.digest([episode("o", "Other Day", "2026-11-12T13:00:00Z", 1)], watching=["o"]))


class NextEpisodeTest(AnimeToolTest):
    async def test_skips_episodes_already_aired(self):
        self.serve(
            {
                TIMETABLE: weekly(
                    episode("f", "Sousou no Frieren", "2026-11-09T16:30:00Z", 4, "Frieren"),
                    episode("f", "Sousou no Frieren", "2026-11-13T16:30:00Z", 5, "Frieren"),
                )
            }
        )
        self.assertEqual(
            await tools.get_next_episode("frieren"), "Frieren: episode 5 airs Friday 2026-11-13 17:30 (local time)"
        )

    async def test_finds_an_episode_next_week(self):
        self.serve({TIMETABLE: weekly(episode("f", "Sousou no Frieren", "2026-11-18T16:30:00Z", 6, "Frieren"))})
        self.assertEqual(
            await tools.get_next_episode("frieren"), "Frieren: episode 6 airs Wednesday 2026-11-18 17:30 (local time)"
        )

    async def test_name_ignores_case_and_punctuation(self):
        self.serve({TIMETABLE: weekly(episode("f", "Sousou no Frieren", "2026-11-13T16:30:00Z", 5, "Frieren"))})
        self.assertEqual(
            await tools.get_next_episode("SOUSOU-no: frieren!"),
            "Frieren: episode 5 airs Friday 2026-11-13 17:30 (local time)",
        )

    async def test_name_matches_without_its_accents(self):
        self.serve({TIMETABLE: weekly(episode("p", "Pokémon Horizons", "2026-11-13T16:30:00Z", 5))})
        self.assertEqual(
            await tools.get_next_episode("pokemon horizons"),
            "Pokémon Horizons: episode 5 airs Friday 2026-11-13 17:30 (local time)",
        )

    async def test_a_misspelt_name_gets_the_closest_match(self):
        self.serve({TIMETABLE: weekly(episode("f", "Sousou no Frieren", "2026-11-13T16:30:00Z", 5, "Frieren"))})
        self.assertEqual(
            await tools.get_next_episode("freiren"),
            'No show is named "freiren"; the closest match is:\n'
            "Frieren: episode 5 airs Friday 2026-11-13 17:30 (local time)",
        )

    async def test_a_name_too_far_from_any_show_finds_nothing(self):
        self.serve({TIMETABLE: weekly(episode("g", "My Girlfriend's Friend", "2026-11-13T16:30:00Z", 5))})
        self.assertEqual(
            await tools.get_next_episode("freiren"),
            "freiren: No upcoming subtitled episode found in this week's or next week's timetable. "
            "It may have finished, be on a break, or the name may differ.",
        )

    async def test_says_when_nothing_is_found(self):
        self.serve({TIMETABLE: []})
        self.assertEqual(
            await tools.get_next_episode("Nope"),
            "Nope: No upcoming subtitled episode found in this week's or next week's timetable. "
            "It may have finished, be on a break, or the name may differ.",
        )


class NextSeasonTest(AnimeToolTest):
    async def answer(self, episodes: int | None, next_number: int | None = 20) -> str:
        timetable = [episode("f", "Frieren", "2026-11-13T16:30:00Z", next_number)] if next_number else []
        self.serve({TIMETABLE: weekly(*timetable), ANIME: ongoing(show("f", "Frieren", episodes=episodes))})
        return await tools.will_air_next_season("Frieren")

    async def test_show_whose_finale_falls_after_the_season_starts_will_still_air(self):
        self.assertEqual(
            await self.answer(28),
            "Next season: Winter 2027 (starts 2027-01-01)\n"
            "Frieren WILL still be airing next season. The next episode is 20 of 28, on 2026-11-13. "
            "At one episode a week, the final episode (28) airs around 2027-01-08, after next season "
            "starts on 2027-01-01. Breaks or delays can push it later.",
        )

    async def test_a_misspelt_name_gets_the_closest_match(self):
        self.serve(
            {
                TIMETABLE: weekly(episode("f", "Frieren", "2026-11-13T16:30:00Z", 20)),
                ANIME: ongoing(show("f", "Frieren", episodes=28)),
            }
        )
        self.assertEqual(
            await tools.will_air_next_season("freiren"),
            'No show is named "freiren"; the closest match is:\n'
            "Next season: Winter 2027 (starts 2027-01-01)\n"
            "Frieren WILL still be airing next season. The next episode is 20 of 28, on 2026-11-13. "
            "At one episode a week, the final episode (28) airs around 2027-01-08, after next season "
            "starts on 2027-01-01. Breaks or delays can push it later.",
        )

    async def test_no_is_a_plain_sentence(self):
        self.assertEqual(
            await self.answer(24),
            "Next season: Winter 2027 (starts 2027-01-01)\n"
            "Frieren will NOT be airing next season. The next episode is 20 of 24, on 2026-11-13. "
            "At one episode a week, the final episode (24) airs around 2026-12-11, before next season "
            "starts on 2027-01-01. Breaks or delays can push it later.",
        )

    async def test_unannounced_episode_count_makes_it_unknown(self):
        self.assertEqual(
            await self.answer(None),
            "Next season: Winter 2027 (starts 2027-01-01)\n"
            "Frieren might or might not air next season; it is unknown. "
            "The total number of episodes hasn't been announced.",
        )

    async def test_unknown_when_not_in_the_timetable(self):
        self.assertEqual(
            await self.answer(24, next_number=None),
            "Next season: Winter 2027 (starts 2027-01-01)\n"
            "Frieren might or might not air next season; it is unknown. "
            "No upcoming episode in this week's or next week's timetable; "
            "it may be on a break or finished.",
        )

    async def test_unknown_when_the_count_is_out_of_date(self):
        self.assertEqual(
            await self.answer(12),
            "Next season: Winter 2027 (starts 2027-01-01)\n"
            "Frieren might or might not air next season; it is unknown. "
            "The announced episode count is already exceeded, so it is out of date.",
        )

    async def test_unknown_show_is_reported_as_unknown(self):
        self.serve({TIMETABLE: [], ANIME: ongoing()})
        self.assertEqual(
            await tools.will_air_next_season("Nope"),
            "Nope: No currently airing anime matches that name, so it is unknown whether it will air "
            "next season. The name may differ, or it isn't airing now.",
        )


class UserListTest(AnimeToolTest):
    def serve_list(self, entries: list[dict], timetable: list[dict]):
        return self.serve(
            {
                USER_LIST: {"shows": {entry["route"]: entry for entry in entries}},
                ANIME: ongoing(),
                TIMETABLE: weekly(*timetable),
            }
        )

    async def test_grouped_under_when_the_next_episode_airs(self):
        self.serve_list(
            [
                listed("a", "Today Show", seen=3),
                listed("b", "Tomorrow Show", score=85),
                listed("c", "Saturday Show"),
                listed("d", "Next Week Show"),
                listed("e", "Finished Show", seen=12),
            ],
            [
                episode("a", "Today Show", "2026-11-11T18:00:00Z", 4),
                episode("b", "Tomorrow Show", "2026-11-12T18:00:00Z", 2),
                episode("c", "Saturday Show", "2026-11-14T18:00:00Z", 2),
                episode("d", "Next Week Show", "2026-11-18T18:00:00Z", 2),
            ],
        )
        self.assertEqual(
            await tools.get_my_anime_list("watching"),
            "Your anime list (watching), 5 shows (today is Wednesday 2026-11-11; "
            "a week runs Monday to Sunday):\n"
            "Airing today:\n"
            "- Today Show: seen 3 of 12, next episode 4 airs Wednesday 2026-11-11 19:00\n"
            "Airing tomorrow:\n"
            "- Tomorrow Show: seen 0 of 12, my score 8.5, next episode 2 airs Thursday 2026-11-12 19:00\n"
            "Airing later this week:\n"
            "- Saturday Show: seen 0 of 12, next episode 2 airs Saturday 2026-11-14 19:00\n"
            "Airing next week:\n"
            "- Next Week Show: seen 0 of 12, next episode 2 airs Wednesday 2026-11-18 19:00\n"
            "No episode in the next two weeks:\n"
            "- Finished Show: seen 12 of 12",
        )

    async def test_weeks_across_a_53_week_year(self):
        # ISO week 53 of 2026 runs from Monday 2026-12-28 to Sunday 2027-01-03
        freeze(self, "2026-12-30 12:00")
        self.serve_list(
            [listed("a", "Saturday Show"), listed("b", "Next Week Show")],
            [
                episode("a", "Saturday Show", "2027-01-02T18:00:00Z", 2),
                episode("b", "Next Week Show", "2027-01-05T18:00:00Z", 2),
            ],
        )
        self.assertEqual(
            await tools.get_my_anime_list("watching"),
            "Your anime list (watching), 2 shows (today is Wednesday 2026-12-30; "
            "a week runs Monday to Sunday):\n"
            "Airing later this week:\n"
            "- Saturday Show: seen 0 of 12, next episode 2 airs Saturday 2027-01-02 19:00\n"
            "Airing next week:\n"
            "- Next Week Show: seen 0 of 12, next episode 2 airs Tuesday 2027-01-05 19:00",
        )

    def serve_list_pages(self, answer):
        return self.serve({USER_LIST: answer, ANIME: ongoing(), TIMETABLE: []})

    async def test_long_lists_are_read_page_by_page(self):
        entries = {f"s{i}": listed(f"s{i}", f"Show {i}") for i in range(201)}

        def answer(request):
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            return {"shows": dict(list(entries.items())[offset : offset + limit])}

        server = self.serve_list_pages(answer)
        self.assertIn(", 201 shows (", await tools.get_my_anime_list("watching"))
        self.assertEqual([r.url.params["offset"] for r in server.requests if r.url.path == USER_LIST], ["0", "200"])

    async def test_an_endless_list_stops_at_the_cap(self):
        full_page = {"shows": {f"s{i}": listed(f"s{i}", f"Show {i}") for i in range(200)}}
        server = self.serve_list_pages(full_page)
        await tools.get_my_anime_list("watching")
        self.assertEqual(len([r for r in server.requests if r.url.path == USER_LIST]), 20)

    async def test_empty_status_says_no_shows(self):
        self.serve_list([], [])
        self.assertEqual(
            await tools.get_my_anime_list("watching"), "No shows with status 'watching' on your anime list."
        )

    async def test_unknown_status_lists_the_valid_ones(self):
        self.assertEqual(
            await tools.run_tool("get_my_anime_list", {"status": "x"}),
            "Tool get_my_anime_list failed: Unknown list status 'x'; use all or one of "
            "watching, completed, on-hold, dropped, to-watch",
        )


if __name__ == "__main__":
    unittest.main()
