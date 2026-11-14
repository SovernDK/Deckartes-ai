"""Bank transactions from Enable Banking.

Docs: https://enablebanking.com/docs/api/reference/
Every request carries a JWT signed with the application's private key (RS256), which
Enable Banking checks against the public key registered for the application. Needs
ENABLE_BANKING_APP_ID and ENABLE_BANKING_KEY_PATH (the .pem file) in .env for the production app.
With config.enable_banking_sandbox (ENABLE_BANKING_SANDBOX=1, or linking with --sandbox) the
sandbox app is used instead, with ENABLE_BANKING_SANDBOX_ID and ENABLE_BANKING_SANDBOX_KEY_PATH
and its own bank link, for testing against made-up banks.

Reading an account needs a bank link, which the user makes by approving access at their bank
in a browser. `python -m tools.enable_banking_api` (from src/) walks through it and saves the
link (Enable Banking's session and its accounts) to the app's file in config. It lasts as
long as the bank allows (at most 180 days under PSD2), then the user links again;
jobs/bank_digest.py reminds them 7 days and 1 day before.
"""

import argparse
import asyncio
import datetime as dt
import json
import secrets
import socket
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt

from core import clock, http_client
from core.config import PROJECT_ROOT, config
from tools import bank_store

API_URL = "https://api.enablebanking.com"

class BankingError(Exception):
    pass

@dataclass(frozen=True)
class _App:
    """One Enable Banking application: production, or the sandbox for testing."""
    name: str
    app_id: str
    key_path: str
    link_path: Path
    # The .env names of app_id and key_path, for the error when they're missing
    settings: str
    # The linking command for this app
    command: str

def _app() -> _App:
    if config.enable_banking_sandbox:
        return _App("sandbox", config.enable_banking_sandbox_id, config.enable_banking_sandbox_key_path,
                    config.banking_sandbox_session_path, "ENABLE_BANKING_SANDBOX_ID and ENABLE_BANKING_SANDBOX_KEY_PATH",
                    "`python -m tools.enable_banking_api --sandbox` from src/")
    return _App("production", config.enable_banking_app_id, config.enable_banking_key_path,
                config.banking_session_path, "ENABLE_BANKING_APP_ID and ENABLE_BANKING_KEY_PATH",
                "`python -m tools.enable_banking_api` from src/")

def _auth() -> str:
    """A fresh JWT for one request, signed with the app's private key."""
    app = _app()
    if not app.app_id or not app.key_path:
        raise BankingError(f"No Enable Banking {app.name} credentials: set {app.settings} in .env")
    try:
        # Relative paths are relative to the repo folder, whatever the working directory is
        key = (PROJECT_ROOT / Path(app.key_path).expanduser()).read_bytes()
    except OSError as e:
        raise BankingError(f"Could not read the Enable Banking private key: {e}") from e
    now = int(time.time())
    # Enable Banking accepts tokens valid for up to 24 hours; a request needs one for seconds
    lifetime = 3600
    return jwt.encode(
        {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + lifetime},
        key, algorithm="RS256", headers={"kid": app.app_id})

def _local_ip() -> str:
    """This Mac's address on its network; connecting a UDP socket sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.connect(("192.0.2.1", 80))
            return udp.getsockname()[0]
    except OSError:
        return "127.0.0.1"

def _user_present() -> dict[str, str]:
    """Headers saying the user asked for this read. PSD2 lets a bank refuse more than 4 reads a day
    made without the user (Pekao answers 429 ASPSP_RATE_LIMIT_EXCEEDED), and Enable Banking takes
    these headers as the user being there. Only for reads the user asked for: a scheduled job
    mustn't send them."""
    return {"Psu-Ip-Address": _local_ip(), "Psu-User-Agent": "Deckartes"}

async def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                   status_messages: dict[int, str] | None = None, user_present: bool = False):
    res = await http_client.request(
        method, f"{API_URL}{path}", name="Enable Banking", error=BankingError,
        params=params, json=body, token=_auth(), status_messages=status_messages,
        headers=_user_present() if user_present else None)
    return res.json()

