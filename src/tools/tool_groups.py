"""Tool groups: named sets of tools with example questions, for tool routing (tool_router.py).

Before each question only the groups it resembles are sent, since tool definitions are most of the
prompt. The examples are what gets embedded: a small embedding model matches questions to example
questions far better than to descriptions. A tool joins a group through @tool(group) (tools.py); a
tool in no group is sent with every question. GROUPS is the app's set; tests can build their own.
"""

from collections.abc import Iterable


class ToolGroups:
    def __init__(self, examples: dict[str, list[str]]) -> None:
        self._examples = examples
        self._tools: dict[str, list[str]] = {name: [] for name in examples}

    @property
    def names(self) -> list[str]:
        return list(self._examples)

    def examples(self, group: str) -> list[str]:
        return self._examples[group]

    def add(self, group: str, tool_name: str) -> None:
        if group not in self._tools:
            raise ValueError(f"unknown tool group {group!r}")
        self._tools[group].append(tool_name)

    def groups_of(self, tool_names: Iterable[str]) -> list[str]:
        names = set(tool_names)
        return [group for group, tools in self._tools.items() if names & set(tools)]

    def is_sent_with(self, tool_name: str, groups: Iterable[str]) -> bool:
        """Whether a tool goes with a question when these groups are chosen: it's in one of them,
        or in no group at all."""
        chosen = set(groups)
        homes = [group for group, tools in self._tools.items() if tool_name in tools]
        return not homes or bool(chosen.intersection(homes))

    def sent_with(self, groups: Iterable[str], tools: list) -> list:
        chosen = list(groups)
        return [tool for tool in tools if self.is_sent_with(tool.__name__, chosen)]


GROUPS = ToolGroups({
    "server": [
        "Is my home server up?", "How is the thinkpad doing?", "What's the CPU usage on the server?",
        "How much RAM is the server using?", "Is the GPU busy?", "What's the server load?",
        "How much disk space is left on the server?", "Which docker containers are running?",
        "Is any container stopped?", "Check the server's swap usage",
        "Give me a server health report", "How hot is the server?", "What's the server's battery level?",
    ],
    "disk": [
        "What files are in my Downloads folder?", "Find a file named report.pdf",
        "How big is my Documents folder?", "Where is the photo I saved yesterday?",
        "List the contents of this directory", "What's taking up space on my computer?",
        "Search my home folder for python files",
    ],
    "web": [
        "Search the web for the latest news", "What's happening in the world today?",
        "Look up the price of a Steam Deck", "What does this website say?",
        "Summarize this article for me", "Who won the election?", "What's the weather tomorrow?",
        "Find reviews of this laptop", "Google how to fix this error",
        "Read this page: https://example.org/article", "Open this link and tell me what it says",
    ],
    "anime": [
        "What anime is airing this season?", "Best anime of next season", "Top 5 anime of fall 2026",
        "What anime airs today?", "When is the next episode of Frieren?", "When does the next episode air?",
        "Will this show continue next season?", "What's on my anime list?",
        "Which of my shows air this week?", "Recommend a good anime to watch",
        "What am I watching right now?", "Is the new season of my show out?",
        "Add the next episode of Frieren to my calendar",
    ],
    "cs": [
        "Who is the best Counter-Strike team?", "Top 10 CS2 teams ranking",
        "When does NAVI play next?", "Is Vitality playing right now?", "Who won the CS match?",
        "What CS matches are live?", "Tell me about the player ZywOo", "What's the Spirit roster?",
        "Which CS tournaments are running?", "Results of the last major", "Esports match schedule",
        "Put NAVI's next match in my calendar",
    ],
    "calendar": [
        "What's on my calendar today?", "Do I have any meetings tomorrow?", "Am I free on Friday?",
        "What does my week look like?", "What's planned for next week?",
        "Are there any events next month?", "What's in my calendar in December?",
        "When is my next dentist appointment?", "Add a meeting with Anna on Monday at 10",
        "Put NAVI's next match in my calendar", "Add the next episode of Frieren to my calendar",
        "Schedule gym tomorrow at 18:00",
        "Remind me about the doctor's appointment on Thursday",
    ],
    "banking": [
        "What did I spend this week?", "Show my bank transactions from October",
        "How much did I pay at Lidl last month?", "Did my salary come in?",
        "What came into my account yesterday?", "List my card payments since Monday",
        "How much money went out of my account this month?", "Who did I send money to last week?",
    ],
})
