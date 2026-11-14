"""Spending by category: labels each payee once, totals payments per category, and keeps each
finished week's and month's totals so later ones can be compared with them.

A payee (the paid side's name, or the description when the bank gives none) is labeled by the local
model the first time it's seen, and the label is remembered, so the same shop lands in the same
category every week. The user's own labels (set_category) are rules matched by words, and win over
the model's. The morning bank job (jobs/bank_digest.py) sends the digest of a finished week on
Monday and of a finished month on the 1st, and saves its totals once Telegram took the message.
Details are in docs/tools-banking.md.
"""

import datetime as dt
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from decimal import Decimal

from core import clock, names
from model import mlx_client
from model.mlx_server import ModelServerError
from tools import bank_store
from tools import enable_banking_api as bank

CATEGORIES = ("Groceries", "Restaurants & cafés", "Transport", "Fuel", "Housing & bills", "Shopping", "Health",
              "Entertainment & subscriptions", "Travel", "Cash", "Transfers", "Other")
# Money moved to people or between the user's own accounts: listed, but not spending
TRANSFERS = "Transfers"
# Where a payee without a label counts; the model is asked about it again next time
OTHER = "Other"
# The category under which a comparison keeps the total spent
SPENT = ""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
    payee TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    -- model: one payee's label; user: a rule for every payee whose name has these words
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS periods (
    kind TEXT NOT NULL,
    start TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    PRIMARY KEY (kind, start)
);
CREATE TABLE IF NOT EXISTS totals (
    kind TEXT NOT NULL,
    start TEXT NOT NULL,
    category TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (kind, start, category, currency),
    FOREIGN KEY (kind, start) REFERENCES periods (kind, start) ON DELETE CASCADE
);
"""

def _database() -> AbstractContextManager[sqlite3.Connection]:
    """A connection to the spending database, which also stores the transactions (bank_store.py)."""
    return bank_store.database(_SCHEMA)

@dataclass
class Total:
    amount: Decimal = Decimal(0)
    count: int = 0

# (category, currency) -> total
Totals = dict[tuple[str, str], Total]

@dataclass(frozen=True)
class Period:
    """A Monday-to-Sunday week or a calendar month."""
    kind: str
    start: dt.date

    @property
    def end(self) -> dt.date:
        if self.kind == "week":
            return self.start + dt.timedelta(days=6)
        return _next_month(self.start) - dt.timedelta(days=1)

    def before(self) -> "Period":
        if self.kind == "week":
            return Period("week", self.start - dt.timedelta(days=7))
        return Period("month", (self.start - dt.timedelta(days=1)).replace(day=1))

    def title(self) -> str:
        if self.kind == "week":
            first = f"{self.start:%A} {self.start.day}" + (f" {self.start:%B}" if self.start.month != self.end.month
                                                           else "")
            return f"the week of {first} – {self.end:%A} {self.end.day} {self.end:%B %Y}"
        return f"{self.start:%B %Y}"

def _next_month(day: dt.date) -> dt.date:
    return (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)

def _last_finished(kind: str, today: dt.date) -> Period:
    if kind == "week":
        return Period("week", today - dt.timedelta(days=today.weekday() + 7))
    return Period("month", (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1))

def _exact_period(start: dt.date, end: dt.date) -> Period | None:
    """The week or month that is exactly this range, if any."""
    for period in (Period("week", start), Period("month", start)):
        if period.end == end and (start.weekday() == 0 if period.kind == "week" else start.day == 1):
            return period
    return None

def _payee(payment: bank.Payment) -> str:
    """The payee as words to match, without store numbers and ids (words with 3 or more digits), so
    "BIEDRONKA 1234 WARSZAWA" and "Biedronka 5678 Warszawa" are one payee."""
    payee_words = names.normalize(payment.party or payment.description).split()
    return " ".join(word for word in payee_words if not re.search(r"\d{3}", word)) or "unknown"

@dataclass(frozen=True)
class Label:
    category: str
    # "model", or "user" for the user's own
    source: str

def _remembered(payee: str, known: dict[str, Label]) -> Label | None:
    """The label the user's rules or an earlier model answer give the payee; the user's longest
    matching rule wins."""
    padded = f" {payee} "
    rules = [(len(words), label.category) for words, label in known.items()
             if label.source == "user" and f" {words} " in padded]
    if rules:
        return Label(max(rules)[1], "user")
    return known.get(payee)

# What the bank's description says about the kind of payment, first match wins. A small model
# reads Pekao's "PŁATNOŚĆ BLIK" and "TRANSAKCJA KARTĄ PŁATNICZĄ" as transfers unless told
_PAYMENT_KINDS = (("PRZELEW", "bank transfer"), ("KART", "card payment"), ("CARD", "card payment"),
                  ("BLIK", "BLIK payment"))

def _describe(payment: bank.Payment) -> str:
    """A payment as the model sees it: the payee, and the kind of payment or the description."""
    details = [payment.party or payment.description]
    description = payment.description.upper()
    kind = next((kind for marker, kind in _PAYMENT_KINDS if marker in description), None)
    if kind:
        details.append(kind)
    elif payment.party and payment.description:
        details.append(f"description: {payment.description}")
    if payment.merchant_code:
        details.append(f"card merchant category code {payment.merchant_code}")
    return "; ".join(details)

# The model's answer for a payee it can't place; the user labels those (the app's s key)
UNSURE = "Unsure"

def _labeling_prompt(payees: list[tuple[str, bank.Payment]]) -> str:
    lines = [f"{number}. {_describe(payment)}" for number, (_, payment) in enumerate(payees, 1)]
    return (f"Sort the user's payments into spending categories: {', '.join(CATEGORIES)}.\n"
            "Each line is a payee: a shop, a company or a person. Card and BLIK payments are purchases, so "
            "give them the category of what the payee sells. Use Transfers only for money sent to a person "
            f"by name or to the user's own account. If the name doesn't tell you what the payee sells, answer "
            f"{UNSURE}.\n"
            'Examples: "BIEDRONKA" Groceries, "ORLEN" Fuel, "MCDONALDS" Restaurants & cafés, '
            '"NETFLIX" Entertainment & subscriptions, "JAN KOWALSKI" Transfers.\n'
            f"Answer with only a JSON object mapping each number to a category or {UNSURE}, like "
            f'{{"1": "Groceries", "2": "{UNSURE}"}}.\n\n' + "\n".join(lines))

async def _ask_model(to_label: list[tuple[str, bank.Payment]]) -> dict[str, str]:
    """The model's category for each payee it labeled with a known category; the rest it was
    unsure of or answered wrongly."""
    try:
        reply = await mlx_client.complete([{"role": "user", "content": _labeling_prompt(to_label)}])
    except ModelServerError:
        return {}
    text = reply.content
    try:
        answers = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except ValueError:
        return {}
    if not isinstance(answers, dict):
        return {}
    known = {category.lower(): category for category in CATEGORIES}
    labeled = {}
    for number, (payee, _) in enumerate(to_label, 1):
        category = known.get(str(answers.get(str(number), "")).strip().lower())
        if category:
            labeled[payee] = category
    return labeled

async def _labels(to_label: list[bank.Payment], *, again: bool = False) -> dict[str, Label | None]:
    """Each payment's payee -> its label, or None when it has none (undecided). The model is asked
    about payees it hasn't labeled, or with again about every payee without the user's label, and
    what it says is remembered; a payee it's unsure of again loses its old label."""
    with _database() as db:
        stored = {payee: Label(category, source) for payee, category, source
                  in db.execute("SELECT payee, category, source FROM labels")}
    found: dict[str, Label] = {}
    ask: dict[str, bank.Payment] = {}
    for payment in to_label:
        payee = _payee(payment)
        label = _remembered(payee, stored)
        if label and (label.source == "user" or not again):
            found[payee] = label
        else:
            ask.setdefault(payee, payment)
    # A small model loses track of a long list, and sometimes rambles until no answer can be read
    batch_size = 15
    pending = list(ask)
    for first in range(0, len(pending), batch_size):
        batch = pending[first:first + batch_size]
        labeled = await _ask_model([(payee, ask[payee]) for payee in batch])
        stamp = clock.now().isoformat(timespec="seconds")
        with _database() as db:
            db.executemany("DELETE FROM labels WHERE payee = ? AND source = 'model'",
                           [(payee,) for payee in batch if payee not in labeled])
            db.executemany("INSERT OR REPLACE INTO labels VALUES (?, ?, 'model', ?)",
                           [(payee, category, stamp) for payee, category in labeled.items()])
        found |= {payee: Label(category, "model") for payee, category in labeled.items()}
    return {payee: found.get(payee) for payee in map(_payee, to_label)}

