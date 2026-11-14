"""Bank transactions from Enable Banking.

Docs: https://enablebanking.com/docs/api/reference/
Every request carries a JWT signed with the application's private key (RS256), which
Enable Banking checks against the public key registered for the application. Needs
ENABLE_BANKING_APP_ID and ENABLE_BANKING_KEY_PATH (the .pem file) in .env.

Reading an account needs a bank link, which the user makes by approving access at their bank
in a browser. `python -m tools.enable_banking_api` (from src/) walks through it and saves the
link (Enable Banking's session and its accounts) to config.banking_session_path. It lasts as
long as the bank allows (at most 180 days under PSD2), then the user links again;
jobs/bank_digest.py reminds them 7 days and 1 day before.
"""

import asyncio
import datetime as dt
import json
import secrets
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt

from core import clock, http_client
from core.config import PROJECT_ROOT, config

API_URL = "https://api.enablebanking.com"
RELINK = "relink with `python -m tools.enable_banking_api` from src/"

class BankingError(Exception):
    pass

def _auth() -> str:
    """A fresh JWT for one request, signed with the app's private key."""
    if not config.enable_banking_app_id or not config.enable_banking_key_path:
        raise BankingError("No Enable Banking credentials: set ENABLE_BANKING_APP_ID and ENABLE_BANKING_KEY_PATH in .env")
    try:
        # Relative paths are relative to the repo folder, whatever the working directory is
        key = (PROJECT_ROOT / Path(config.enable_banking_key_path).expanduser()).read_bytes()
    except OSError as e:
        raise BankingError(f"Could not read the Enable Banking private key: {e}") from e
    now = int(time.time())
    # Enable Banking accepts tokens valid for up to 24 hours; a request needs one for seconds
    lifetime = 3600
    return jwt.encode(
        {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + lifetime},
        key, algorithm="RS256", headers={"kid": config.enable_banking_app_id})

async def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None):
    res = await http_client.request(
        method, f"{API_URL}{path}", name="Enable Banking", error=BankingError,
        params=params, json=body, token=_auth())
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
    path = config.banking_session_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(link, indent=2))
    # The session id reads the user's accounts, so only the user may read it
    path.chmod(0o600)
    return link

def _saved_link() -> dict:
    """The saved bank link, or a BankingError saying how to make one."""
    try:
        link = json.loads(config.banking_session_path.read_text())
    except OSError as e:
        raise BankingError("No bank is linked: link one with `python -m tools.enable_banking_api` from src/") from e
    except ValueError as e:
        raise BankingError(f"The saved bank link is damaged: {RELINK}") from e
    if not isinstance(link, dict) or not isinstance(link.get("accounts"), list) or not link.get("valid_until"):
        raise BankingError(f"The saved bank link is damaged: {RELINK}")
    return link

def _expiry(link: dict) -> dt.datetime:
    return clock.to_local(link["valid_until"])

def _live_link() -> dict:
    """The saved bank link, if it hasn't expired."""
    link = _saved_link()
    if _expiry(link) <= clock.now():
        raise BankingError(f"The bank link expired on {_expiry(link):%Y-%m-%d}: {RELINK}")
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
    return f"Your bank link expires {when} ({expires:%A %d %B %H:%M}). To keep the bank tools working, {RELINK}."

async def _get_transactions(uid: str, date_from: dt.date, date_to: dt.date) -> tuple[list[dict], bool]:
    """An account's transactions in the date range, following the API's continuation keys, and
    whether that got all of them."""
    params: dict = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    entries: list[dict] = []
    # Safety cap on following continuation keys (the mock bank pages by 10)
    for _ in range(50):
        data = await _request("GET", f"/accounts/{uid}/transactions", params=params)
        entries += data["transactions"]
        if not data.get("continuation_key"):
            return entries, True
        params["continuation_key"] = data["continuation_key"]
    return entries, False

def _date(entry: dict) -> str:
    return entry.get("booking_date") or entry.get("value_date") or entry.get("transaction_date") or ""

def _line(entry: dict) -> str:
    """One transaction as 'date  amount currency  counterparty: description'."""
    money = entry["transaction_amount"]
    outgoing = entry["credit_debit_indicator"] == "DBIT"
    # The other side: who was paid for money out, who paid for money in
    party = (entry.get("creditor") if outgoing else entry.get("debtor")) or {}
    description = " ".join(entry.get("remittance_information") or [])
    details = ": ".join(part for part in (party.get("name"), description) if part) or "no description"
    pending = " (pending)" if entry.get("status") == "PDNG" else ""
    return f"{_date(entry) or 'no date'}  {'-' if outgoing else '+'}{money['amount']} {money['currency']}  {details}{pending}"

def _totals(entries: list[dict]) -> str:
    """Money in and out per currency."""
    sums: dict[tuple[str, bool], Decimal] = defaultdict(Decimal)
    for entry in entries:
        money = entry["transaction_amount"]
        sums[money["currency"], entry["credit_debit_indicator"] == "DBIT"] += Decimal(money["amount"])
    currencies = sorted({currency for currency, _ in sums})
    return "; ".join(f"in +{sums[currency, False]:.2f} {currency}, out -{sums[currency, True]:.2f} {currency}"
                     for currency in currencies)

async def transactions(date_from: str, date_to: str | None = None) -> str:
    """Tool text: every linked account's transactions between two YYYY-MM-DD dates, newest first."""
    start, end = sorted((clock.parse_date(date_from), clock.parse_date(date_to) if date_to else clock.today()))
    # Nothing has happened after today
    end = min(end, clock.today())
    start = min(start, end)
    blocks = []
    for account in _live_link()["accounts"]:
        entries, complete = await _get_transactions(account["uid"], start, end)
        title = f"{account.get('name') or 'Account'} ({account.get('currency') or 'unknown currency'})"
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
    return "\n\n".join(blocks)

def _choose(banks: list[dict]) -> dict:
    while True:
        answer = input("Bank number: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(banks):
            return banks[int(answer) - 1]
        print(f"Type a number from 1 to {len(banks)}")

async def _link() -> None:
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
          f"saved to {config.banking_session_path}")

if __name__ == "__main__":
    try:
        asyncio.run(_link())
    except BankingError as e:
        raise SystemExit(str(e)) from e
