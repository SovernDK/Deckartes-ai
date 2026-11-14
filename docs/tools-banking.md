# Bank tools (`enable_banking_api.py`)

`get_bank_transactions(date_from, date_to=None)` lists every linked account's transactions between two dates. The data comes from [Enable Banking](https://enablebanking.com/docs/api/reference/), an open-banking (PSD2) API that reads accounts at many European banks. The app uses a production application in restricted mode, which can read only the accounts its owner linked; that is free for personal use, and a contract is needed only to offer it to others. A sandbox application stays available for testing (see below).

## Setup

1. In the Enable Banking Control Panel, under API applications, register an application with environment **Production**. It gives an application id and a private key (`.pem`). As its redirect URL, register `config.enable_banking_redirect_url`, exactly: `https://soverndk.github.io/Deckartes-ai/callback/`.
2. The new application is *Inactive*. Press **Activate by linking accounts** and approve at your bank: the application becomes active in restricted mode, for the accounts linked there. This doesn't give the app access to them; step 4 does.
3. Put both in `.env` (see `.env.example`): `ENABLE_BANKING_APP_ID`, and `ENABLE_BANKING_KEY_PATH`, the path of the `.pem`, absolute or relative to the repo folder. `private/` is ignored by git, so `private/key.pem` is a good place.
4. Link the bank through the API, from `src/`:

```bash
../venv/bin/python -m tools.enable_banking_api
```

It asks for a country (two letters), lists that country's banks and prints a link. Approve access at the bank in a browser, choosing the same accounts as in step 2. The bank then sends the browser to the callback page, which shows its own address with a Copy button; paste that into the terminal. The bank link (Enable Banking's session id, the accounts and when access ends) is saved to `config.banking_session_path` (`~/.local/share/deckartes/banking_session.json`), readable only by the user. It isn't kept in `~/.cache`, because making it again needs the user.

## Sandbox for testing

The sandbox application (made-up banks such as Mock ASPSP, no real money) stays available next to production. Its id and key are `ENABLE_BANKING_SANDBOX_ID` and `ENABLE_BANKING_SANDBOX_KEY_PATH`. `config.enable_banking_sandbox` switches every request to it and to its own bank link, `config.banking_sandbox_session_path`, so testing never replaces the real link:

```bash
cd src && ../venv/bin/python -m tools.enable_banking_api --sandbox   # link a mock bank
ENABLE_BANKING_SANDBOX=1 venv/bin/python src/main.py                  # the app on the sandbox (from the repo folder)
```

Messages that say how to link name the `--sandbox` command while it's on. Register the same redirect URL in the sandbox application too. `_app()` is the one place that picks the application; everything else asks it.

## The redirect URL

Production applications need an https redirect URL that isn't localhost, and Enable Banking refuses one that isn't registered (`REDIRECT_URI_NOT_ALLOWED`). Nothing has to run there: the code in the address is pasted back by hand, and is useless without the application's private key. `pages/callback/index.html` is a static page that only shows the address. The application's privacy URL is `pages/privacy/` (`https://soverndk.github.io/Deckartes-ai/privacy/`), a short notice of what the app reads and where it goes; update it if that changes. `.github/workflows/pages.yml` publishes `pages/` (and nothing else) to GitHub Pages on each push that changes it; it needs Settings > Pages > Source set to *GitHub Actions* once. Changing the URL means changing it in the Control Panel and in `config.enable_banking_redirect_url` together.

## Signing requests

Every request carries a JWT, signed with the private key (RS256), with the application id as `kid`, `iss` `enablebanking.com` and `aud` `api.enablebanking.com`. Enable Banking checks it against the public key registered for the application. `_auth` signs a fresh token, valid for an hour, for each request. A missing setting or unreadable key is a `BankingError` naming what to set.

## How long a bank link lasts

PSD2 lets a bank's approval last at most 180 days (90 before July 2023). Each bank sets its own limit, which `/aspsps` gives as `maximum_consent_validity` (seconds). Linking asks for that, capped at 180 days, and for 90 days when the bank doesn't say. The bank may still grant less; the saved `valid_until` is what `/sessions` returned.

