"""Tools the AI model can call.

To add a tool: write an async function with type hints and a docstring, then add it
to TOOLS. Ollama builds the tool definition the model sees from the function name,
parameters and docstring, so describe clearly what it returns and when to use it.
"""

import asyncio
import datetime as dt
import json

import animeschedule_api as anime
import calendar_api as cal
import cs_api as cs
import disk_scan
import glances_api as glances
import web_search as web

# Caps on web text handed to the model, so results can't fill its context window
MAX_PAGE_CHARS = 8000
MAX_SNIPPET_CHARS = 1500

# Set by the UI: an async function (question) -> bool that asks the user before a tool changes
# something outside the app, like adding a calendar event. Without a UI (None) changes go through.
confirm_handler = None

async def confirm(question: str) -> bool:
    return True if confirm_handler is None else await confirm_handler(question)

async def get_server_overview() -> str:
    """Get a summary of the home server (thinkpad) in one call: online status and uptime, CPU,
    load, RAM, swap, disk, GPU, temperatures, battery and containers (counts, problems, busiest).
    Use it for "how is the server doing" or whenever several of these are asked about."""
    return await glances.overview()

async def get_server_status() -> str:
    """Check whether the home server (thinkpad) is online, and how long it has been up."""
    return await glances.status()

async def get_cpu_usage() -> str:
    """Get current CPU usage of the home server (thinkpad) in percent and its core count."""
    return await glances.cpu()

async def get_gpu_usage() -> str:
    """Get GPU usage of the home server (thinkpad): load, memory and temperature when known."""
    return await glances.gpu()

async def get_system_load() -> str:
    """Get system load average of the home server (thinkpad) over 1, 5 and 15 minutes, and how
    much of its CPU capacity that is."""
    return await glances.load()

async def get_memory_usage() -> str:
    """Get RAM usage of the home server (thinkpad): used, total and available."""
    return await glances.mem()

async def get_swap_usage() -> str:
    """Get swap memory usage of the home server (thinkpad)."""
    return await glances.memswap()

async def get_disk_usage() -> str:
    """Get disk usage of the home server (thinkpad): used, total and free space per disk."""
    return await glances.fs()

async def get_server_temperatures() -> str:
    """Get temperatures (CPU and other sensors), fan speeds and battery level of the home server (thinkpad)."""
    return await glances.sensors()

async def get_containers() -> str:
    """List Docker containers on the home server (thinkpad): status, uptime, CPU and RAM use.
    Stopped or unhealthy ones come first, then the busiest."""
    return await glances.containers()

async def list_folder(path: str) -> dict:
    """List the files and folders inside a folder on this computer,
    with sizes. Folder sizes include everything inside them. Sorted biggest first, at most 100 entries.

    Args:
        path: Folder to list, e.g. "~" for the home folder, "~/Downloads" or "/Applications"
    """
    return await asyncio.to_thread(disk_scan.list_directory, path)

async def search_files(name: str, path: str) -> dict:
    """Search for files and folders by name on this computer.
    Case-insensitive; returns full paths and sizes of up to 50 matches.

    Args:
        name: Part of the name to look for, e.g. "invoice", or a pattern like "*.pdf"
        path: Folder to search in, including all subfolders, e.g. "~" for the whole home folder
    """
    return await asyncio.to_thread(disk_scan.find_files, name, path)

async def get_folder_size(path: str) -> dict:
    """Get the total size and number of files of a folder on this computer,
    including all subfolders.

    Args:
        path: Folder to measure, e.g. "~/Downloads"
    """
    return await asyncio.to_thread(disk_scan.folder_size, path)

async def search_web(query: str) -> list:
    """Search the internet. Use it for current events, facts you are unsure of, or anything
    after your training data. Returns the top 5 results with title, url and a text snippet.

    Args:
        query: What to search for, e.g. "textual python latest version"
    """
    results = await web.web_search(query, max_results=5)
    for result in results:
        content = result.get("content") or ""
        if len(content) > MAX_SNIPPET_CHARS:
            result["content"] = content[:MAX_SNIPPET_CHARS] + " [...]"
    return results

async def fetch_web_page(url: str) -> dict:
    """Read the text of a web page. Use it after search_web when a snippet isn't enough,
    or when the user gives a link. Returns the title, main text and links on the page.

    Args:
        url: Full address of the page, starting with http:// or https://
    """
    page = await web.web_fetch(url)
    content = page.get("content") or ""
    if len(content) > MAX_PAGE_CHARS:
        page["content"] = content[:MAX_PAGE_CHARS] + "\n[...page truncated]"
    page["links"] = (page.get("links") or [])[:20]
    return page