# Linking a bank: banks -> start_auth -> (user approves in a browser) -> _session

async def _banks(country: str) -> list[dict]:
    """The banks Enable Banking offers in a country (two letters),
    each with its name and maximum_consent_validity (seconds) when the bank gives one."""
    data = await _request("GET", "/aspsps", params={"country": country})
    return data["aspsps"]

def _consent_length(bank: dict) -> dt.timedelta:
    """How long to ask the bank for access: as long as it allows, which PSD2 caps at 180 days."""
    allowed = bank.get("maximum_consent_validity")
    if not allowed:
        # The usual limit of banks that don't say
        return dt.timedelta(days=90)
    return min(dt.timedelta(seconds=allowed), dt.timedelta(days=180))

async def _start_auth(bank: dict, country: str) -> tuple[str, str]:
    """Begin authorising access to a bank; returns (url the user opens, state to check on return)."""
    state = secrets.token_urlsafe(16)
    valid_until = clock.now() + _consent_length(bank)
    data = await _request("POST", "/auth", body={
        "access": {"valid_until": valid_until.astimezone(dt.UTC).isoformat()},
        "aspsp": {"name": bank["name"], "country": country},
        "state": state,
        "redirect_url": config.enable_banking_redirect_url,
        "psu_type": "personal"})
    return data["url"], state

def _code_from(redirect: str, state: str) -> str:
    """The authorisation code in the URL the bank sent the user back to, after checking its state."""
    query = parse_qs(urlparse(redirect).query)
    if query.get("state", [""])[0] != state:
        raise BankingError("The redirect doesn't belong to this authorisation: its state differs")
    if "error" in query:
        raise BankingError(f"The bank refused access: {query['error'][0]}")
    if "code" not in query:
        raise BankingError("No code in the redirect URL")
    return query["code"][0]

async def _session(code: str) -> dict:
    """Exchange the code for a session and save it as the bank link; returns
    {session_id, valid_until, accounts}."""
    data = await _request("POST", "/sessions", body={"code": code})
    link = {"session_id": data["session_id"], "accounts": data["accounts"],
            "valid_until": data["access"]["valid_until"]}
    path = _app().link_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(link, indent=2))
    # The session id reads the user's accounts, so only the user may read it
    path.chmod(0o600)
    return link

def _saved_link() -> dict:
    """The saved bank link, or a BankingError saying how to make one."""
    app = _app()
    try:
        link = json.loads(app.link_path.read_text())
    except OSError as e:
        raise BankingError(f"No bank is linked: link one with {app.command}") from e
    except ValueError as e:
        raise BankingError(f"The saved bank link is damaged: relink with {app.command}") from e
    if not isinstance(link, dict) or not isinstance(link.get("accounts"), list) or not link.get("valid_until"):
        raise BankingError(f"The saved bank link is damaged: relink with {app.command}")
    return link

def _expiry(link: dict) -> dt.datetime:
    return clock.to_local(link["valid_until"])

def _live_link() -> dict:
    """The saved bank link, if it hasn't expired."""
    link = _saved_link()
    if _expiry(link) <= clock.now():
        raise BankingError(f"The bank link expired on {_expiry(link):%Y-%m-%d}: relink with {_app().command}")
    return link

def link_reminder() -> str | None:
    """Telegram text warning that the bank link expires in 7 days or 1 day; None on other days
    or when no bank is linked."""
    try:
        expires = _expiry(_saved_link())
    except BankingError:
        return None
    days_left = (expires.date() - clock.today()).days
    if days_left not in (7, 1):
        return None
    when = "tomorrow" if days_left == 1 else "in 7 days"
    return f"Your bank link expires {when} ({expires:%A %d %B %H:%M}). To keep the bank tools working, relink with {_app().command}."

