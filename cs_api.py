"""Counter-Strike esports data: matches, teams and players from PandaScore, ranking from Valve.

PandaScore docs: https://developers.pandascore.co/reference
Needs a token in config.pandascore_token (PANDA_SCORE_API_TOKEN in .env). The free plan
covers matches, teams, players and tournaments, not stats (rating, K/D), and allows 1000
requests an hour, so responses are cached.

The ranking is Valve's Regional Standings, published as markdown tables on GitHub about
once a month: https://github.com/ValveSoftware/counter-strike_regional_standings

Lists of all matches or tournaments keep only events of tier config.cs_min_tier or better
(PandaScore tiers s, a, b, c, d), since most of PandaScore's matches are small leagues, plus
matches of teams in Valve's top MAJOR_TEAM_RANK, which also play smaller events.
"""

import datetime as dt
import re
import time
from zoneinfo import ZoneInfo

import httpx

from config import config

API_URL = "https://api.pandascore.co/csgo"
STANDINGS_REPO = "ValveSoftware/counter-strike_regional_standings"
STANDINGS_FILES_URL = f"https://api.github.com/repos/{STANDINGS_REPO}/contents/live"
STANDINGS_RAW_URL = f"https://raw.githubusercontent.com/{STANDINGS_REPO}/main/live"
REGIONS = ["global", "europe", "americas", "asia"]
TIERS = "sabcd"
CACHE_SECONDS = 3600
# Scores and schedules change during the day
MATCH_CACHE_SECONDS = 60
PAGE_SIZE = 100
# Safety cap on paging through upcoming matches to find enough top-tier ones
MAX_PAGES = 4
MAX_MATCHES = 10
MAX_TEAM_MATCHES = 5
MAX_RANKING = 30
# Matches of teams this high in Valve's global standings count as major whatever the event tier
MAJOR_TEAM_RANK = 30
# "All matches" results older than this are left out (PandaScore has stale forfeits from years ago)
MAX_RESULT_AGE = dt.timedelta(days=30)
# Words that differ between PandaScore and Valve team names ("Team Falcons" vs "Falcons")
TEAM_NAME_FILLER = {"team", "gaming", "esports", "esport", "club", "gg"}

class CsError(Exception):
    pass

_cache: dict = {}

async def _fetch(url: str, params: dict, headers: dict, max_age: float) -> httpx.Response:
    """GET url, cached for max_age seconds; raises CsError when unreachable or not 200."""
    key = (url, tuple(sorted(params.items())))
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < max_age:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            res = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as e:
        raise CsError(f"Could not reach {httpx.URL(url).host}: {e}") from e
    if res.status_code == 403:
        raise CsError("Access denied: this data isn't included in the PandaScore plan")
    if res.status_code == 429:
        raise CsError("Rate limit reached, try again later")
    if res.status_code != 200:
        raise CsError(f"{httpx.URL(url).host} responded with status {res.status_code}: {res.text[:200]}")
    _cache[key] = (time.monotonic(), res)
    return res

async def _get(path: str, params: dict, max_age: float = CACHE_SECONDS) -> list:
    if not config.pandascore_token:
        raise CsError("No PandaScore token: set PANDA_SCORE_API_TOKEN in the environment or .env")
    res = await _fetch(f"{API_URL}{path}", params,
                       {"Authorization": f"Bearer {config.pandascore_token}"}, max_age)
    return res.json()

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

def _local(timestamp: str | None) -> dt.datetime | None:
    if not timestamp:
        return None
    return dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(ZoneInfo(config.timezone))

def _format_time(timestamp: str | None) -> str | None:
    when = _local(timestamp)
    return when.strftime("%A %Y-%m-%d %H:%M") if when else None

def _top_tier(item: dict) -> bool:
    tier = (item.get("tournament") or item).get("tier") or "d"
    return TIERS.index(tier) <= TIERS.index(config.cs_min_tier)