async def get_current_season() -> str:
    """Get today's date, weekday and the current anime season and year (e.g. Fall 2026).
    Use it whenever you need today's date or to know which anime season is airing now."""
    return anime.current_season_text(anime.today())

async def list_season_anime(season: str, year: int) -> str:
    """List anime that premiered in an anime season, best rated first. Each has its English
    name, rating (0-10, null if nobody rated it yet), number of episodes and premiere date.
    Shows rated 6 or lower are left out.

    Args:
        season: One of "winter", "spring", "summer", "fall"
        year: Year of the season, e.g. 2026
    """
    return anime.season_text(season, int(year), await anime.season_anime(season, int(year)))

async def get_top_season_anime(season: str, count: int) -> str:
    """Get the best rated anime of an anime season, e.g. "5 best anime of next season". Each has
    its English name, rating (0-10, null if nobody rated it yet), how many users track it, number
    of episodes and premiere date. Seasons that haven't aired yet have few ratings, so the list is
    filled with the most anticipated unrated shows; the note in the result says when.

    Args:
        season: "current", "next", "previous", or a season and year like "winter 2027"
        count: How many shows to return, e.g. 5
    """
    return anime.top_season_text(await anime.top_season_anime(season, count))

async def get_next_episode(name: str) -> str:
    """Find when the next subtitled episode of a specific anime airs (weekday, date, local time)
    and its episode number. Use this whenever the user asks when a particular show airs or when
    its next episode comes out, instead of checking day by day.

    Args:
        name: The anime's name or part of it, in English or romaji, e.g. "Apothecary Diaries"
    """
    return anime.next_episode_text(await anime.next_episode(name))

async def will_air_next_season(name: str) -> str:
    """Check whether a currently airing anime will still be airing next season, e.g. "is Red River
    continuing next season?". Gives "yes", "no" or "unknown" with the reason, the estimated date
    of the final episode and the episode count. If the answer is "unknown", tell the user you
    don't know rather than guessing.

    Args:
        name: The anime's name or part of it, in English or romaji, e.g. "Red River"
    """
    return anime.continues_text(await anime.continues_next_season(name))

async def get_anime_airing_on(date: str) -> str:
    """List anime with a new subtitled episode on a given day, best rated first. Each has its
    English name, rating (0-10, null if nobody rated it yet), episode number and airing time
    (local time). Shows rated 6 or lower are left out. A show missing from this list does not
    air that day; to find when a particular show airs, use get_next_episode instead.

    Args:
        date: "today", "tomorrow", "yesterday" or a date as YYYY-MM-DD
    """
    today = anime.today()
    relative = {"today": 0, "tomorrow": 1, "yesterday": -1}
    key = date.strip().lower()
    if key in relative:
        day = today + dt.timedelta(days=relative[key])
    else:
        try:
            day = dt.date.fromisoformat(key)
        except ValueError:
            raise ValueError(f"Date {date!r} must be today, tomorrow, yesterday or YYYY-MM-DD")
    return anime.airing_text(day, await anime.airing_on(day))

async def get_my_anime_list(status: str) -> str:
    """Get the user's own anime list (the shows they track on AnimeSchedule). Each entry has the
    English name, list status, episodes seen, total episodes (null if not announced), the user's
    own score (0-10, null if not scored), season, and for airing shows the next subtitled episode
    number and when it airs (weekday, date, local time). Use it for questions like "what am I
    watching", "what have I completed", "when is the next episode" or "which of my shows air this week".

    Args:
        status: "watching", "completed", "on-hold", "dropped", "to-watch", or "all"
    """
    return anime.user_list_text(status, await anime.user_list(status))

async def get_cs_ranking(region: str, count: int) -> str:
    """Get Valve's official Counter-Strike (CS2) team ranking (Valve Regional Standings): place,
    team, points and roster. Use it for "who is the best CS team" or "top 10 CS teams".

    Args:
        region: "global", "europe", "americas" or "asia"
        count: How many teams to return, e.g. 10
    """
    return cs.ranking_text(await cs.ranking(region, count))

async def get_cs_team(name: str) -> str:
    """Get a Counter-Strike (CS2) esports team: country, current players, Valve ranking,
    last 5 results, the match it is playing right now (if any) and its next match.

    Args:
        name: Team name or acronym, e.g. "Vitality" or "NAVI"
    """
    return cs.team_text(await cs.team_info(name))

async def get_cs_player(name: str) -> str:
    """Get a Counter-Strike (CS2) pro player: real name, nationality, age, current team and
    the team's Valve ranking. Performance stats (rating, K/D) are not available.

    Args:
        name: The player's nickname, e.g. "ZywOo"
    """
    return cs.player_text(await cs.player_info(name))