Once it passes, the tool returns `The bank link expired on <date>: relink with ...` without calling the API. The morning bank job (`jobs/bank_digest.py`, see [telegram.md](telegram.md)) messages the user when 7 days and 1 day are left. It keeps no state: it sends on the mornings when the days left are exactly 7 or 1, so a morning with the Mac off skips that reminder.

## Tool text

One block per account: `<product> ••<last 4 of the IBAN> (<currency>), N transactions from <start> to <end>, newest first:`, then one line per transaction, `date  ±amount currency  counterparty: description`, with `(pending)` for `PDNG` ones, and `Total: in +X, out -Y` per currency. `-` is money out (`DBIT`), and the counterparty is then the creditor; `+` is money in, from the debtor. Amounts stay the bank's strings; totals add them as `Decimal` and show two places. An account with nothing in the range says so. The title uses the product before the account name because some banks (Pekao) put the holder's name there, the same for every account; it falls back to the name, then `Account`, and leaves out the number when there's no IBAN (the sandbox).

- **Dates.** `date_to` defaults to today. A reversed range is swapped, and an end after today is cut to today, rather than failing, since a small model retries poorly. The bank is never asked for more than 90 days back: PSD2 lets banks refuse older history unless the user has just approved access, and Pekao answers day 91 with a bare `400 ASPSP_ERROR`. Older days come from the store when it has them; for the rest the text starts with `Nothing is stored from <date> to <date>, and the bank gives only the last 90 days.`
- **Size.** Every page is read (following `continuation_key`), sorted newest first by booking date (then value or transaction date), and only the newest `config.bank_max_transactions` (100) per account are listed, with `...and N older transactions not listed`. Totals still count all of them. After 50 pages the tool stops and says some transactions are missing from the list and the totals.
- **Failures** reach the model as `Tool get_bank_transactions failed: <message>`: no bank linked, an expired or damaged bank link (each says how to relink), missing credentials, and Enable Banking's HTTP errors through `http_client`.

## Stored transactions (`bank_store.py`)