async def _get_transactions(uid: str, date_from: dt.date, date_to: dt.date, *,
                            user_present: bool) -> tuple[list[dict], bool]:
    """An account's transactions in the date range, following the API's continuation keys, and
    whether that got all of them. user_present: the user asked for them (see _user_present)."""
    params: dict = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    entries: list[dict] = []
    refusals = {
        # A session belongs to the app that made it, so a link made with the other app isn't found
        404: f"Enable Banking doesn't know the saved bank link: it was made with another Enable Banking "
             f"app or withdrawn at the bank. Relink with {_app().command}",
        # PSD2's daily limit, which reads with user_present don't count against
        429: "The bank refused: too many reads of the account today. Try again tomorrow"}
    # Safety cap on following continuation keys (the mock bank pages by 10)
    for _ in range(50):
        data = await _request("GET", f"/accounts/{uid}/transactions", params=params, user_present=user_present,
                              status_messages=refusals)
        entries += data["transactions"]
        if not data.get("continuation_key"):
            return entries, True
        params["continuation_key"] = data["continuation_key"]
    return entries, False

def _account_title(account: dict) -> str:
    """'<product> ••<last 4 of the IBAN> (<currency>)'. The product before the name, since some banks
    (Pekao) fill the name with the holder's, the same for every account; the IBAN's end then tells
    accounts of one product apart."""
    iban = (account.get("account_id") or {}).get("iban") or ""
    label = account.get("product") or account.get("name") or "Account"
    number = f" ••{iban[-4:]}" if iban else ""
    return f"{label}{number} ({account.get('currency') or 'unknown currency'})"

def _date(entry: dict) -> str:
    return entry.get("booking_date") or entry.get("value_date") or entry.get("transaction_date") or ""

def _is_outgoing(entry: dict) -> bool:
    return entry["credit_debit_indicator"] == "DBIT"

def _counterparty(entry: dict) -> tuple[str, str]:
    """(the other side's name, the description), each "" when the bank gives none. The other side
    is who was paid for money out, who paid for money in."""
    party = (entry.get("creditor") if _is_outgoing(entry) else entry.get("debtor")) or {}
    return party.get("name") or "", " ".join(entry.get("remittance_information") or [])

def _line(entry: dict) -> str:
    """One transaction as 'date  amount currency  counterparty: description'."""
    money = entry["transaction_amount"]
    details = ": ".join(part for part in _counterparty(entry) if part) or "no description"
    pending = " (pending)" if entry.get("status") == "PDNG" else ""
    sign = "-" if _is_outgoing(entry) else "+"
    return f"{_date(entry) or 'no date'}  {sign}{money['amount']} {money['currency']}  {details}{pending}"

def _totals(entries: list[dict]) -> str:
    """Money in and out per currency."""
    sums: dict[tuple[str, bool], Decimal] = defaultdict(Decimal)
    for entry in entries:
        money = entry["transaction_amount"]
        sums[money["currency"], _is_outgoing(entry)] += Decimal(money["amount"])
    currencies = sorted({currency for currency, _ in sums})
    return "; ".join(f"in +{sums[currency, False]:.2f} {currency}, out -{sums[currency, True]:.2f} {currency}"
                     for currency in currencies)

def date_range(date_from: str, date_to: str | None) -> tuple[dt.date, dt.date]:
    """A tool's YYYY-MM-DD range, up to today when there's no end; a reversed range is swapped and
    the future cut off rather than failing, since a small model retries poorly."""
    start, end = sorted((clock.parse_date(date_from), clock.parse_date(date_to) if date_to else clock.today()))
    # Nothing has happened after today
    end = min(end, clock.today())
    return min(start, end), end

# PSD2 makes banks give the last 90 days without the user approving again, and no more (Pekao
# refuses day 91 with ASPSP_ERROR)
BANK_HISTORY_DAYS = 90