@dataclass
class Payee:
    """One payee of the payments under review."""
    payee: str
    # The name as the bank gives it, from its latest payment
    name: str
    label: Label | None
    count: int = 0
    amounts: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))

async def review(days: int = 30) -> list[Payee]:
    """The payees of the last `days` days, labeled again by the model except those the user
    labeled; undecided ones first, then the most paid."""
    today = clock.today()
    recent, _ = await bank.payments(today - dt.timedelta(days=days), today, user_present=True)
    labels = await _labels(recent, again=True)
    by_payee: dict[str, Payee] = {}
    for payment in sorted(recent, key=lambda payment: payment.date, reverse=True):
        key = _payee(payment)
        payee = by_payee.setdefault(key, Payee(key, payment.party or payment.description or key, labels[key]))
        payee.count += 1
        payee.amounts[payment.currency] += payment.amount
    return sorted(by_payee.values(), key=lambda payee: (payee.label is not None, -sum(payee.amounts.values())))

async def _totals(start: dt.date, end: dt.date, *, user_present: bool) -> tuple[Totals, list[str]]:
    """The totals per category and currency, and notes about days that couldn't be read."""
    paid, notes = await bank.payments(start, end, user_present=user_present)
    labeled = await _labels(paid)
    by_category: Totals = defaultdict(Total)
    for payment in paid:
        label = labeled[_payee(payment)]
        total = by_category[label.category if label else OTHER, payment.currency]
        total.amount += payment.amount
        total.count += 1
    return dict(by_category), notes

