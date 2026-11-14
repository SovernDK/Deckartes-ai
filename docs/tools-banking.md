# Bank tools (`enable_banking_api.py`)

`get_bank_transactions(date_from, date_to=None)` lists every linked account's transactions between two dates. The data comes from [Enable Banking](https://enablebanking.com/docs/api/reference/), an open-banking (PSD2) API that reads accounts at many European banks. The app is still on Enable Banking's sandbox, whose Mock ASPSP bank has made-up accounts.

## Setup

1. Register an application at Enable Banking. It gives an application id and a private key (`.pem`), and needs a redirect URL: register `config.enable_banking_redirect_url` (`https://localhost:8080/callback`).
2. Put both in `.env` (see `.env.example`): `ENABLE_BANKING_APP_ID`, and `ENABLE_BANKING_KEY_PATH`, the path of the `.pem`, absolute or relative to the repo folder. `private/` is ignored by git, so `private/key.pem` is a good place.
3. Link the bank, from `src/`:

```bash
../venv/bin/python -m tools.enable_banking_api
```

It asks for a country (two letters), lists that country's banks and prints a link. Approve access at the bank in a browser. It then redirects to the redirect URL, which doesn't load; paste that URL back into the terminal. The bank link (Enable Banking's session id, the accounts and when access ends) is saved to `config.banking_session_path` (`~/.local/share/deckartes/banking_session.json`), readable only by the user. It isn't kept in `~/.cache`, because making it again needs the user.

## Signing requests

Every request carries a JWT, signed with the private key (RS256), with the application id as `kid`, `iss` `enablebanking.com` and `aud` `api.enablebanking.com`. Enable Banking checks it against the public key registered for the application. `_auth` signs a fresh token, valid for an hour, for each request. A missing setting or unreadable key is a `BankingError` naming what to set.

## How long a bank link lasts

PSD2 lets a bank's approval last at most 180 days (90 before July 2023). Each bank sets its own limit, which `/aspsps` gives as `maximum_consent_validity` (seconds). Linking asks for that, capped at 180 days, and for 90 days when the bank doesn't say. The bank may still grant less; the saved `valid_until` is what `/sessions` returned.

Once it passes, the tool returns `The bank link expired on <date>: relink with ...` without calling the API. The morning bank job (`jobs/bank_digest.py`, see [telegram.md](telegram.md)) messages the user when 7 days and 1 day are left. It keeps no state: it sends on the mornings when the days left are exactly 7 or 1, so a morning with the Mac off skips that reminder.

## Tool text

One block per account: `<name> (<currency>), N transactions from <start> to <end>, newest first:`, then one line per transaction, `date  ±amount currency  counterparty: description`, with `(pending)` for `PDNG` ones, and `Total: in +X, out -Y` per currency. `-` is money out (`DBIT`), and the counterparty is then the creditor; `+` is money in, from the debtor. Amounts stay the bank's strings; totals add them as `Decimal` and show two places. An account with nothing in the range says so.

- **Dates.** `date_to` defaults to today. A reversed range is swapped, and an end after today is cut to today, rather than failing, since a small model retries poorly.
- **Size.** Every page is read (following `continuation_key`), sorted newest first by booking date (then value or transaction date), and only the newest `config.bank_max_transactions` (100) per account are listed, with `...and N older transactions not listed`. Totals still count all of them. After 50 pages the tool stops and says some transactions are missing from the list and the totals.
- **Failures** reach the model as `Tool get_bank_transactions failed: <message>`: no bank linked, an expired or damaged bank link (each says how to relink), missing credentials, and Enable Banking's HTTP errors through `http_client`.

Responses aren't cached, since a new payment should show at once.

## Tests

`tests/test_banking_tools.py` calls the tool and the linking command (`_link`, with `input` faked) against `HttpTest.serve`, with a real RSA key generated for the test. It also runs the bank job with Telegram faked.