# Valve ranking

async def _standings(region: str) -> tuple[str, list[dict]]:
    """Latest Valve standings for region, as (date, rows); looks back a year when the
    current year has no file yet (early January)."""
    year = dt.date.today().year
    for folder in (year, year - 1):
        res = await _fetch(f"{STANDINGS_FILES_URL}/{folder}", {}, {}, CACHE_SECONDS)
        names = sorted(item["name"] for item in res.json()
                       if item["name"].startswith(f"standings_{region}_"))
        if names:
            break
    else:
        raise CsError(f"No Valve standings found for {region}")

    latest = names[-1]
    date = latest.removeprefix(f"standings_{region}_").removesuffix(".md").replace("_", "-")
    text = (await _fetch(f"{STANDINGS_RAW_URL}/{folder}/{latest}", {}, {}, CACHE_SECONDS)).text
    rows = []
    for match in re.finditer(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", text, re.M):
        rows.append({"rank": int(match[1]), "team": match[3], "points": int(match[2]),
                     "roster": match[4]})
    return date, rows

async def ranking(region: str, count: int) -> dict:
    region = region.strip().lower()
    region = {"world": "global", "america": "americas", "north america": "americas",
              "south america": "americas", "eu": "europe"}.get(region, region)
    if region not in REGIONS:
        raise CsError(f"Unknown region {region!r}; use one of {', '.join(REGIONS)}")
    date, rows = await _standings(region)
    count = max(1, min(int(count), MAX_RANKING))
    return {"ranking": f"Valve Regional Standings ({region})", "as_of": date, "teams": rows[:count]}

def _team_key(name: str) -> str:
    return " ".join(word for word in _normalize(name).split() if word not in TEAM_NAME_FILLER)

async def _valve_rank(team_name: str, players: list[str] = ()) -> dict | None:
    """The team's place in Valve's global standings, or None when it isn't ranked. Matched by
    name, else by sharing at least 3 players, since the two sources name some teams differently."""
    try:
        date, rows = await _standings("global")
    except CsError:
        return None
    wanted = _team_key(team_name)
    roster = {_normalize(player) for player in players}
    for row in rows:
        valve_roster = {_normalize(player) for player in row["roster"].split(",")}
        if _team_key(row["team"]) == wanted or len(roster & valve_roster) >= 3:
            return {"rank": row["rank"], "points": row["points"], "as_of": date}
    return None

async def _ranked_teams() -> set[str]:
    """Name keys of the teams in Valve's global top MAJOR_TEAM_RANK (empty if unavailable)."""
    try:
        _, rows = await _standings("global")
    except CsError:
        return set()
    return {_team_key(row["team"]) for row in rows[:MAJOR_TEAM_RANK]}

def _major(match: dict, ranked: set[str]) -> bool:
    """A top-tier event, or a match with a Valve top-ranked team in it."""
    return _top_tier(match) or any(_team_key(o["opponent"]["name"]) in ranked
                                   for o in match.get("opponents") or [])

def _age(player: dict) -> int | None:
    if player.get("age"):
        return player["age"]
    if not player.get("birthday"):
        return None
    born, today = dt.date.fromisoformat(player["birthday"]), dt.date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))

# Teams and players

def _best_match(items: list[dict], name: str, keys: tuple[str, ...]) -> list[dict]:
    """Exact name (or acronym) matches first, then the rest, preferring shorter names, so
    "Spirit" finds Spirit rather than Spirit Academy."""
    wanted = _normalize(name)
    def score(item):
        exact = any(_normalize(item.get(key) or "") == wanted for key in keys)
        return (not exact, len(item.get("name") or ""))
    return sorted(items, key=score)