async def get_cs_live_matches() -> str:
    """Get Counter-Strike (CS2) esports matches being played right now in major events or by
    top-ranked teams, with teams, current score in maps, event, stage, format (BO1/BO3) and
    stream link."""
    return cs.live_text(await cs.live_matches())

async def get_cs_upcoming_matches(team: str) -> str:
    """Get upcoming Counter-Strike (CS2) esports matches, soonest first, with teams, event, stage,
    format and start time (local time). For one team it also lists a match it is playing right
    now (marked live_now). Use it for "when does NAVI play next" or "is NAVI playing now".

    Args:
        team: A team name to get only its matches, or "all" for upcoming major-event matches
    """
    return cs.matches_text("upcoming", await cs.upcoming_matches(team))

async def get_cs_match_results(team: str) -> str:
    """Get recent Counter-Strike (CS2) esports match results, newest first, with teams, score in
    maps, winner, event, stage and date.

    Args:
        team: A team name to get only its results, or "all" for recent major-event results
    """
    return cs.matches_text("past", await cs.match_results(team))

async def get_cs_tournaments(status: str) -> str:
    """Get major Counter-Strike (CS2) esports tournaments that are running now or coming up,
    with tier (S is the biggest), stages, start and end dates and prize pool.

    Args:
        status: "running" or "upcoming"
    """
    return cs.tournaments_text(await cs.tournaments(status))

async def get_calendar_events(date: str) -> str:
    """Get the events in the user's own calendar (Apple Calendar) on one day: title, time
    (local), calendar and location. Use it for "what do I have today" or "am I free on Friday".

    Args:
        date: "today", "tomorrow", "yesterday", a weekday like "friday", or a date as YYYY-MM-DD
    """
    day = cal.parse_day(date)
    return cal.events_text(await asyncio.to_thread(cal.events_on, day))

async def get_calendar_period(period: str) -> str:
    """Get the events in the user's own calendar (Apple Calendar) for a week or a month, grouped
    by day: title, time (local), calendar and location. Use it for "what's on this week" or "any
    events next month". "this week" and "this month" start today.

    Args:
        period: "this week", "next week", "this month", "next month", a month name like "december", or YYYY-MM
    """
    return cal.events_text(await asyncio.to_thread(cal.period_events, period))

async def find_calendar_events(query: str) -> str:
    """Search the user's own calendar (Apple Calendar) for events by a word in their title,
    location or notes, e.g. "when is my next dentist appointment". Returns the upcoming matches
    (up to a year ahead) with date and time, and the most recent past one.

    Args:
        query: Word to look for, e.g. "dentist"
    """
    return cal.found_text(await asyncio.to_thread(cal.find_events, query))

async def add_calendar_event(title: str, date: str, start_time: str, duration_minutes: int) -> str:
    """Add an event to the user's own calendar (Apple Calendar). Use it only when the user asks
    to add, schedule or put something in their calendar; the user confirms it before it's saved.
    Use the date and time from the user or from another tool's result, never a guess.

    Args:
        title: Event title, e.g. "Dentist" or "NAVI vs Vitality"
        date: "today", "tomorrow", a weekday like "friday", or a date as YYYY-MM-DD
        start_time: Local start time as HH:MM (24-hour), e.g. "14:30", or "all-day"
        duration_minutes: Length in minutes, e.g. 60 (ignored for all-day events)
    """
    event = await asyncio.to_thread(cal.new_event, title, date, start_time, int(duration_minutes))
    summary = cal.event_line(event, with_date=True)
    if await asyncio.to_thread(cal.is_duplicate, event):
        return f"Not added, it's already in the calendar: {summary}"
    if not await confirm(f"Add this event to your calendar?\n\n{summary}"):
        return f"The user declined, so the event was NOT added: {summary}"
    await asyncio.to_thread(cal.save_event, event)
    return f"Added to the calendar: {summary}"

TOOLS = [
    get_server_overview,
    get_server_status,
    get_cpu_usage,
    get_gpu_usage,
    get_system_load,
    get_memory_usage,
    get_swap_usage,
    get_disk_usage,
    get_server_temperatures,
    get_containers,
    list_folder,
    search_files,
    get_folder_size,
    search_web,
    fetch_web_page,
    get_current_season,
    list_season_anime,
    get_top_season_anime,
    get_anime_airing_on,
    get_next_episode,
    will_air_next_season,
    get_my_anime_list,
    get_cs_ranking,
    get_cs_team,
    get_cs_player,
    get_cs_live_matches,
    get_cs_upcoming_matches,
    get_cs_match_results,
    get_cs_tournaments,
    get_calendar_events,
    get_calendar_period,
    find_calendar_events,
    add_calendar_event,
]