def _bank_earliest() -> dt.date:
    """The first day the bank still gives, counted from the later of the local and UTC dates in
    case the bank counts in UTC."""
    return max(clock.today(), clock.now().astimezone(dt.UTC).date()) - dt.timedelta(days=BANK_HISTORY_DAYS)

def _account_key(account: dict) -> str:
    """What the store files an account under: its IBAN and currency, which stay the same when the
    user relinks, unlike Enable Banking's uid; the uid when there's no IBAN (the sandbox)."""
    iban = ((account.get("account_id") or {}).get("iban") or "").replace(" ", "")
    return f"{iban} {account.get('currency') or ''}".strip() if iban else account["uid"]

def _days(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]

def _runs(days: list[dt.date]) -> list[tuple[dt.date, dt.date]]:
    """Sorted days as (first, last) runs of consecutive days."""
    runs: list[tuple[dt.date, dt.date]] = []
    for day in days:
        if runs and day == runs[-1][1] + dt.timedelta(days=1):
            runs[-1] = (runs[-1][0], day)
        else:
            runs.append((day, day))
    return runs

@dataclass
class _Read:
    # (account, its stored entries in the range, whether every fetch was complete)
    accounts: list[tuple[dict, list[dict], bool]]
    # Paragraphs for the tool text about days the range couldn't get
    notes: list[str]

async def _sync(account: dict, start: dt.date, end: dt.date, *, user_present: bool) -> tuple[bool, str | None]:
    """Fetch the account's days from the bank that the store lacks or that may have changed since
    (the last config.bank_settle_days, after config.bank_fresh_minutes); the first read of an
    account fetches everything the bank gives. Returns whether every fetch was complete, and a note
    when the bank couldn't be read but every day asked for is stored."""
    key = _account_key(account)
    earliest = _bank_earliest()
    first, last = (earliest, clock.today()) if not bank_store.has_any(key) else (max(start, earliest), end)
    if first > last:
        return True, None
    synced = bank_store.synced(key, first, last)
    settled_before = clock.today() - dt.timedelta(days=config.bank_settle_days - 1)
    stale_before = clock.now() - dt.timedelta(minutes=config.bank_fresh_minutes)
    needed = [day for day in _days(first, last)
              if day not in synced or (day >= settled_before and synced[day] < stale_before)]
    complete = True
    for run_start, run_end in _runs(needed):
        try:
            entries, done = await _get_transactions(account["uid"], run_start, run_end, user_present=user_present)
        except BankingError as e:
            if any(day not in bank_store.synced(key, start, end) for day in _days(start, end)):
                raise
            return complete, f"Could not refresh from the bank ({e}), so payments since {run_start} may be missing."
        bank_store.replace(key, run_start, run_end, [(_date(entry), entry) for entry in entries], complete=done)
        complete = complete and done
    return complete, None

async def _read(start: dt.date, end: dt.date, *, user_present: bool) -> _Read:
    """Every linked account's transactions between two dates, from the store after fetching what it
    lacks. With an expired link, the stored days are still read."""
    link = _saved_link()
    expires = _expiry(link)
    earliest = _bank_earliest()
    read = _Read([], [])
    for account in link["accounts"]:
        complete = True
        if expires > clock.now():
            complete, note = await _sync(account, start, end, user_present=user_present)
            if note:
                read.notes.append(note)
        key = _account_key(account)
        synced = bank_store.synced(key, start, end)
        for run_start, run_end in _runs([day for day in _days(start, end) if day not in synced]):
            if expires <= clock.now():
                read.notes.append(f"The bank link expired on {expires:%Y-%m-%d}, so nothing from {run_start} to "
                                  f"{run_end} is stored: relink with {_app().command}.")
            elif run_start < earliest:
                read.notes.append(f"Nothing is stored from {run_start} to {min(run_end, earliest - dt.timedelta(days=1))}, "
                                  f"and the bank gives only the last {BANK_HISTORY_DAYS} days.")
        read.accounts.append((account, bank_store.entries(key, start, end), complete))
    # Accounts usually share a note
    read.notes = list(dict.fromkeys(read.notes))
    return read