async def _find_team(name: str) -> dict | None:
    if not _normalize(name):
        raise CsError("Give the name of a team")
    teams = await _get("/teams", {"search[name]": name.strip(), "per_page": 50})
    teams += await _get("/teams", {"search[acronym]": name.strip(), "per_page": 20})
    # Teams with a roster first, so a disbanded namesake doesn't win
    teams = _best_match(teams, name, ("name", "acronym"))
    teams.sort(key=lambda team: not team.get("players"))
    return teams[0] if teams else None

def _event(match: dict) -> str:
    league = (match.get("league") or {}).get("name") or ""
    serie = (match.get("serie") or {}).get("full_name") or ""
    return f"{league} {serie}".strip()

def _match(match: dict) -> dict:
    """Slim match: teams by full name, event, stage, format, and score/winner or start time."""
    teams = {o["opponent"]["id"]: o["opponent"]["name"] for o in match.get("opponents") or []}
    names = list(teams.values()) + ["TBD"] * (2 - len(teams))
    stage = match["name"].split(":")[0] if ":" in match.get("name", "") else None
    result = {
        "teams": f"{names[0]} vs {names[1]}",
        "event": _event(match),
        "stage": stage,
        "format": f"BO{match.get('number_of_games')}",
    }
    if match.get("status") == "not_started":
        result["starts"] = _format_time(match.get("scheduled_at") or match.get("begin_at"))
        return result

    scores = {r["team_id"]: r["score"] for r in match.get("results") or []}
    result["score"] = "-".join(str(scores.get(team_id, 0)) for team_id in teams)
    if match.get("status") == "running":
        result["live_now"] = True
        streams = [s["raw_url"] for s in match.get("streams_list") or [] if s.get("main")]
        result["stream"] = streams[0] if streams else None
    else:
        result["winner"] = teams.get(match.get("winner_id"))
        result["date"] = _format_time(match.get("end_at") or match.get("begin_at"))
        if match.get("forfeit"):
            result["note"] = "Forfeit"
    return result

async def team_info(name: str) -> dict:
    team = await _find_team(name)
    if team is None:
        return {"searched_for": name, "note": "No team found with that name. Check the spelling "
                                              "or try the full name (e.g. Natus Vincere)."}
    players = [{
        "name": p["name"],
        "real_name": " ".join(filter(None, [p.get("first_name"), p.get("last_name")])) or None,
        "nationality": p.get("nationality"),
        "age": _age(p),
    } for p in team.get("players") or [] if p.get("active", True)]
    past = await _get("/matches/past", {"filter[opponent_id]": team["id"], "filter[status]": "finished",
                                        "sort": "-end_at", "per_page": MAX_TEAM_MATCHES},
                      MATCH_CACHE_SECONDS)
    upcoming = await _get("/matches/upcoming", {"filter[opponent_id]": team["id"],
                                                "sort": "scheduled_at", "per_page": 1},
                          MATCH_CACHE_SECONDS)
    live = await _get("/matches/running", {"filter[opponent_id]": team["id"]}, MATCH_CACHE_SECONDS)
    return {
        "name": team["name"],
        "acronym": team.get("acronym"),
        "country": team.get("location") or None,
        "valve_rank": await _valve_rank(team["name"], [p["name"] for p in players])
                      or "Not in Valve's global standings",
        "players": players or "No roster listed",
        "recent_results": [_match(m) for m in past] or "No finished matches found",
        "playing_now": [_match(m) for m in live] or "Not playing right now",
        "next_match": _match(upcoming[0]) if upcoming else "No upcoming match scheduled",
    }