def _saved(db: sqlite3.Connection, period: Period) -> Totals | None:
    """A saved period's totals; None when it wasn't saved."""
    if not db.execute("SELECT 1 FROM periods WHERE kind = ? AND start = ?",
                      (period.kind, period.start.isoformat())).fetchone():
        return None
    rows = db.execute("SELECT category, currency, amount, count FROM totals WHERE kind = ? AND start = ?",
                      (period.kind, period.start.isoformat()))
    return {(category, currency): Total(Decimal(amount), count) for category, currency, amount, count in rows}

@dataclass
class _Baseline:
    """What a period is compared with: a name ("month on month") and its amounts, or why there are none."""
    name: str
    amounts: dict[tuple[str, str], Decimal] | None
    missing: str = ""

def _baselines(period: Period) -> list[_Baseline]:
    with _database() as db:
        if period.kind == "month":
            last_month, year_ago = period.before(), Period("month", period.start.replace(year=period.start.year - 1))
            return [_Baseline(name, None if saved is None else _amounts(saved), f"no saved totals for {other.title()}")
                    for name, other in (("month on month", last_month), ("year on year", year_ago))
                    for saved in [_saved(db, other)]]
        saved_weeks = []
        other = period
        for _ in range(4):
            other = other.before()
            saved = _saved(db, other)
            if saved is not None:
                saved_weeks.append(saved)
    if not saved_weeks:
        return [_Baseline("on the weeks before", None, "no saved totals for the 4 weeks before")]
    sums: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for week_totals in saved_weeks:
        for key, amount in _amounts(week_totals).items():
            sums[key] += amount
    count = len(saved_weeks)
    name = "on the week before" if count == 1 else f"on the average of the {count} weeks before"
    return [_Baseline(name, {key: amount / count for key, amount in sums.items()})]

def _amounts(period_totals: Totals) -> dict[tuple[str, str], Decimal]:
    """The amount per (category, currency), plus the total spent per currency under SPENT."""
    amounts: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for (category, currency), total in period_totals.items():
        amounts[category, currency] += total.amount
        if category != TRANSFERS:
            amounts[SPENT, currency] += total.amount
    return dict(amounts)

def _change(now: Decimal, before: Decimal) -> str:
    if before == 0:
        return "new"
    percent = round((now - before) / before * 100)
    if percent == 0:
        return "unchanged"
    return f"up {percent}%" if percent > 0 else f"down {-percent}%"

def _changes(key: tuple[str, str], now: Decimal, compared: list[_Baseline]) -> str:
    return "".join(f", {_change(now, baseline.amounts.get(key, Decimal(0)))} {baseline.name}"
                   for baseline in compared if baseline.amounts is not None)

