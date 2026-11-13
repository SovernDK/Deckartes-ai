"""AnimeSchedule.net API v3 client: season lists, weekly timetables and ratings.

Docs: https://animeschedule.net/api/v3/documentation/anime
Needs an application token in config.animeschedule_token (ANIMESCHEDULE_TOKEN in .env).
Limit is 120 requests per minute, so responses are cached for CACHE_SECONDS.

Results are slimmed to name, rating (0-10, converted from the API's 1-100; None when unrated),
episodes and date. Shows rated at or below config.anime_min_rating are dropped; unrated ones stay.

The public functions at the bottom take a tool's arguments and return tool text.
"""

import datetime as dt
import re

from core import clock, http_client
from core.config import config
from core.names import normalize

API_URL = "https://animeschedule.net/api/v3"
PAGE_SIZE = 18
CACHE_SECONDS = 3600
# Safety cap on paging through one list
MAX_PAGES = 20
SEASONS = ["winter", "spring", "summer", "fall"]
LIST_STATUSES = ["watching", "completed", "on-hold", "dropped", "to-watch"]
# The user's list changes often (episodes seen), so it is cached only briefly
LIST_CACHE_SECONDS = 60
LIST_PAGE_SIZE = 200

class AnimeScheduleError(Exception):
    pass

async def _get(path: str, params: dict, max_age: float = CACHE_SECONDS) -> dict | list:
    res = await http_client.request("GET", f"{API_URL}{path}", name="AnimeSchedule", error=AnimeScheduleError,
                                    params=params, token=config.animeschedule_token,
                                    token_env="ANIMESCHEDULE_TOKEN", max_age=max_age)
    return res.json()