async def player_info(name: str) -> dict:
    if not _normalize(name):
        raise CsError("Give the name of a player")
    players = _best_match(await _get("/players", {"search[name]": name.strip(), "per_page": 20}),
                          name, ("name",))
    players = [p for p in players if (p.get("current_videogame") or {}).get("slug") in (None, "cs-go")]
    if not players:
        return {"searched_for": name, "note": "No player found with that nickname."}

    results = []
    for p in players[:3]:
        team = p.get("current_team") or {}
        if team.get("id") and not _age(p):
            # Player search leaves out age and birthday; the team's roster has them
            found = await _get("/teams", {"filter[id]": team["id"]})
            roster = found[0].get("players") or [] if found else []
            p = next((member for member in roster if member["id"] == p["id"]), p) | {"current_team": team}
        results.append({
            "name": p["name"],
            "real_name": " ".join(filter(None, [p.get("first_name"), p.get("last_name")])) or None,
            "nationality": p.get("nationality"),
            "age": _age(p),
            "team": team.get("name") or "No current team",
            "team_valve_rank": (await _valve_rank(team["name"]) if team.get("name") else None)
                               or "Not ranked",
        })
    return {"searched_for": name, "matches": results,
            "note": "Performance stats (rating, K/D, ADR) aren't available from this data source."}

# Matches and tournaments

async def live_matches() -> dict:
    matches = await _get("/matches/running", {"per_page": PAGE_SIZE}, MATCH_CACHE_SECONDS)
    ranked = await _ranked_teams()
    top = [_match(m) for m in matches if _major(m, ranked)]
    result = {"matches": top}
    skipped = len(matches) - len(top)
    if not top:
        result["note"] = "No major matches are being played right now."
    if skipped:
        result["note"] = (result.get("note", "") + f" {skipped} smaller-league matches are live "
                          "but not listed.").strip()
    return result

def _when(match: dict) -> str | None:
    """Start time of an upcoming match, end time of a finished one (ISO, UTC)."""
    if match.get("status") == "not_started":
        return match.get("scheduled_at") or match.get("begin_at")
    return match.get("end_at") or match.get("begin_at") or match.get("scheduled_at")

async def _team_matches(kind: str, team: str) -> dict:
    """Upcoming or past ("upcoming"/"past") matches, of one team or all top-tier ones."""
    if kind == "upcoming":
        params, empty = {"sort": "scheduled_at"}, "No upcoming matches scheduled"
    else:
        params, empty = {"sort": "-end_at", "filter[status]": "finished"}, "No finished matches found"

    if _normalize(team) not in ("", "all"):
        found = await _find_team(team)
        if found is None:
            return {"searched_for": team, "note": "No team found with that name."}
        matches = await _get(f"/matches/{kind}", {**params, "filter[opponent_id]": found["id"],
                                                  "per_page": MAX_MATCHES}, MATCH_CACHE_SECONDS)
        if kind == "upcoming":
            # A match that has started is no longer "upcoming" in PandaScore
            matches = await _get("/matches/running", {"filter[opponent_id]": found["id"]},
                                 MATCH_CACHE_SECONDS) + matches
        return {"team": found["name"], "matches": [_match(m) for m in matches] or empty}

    cutoff = dt.datetime.now(dt.timezone.utc) - MAX_RESULT_AGE
    ranked = await _ranked_teams()
    top = []
    for page in range(1, MAX_PAGES + 1):
        matches = await _get(f"/matches/{kind}", {**params, "per_page": PAGE_SIZE, "page": page},
                             MATCH_CACHE_SECONDS)
        top += [m for m in matches if _major(m, ranked)
                and (kind == "upcoming" or (_local(_when(m)) or cutoff) > cutoff)]
        if len(top) >= MAX_MATCHES or len(matches) < PAGE_SIZE:
            break
    top.sort(key=lambda m: _when(m) or "", reverse=kind == "past")
    return {"matches": [_match(m) for m in top[:MAX_MATCHES]] or f"{empty} in major events"}

async def upcoming_matches(team: str) -> dict:
    return await _team_matches("upcoming", team)

async def match_results(team: str) -> dict:
    return await _team_matches("past", team)