def _with_notes(notes: list[str], text: str) -> str:
    return "".join(f"{note}\n\n" for note in notes) + text

async def transactions(date_from: str, date_to: str | None = None) -> str:
    """Tool text: every linked account's transactions between two YYYY-MM-DD dates, newest first."""
    start, end = date_range(date_from, date_to)
    read = await _read(start, end, user_present=True)
    blocks = []
    for account, entries, complete in read.accounts:
        title = _account_title(account)
        if not entries:
            blocks.append(f"{title}: no transactions from {start} to {end}.")
            continue
        entries.sort(key=_date, reverse=True)
        lines = [_line(entry) for entry in entries[:config.bank_max_transactions]]
        if len(entries) > config.bank_max_transactions:
            lines.append(f"...and {len(entries) - config.bank_max_transactions} older transactions not listed")
        if not complete:
            lines.append("The bank returned more pages than are read, so some transactions are missing "
                         "from this list and the totals")
        blocks.append(f"{title}, {len(entries)} transactions from {start} to {end}, newest first:\n"
                      + "\n".join(lines) + f"\nTotal: {_totals(entries)}")
    return _with_notes(read.notes, "\n\n".join(blocks))

@dataclass(frozen=True)
class Payment:
    """Money that went out of a linked account."""
    date: dt.date
    amount: Decimal
    currency: str
    # Who was paid and the description, each "" when the bank gives none
    party: str
    description: str
    # The card payment's merchant category code (ISO 18245), when the bank gives one
    merchant_code: str

def has_live_link() -> bool:
    """Whether a bank is linked and the link hasn't expired."""
    try:
        _live_link()
    except BankingError:
        return False
    return True

async def payments(start: dt.date, end: dt.date, *, user_present: bool) -> tuple[list[Payment], list[str]]:
    """The booked payments out of every linked account between two dates, by booking date, so a
    payment pending at the end of a range lands in the range it books in and is counted once; and
    notes about days that couldn't be read. user_present: the user asked for them, rather than a
    scheduled job (see _user_present)."""
    read = await _read(start, end, user_present=user_present)
    found = []
    for _, entries, _ in read.accounts:
        for entry in entries:
            if not _is_outgoing(entry) or entry.get("status") == "PDNG" or not _date(entry):
                continue
            money = entry["transaction_amount"]
            party, description = _counterparty(entry)
            found.append(Payment(dt.date.fromisoformat(_date(entry)), Decimal(money["amount"]), money["currency"],
                                 party, description, entry.get("merchant_category_code") or ""))
    return found, read.notes

def _choose(banks: list[dict]) -> dict:
    while True:
        answer = input("Bank number: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(banks):
            return banks[int(answer) - 1]
        print(f"Type a number from 1 to {len(banks)}")

async def _link() -> None:
    print(f"Linking a bank to the Enable Banking {_app().name} app")
    country = input("Country (two letters, e.g. PL): ").strip().upper()
    banks = await _banks(country)
    if not banks:
        raise BankingError(f"Enable Banking offers no banks in {country!r}")
    for number, bank in enumerate(banks, 1):
        print(f"{number}. {bank['name']}")
    url, state = await _start_auth(_choose(banks), country)
    print(f"\nOpen this link, log in and approve:\n{url}\n")
    redirect = input("Paste the address the page shows after you approve:\n").strip()
    link = await _session(_code_from(redirect, state))
    print(f"Linked {len(link['accounts'])} accounts, valid until {_expiry(link):%Y-%m-%d %H:%M}, "
          f"saved to {_app().link_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Link a bank, so the bank tools can read its accounts")
    parser.add_argument("--sandbox", action="store_true", help="link to the sandbox app, with made-up banks")
    if parser.parse_args().sandbox:
        config.enable_banking_sandbox = True
    try:
        asyncio.run(_link())
    except BankingError as e:
        raise SystemExit(str(e)) from e