def _season_of(date: dt.date) -> tuple[str, int]:
    """Anime season for a date: Jan-Mar winter, Apr-Jun spring, Jul-Sep summer, Oct-Dec fall."""
    return SEASONS[(date.month - 1) // 3], date.year

def _rating(anime: dict) -> float | None:
    """Average user score converted from the API's 1-100 to a 0-10 scale; None when nobody rated it yet."""
    stats = anime.get("stats") or {}
    if not stats.get("ratingCount") or not stats.get("averageScore"):
        return None
    return round(stats["averageScore"] / 10, 1)

def _keep(show_rating: float | None) -> bool:
    return show_rating is None or show_rating > config.anime_min_rating

def _best_first(shows: list[dict]) -> list[dict]:
    return sorted(shows, key=lambda show: (show["rating"] is None, -(show["rating"] or 0)))

def _premiere_date(anime: dict) -> str | None:
    """Subtitled premiere date, falling back to the Japanese one (unset dates are 0001-01-01)."""
    for field in ("subPremier", "premier"):
        value = anime.get(field) or ""
        if value and not value.startswith("0001"):
            return value[:10]
    return None

async def _all_anime(filters: dict) -> list[dict]:
    """Every anime matching filters (up to MAX_PAGES pages). Unrated shows sort last by score,
    so all pages are needed to include them."""
    async def fetch(index: int) -> tuple[list, int]:
        data = await _get("/anime", {**filters, "st": "score", "page": index + 1})
        return data.get("anime") or [], data.get("totalAmount", 0)
    return [anime async for page in http_client.pages(fetch, PAGE_SIZE, MAX_PAGES) for anime in page]

def _show(anime: dict) -> dict:
    return {
        "name": (anime.get("names") or {}).get("english") or anime["title"],
        "rating": _rating(anime),
        "tracked_by": (anime.get("stats") or {}).get("trackedCount", 0),
        "episodes": anime.get("episodes"),
        "premiere": _premiere_date(anime),
    }

def _season_name(season: str) -> str:
    season = season.strip().lower()
    season = "fall" if season == "autumn" else season
    if season not in SEASONS:
        raise AnimeScheduleError(f"Unknown season {season!r}; use one of {', '.join(SEASONS)}")
    return season

async def _season_shows(season: str, year: int) -> list[dict]:
    shows = [_show(a) for a in await _all_anime({"seasons": season, "years": year})]
    return _best_first([show for show in shows if _keep(show["rating"])])

def _resolve_season(text: str) -> tuple[str, int]:
    """Season and year from "current", "next", "previous" or e.g. "winter 2027"."""
    key = text.strip().lower()
    season, year = _season_of(clock.today())
    shift = {"current": 0, "this": 0, "now": 0, "next": 1, "previous": -1, "last": -1}
    if key in shift:
        index = SEASONS.index(season) + shift[key]
        return SEASONS[index % 4], year + index // 4
    match = re.fullmatch(r"(winter|spring|summer|fall|autumn)\s+(\d{4})", key)
    if not match:
        raise AnimeScheduleError(f"Season {text!r} must be current, next, previous or like \"winter 2027\"")
    return ("fall" if match[1] == "autumn" else match[1]), int(match[2])

async def _top_season(season: str, count: int) -> dict:
    """The count best-rated anime premiering in a season. Before a season airs few shows have
    ratings, so the rest are filled with the most-tracked unrated shows."""
    name, year = _resolve_season(season)
    count = max(1, min(int(count), 25))
    shows = [_show(a) for a in await _all_anime({"seasons": name, "years": year})]
    rated = _best_first([show for show in shows if show["rating"] is not None and _keep(show["rating"])])
    top = rated[:count]
    if len(top) < count:
        unrated = sorted((show for show in shows if show["rating"] is None),
                         key=lambda show: -show["tracked_by"])
        top += unrated[:count - len(top)]
    return {"season": f"{name.title()} {year}", "rated": len(rated), "shows": top}

async def _timetable(day: dt.date) -> list[tuple[dt.datetime, dict]]:
    """The config.anime_air_type timetable of day's ISO week, as (local airing time, entry)."""
    year, week, _ = day.isocalendar()
    entries = await _get(f"/timetables/{config.anime_air_type}", {"year": year, "week": week, "tz": config.timezone})
    return [(clock.to_local(entry["episodeDate"]), entry) for entry in entries]

async def _airing_shows(date: dt.date) -> list[dict]:
    """Anime with a subtitled (config.anime_air_type) episode on date, best rated first;
    low-rated ones are left out. Times are in config.timezone. Timetables carry no ratings,
    so they are matched against all currently airing anime (which includes shows continuing
    from earlier seasons); shows not found there, like this week's premieres, count as unrated."""
    timetable = await _timetable(date)
    ratings = {a["route"]: _rating(a) for a in await _all_anime({"airing-statuses": "ongoing"})}

    shows = []
    for airs, entry in timetable:
        show_rating = ratings.get(entry["route"])
        if airs.date() != date or not _keep(show_rating):
            continue
        shows.append({
            "name": entry.get("english") or entry["title"],
            "rating": show_rating,
            "episode": entry.get("episodeNumber"),
            "airs": airs,
        })

    return _best_first(shows)

async def _upcoming_episodes() -> dict[str, tuple[dt.datetime, dict]]:
    """Next upcoming subtitled (config.anime_air_type) episode per anime route, as
    (local airing time, timetable entry), from this week's and next week's timetables."""
    now = clock.now()
    upcoming = {}
    for day in (now.date(), now.date() + dt.timedelta(days=7)):
        for airs, entry in await _timetable(day):
            if airs >= now and (entry["route"] not in upcoming or airs < upcoming[entry["route"]][0]):
                upcoming[entry["route"]] = (airs, entry)
    return upcoming

async def _next_episodes() -> dict[str, tuple[dt.datetime, int]]:
    return {route: (airs, entry.get("episodeNumber"))
            for route, (airs, entry) in (await _upcoming_episodes()).items()}

def _format_airs(airs: dt.datetime) -> str:
    return airs.strftime("%A %Y-%m-%d %H:%M")

async def _find_episodes(name: str) -> list[dict]:
    """Next subtitled episode of every show whose English, romaji or main title contains all
    words of name (case and punctuation ignored), soonest first."""
    words = normalize(name).split()
    if not words:
        raise AnimeScheduleError("Give the name of an anime")

    matches = []
    for airs, entry in (await _upcoming_episodes()).values():
        titles = [normalize(entry.get(key) or "") for key in ("english", "romaji", "title")]
        if any(all(word in title for word in words) for title in titles):
            matches.append((airs, entry))
    matches.sort(key=lambda match: match[0])

    return [{
        "name": entry.get("english") or entry["title"],
        "episode": entry.get("episodeNumber"),
        "airs": airs,
    } for airs, entry in matches]

def _titles(anime: dict) -> list[str]:
    names = anime.get("names") or {}
    titles = [anime.get("title"), names.get("english"), names.get("romaji"), *(names.get("synonyms") or [])]
    return [normalize(title) for title in titles if title]

async def _continuing_shows(name: str) -> tuple[str, list[dict]]:
    """Next season's name, and whether currently airing shows matching name will still air then, estimated from
    the next subtitled episode and the announced episode count (one episode a week). Says
    "unknown" with the reason when the count isn't announced or the show isn't in the timetable."""
    words = normalize(name).split()
    if not words:
        raise AnimeScheduleError("Give the name of an anime")

    season, year = _resolve_season("next")
    season_start = dt.date(year, SEASONS.index(season) * 3 + 1, 1)
    next_season = f"{season.title()} {year} (starts {season_start})"
    upcoming = await _next_episodes()

    shows = []
    for anime in await _all_anime({"airing-statuses": "ongoing"}):
        if not any(all(word in title for word in words) for title in _titles(anime)):
            continue
        total = anime.get("episodes") or None
        next_airs, next_number = upcoming.get(anime["route"], (None, None))
        show = {"name": (anime.get("names") or {}).get("english") or anime["title"]}
        if total is None:
            show["continues_next_season"] = "unknown"
            show["reason"] = "The total number of episodes hasn't been announced."
        elif next_airs is None or not next_number:
            show["continues_next_season"] = "unknown"
            show["reason"] = ("No upcoming episode in this week's or next week's timetable; "
                              "it may be on a break or finished.")
        elif next_number > total:
            show["continues_next_season"] = "unknown"
            show["reason"] = "The announced episode count is already exceeded, so it is out of date."
        else:
            final = next_airs.date() + dt.timedelta(weeks=total - next_number)
            continues = final >= season_start
            show["continues_next_season"] = "yes" if continues else "no"
            # Spelled out: models conflated "episode 16 airs X; the last one airs around Y"
            show["reason"] = (f"The next episode is {next_number} of {total}, on {next_airs.date()}. At one "
                              f"episode a week, the final episode ({total}) airs around {final}, "
                              f"{'after' if continues else 'before'} next season starts on {season_start}. "
                              "Breaks or delays can push it later.")
        shows.append(show)

    return next_season, shows

async def _list_entries(status: str) -> list[dict]:
    async def fetch(index: int) -> tuple[list, None]:
        params = {"offset": index * LIST_PAGE_SIZE, "limit": LIST_PAGE_SIZE}
        if status != "all":
            params["list"] = status
        data = await _get(f"/animelists/{config.animeschedule_user}", params, max_age=LIST_CACHE_SECONDS)
        return list((data.get("shows") or {}).values()), None
    return [entry async for page in http_client.pages(fetch, LIST_PAGE_SIZE, MAX_PAGES) for entry in page]

async def _list_airing(day: dt.date) -> list[dict]:
    """Episodes on day of shows on the user's list with a config.anime_digest_statuses status,
    by airing time. Reads the day's timetable rather than _next_episodes, which drops episodes
    that have already aired, and keeps low-rated shows, since the user chose them."""
    routes = {entry["route"] for status in config.anime_digest_statuses for entry in await _list_entries(status)}
    airing = [(airs, entry) for airs, entry in await _timetable(day) if airs.date() == day and entry["route"] in routes]
    return [{"name": entry.get("english") or entry["title"], "episode": entry.get("episodeNumber"), "airs": airs}
            for airs, entry in sorted(airing, key=lambda pair: pair[0])]

async def _list_shows(status: str) -> list[dict]:
    """Entries on config.animeschedule_user's anime list, filtered by status ("all" for every
    status), best scored first. Names are English where known so they match the other tools.
    Shows with an episode coming up within about two weeks get its number and local airing time."""
    if status != "all" and status not in LIST_STATUSES:
        raise AnimeScheduleError(f"Unknown list status {status!r}; use all or one of {', '.join(LIST_STATUSES)}")

    entries = await _list_entries(status)

    # List entries only carry the user's preferred (often romaji) title
    english = {a["route"]: (a.get("names") or {}).get("english")
               for a in await _all_anime({"airing-statuses": "ongoing"})}
    upcoming = await _next_episodes()

    shows = []
    for entry in entries:
        use_auto = entry.get("useAutoScores")
        score = entry.get("averageAutoScore") if use_auto else entry.get("manualScore")
        next_airs, next_episode = upcoming.get(entry["route"], (None, None))
        shows.append({
            "name": english.get(entry["route"]) or entry.get("preferredTitle"),
            "status": entry.get("listStatus"),
            "episodes_seen": entry.get("episodesSeen") or 0,
            "episodes": entry.get("episodes") or None,  # 0 means not announced yet
            "my_score": round(score / 10, 1) if score else None,
            "season": entry.get("season"),
            "next_episode": next_episode,
            "next_episode_airs": next_airs,
        })

    return sorted(shows, key=lambda show: (show["my_score"] is None, -(show["my_score"] or 0), show["name"]))

# Text for the model: one line per show, shared context in a header, no null fields.
# About half the tokens of the JSON, and nothing for a small model to misread.

# A season has ~100 shows; the tail is unrated or lower-rated ones
MAX_SEASON_LIST = 40

def _rating_text(show_rating: float | None) -> str:
    return f"rated {show_rating}" if show_rating is not None else "unrated"

def _details(show: dict, *keys: str) -> str:
    """", 12 episodes, premiere 2026-10-03" with only the values that are known."""
    parts = []
    if "episodes" in keys and show.get("episodes"):
        parts.append(f"{show['episodes']} episodes")
    if "premiere" in keys and show.get("premiere"):
        parts.append(f"premiere {show['premiere']}")
    return "".join(f", {part}" for part in parts)

# Said once in a list's header instead of "not announced" on most lines
KNOWN_ONLY = "episode counts and premiere dates are given only when announced"

def _count_shows(count: int) -> str:
    return f"{count} {'show' if count == 1 else 'shows'}"

def _season_text(season: str, year: int, shows: list[dict]) -> str:
    title = f"{season.title()} {year}"
    if not shows:
        return f"No anime found for {title}."
    lines = [(f"{title} anime, best rated first, {_count_shows(len(shows))} (ratings out of 10; shows rated "
             f"{config.anime_min_rating:g} or lower left out; {KNOWN_ONLY}):")]
    for i, show in enumerate(shows[:MAX_SEASON_LIST], 1):
        lines.append(f"{i}. {show['name']}: {_rating_text(show['rating'])}"
                     f"{_details(show, 'episodes', 'premiere')}")
    if len(shows) > MAX_SEASON_LIST:
        cut = len(shows) - MAX_SEASON_LIST
        lines.append(f"...and {cut} more lower-rated or unrated {'show' if cut == 1 else 'shows'} not listed. "
                     "To check a specific show, use get_next_episode.")
    return "\n".join(lines)

def _top_season_note(result: dict) -> str:
    title, shows, rated = result["season"], result["shows"], result["rated"]
    if not shows:
        return f"No anime found for {title}."
    if any(show["rating"] is None for show in shows):
        verb = "show has" if rated == 1 else "shows have"
        return (f"Only {rated} {title} {verb} a rating yet; the rest of the list are unrated "
                "shows ordered by how many users track them (anticipation, not quality).")
    return f"Top {len(shows)} {title} shows by user rating (0-10)."

def _top_season_text(result: dict) -> str:
    lines = [f"{_top_season_note(result)} ({KNOWN_ONLY})"]
    for i, show in enumerate(result["shows"], 1):
        tracked = f" (tracked by {show['tracked_by']} users)" if show["rating"] is None else ""
        lines.append(f"{i}. {show['name']}: {_rating_text(show['rating'])}{tracked}"
                     f"{_details(show, 'episodes', 'premiere')}")
    return "\n".join(lines)

def _airing_text(date: dt.date, shows: list[dict]) -> str:
    if not shows:
        return (f"No anime with a new subtitled episode on {date:%A %Y-%m-%d} "
                f"(shows rated {config.anime_min_rating:g} or lower are left out).")
    lines = [f"Anime with a new subtitled episode on {date:%A %Y-%m-%d}, by airing time (local time):"]
    for show in sorted(shows, key=lambda show: f"{show['airs']:%H:%M}"):
        # Name first: "14:31 FX Fighter Kurumi-chan" was read as a show on a channel called FX
        lines.append(f"{show['name']}: episode {show['episode']} at {show['airs']:%H:%M} "
                     f"({_rating_text(show['rating'])})")
    return "\n".join(lines)

def _next_episode_text(name: str, episodes: list[dict]) -> str:
    if not episodes:
        return (f"{name}: No upcoming subtitled episode found in this week's or next week's "
                "timetable. It may have finished, be on a break, or the name may differ.")
    return "\n".join(f"{m['name']}: episode {m['episode']} airs {_format_airs(m['airs'])} (local time)"
                     for m in episodes)

def _continues_text(name: str, next_season: str, shows: list[dict]) -> str:
    if not shows:
        return (f"{name}: No currently airing anime matches that name, so it is unknown whether "
                "it will air next season. The name may differ, or it isn't airing now.")
    lines = [f"Next season: {next_season}"]
    # A plain sentence first: "X: continues next season: no." was read as a yes
    verdicts = {"yes": "WILL still be airing next season", "no": "will NOT be airing next season",
                "unknown": "might or might not air next season; it is unknown"}
    for show in shows:
        lines.append(f"{show['name']} {verdicts[show['continues_next_season']]}. {show['reason']}")
    return "\n".join(lines)

def _relative_day(day: dt.date) -> str:
    """"today", "tomorrow", "this week" or "next week" (ISO weeks, Monday-Sunday) for a day,
    so the model needn't work out which days "this week" covers."""
    now = clock.today()
    if day == now:
        return "today"
    if day == now + dt.timedelta(days=1):
        return "tomorrow"
    # Counted between Mondays, since an ISO year can have 52 or 53 weeks
    weeks = ((day - dt.timedelta(days=day.weekday())) - (now - dt.timedelta(days=now.weekday()))).days // 7
    return {0: "this week", 1: "next week"}.get(weeks, "later")

def _user_list_text(status: str, shows: list[dict]) -> str:
    if not shows:
        return f"No shows with status {status!r} on your anime list."
    # Grouped by when the next episode airs, so "which of my shows air this week" is a
    # matter of reading headings: small models filtered a flat list unreliably
    groups = {"today": [], "tomorrow": [], "this week": [], "next week": [], "later": []}
    for show in sorted(shows, key=lambda show: show["next_episode_airs"].date() if show["next_episode_airs"]
                       else dt.date.max):
        parts = [show["status"]] if status == "all" else []
        total = f"of {show['episodes']}" if show["episodes"] else "(total not announced)"
        parts.append(f"seen {show['episodes_seen']} {total}")
        if show["my_score"] is not None:
            parts.append(f"my score {show['my_score']}")
        when = "later"
        if show["next_episode_airs"]:
            when = _relative_day(show["next_episode_airs"].date())
            parts.append(f"next episode {show['next_episode']} airs {_format_airs(show['next_episode_airs'])}")
        if show["season"]:
            parts.append(show["season"])
        groups[when].append(f"- {show['name']}: {', '.join(parts)}")

    headings = {"today": "Airing today", "tomorrow": "Airing tomorrow", "this week": "Airing later this week",
                "next week": "Airing next week", "later": "No episode in the next two weeks"}
    lines = [(f"Your anime list ({status}), {_count_shows(len(shows))} (today is {clock.today():%A %Y-%m-%d}; "
             "a week runs Monday to Sunday):")]
    for when, entries in groups.items():
        if entries:
            lines += [f"{headings[when]}:", *entries]
    return "\n".join(lines)

def _digest_text(day: dt.date, shows: list[dict]) -> str | None:
    if not shows:
        return None
    lines = [f"Airing today, {day:%A} {day.day} {day:%B}:"]
    for show in shows:
        number = f"episode {show['episode']} " if show["episode"] else ""
        lines.append(f"- {show['name']}: {number}at {show['airs']:%H:%M}")
    return "\n".join(lines)

async def airing_digest() -> str | None:
    day = clock.today()
    return _digest_text(day, await _list_airing(day))

def current_season() -> str:
    today = clock.today()
    season, year = _season_of(today)
    return f"Today is {today:%A %Y-%m-%d}. Current anime season: {season.title()} {year}."

async def season_anime(season: str, year: int) -> str:
    year = int(year)
    season = _season_name(season)
    return _season_text(season, year, await _season_shows(season, year))

async def top_season_anime(season: str, count: int) -> str:
    return _top_season_text(await _top_season(season, count))

async def airing_on(date: str) -> str:
    day = clock.parse_day(date)
    return _airing_text(day, await _airing_shows(day))

async def next_episode(name: str) -> str:
    return _next_episode_text(name, await _find_episodes(name))

async def continues_next_season(name: str) -> str:
    next_season, shows = await _continuing_shows(name)
    return _continues_text(name, next_season, shows)

async def user_list(status: str) -> str:
    status = status.strip().lower()
    return _user_list_text(status, await _list_shows(status))
