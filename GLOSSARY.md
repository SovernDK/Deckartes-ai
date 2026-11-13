# Deckartes

A terminal chat for local models that can answer from tools: a home server, the local disk, the web, anime schedules, Counter-Strike esports, the user's calendar and their saved files.

## Language

**Turn**:
One question from the user and everything done to answer it, including any tool rounds.
_Avoid_: exchange, request

**Conversation**:
The turns from the first question until `/clear`, kept in the history so it can be reopened and continued later.
_Avoid_: chat, session, thread

**History**:
The saved conversations from both the app and Telegram, kept between runs.
_Avoid_: log, transcript, archive

**Library**:
The files the user has saved, kept between conversations, which the model can search.
_Avoid_: loaded file, knowledge base, vector store

**Tool text**:
What a tool hands back to the model: short plain text whose exact wording and order are chosen so a small model can't misread it.
_Avoid_: tool output, formatted result

**Backend**:
The module that gets a tool's data from one outside source and turns it into tool text.
_Avoid_: API client, service

**Tool group**:
A named set of tools with example questions. Before each question the router sends only the groups it resembles; tools in no group are always sent.
_Avoid_: category, toolset

**Confirmation**:
The user's yes or no, asked by the front end running the turn, before a tool changes something outside the app. Without a way to ask, the change is refused.
_Avoid_: approval, permission, confirm handler

**Closest match**:
A show, team or player found for a name that matched nothing exactly, usually because of a typo. Tool text always says it is a guess, so the model can tell the user.
_Avoid_: fuzzy match, suggestion, correction

**Local time**:
A time expressed in the user's configured time zone, which is what every date and time in tool text uses.
_Avoid_: system time, server time
