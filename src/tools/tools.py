"""Tools the AI model can call.

To add a tool: write an async function with type hints and a docstring, decorated with
@tool("<group>") (a group in tool_groups.py; add one with example questions if none fits). tool_schema.py builds the tool definition the model sees from the function name,
parameters and docstring, so describe clearly what it returns and when to use it. Its body is one
call to the backend, which takes the tool's arguments and returns the tool text.
"""

import inspect
import json
from collections.abc import Awaitable, Callable

from model.tool_schema import CONFIRM, model_signature
from tools import animeschedule_api as anime
from tools import calendar_api as cal
from tools import cs_api as cs
from tools import disk_scan, library, spending
from tools import enable_banking_api as bank
from tools import glances_api as glances
from tools import web_search as web
from tools.tool_groups import GROUPS

# How a turn asks the user yes or no before a tool changes something outside the app, like adding
# a calendar event. The front end running the turn passes it (Conversation.stream_reply); a tool
# that needs it declares a keyword-only `confirm` parameter, and without one it is refused.
Confirm = Callable[[str], Awaitable[bool]]

# Every tool, in the order they're defined
TOOLS: list = []
TOOLS_BY_NAME: dict = {}

def tool(group: str | None):
    """Register an async function as a tool the model can call, in a tool group (tool_groups.py;
    None = in no group, sent with every question)."""
    def register(function):
        # First, so a tool in an unknown group raises before it's registered
        if group is not None:
            GROUPS.add(group, function.__name__)
        TOOLS.append(function)
        TOOLS_BY_NAME[function.__name__] = function
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
        date: Date as YYYY-MM-DD. Work it out from the current date: "tomorrow" or "friday" must be given as its YYYY-MM-DD
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
        date: Date as YYYY-MM-DD. Work it out from the current date: "tomorrow" or "friday" must be given as its YYYY-MM-DD
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
                             end_time: str | None = None, *, confirm: Confirm) -> str:
    """Add an event to the user's own calendar (Apple Calendar). Use it only when the user asks
    to add, schedule or put something in their calendar. The user confirms it before it's saved,
    so call it right away with what you know. Take the title, date and time from the user, from
    earlier in this conversation or from another tool's result. Ask only if the start date or
    time appears nowhere. The end is optional.

    Args:
        title: Event title, e.g. "Dentist" or "NAVI vs Vitality"
        date: Date as YYYY-MM-DD. Work it out from the current date: "tomorrow" or "friday" must be given as its YYYY-MM-DD
        start_time: Local start time as HH:MM (24-hour), e.g. "14:30", or "all-day"
        end_date: Optional. The event's last day as YYYY-MM-DD, only if it runs over several days
        end_time: Optional. Local end time as HH:MM (24-hour), e.g. "15:30"; leave it out for a one-hour event
    """
    return await cal.add_event(title, date, start_time, end_date, end_time, confirm)

@tool("banking")
async def get_bank_transactions(date_from: str, date_to: str | None = None) -> str:
    """Get the transactions of the user's own bank accounts between two dates, newest first: date,
    amount (- money out, + money in), who was paid or who paid, description, plus total money in
    and out. Use it for spending, income, payments and "did my salary come in".

    Args:
        date_from: First day as YYYY-MM-DD. Work it out from the current date: "this week" starts on Monday, "last month" on its 1st
        date_to: Optional. Last day as YYYY-MM-DD; leave it out to go up to today
    """
    return await bank.transactions(date_from, date_to)

@tool("banking")
async def get_spending_by_category(date_from: str, date_to: str | None = None) -> str:
    """Get how much the user spent per category (Groceries, Restaurants & cafés, Transport...)
    between two dates, with the total spent. For exactly a Monday-to-Sunday week or a calendar
    month it also says how each category changed: month on month and year on year for a month,
    against the weeks before for a week. Use it for "what did I spend on groceries", "where does
    my money go" and comparisons with earlier months.

    Args:
        date_from: First day as YYYY-MM-DD. Work it out from the current date: "last month" starts on its 1st, "last week" on its Monday
        date_to: Optional. Last day as YYYY-MM-DD, e.g. the month's last day; leave it out to go up to today
    """
    return await spending.by_category(date_from, date_to)

@tool("banking")
async def set_spending_category(payee: str, category: str) -> str:
    """Put a shop or payee in a spending category from now on, when the user says one is labeled
    wrong or asks to file it somewhere, e.g. "Żabka is groceries".

    Args:
        payee: The shop or payee's name, as the user or the transactions give it, e.g. "Żabka"
        category: One of: Groceries, Restaurants & cafés, Transport, Fuel, Housing & bills, Shopping, Health, Entertainment & subscriptions, Travel, Cash, Transfers, Other
    """
    return spending.set_category(payee, category)

def tools_for(names: list[str] | None) -> list:
    if names is None:
        return TOOLS
    return [TOOLS_BY_NAME[name] for name in names]

async def run_tool(name: str, arguments: dict | None, confirm: Confirm | None = None, *,
                   results: Callable[[str, dict], str] | None = None) -> str:
    """Run a tool the model asked for and return the tool text: its result, or what went wrong.

    arguments is None when the model's arguments weren't valid JSON. confirm asks the user before a
    tool changes something; a tool that needs it is refused without it. results (validation) gives
    a call's result in place of the tool, once the name and arguments have been checked."""
    if arguments is None:
        return f"Tool {name} failed: its arguments were not valid JSON"
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return f"Unknown tool: {name}"
    try:
        model_signature(tool).bind(**arguments)
    except TypeError as e:
        return f"Tool {name} failed: {e}"
    if results is not None:
        return results(name, arguments)
    needs_confirm = CONFIRM in inspect.signature(tool).parameters
    if needs_confirm and confirm is None:
        return (f"Tool {name} needs the user's confirmation, which can't be asked for here, "
                "so nothing was changed")
    try:
        result = await (tool(**arguments, confirm=confirm) if needs_confirm else tool(**arguments))
    except Exception as e:
        return f"Tool {name} failed: {e}"
    # Text results go as they are; JSON-encoding them would add quotes and escaped newlines
    return result if isinstance(result, str) else json.dumps(result)
