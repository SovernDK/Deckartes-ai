"""Tools the AI model can call.

To add a tool: write an async function with type hints and a docstring, decorated with
@tool("<group>") (a TOOL_GROUPS key; add a group with example questions if none fits). tool_schema.py builds the tool definition the model sees from the function name,
parameters and docstring, so describe clearly what it returns and when to use it. Its body is one
call to the backend, which takes the tool's arguments and returns the tool text.
"""

import json

from tools import animeschedule_api as anime
from tools import calendar_api as cal
from tools import cs_api as cs
from tools import disk_scan, library
from tools import glances_api as glances
from tools import web_search as web

# Set by the UI: an async function (question) -> bool that asks the user before a tool changes
# something outside the app, like adding a calendar event. Without a UI (None) changes go through.
confirm_handler = None

async def confirm(question: str) -> bool:
    return True if confirm_handler is None else await confirm_handler(question)

# Groups for tool routing (tool_router.py): before each question, only the groups it resembles
# are sent, since tool definitions are most of the prompt. The examples are what gets embedded:
# a small embedding model matches questions to example questions far better than to
# descriptions. Each group's "tools" list is filled in by @tool.
TOOL_GROUPS: dict[str, dict] = {
    "server": {
        "examples": [
            "Is my home server up?", "How is the thinkpad doing?", "What's the CPU usage on the server?",
            "How much RAM is the server using?", "Is the GPU busy?", "What's the server load?",
            "How much disk space is left on the server?", "Which docker containers are running?",
            "Is any container stopped?", "Check the server's swap usage",
            "Give me a server health report", "How hot is the server?", "What's the server's battery level?",
        ],
    },
    "disk": {
        "examples": [
            "What files are in my Downloads folder?", "Find a file named report.pdf",
            "How big is my Documents folder?", "Where is the photo I saved yesterday?",
            "List the contents of this directory", "What's taking up space on my computer?",
            "Search my home folder for python files",
        ],
    },
    "web": {
        "examples": [
            "Search the web for the latest news", "What's happening in the world today?",
            "Look up the price of a Steam Deck", "What does this website say?",
            "Summarize this article for me", "Who won the election?", "What's the weather tomorrow?",
            "Find reviews of this laptop", "Google how to fix this error",
            "Read this page: https://example.org/article", "Open this link and tell me what it says",
        ],
    },
    "anime": {
        "examples": [
            "What anime is airing this season?", "Best anime of next season", "Top 5 anime of fall 2026",
            "What anime airs today?", "When is the next episode of Frieren?", "When does the next episode air?",
            "Will this show continue next season?", "What's on my anime list?",
            "Which of my shows air this week?", "Recommend a good anime to watch",
            "What am I watching right now?", "Is the new season of my show out?",
            "Add the next episode of Frieren to my calendar",
        ],
    },
    "cs": {
        "examples": [
            "Who is the best Counter-Strike team?", "Top 10 CS2 teams ranking",
            "When does NAVI play next?", "Is Vitality playing right now?", "Who won the CS match?",
            "What CS matches are live?", "Tell me about the player ZywOo", "What's the Spirit roster?",
            "Which CS tournaments are running?", "Results of the last major", "Esports match schedule",
            "Put NAVI's next match in my calendar",
        ],
    },
    "calendar": {
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

for _group in TOOL_GROUPS.values():
    _group["tools"] = []

# Every tool, in the order they're defined
TOOLS: list = []
TOOLS_BY_NAME: dict = {}

def tool(group: str | None):
    """Register an async function as a tool the model can call, in a TOOL_GROUPS group
    (None = in no group, sent with every question)."""
    if group is not None and group not in TOOL_GROUPS:
        raise ValueError(f"unknown tool group {group!r}")
    def register(function):
        TOOLS.append(function)
        TOOLS_BY_NAME[function.__name__] = function
        if group is not None:
            TOOL_GROUPS[group]["tools"].append(function.__name__)
        return function
    return register

@tool("server")
async def get_server_overview() -> str:
    """Get a summary of the home server (thinkpad) in one call: online status and uptime, CPU,
    load, RAM, swap, disk, GPU, temperatures, battery and containers (counts, problems, busiest).
    Use it for "how is the server doing" or whenever several of these are asked about."""
    return await glances.overview()

@tool("server")
async def get_server_status() -> str:
    """Check whether the home server (thinkpad) is online, and how long it has been up."""
    return await glances.status()

@tool("server")
async def get_cpu_usage() -> str:
    """Get current CPU usage of the home server (thinkpad) in percent and its core count."""
    return await glances.cpu()

@tool("server")
async def get_gpu_usage() -> str:
    """Get GPU usage of the home server (thinkpad): load, memory and temperature when known."""
    return await glances.gpu()

@tool("server")
async def get_system_load() -> str:
    """Get system load average of the home server (thinkpad) over 1, 5 and 15 minutes, and how
    much of its CPU capacity that is."""
    return await glances.load()

@tool("server")
async def get_memory_usage() -> str:
    """Get RAM usage of the home server (thinkpad): used, total and available."""
    return await glances.mem()

@tool("server")
async def get_swap_usage() -> str:
    """Get swap memory usage of the home server (thinkpad)."""
    return await glances.memswap()

@tool("server")
async def get_disk_usage() -> str:
    """Get disk usage of the home server (thinkpad): used, total and free space per disk."""
    return await glances.fs()

@tool("server")
async def get_server_temperatures() -> str:
    """Get temperatures (CPU and other sensors), fan speeds and battery level of the home server (thinkpad)."""
    return await glances.sensors()

@tool("server")
async def get_containers() -> str:
    """List Docker containers on the home server (thinkpad): status, uptime, CPU and RAM use.
    Stopped or unhealthy ones come first, then the busiest."""
    return await glances.containers()

@tool("disk")
async def list_folder(path: str) -> dict:
    """List the files and folders inside a folder on this computer,
    with sizes. Folder sizes include everything inside them. Sorted biggest first, at most 100 entries.

    Args:
        path: Folder to list, e.g. "~" for the home folder, "~/Downloads" or "/Applications"
    """
    return await disk_scan.list_directory(path)

@tool("disk")
async def search_files(name: str, path: str) -> dict:
    """Search for files and folders by name on this computer.
    Case-insensitive; returns full paths and sizes of up to 50 matches.

    Args:
        name: Part of the name to look for, e.g. "invoice", or a pattern like "*.pdf"
        path: Folder to search in, including all subfolders, e.g. "~" for the whole home folder
    """
    return await disk_scan.find_files(name, path)

@tool("disk")
async def get_folder_size(path: str) -> dict:
    """Get the total size and number of files of a folder on this computer,
    including all subfolders.

    Args:
        path: Folder to measure, e.g. "~/Downloads"
    """
    return await disk_scan.measure_folder(path)

# In no group: the router matches questions to example questions and can't know what the user's files contain
@tool(None)
async def search_library(query: str) -> str:
    """Search the user's library: documents they saved here, such as their own notes, PDFs and other
    files. Returns the passages most related to the query, labelled with the file and page or line.
    Use it for any question about the user's documents or what they have saved.

    Args:
        query: What to look for, in words the document would use, e.g. "notice period for ending the lease"
    """
    return await library.search(query)

@tool("web")
async def search_web(query: str) -> list:
    """Search the internet. Use it for current events, facts you are unsure of, or anything
    after your training data. Returns the top 5 results with title, url and a text snippet.

    Args:
        query: What to search for, e.g. "textual python latest version"
    """
    return await web.web_search(query)

@tool("web")
async def fetch_web_page(url: str) -> dict:
    """Read the text of a web page. Use it after search_web when a snippet isn't enough,
    or when the user gives a link. Returns the title, main text and links on the page.

    Args:
        url: Full address of the page, starting with http:// or https://
    """
    return await web.web_fetch(url)

@tool("anime")
async def get_current_season() -> str:
    """Get today's date, weekday and the current anime season and year (e.g. Fall 2026).
    Use it whenever you need today's date or to know which anime season is airing now."""
    return anime.current_season()

@tool("anime")
async def list_season_anime(season: str, year: int) -> str:
    """List anime that premiered in an anime season, best rated first. Each has its English
    name, rating (0-10, null if nobody rated it yet), number of episodes and premiere date.
    Shows rated 6 or lower are left out.

    Args:
        season: One of "winter", "spring", "summer", "fall"
        year: Year of the season, e.g. 2026
    """
    return await anime.season_anime(season, year)

@tool("anime")
async def get_top_season_anime(season: str, count: int) -> str:
    """Get the best rated anime of an anime season, e.g. "5 best anime of next season". Each has
    its English name, rating (0-10, null if nobody rated it yet), how many users track it, number
    of episodes and premiere date. Seasons that haven't aired yet have few ratings, so the list is
    filled with the most anticipated unrated shows; the note in the result says when.

    Args:
        season: "current", "next", "previous", or a season and year like "winter 2027"
        count: How many shows to return, e.g. 5
    """
    return await anime.top_season_anime(season, count)

@tool("anime")
async def get_anime_airing_on(date: str) -> str:
    """List anime with a new subtitled episode on a given day, best rated first. Each has its
    English name, rating (0-10, null if nobody rated it yet), episode number and airing time
    (local time). Shows rated 6 or lower are left out. A show missing from this list does not
    air that day; to find when a particular show airs, use get_next_episode instead.

    Args:
        date: "today", "tomorrow", "yesterday" or a date as YYYY-MM-DD
    """
    return await anime.airing_on(date)

@tool("anime")
async def get_next_episode(name: str) -> str:
    """Find when the next subtitled episode of a specific anime airs (weekday, date, local time)
    and its episode number. Use this whenever the user asks when a particular show airs or when
    its next episode comes out, instead of checking day by day.

    Args:
        name: The anime's name or part of it, in English or romaji, e.g. "Apothecary Diaries"
    """
    return await anime.next_episode(name)

@tool("anime")
async def will_air_next_season(name: str) -> str:
    """Check whether a currently airing anime will still be airing next season, e.g. "is Red River
    continuing next season?". Gives "yes", "no" or "unknown" with the reason, the estimated date
    of the final episode and the episode count. If the answer is "unknown", tell the user you
    don't know rather than guessing.

    Args:
        name: The anime's name or part of it, in English or romaji, e.g. "Red River"
    """
    return await anime.continues_next_season(name)

@tool("anime")
async def get_my_anime_list(status: str) -> str:
    """Get the user's own anime list (the shows they track on AnimeSchedule). Each entry has the
    English name, list status, episodes seen, total episodes (null if not announced), the user's
    own score (0-10, null if not scored), season, and for airing shows the next subtitled episode
    number and when it airs (weekday, date, local time). Use it for questions like "what am I
    watching", "what have I completed", "when is the next episode" or "which of my shows air this week".

    Args:
        status: "watching", "completed", "on-hold", "dropped", "to-watch", or "all"
    """
    return await anime.user_list(status)

@tool("cs")
async def get_cs_ranking(region: str, count: int) -> str:
    """Get Valve's official Counter-Strike (CS2) team ranking (Valve Regional Standings): place,
    team, points and roster. Use it for "who is the best CS team" or "top 10 CS teams".

    Args:
        region: "global", "europe", "americas" or "asia"
        count: How many teams to return, e.g. 10
    """
    return await cs.ranking(region, count)

@tool("cs")
async def get_cs_team(name: str) -> str:
    """Get a Counter-Strike (CS2) esports team: country, current players, Valve ranking,
    last 5 results, the match it is playing right now (if any) and its next match.

    Args:
        name: Team name or acronym, e.g. "Vitality" or "NAVI"
    """
    return await cs.team_info(name)

@tool("cs")
async def get_cs_player(name: str) -> str:
    """Get a Counter-Strike (CS2) pro player: real name, nationality, age, current team and
    the team's Valve ranking. Performance stats (rating, K/D) are not available.

    Args:
        name: The player's nickname, e.g. "ZywOo"
    """
    return await cs.player_info(name)

@tool("cs")
async def get_cs_live_matches() -> str:
    """Get Counter-Strike (CS2) esports matches being played right now in major events or by
    top-ranked teams, with teams, current score in maps, event, stage, format (BO1/BO3) and
    stream link."""
    return await cs.live_matches()

@tool("cs")
async def get_cs_upcoming_matches(team: str) -> str:
    """Get upcoming Counter-Strike (CS2) esports matches, soonest first, with teams, event, stage,
    format and start time (local time). For one team it also lists a match it is playing right
    now (marked live_now). Use it for "when does NAVI play next" or "is NAVI playing now".

    Args:
        team: A team name to get only its matches, or "all" for upcoming major-event matches
    """
    return await cs.upcoming_matches(team)

@tool("cs")
async def get_cs_match_results(team: str) -> str:
    """Get recent Counter-Strike (CS2) esports match results, newest first, with teams, score in
    maps, winner, event, stage and date.

    Args:
        team: A team name to get only its results, or "all" for recent major-event results
    """
    return await cs.match_results(team)

@tool("cs")
async def get_cs_tournaments(status: str) -> str:
    """Get major Counter-Strike (CS2) esports tournaments that are running now or coming up,
    with tier (S is the biggest), stages, start and end dates and prize pool.

    Args:
        status: "running" or "upcoming"
    """
    return await cs.tournaments(status)

@tool("calendar")
async def get_calendar_events(date: str) -> str:
    """Get the events in the user's own calendar (Apple Calendar) on one day: title, time
    (local), calendar and location. Use it for "what do I have today" or "am I free on Friday".

    Args:
        date: "today", "tomorrow", "yesterday", a weekday like "friday", or a date as YYYY-MM-DD
    """
    return await cal.events_on(date)

@tool("calendar")
async def get_calendar_period(period: str) -> str:
    """Get the events in the user's own calendar (Apple Calendar) for a week or a month, grouped
    by day: title, time (local), calendar and location. Use it for "what's on this week" or "any
    events next month". "this week" and "this month" start today.

    Args:
        period: "this week", "next week", "this month", "next month", a month name like "december", or YYYY-MM
    """
    return await cal.period_events(period)

@tool("calendar")
async def find_calendar_events(query: str) -> str:
    """Search the user's own calendar (Apple Calendar) for events by a word in their title,
    location or notes, e.g. "when is my next dentist appointment". Returns the upcoming matches
    (up to a year ahead) with date and time, and the most recent past one.

    Args:
        query: Word to look for, e.g. "dentist"
    """
    return await cal.find_events(query)

@tool("calendar")
async def add_calendar_event(title: str, date: str, start_time: str, end_date: str | None = None,
                             end_time: str | None = None) -> str:
    """Add an event to the user's own calendar (Apple Calendar). Use it only when the user asks
    to add, schedule or put something in their calendar. The user confirms it before it's saved,
    so call it right away with what you know. Take the title, date and time from the user, from
    earlier in this conversation or from another tool's result. Ask only if the start date or
    time appears nowhere. The end is optional.

    Args:
        title: Event title, e.g. "Dentist" or "NAVI vs Vitality"
        date: "today", "tomorrow", a weekday like "friday", or a date as YYYY-MM-DD
        start_time: Local start time as HH:MM (24-hour), e.g. "14:30", or "all-day"
        end_date: Optional. The event's last day, in the same formats as date, only if it runs over several days
        end_time: Optional. Local end time as HH:MM (24-hour), e.g. "15:30"; leave it out for a one-hour event
    """
    return await cal.add_event(title, date, start_time, end_date, end_time, confirm)

def tools_for(names: list[str] | None) -> list:
    """Tools with the given names (prompts.allowed_tool_names has checked them); None means all tools."""
    if names is None:
        return TOOLS
    return [TOOLS_BY_NAME[name] for name in names]

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