async def tournaments(status: str) -> dict:
    status = status.strip().lower()
    status = {"ongoing": "running", "live": "running", "current": "running", "next": "upcoming"}.get(status, status)
    if status not in ("running", "upcoming"):
        raise CsError("Status must be running or upcoming")

    # PandaScore tournaments are stages (groups, playoffs); group them by event
    events = {}
    for stage in await _get(f"/tournaments/{status}", {"per_page": PAGE_SIZE}):
        if not _top_tier(stage):
            continue
        event = events.setdefault(stage["serie_id"], {
            "name": _event(stage), "tier": stage["tier"].upper(), "stages": [],
            "begins": stage.get("begin_at"), "ends": stage.get("end_at"), "prize_pool": None})
        event["stages"].append(stage["name"])
        event["begins"] = min(filter(None, [event["begins"], stage.get("begin_at")]), default=None)
        event["ends"] = max(filter(None, [event["ends"], stage.get("end_at")]), default=None)
        event["prize_pool"] = event["prize_pool"] or stage.get("prizepool")

    found = sorted(events.values(), key=lambda e: (TIERS.index(e["tier"].lower()), e["begins"] or ""))
    for event in found:
        event["begins"] = (_local(event["begins"]).date().isoformat() if event["begins"] else None)
        event["ends"] = (_local(event["ends"]).date().isoformat() if event["ends"] else None)
    return {"status": status, "events": found or f"No {status} major events found"}

# Text for the model: one line per match or team, shared context in headers, no null fields.
# A team's matches read from its side ("won 3-0 vs MOUZ"), so a small model can't mix up
# which score is whose.

def _details(match: dict) -> str:
    return ", ".join(filter(None, [match["format"], match["event"], match.get("stage")]))

def _sides(match: dict) -> tuple[list[str], list[str]]:
    """Team names and their scores, in the same order."""
    names = match["teams"].split(" vs ")
    scores = match.get("score", "").split("-") if match.get("score") else []
    return names, scores

def match_line(match: dict, team: str | None = None) -> str:
    """One match. For a team's own matches (team given), the result and opponent are from its side."""
    names, scores = _sides(match)
    details = _details(match)
    if team in names and len(names) == 2:
        mine = names.index(team)
        opponent = names[1 - mine]
        if "starts" in match:
            return f"vs {opponent} ({details})"
        score = f"{scores[mine]}-{scores[1 - mine]}" if len(scores) == 2 else "?"
        if match.get("live_now"):
            stream = f", stream {match['stream']}" if match.get("stream") else ""
            return f"LIVE NOW vs {opponent}, score {score} ({details}{stream})"
        outcome = "won" if match.get("winner") == team else "lost" if match.get("winner") else "drew"
        forfeit = ", forfeit" if match.get("note") == "Forfeit" else ""
        return f"{outcome} {score} vs {opponent} ({details}{forfeit})"

    if "starts" in match:
        return f"{match['teams']} ({details})"
    if match.get("live_now"):
        stream = f", stream {match['stream']}" if match.get("stream") else ""
        return f"LIVE NOW {names[0]} {match.get('score', '?')} {names[-1]} ({details}{stream})"
    winner = match.get("winner")
    if winner in names and len(names) == 2 and len(scores) == 2:
        w = names.index(winner)
        forfeit = ", forfeit" if match.get("note") == "Forfeit" else ""
        return f"{winner} beat {names[1 - w]} {scores[w]}-{scores[1 - w]} ({details}{forfeit})"
    return f"{match['teams']} {match.get('score', '')} ({details})"

def _by_day(matches: list[dict], team: str | None = None) -> list[str]:
    """Match lines under a header per day ("Thursday 2026-10-15:"), each starting with its time."""
    lines, day = [], None
    for match in matches:
        when = match.get("starts") or match.get("date")
        if match.get("live_now") or not when:
            lines.append(f"- {match_line(match, team)}")
            continue
        this_day, time = when.rsplit(" ", 1)
        if this_day != day:
            day = this_day
            lines.append(f"{day}:")
        lines.append(f"- {time} {match_line(match, team)}")
    return lines