TOOLS_BY_NAME = {tool.__name__: tool for tool in TOOLS}

# Groups for tool routing (tool_router.py): before each question, only the groups it resembles
# are sent, since tool definitions are most of the prompt. The examples are what gets embedded:
# a small embedding model matches questions to example questions far better than to
# descriptions. A tool in no group is always sent.
TOOL_GROUPS = {
    "server": {
        "tools": ["get_server_overview", "get_server_status", "get_server_temperatures", "get_cpu_usage", "get_gpu_usage", "get_system_load",
                  "get_memory_usage", "get_swap_usage", "get_disk_usage", "get_containers"],
        "examples": [
            "Is my home server up?", "How is the thinkpad doing?", "What's the CPU usage on the server?",
            "How much RAM is the server using?", "Is the GPU busy?", "What's the server load?",
            "How much disk space is left on the server?", "Which docker containers are running?",
            "Is any container stopped?", "Check the server's swap usage",
            "Give me a server health report", "How hot is the server?", "What's the server's battery level?",
        ],
    },
    "disk": {
        "tools": ["list_folder", "search_files", "get_folder_size"],
        "examples": [
            "What files are in my Downloads folder?", "Find a file named report.pdf",
            "How big is my Documents folder?", "Where is the photo I saved yesterday?",
            "List the contents of this directory", "What's taking up space on my computer?",
            "Search my home folder for python files",
        ],
    },
    "web": {
        "tools": ["search_web", "fetch_web_page"],
        "examples": [
            "Search the web for the latest news", "What's happening in the world today?",
            "Look up the price of a Steam Deck", "What does this website say?",
            "Summarize this article for me", "Who won the election?", "What's the weather tomorrow?",
            "Find reviews of this laptop", "Google how to fix this error",
        ],
    },
    "anime": {
        "tools": ["get_current_season", "list_season_anime", "get_top_season_anime",
                  "get_anime_airing_on", "get_next_episode", "will_air_next_season", "get_my_anime_list"],
        "examples": [
            "What anime is airing this season?", "Best anime of next season", "Top 5 anime of fall 2026",
            "What anime airs today?", "When is the next episode of Frieren?",
            "Will this show continue next season?", "What's on my anime list?",
            "Which of my shows air this week?", "Recommend a good anime to watch",
            "What am I watching right now?", "Is the new season of my show out?",
            "Add the next episode of Frieren to my calendar",
        ],
    },
    "cs": {
        "tools": ["get_cs_ranking", "get_cs_team", "get_cs_player", "get_cs_live_matches",
                  "get_cs_upcoming_matches", "get_cs_match_results", "get_cs_tournaments"],
        "examples": [
            "Who is the best Counter-Strike team?", "Top 10 CS2 teams ranking",
            "When does NAVI play next?", "Is Vitality playing right now?", "Who won the CS match?",
            "What CS matches are live?", "Tell me about the player ZywOo", "What's the Spirit roster?",
            "Which CS tournaments are running?", "Results of the last major", "Esports match schedule",
            "Put NAVI's next match in my calendar",
        ],
    },
    "calendar": {
        "tools": ["get_calendar_events", "get_calendar_period", "find_calendar_events", "add_calendar_event"],
        "examples": [
            "What's on my calendar today?", "Do I have any meetings tomorrow?", "Am I free on Friday?",
            "What does my week look like?", "What's planned for next week?",
            "Are there any events next month?", "What's in my calendar in December?",
            "When is my next dentist appointment?", "Add a meeting with Anna on Monday at 10",
            "Put NAVI's next match in my calendar", "Add the next episode of Frieren to my calendar",
            "Schedule gym tomorrow at 18:00",
            "Remind me about the doctor's appointment on Thursday",
        ],
    },
}

def tools_for(names: list[str] | None) -> list:
    """Tools with the given names (unknown names are skipped); None means all tools."""
    if names is None:
        return TOOLS
    return [TOOLS_BY_NAME[name] for name in names if name in TOOLS_BY_NAME]

async def run_tool(name: str, arguments: dict | None) -> str:
    """Run a tool the model asked for and return its result as text for the model."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return f"Unknown tool: {name}"
    try:
        result = await tool(**(arguments or {}))
    except Exception as e:
        return f"Tool {name} failed: {e}"
    # Text results go as they are; JSON-encoding them would add quotes and escaped newlines
    return result if isinstance(result, str) else json.dumps(result)