def _report(title: str, period_totals: Totals, compared: list[_Baseline], *, notes: bool) -> str:
    """The totals per category, biggest first, then the total spent per currency (transfers left
    out); with notes, also which comparisons are missing and why."""
    if not period_totals:
        return f"No payments out in {title}."
    spent: dict[str, Decimal] = defaultdict(Decimal)
    lines = []
    transfers = []
    for (category, currency), total in sorted(period_totals.items(), key=lambda item: (item[0][1], -item[1].amount)):
        payments = "1 payment" if total.count == 1 else f"{total.count} payments"
        amount_text = f"{total.amount:.2f} {currency} ({payments})"
        if category == TRANSFERS:
            transfers.append(amount_text)
            continue
        spent[currency] += total.amount
        lines.append(f"- {category}: {amount_text}{_changes((category, currency), total.amount, compared)}")
    for currency, amount in spent.items():
        lines.append(f"Total spent: {amount:.2f} {currency}{_changes((SPENT, currency), amount, compared)}")
    if transfers:
        lines.append(f"{TRANSFERS}, not counted as spending: {'; '.join(transfers)}")
    if notes:
        lines += [f"Not compared {baseline.name}: {baseline.missing}." for baseline in compared
                  if baseline.amounts is None]
    return f"Spending in {title}:\n" + "\n".join(lines)

async def by_category(date_from: str, date_to: str | None = None) -> str:
    """Tool text: spending per category between two YYYY-MM-DD dates; for exactly a week or a
    month, compared with the saved totals of the ones before."""
    start, end = bank.date_range(date_from, date_to)
    period = _exact_period(start, end)
    title = period.title() if period else f"{start} to {end}"
    totals, notes = await _totals(start, end, user_present=True)
    return "\n\n".join([*notes, _report(title, totals, _baselines(period) if period else [], notes=True)])

def set_category(payee: str, category: str) -> str:
    """Tool text: from now on, payments whose payee has these words count as this category."""
    chosen = next((known for known in CATEGORIES if known.lower() == category.strip().lower()), None)
    chosen = chosen or names.closest_name(category, list(CATEGORIES))
    if chosen is None:
        raise ValueError(f"No category {category!r}; use one of: {', '.join(CATEGORIES)}")
    words = names.normalize(payee)
    if not words:
        raise ValueError("The payee's name is empty")
    with _database() as db:
        db.execute("INSERT OR REPLACE INTO labels VALUES (?, ?, 'user', ?)",
                   (words, chosen, clock.now().isoformat(timespec="seconds")))
        seen = sum(1 for (other,) in db.execute("SELECT payee FROM labels WHERE source = 'model'")
                   if f" {words} " in f" {other} ")
    matches = f"{seen} payee seen so far matches" if seen == 1 else f"{seen} payees seen so far match"
    return f"Saved: payments to {payee} now count as {chosen} ({matches})."

@dataclass
class Digest:
    """A finished week's or month's digest; its totals are saved once it's been sent."""
    period: Period
    totals: Totals
    # "" when there was nothing to send
    text: str = field(default="")

async def _comment(report: str) -> str:
    """The model's few sentences on what stands out; "" when the model can't be reached."""
    prompt = ("Below is the user's spending digest. In 2 or 3 short sentences, tell them what stands out. "
              "Use only the numbers given, and don't repeat the whole list.\n\n" + report)
    try:
        reply = await mlx_client.complete([{"role": "user", "content": prompt}])
    except ModelServerError:
        return ""
    return reply.content.strip()

async def due_digests() -> list[Digest]:
    """The digests of the last finished week and month that haven't been saved yet: on Monday and
    the 1st, or the next morning the job runs when one was missed. Older periods are left out."""
    today = clock.today()
    with _database() as db:
        due = [period for period in (_last_finished("week", today), _last_finished("month", today))
               if _saved(db, period) is None]
    ready = []
    for period in due:
        totals, _ = await _totals(period.start, period.end, user_present=False)
        digest = Digest(period, totals)
        if totals:
            report = _report(period.title(), totals, _baselines(period), notes=False)
            comment = await _comment(report)
            digest.text = f"{report}\n\n{comment}" if comment else report
        ready.append(digest)
    return ready

def save(sent: list[Digest]) -> None:
    """Keep the digests' totals, for comparing later periods with."""
    stamp = clock.now().isoformat(timespec="seconds")
    with _database() as db:
        for digest in sent:
            period = (digest.period.kind, digest.period.start.isoformat())
            db.execute("INSERT OR REPLACE INTO periods VALUES (?, ?, ?)", (*period, stamp))
            db.execute("DELETE FROM totals WHERE kind = ? AND start = ?", period)
            db.executemany("INSERT INTO totals VALUES (?, ?, ?, ?, ?, ?)",
                           [(*period, category, currency, str(total.amount), total.count)
                            for (category, currency), total in digest.totals.items()])
