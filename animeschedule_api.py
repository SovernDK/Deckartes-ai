"""AnimeSchedule.net API v3 client: season lists, weekly timetables and ratings.

Docs: https://animeschedule.net/api/v3/documentation/anime
Needs an application token in config.animeschedule_token (ANIMESCHEDULE_TOKEN in .env).
Limit is 120 requests per minute, so responses are cached for CACHE_SECONDS.

Results are slimmed to name, rating (0-10, converted from the API's 1-100; None when unrated),
episodes and date. Shows rated at or below config.anime_min_rating are dropped; unrated ones stay.
"""

import datetime as dt
import re
import time
from zoneinfo import ZoneInfo

import httpx

from config import config

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

_cache: dict = {}

async def _get(path: str, params: dict, max_age: float = CACHE_SECONDS) -> dict | list:
    """GET from the API with the app token; cached for max_age seconds, raises AnimeScheduleError on failure."""
    key = (path, tuple(sorted(params.items())))
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < max_age:
        return cached[1]

    if not config.animeschedule_token:
        raise AnimeScheduleError("No AnimeSchedule token: set ANIMESCHEDULE_TOKEN in the environment or .env")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(
                f"{API_URL}{path}",
                params=params,
                headers={"Authorization": f"Bearer {config.animeschedule_token}"},
            )
    except httpx.HTTPError as e:
        raise AnimeScheduleError(f"Could not reach AnimeSchedule: {e}") from e

    if res.status_code == 429:
        reset = res.headers.get("x-ratelimit-reset")
        when = dt.datetime.fromtimestamp(int(reset)).strftime("%H:%M:%S") if reset else "soon"
        raise AnimeScheduleError(f"AnimeSchedule rate limit reached, resets at {when}")
    if res.status_code != 200:
        raise AnimeScheduleError(f"AnimeSchedule responded with status {res.status_code}: {res.text[:200]}")

    data = res.json()
    _cache[key] = (time.monotonic(), data)
    return data

