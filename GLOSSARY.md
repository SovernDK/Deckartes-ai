# Deckartes

A terminal chat for local models that can answer from tools: a home server, the local disk, the web, anime schedules, Counter-Strike esports and the user's calendar.

## Language

**Tool text**:
What a tool hands back to the model: short plain text whose exact wording and order are chosen so a small model can't misread it.
_Avoid_: tool output, formatted result

**Backend**:
The module that gets a tool's data from one outside source and turns it into tool text.
_Avoid_: API client, service

**Local time**:
A time expressed in the user's configured time zone, which is what every date and time in tool text uses.
_Avoid_: system time, server time