def ranking_text(result: dict) -> str:
    lines = [f"{result['ranking']}, as of {result['as_of']}:"]
    lines += [f"{t['rank']}. {t['team']}: {t['points']} points ({t['roster']})" for t in result["teams"]]
    return "\n".join(lines)

def _rank_text(rank) -> str:
    if isinstance(rank, dict):
        return f"Valve rank {rank['rank']} ({rank['points']} points, as of {rank['as_of']})"
    return "not in Valve's global standings"

def _person(p: dict) -> str:
    about = ", ".join(str(x) for x in (p.get("real_name"), p.get("nationality"), p.get("age")) if x)
    return f"{p['name']} ({about})" if about else p["name"]

def team_text(team: dict) -> str:
    if "note" in team:
        return f"{team['searched_for']}: {team['note']}"
    name = team["name"]
    about = ", ".join(filter(None, [team.get("acronym"), team.get("country")]))
    lines = [f"{name} ({about}), {_rank_text(team['valve_rank'])}"]
    players = team["players"]
    lines.append("Players: " + (", ".join(map(_person, players)) if isinstance(players, list) else players))
    live = team["playing_now"]
    lines.append("Playing now: " + ("; ".join(match_line(m, name) for m in live)
                                    if isinstance(live, list) else live))
    upcoming = team["next_match"]
    lines.append("Next match: " + (f"{upcoming['starts']} {match_line(upcoming, name)}"
                                   if isinstance(upcoming, dict) else upcoming))
    results = team["recent_results"]
    if isinstance(results, list):
        lines.append("Recent results (newest first):")
        lines += [f"- {m['date']}: {match_line(m, name)}" for m in results]
    else:
        lines.append(f"Recent results: {results}")
    return "\n".join(lines)

def player_text(result: dict) -> str:
    if not result.get("matches"):
        return f"{result['searched_for']}: {result['note']}"
    lines = []
    for p in result["matches"]:
        rank = p["team_valve_rank"]
        team = p["team"]
        rank_part = f", {_rank_text(rank)}" if isinstance(rank, dict) else ""
        plays = f"plays for {team}{rank_part}" if team != "No current team" else "has no current team"
        lines.append(f"{_person(p)}: {plays}")
    lines.append(result["note"])
    return "\n".join(lines)

def live_text(result: dict) -> str:
    lines = ["Live CS matches now:", *(f"- {match_line(m)}" for m in result["matches"])] if result["matches"] else []
    if result.get("note"):
        lines.append(result["note"])
    return "\n".join(lines)

def matches_text(kind: str, result: dict) -> str:
    """Upcoming ("upcoming") or finished ("past") matches, of one team or of major events."""
    if "searched_for" in result:
        return f"{result['searched_for']}: {result['note']}"
    team = result.get("team")
    what = "Upcoming matches" if kind == "upcoming" else "Recent results"
    header = f"{what} of {team}" if team else f"{what} in major CS events"
    order = "soonest first" if kind == "upcoming" else "newest first"
    if not isinstance(result["matches"], list):
        return f"{header}: {result['matches']}"
    return "\n".join([f"{header}, {order} (local time):", *_by_day(result["matches"], team)])

def _prize(text: str | None) -> str | None:
    if not text:
        return None
    return text.replace(" United States Dollar", " USD").replace(" Euro", " EUR")

def tournaments_text(result: dict) -> str:
    title = "Running" if result["status"] == "running" else "Upcoming"
    if not isinstance(result["events"], list):
        return result["events"]
    lines = [f"{title} major CS events (tier S is the biggest):"]
    for e in result["events"]:
        dates = f"{e['begins']} to {e['ends']}" if e["begins"] else "dates not announced"
        prize = f", prize pool {_prize(e['prize_pool'])}" if e["prize_pool"] else ""
        lines.append(f"- {e['name']} (tier {e['tier']}): {dates}{prize}, stages: {', '.join(e['stages'])}")
    return "\n".join(lines)