def season_of(date: dt.date) -> tuple[str, int]:
    """Anime season for a date: Jan-Mar winter, Apr-Jun spring, Jul-Sep summer, Oct-Dec fall."""
    return SEASONS[(date.month - 1) // 3], date.year

def today() -> dt.date:
    return dt.datetime.now(ZoneInfo(config.timezone)).date()

def rating(anime: dict) -> float | None:
    """Average user score converted from the API's 1-100 to a 0-10 scale; None when nobody rated it yet."""
    stats = anime.get("stats") or {}
    if not stats.get("ratingCount") or not stats.get("averageScore"):
        return None
    return round(stats["averageScore"] / 10, 1)

def keep(show_rating: float | None) -> bool:
    """Leave out low-rated shows (at or below config.anime_min_rating); unrated ones stay."""
    return show_rating is None or show_rating > config.anime_min_rating

def best_first(shows: list[dict]) -> list[dict]:
    """Sort by rating, highest first, with unrated shows at the end."""
    return sorted(shows, key=lambda show: (show["rating"] is None, -(show["rating"] or 0)))

def premiere_date(anime: dict) -> str | None:
    """Subtitled premiere date, falling back to the Japanese one (unset dates are 0001-01-01)."""
    for field in ("subPremier", "premier"):
        value = anime.get(field) or ""
        if value and not value.startswith("0001"):
            return value[:10]
    return None

async def _all_anime(filters: dict) -> list[dict]:
    """Every anime matching filters (up to MAX_PAGES pages). Unrated shows sort last by score,
    so all pages are needed to include them."""
    results = []
    for page in range(1, MAX_PAGES + 1):
        data = await _get("/anime", {**filters, "st": "score", "page": page})
        results.extend(data.get("anime") or [])
        if page * PAGE_SIZE >= data.get("totalAmount", 0):
            break
    return results

async def season_anime(season: str, year: int) -> list[dict]:
    """Anime that premiered in a season, best rated first; low-rated ones are left out."""
    season = season.strip().lower()
    season = "fall" if season == "autumn" else season
    if season not in SEASONS:
        raise AnimeScheduleError(f"Unknown season {season!r}; use one of {', '.join(SEASONS)}")

    anime = await _all_anime({"seasons": season, "years": int(year)})
    shows = [
        {
            "name": (a.get("names") or {}).get("english") or a["title"],
            "rating": rating(a),
            "episodes": a.get("episodes"),
            "premiere": premiere_date(a),
        }
        for a in anime
    ]
    return best_first([show for show in shows if keep(show["rating"])])

def resolve_season(text: str) -> tuple[str, int]:
    """Season and year from "current", "next", "previous" or e.g. "winter 2027"."""
    key = text.strip().lower()
    season, year = season_of(today())
    shift = {"current": 0, "this": 0, "now": 0, "next": 1, "previous": -1, "last": -1}
    if key in shift:
        index = SEASONS.index(season) + shift[key]
        return SEASONS[index % 4], year + index // 4
    match = re.fullmatch(r"(winter|spring|summer|fall|autumn)\s+(\d{4})", key)
    if not match:
        raise AnimeScheduleError(f"Season {text!r} must be current, next, previous or like \"winter 2027\"")
    return ("fall" if match[1] == "autumn" else match[1]), int(match[2])

async def top_season_anime(season: str, count: int) -> dict:
    """The count best-rated anime premiering in a season. Before a season airs few shows have
    ratings, so the rest are filled with the most-tracked unrated shows, marked as such."""
    name, year = resolve_season(season)
    count = max(1, min(int(count), 25))
    anime = await _all_anime({"seasons": name, "years": year})
    shows = [
        {
            "name": (a.get("names") or {}).get("english") or a["title"],
            "rating": rating(a),
            "tracked_by": (a.get("stats") or {}).get("trackedCount", 0),
            "episodes": a.get("episodes"),
            "premiere": premiere_date(a),
        }
        for a in anime
    ]
    rated = best_first([show for show in shows if show["rating"] is not None and keep(show["rating"])])
    top = rated[:count]
    if len(top) < count:
        unrated = sorted((show for show in shows if show["rating"] is None),
                         key=lambda show: -show["tracked_by"])
        top += unrated[:count - len(top)]

    title = f"{name.title()} {year}"
    if not top:
        note = f"No anime found for {title}."
    elif len(top) > len(rated[:count]):
        note = (f"Only {len(rated)} {title} shows have a rating yet; the rest of the list are unrated "
                "shows ordered by how many users track them (anticipation, not quality).")
    else:
        note = f"Top {len(top)} {title} shows by user rating (0-10)."
    return {"season": title, "note": note, "shows": top}

async def airing_on(date: dt.date) -> list[dict]:
    """Anime with a subtitled (config.anime_air_type) episode on date, best rated first;
    low-rated ones are left out. Times are in config.timezone. Timetables carry no ratings,
    so they are matched against all currently airing anime (which includes shows continuing
    from earlier seasons); shows not found there, like this week's premieres, count as unrated."""
    year, week, _ = date.isocalendar()
    timetable = await _get(
        f"/timetables/{config.anime_air_type}",
        {"year": year, "week": week, "tz": config.timezone},
    )
    ratings = {a["route"]: rating(a) for a in await _all_anime({"airing-statuses": "ongoing"})}

    tz = ZoneInfo(config.timezone)
    shows = []
    for entry in timetable:
        airs = dt.datetime.fromisoformat(entry["episodeDate"]).astimezone(tz)
        show_rating = ratings.get(entry["route"])
        if airs.date() != date or not keep(show_rating):
            continue
        shows.append({
            "name": entry.get("english") or entry["title"],
            "rating": show_rating,
            "episode": entry.get("episodeNumber"),
            "airs": airs.strftime("%Y-%m-%d %H:%M"),
        })

    return best_first(shows)

async def _upcoming_episodes() -> dict[str, tuple[dt.datetime, dict]]:
    """Next upcoming subtitled (config.anime_air_type) episode per anime route, as
    (local airing time, timetable entry), from this week's and next week's timetables."""
    tz = ZoneInfo(config.timezone)
    now = dt.datetime.now(tz)
    upcoming = {}
    for day in (now.date(), now.date() + dt.timedelta(days=7)):
        year, week, _ = day.isocalendar()
        timetable = await _get(
            f"/timetables/{config.anime_air_type}",
            {"year": year, "week": week, "tz": config.timezone},
        )
        for entry in timetable:
            airs = dt.datetime.fromisoformat(entry["episodeDate"]).astimezone(tz)
            if airs >= now and (entry["route"] not in upcoming or airs < upcoming[entry["route"]][0]):
                upcoming[entry["route"]] = (airs, entry)
    return upcoming

async def _next_episodes() -> dict[str, tuple[dt.datetime, int]]:
    """Next upcoming episode per anime route, as (local airing time, episode number)."""
    return {route: (airs, entry.get("episodeNumber"))
            for route, (airs, entry) in (await _upcoming_episodes()).items()}

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

def format_airs(airs: dt.datetime) -> str:
    return airs.strftime("%A %Y-%m-%d %H:%M")

async def next_episode(name: str) -> dict:
    """Next subtitled episode of every show whose English, romaji or main title contains all
    words of name (case and punctuation ignored), soonest first. Says so when nothing is found,
    so the model has no gap to fill with a guess."""
    words = _normalize(name).split()
    if not words:
        raise AnimeScheduleError("Give the name of an anime")

    matches = []
    for airs, entry in (await _upcoming_episodes()).values():
        titles = [_normalize(entry.get(key) or "") for key in ("english", "romaji", "title")]
        if any(all(word in title for word in words) for title in titles):
            matches.append((airs, entry))
    matches.sort(key=lambda match: match[0])

    episodes = [{
        "name": entry.get("english") or entry["title"],
        "episode": entry.get("episodeNumber"),
        "airs": format_airs(airs),
    } for airs, entry in matches]
    result = {"searched_for": name, "matches": episodes}
    if not episodes:
        result["note"] = ("No upcoming subtitled episode found in this week's or next week's "
                          "timetable. It may have finished, be on a break, or the name may differ.")
    return result

def _titles(anime: dict) -> list[str]:
    names = anime.get("names") or {}
    titles = [anime.get("title"), names.get("english"), names.get("romaji"), *(names.get("synonyms") or [])]
    return [_normalize(title) for title in titles if title]

async def continues_next_season(name: str) -> dict:
    """Whether currently airing shows matching name will still air next season, estimated from
    the next subtitled episode and the announced episode count (one episode a week). Says
    "unknown" with the reason when the count isn't announced or the show isn't in the timetable."""
    words = _normalize(name).split()
    if not words:
        raise AnimeScheduleError("Give the name of an anime")

    season, year = resolve_season("next")
    season_start = dt.date(year, SEASONS.index(season) * 3 + 1, 1)
    next_season = f"{season.title()} {year} (starts {season_start})"
    upcoming = await _next_episodes()

    shows = []
    for anime in await _all_anime({"airing-statuses": "ongoing"}):
        if not any(all(word in title for word in words) for title in _titles(anime)):
            continue
        total = anime.get("episodes") or None
        next_airs, next_number = upcoming.get(anime["route"], (None, None))
        show = {
            "name": (anime.get("names") or {}).get("english") or anime["title"],
            "episodes": total,
            "next_episode": next_number,
            "next_episode_airs": format_airs(next_airs) if next_airs else None,
        }
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
            show["estimated_final_episode"] = final.isoformat()
            continues = final >= season_start
            show["continues_next_season"] = "yes" if continues else "no"
            # Spelled out: models conflated "episode 16 airs X; the last one airs around Y"
            show["reason"] = (f"The next episode is {next_number} of {total}, on {next_airs.date()}. At one "
                              f"episode a week, the final episode ({total}) airs around {final}, "
                              f"{'after' if continues else 'before'} next season starts on {season_start}. "
                              "Breaks or delays can push it later.")
        shows.append(show)

    result = {"searched_for": name, "next_season": next_season, "matches": shows}
    if not shows:
        result["note"] = ("No currently airing anime matches that name, so it is unknown whether "
                          "it will air next season. The name may differ, or it isn't airing now.")
    return result

async def user_list(status: str) -> list[dict]:
    """Entries on config.animeschedule_user's anime list, filtered by status ("all" for every
    status), best scored first. Names are English where known so they match the other tools.
    Shows with an episode coming up within about two weeks get its number and local airing time."""
    status = status.strip().lower()
    if status != "all" and status not in LIST_STATUSES:
        raise AnimeScheduleError(f"Unknown list status {status!r}; use all or one of {', '.join(LIST_STATUSES)}")

    entries = []
    offset = 0
    while True:
        params = {"offset": offset, "limit": LIST_PAGE_SIZE}
        if status != "all":
            params["list"] = status
        data = await _get(f"/animelists/{config.animeschedule_user}", params, max_age=LIST_CACHE_SECONDS)
        page = list((data.get("shows") or {}).values())
        entries.extend(page)
        if len(page) < LIST_PAGE_SIZE:
            break
        offset += LIST_PAGE_SIZE

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
            "next_episode_airs": format_airs(next_airs) if next_airs else None,
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

def current_season_text(date: dt.date) -> str:
    season, year = season_of(date)
    return f"Today is {date:%A %Y-%m-%d}. Current anime season: {season.title()} {year}."

def season_text(season: str, year: int, shows: list[dict]) -> str:
    title = f"{season.strip().lower().replace('autumn', 'fall').title()} {year}"
    if not shows:
        return f"No anime found for {title}."
    lines = [f"{title} anime, best rated first, {len(shows)} shows (ratings out of 10; shows rated "
             f"{config.anime_min_rating:g} or lower left out; {KNOWN_ONLY}):"]
    for i, show in enumerate(shows[:MAX_SEASON_LIST], 1):
        lines.append(f"{i}. {show['name']}: {_rating_text(show['rating'])}"
                     f"{_details(show, 'episodes', 'premiere')}")
    if len(shows) > MAX_SEASON_LIST:
        lines.append(f"...and {len(shows) - MAX_SEASON_LIST} more lower-rated or unrated shows not listed. "
                     "To check a specific show, use get_next_episode.")
    return "\n".join(lines)

def top_season_text(result: dict) -> str:
    lines = [f"{result['note']} ({KNOWN_ONLY})"]
    for i, show in enumerate(result["shows"], 1):
        tracked = f" (tracked by {show['tracked_by']} users)" if show["rating"] is None else ""
        lines.append(f"{i}. {show['name']}: {_rating_text(show['rating'])}{tracked}"
                     f"{_details(show, 'episodes', 'premiere')}")
    return "\n".join(lines)

def airing_text(date: dt.date, shows: list[dict]) -> str:
    if not shows:
        return (f"No anime with a new subtitled episode on {date:%A %Y-%m-%d} "
                f"(shows rated {config.anime_min_rating:g} or lower are left out).")
    lines = [f"Anime with a new subtitled episode on {date:%A %Y-%m-%d}, by airing time (local time):"]
    for show in sorted(shows, key=lambda show: show["airs"]):
        # Name first: "14:31 FX Fighter Kurumi-chan" was read as a show on a channel called FX
        lines.append(f"{show['name']}: episode {show['episode']} at {show['airs'][-5:]} "
                     f"({_rating_text(show['rating'])})")
    return "\n".join(lines)

def next_episode_text(result: dict) -> str:
    if not result["matches"]:
        return f"{result['searched_for']}: {result['note']}"
    return "\n".join(f"{m['name']}: episode {m['episode']} airs {m['airs']} (local time)"
                     for m in result["matches"])

def continues_text(result: dict) -> str:
    if not result["matches"]:
        return f"{result['searched_for']}: {result['note']}"
    lines = [f"Next season: {result['next_season']}"]
    # A plain sentence first: "X: continues next season: no." was read as a yes
    verdicts = {"yes": "WILL still be airing next season", "no": "will NOT be airing next season",
                "unknown": "might or might not air next season; it is unknown"}
    for show in result["matches"]:
        lines.append(f"{show['name']} {verdicts[show['continues_next_season']]}. {show['reason']}")
    return "\n".join(lines)

def _relative_day(airs: str) -> str:
    """"today", "tomorrow", "this week" or "next week" (ISO weeks, Monday-Sunday) for a
    format_airs string, so the model needn't work out which days "this week" covers."""
    day = dt.date.fromisoformat(airs.split()[1])
    now = today()
    if day == now:
        return "today"
    if day == now + dt.timedelta(days=1):
        return "tomorrow"
    weeks = day.isocalendar()[1] - now.isocalendar()[1] + 52 * (day.isocalendar()[0] - now.isocalendar()[0])
    return {0: "this week", 1: "next week"}.get(weeks, "later")

def user_list_text(status: str, shows: list[dict]) -> str:
    status = status.strip().lower()
    if not shows:
        return f"No shows with status {status!r} on your anime list."
    # Grouped by when the next episode airs, so "which of my shows air this week" is a
    # matter of reading headings: small models filtered a flat list unreliably
    groups = {"today": [], "tomorrow": [], "this week": [], "next week": [], "later": []}
    for show in sorted(shows, key=lambda show: show["next_episode_airs"] and
                       dt.date.fromisoformat(show["next_episode_airs"].split()[1]) or dt.date.max):
        parts = [show["status"]] if status == "all" else []
        total = f"of {show['episodes']}" if show["episodes"] else "(total not announced)"
        parts.append(f"seen {show['episodes_seen']} {total}")
        if show["my_score"] is not None:
            parts.append(f"my score {show['my_score']}")
        when = "later"
        if show["next_episode_airs"]:
            when = _relative_day(show["next_episode_airs"])
            parts.append(f"next episode {show['next_episode']} airs {show['next_episode_airs']}")
        if show["season"]:
            parts.append(show["season"])
        groups[when].append(f"- {show['name']}: {', '.join(parts)}")

    headings = {"today": "Airing today", "tomorrow": "Airing tomorrow", "this week": "Airing later this week",
                "next week": "Airing next week", "later": "No episode in the next two weeks"}
    lines = [f"Your anime list ({status}), {len(shows)} shows (today is {today():%A %Y-%m-%d}; "
             "a week runs Monday to Sunday):"]
    for when, entries in groups.items():
        if entries:
            lines += [f"{headings[when]}:", *entries]
    return "\n".join(lines)