Every transaction read is kept in the spending database (`config.spending_path`, the sandbox's own in sandbox mode, mode 0600, kept forever), so a question reads the bank only for what the store lacks. Both tools, the spending popup and the digests read through `_read`, which syncs each account (`_sync`) and then answers from the store.

- **What's fetched.** An account's first read fetches all the bank gives (90 days). After that, only days in the asked range that aren't synced, plus the last `config.bank_settle_days` (7) when their last read is older than `config.bank_fresh_minutes` (15), since recent payments still change: pending ones book, sometimes on another day and for another amount. Older days are final. Missing days are fetched as runs of consecutive days, one read each. There's no sync job: the bank is read only when the user asks, opens the popup, or a digest is built.
- **Replacing.** A fetch of an account's days deletes the rows it stored for those days and inserts what the bank returned, in one transaction, so nothing is kept twice. Rows are matched by `slot`: the transaction's date kept inside the fetched range, since the bank may return one dated just outside it. Each row keeps the bank's whole entry as JSON, plus account, date, status, direction, amount, currency, payee and description.
- **Synced days.** `synced_days` has a row per account and day with when it was read; a read cut short after 50 pages stores what it got but marks no day, so the next question tries again.
- **Accounts** are filed by IBAN and currency (`_account_key`), which stay the same when the user relinks, unlike Enable Banking's uid; the sandbox's accounts have no IBAN and use the uid. The accounts shown are those in the saved bank link.
- **Gaps and failures.** With an expired link, stored days are still answered, and days not stored get `The bank link expired on <date>, so nothing from <date> to <date> is stored: relink with ...`. When a fetch fails but every asked day is stored, the answer comes from the store with `Could not refresh from the bank (<error>), so payments since <date> may be missing.`; otherwise the tool fails as before. Otherwise the text is the same as without the store.
- **Wiping.** There's no command: delete `spending.db` to forget the transactions (and the labels and saved totals with them).

## The bank's daily limit and the user being present

PSD2 lets a bank refuse more than 4 reads a day of an account made without the user present, and Pekao does: a 429 `ASPSP_RATE_LIMIT_EXCEEDED`, which a few chat questions reached. Enable Banking takes the `Psu-*` headers as the user being there, and then the limit doesn't apply (checked on Pekao: the same read went from 429 to 200 with them). So reads the user asked for (both chat tools and the app's spending popup) send `Psu-Ip-Address` (this Mac's local address, `_local_ip`) and `Psu-User-Agent` (`Deckartes`), through `user_present=True` on `payments` and `_get_transactions`. The morning job reads with `user_present=False`, since claiming the user is there when they aren't isn't allowed; its few reads a day stay inside the limit. Enable Banking wants either all the PSU headers a bank requires or none; Pekao's `/aspsps` entry lists none, and these two are accepted. A 429 that still comes says `The bank refused: too many reads of the account today. Try again tomorrow`.

## Spending categories and digests (`spending.py`)

`get_spending_by_category(date_from, date_to=None)` totals the payments out per category, and `set_spending_category(payee, category)` files a payee under one. The morning bank job sends the same table as a digest.

- **What counts.** Booked (`BOOK`) payments out (`DBIT`) of every linked account (`enable_banking_api.payments`), placed by booking date, so a payment pending at a period's end is counted once, in the period it books in. Money in is left out. `Transfers` (to people or the user's own accounts) is listed on its own line and not counted as spending.
- **Categories** are the fixed `spending.CATEGORIES`, also written out in `set_spending_category`'s docstring: change both together.
- **Labels.** A payee is the paid side's name, or the description without one, normalised (`names.normalize`) and without words of 3 or more digits, so `BIEDRONKA 1234 WARSZAWA` and `Biedronka 5678 Warszawa` are one. A payee not seen before goes to the local model, up to 15 in one `mlx_client.complete` call (a longer list made LFM3 ramble until no answer could be read), with the card merchant category code when the bank gives one. The model sees the kind of payment instead of the bank's boilerplate (`_PAYMENT_KINDS`: Pekao's `TRANSAKCJA KARTĄ PŁATNICZĄ` is a card payment, `PŁATNOŚĆ BLIK` a BLIK payment, `PRZELEW` a bank transfer), since given the raw text it labeled every card and BLIK purchase as Transfers; other descriptions go as they are. The prompt has a few examples and lets the model answer `Unsure`. It answers a JSON object of number → category. An answer that's a known category is saved (`source` `model`); anything else leaves the payee undecided: it counts as `Other` and is asked again next time, and so is everything when the model server fails. The app's spending popup (key `s`, [ui.md](ui.md)) uses `review`: it asks the model again about every payee of the last 30 days without the user's label, drops a model label it's now unsure of, and lets the user label the undecided ones. `set_spending_category` saves a `user` rule: the payee's words, matching every payee that contains them as whole words, the longest rule winning, and always before the model's label. A category is matched ignoring case, then with `names.closest_name`.
- **Storage.** `config.spending_path` (`~/.local/share/deckartes/spending.db`), or `spending_sandbox_path` in sandbox mode, so made-up payments never reach the real history: `labels`, `periods` (each saved digest) and `totals` (per period, category and currency, amounts as `Decimal` strings).
- **Comparisons.** Only a range that is exactly a Monday-to-Sunday week or a calendar month is compared, and only with saved periods; there's no backfill, so comparisons appear as digests are saved. A month: with the month before (`month on month`) and the same month a year earlier (`year on year`). A week: with the average of whichever of the 4 weeks before are saved. Each category and the total spent per currency get `up N%`, `down N%`, `unchanged`, or `new` when the earlier period had none of it. The tool adds `Not compared ...: no saved totals for ...` for a missing baseline, so the model doesn't invent one; the digest leaves that out.
- **Digests.** `due_digests` gives the last finished week and month not saved yet: on Monday and the 1st, or the next morning the job runs if that one was missed; older periods are skipped. A period with payments gets the table plus 2–3 sentences from the model on what stands out (left out when the model can't be reached); an empty one sends nothing but is still saved. `bank_digest.py` puts the digests and the link reminder into one message and saves the digests' totals in the runner's `done` callback, only after Telegram took it, so a digest that couldn't be sent within the hour of retries is sent the next morning. Without a live bank link it sends only the reminder.

## Tests

`tests/test_banking_tools.py` calls the tool and the linking command (`_link`, with `input` faked) against `HttpTest.serve`, with a real RSA key generated for the test. It also runs the bank job with Telegram faked.

`tests/test_spending.py` covers the spending tools and the digest job with the bank, the model (`FakeClient.complete`) and Telegram faked; it reuses `use_fake_bank` and `link` from the bank tests.
