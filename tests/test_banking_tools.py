"""The bank transactions tool and linking a bank, against a fake Enable Banking."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core.config import config
from frozen_clock import freeze
from http_fake import HttpTest
from jobs import bank_digest
from tools import enable_banking_api as banking
from tools import tools

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
ACCOUNT = {"uid": "acc-1", "name": "Ella", "currency": "EUR"}
TRANSACTIONS = "/accounts/acc-1/transactions"
RELINK = "relink with `python -m tools.enable_banking_api` from src/"


def entry(
    direction: str,
    amount: str,
    day: str = "2026-11-13",
    *,
    creditor: str | None = None,
    debtor: str | None = None,
    remittance: list[str] | None = None,
    status: str = "BOOK",
) -> dict:
    """A transaction in the shape the Mock ASPSP sandbox returns."""
    return {
        "transaction_amount": {"currency": "EUR", "amount": amount},
        "credit_debit_indicator": direction,
        "status": status,
        "creditor": {"name": creditor} if creditor else None,
        "debtor": {"name": debtor} if debtor else None,
        "booking_date": day,
        "value_date": day,
        "transaction_date": None,
        "remittance_information": remittance or [],
    }


def page(*entries: dict, continuation_key: str | None = None) -> dict:
    return {"transactions": list(entries), "continuation_key": continuation_key}


def use_fake_bank(test: unittest.TestCase) -> None:
    """Point the bank settings, the bank link and the spending history at a temporary folder for one test."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    (Path(folder.name) / "key.pem").write_bytes(PEM)
    for name, value in [
        ("enable_banking_sandbox", False),
        ("enable_banking_app_id", "app-1"),
        ("enable_banking_key_path", str(Path(folder.name) / "key.pem")),
        ("banking_session_path", Path(folder.name) / "link" / "session.json"),
        ("enable_banking_sandbox_id", "sandbox-1"),
        ("enable_banking_sandbox_key_path", str(Path(folder.name) / "key.pem")),
        ("banking_sandbox_session_path", Path(folder.name) / "link" / "sandbox.json"),
        ("spending_path", Path(folder.name) / "spending.db"),
        ("spending_sandbox_path", Path(folder.name) / "spending_sandbox.db"),
    ]:
        patcher = mock.patch.object(config, name, value)
        patcher.start()
        test.addCleanup(patcher.stop)


def link(*accounts: dict, valid_until: str = "2027-05-13T10:00:00Z") -> None:
    """Save a bank link, as linking does."""
    config.banking_session_path.parent.mkdir(parents=True, exist_ok=True)
    config.banking_session_path.write_text(
        json.dumps({"session_id": "s-1", "accounts": list(accounts) or [ACCOUNT], "valid_until": valid_until})
    )


class BankingTest(HttpTest):
    def setUp(self):
        freeze(self, "2026-11-14 12:00")
        use_fake_bank(self)

    def link(self, *accounts: dict, valid_until: str = "2027-05-13T10:00:00Z"):
        link(*accounts, valid_until=valid_until)

    async def test_transactions_are_listed_newest_first_with_totals(self):
        self.link()
        server = self.serve(
            {
                TRANSACTIONS: page(
                    entry("DBIT", "2", "2026-11-02"),
                    entry("CRDT", "10.50", "2026-11-13", debtor="Employer", status="PDNG"),
                    entry("DBIT", "1.02", "2026-11-10", creditor="Onni", remittance=["Coffee", "and cake"]),
                )
            }
        )

        text = await tools.get_bank_transactions("2026-11-01")

        self.assertEqual(
            text,
            "Ella (EUR), 3 transactions from 2026-11-01 to 2026-11-14, newest first:\n"
            "2026-11-13  +10.50 EUR  Employer (pending)\n"
            "2026-11-10  -1.02 EUR  Onni: Coffee and cake\n"
            "2026-11-02  -2 EUR  no description\n"
            "Total: in +10.50 EUR, out -3.02 EUR",
        )
        # The first read fills the store with all the bank gives
        self.assertEqual(dict(server.requests[0].url.params), {"date_from": "2026-08-16", "date_to": "2026-11-14"})
        # The user asked, so the bank's limit of 4 reads a day without them doesn't apply
        self.assertEqual(server.requests[0].headers["psu-user-agent"], "Deckartes")
        self.assertRegex(server.requests[0].headers["psu-ip-address"], r"^\d+\.\d+\.\d+\.\d+$")
        self.assertTrue(
            jwt.decode(
                server.requests[0].headers["authorization"].removeprefix("Bearer "),
                KEY.public_key(),
                algorithms=["RS256"],
                audience="api.enablebanking.com",
            )
        )

    async def test_only_the_newest_are_listed_past_the_cap_but_totals_count_all(self):
        self.link()
        self.serve(
            {
                TRANSACTIONS: page(
                    entry("DBIT", "1.00", "2026-11-01"),
                    entry("DBIT", "2.00", "2026-11-03"),
                    entry("DBIT", "4.00", "2026-11-02"),
                )
            }
        )

        with mock.patch.object(config, "bank_max_transactions", 2):
            text = await tools.get_bank_transactions("2026-11-01", "2026-11-13")

        self.assertEqual(
            text,
            "Ella (EUR), 3 transactions from 2026-11-01 to 2026-11-13, newest first:\n"
            "2026-11-03  -2.00 EUR  no description\n"
            "2026-11-02  -4.00 EUR  no description\n"
            "...and 1 older transactions not listed\n"
            "Total: in +0.00 EUR, out -7.00 EUR",
        )

    async def test_every_page_is_read(self):
        self.link()
        self.serve(
            {
                TRANSACTIONS: lambda request: (
                    page(entry("DBIT", "2.00"))
                    if request.url.params.get("continuation_key") == "next"
                    else page(entry("DBIT", "1.00"), continuation_key="next")
                )
            }
        )

        text = await tools.get_bank_transactions("2026-11-01", "2026-11-13")

        self.assertIn("2 transactions", text)
        self.assertTrue(text.endswith("Total: in +0.00 EUR, out -3.00 EUR"), text)

    async def test_says_transactions_are_missing_when_pages_never_end(self):
        self.link()
        self.serve({TRANSACTIONS: page(entry("DBIT", "1.00"), continuation_key="more")})

        text = await tools.get_bank_transactions("2026-11-01", "2026-11-13")

        self.assertIn(
            "\nThe bank returned more pages than are read, so some transactions are missing "
            "from this list and the totals\nTotal:",
            text,
        )

    async def test_a_reversed_range_is_swapped_and_the_future_cut_to_today(self):
        self.link()
        self.serve({TRANSACTIONS: page()})

        text = await tools.get_bank_transactions("2026-12-01", "2026-11-01")

        self.assertEqual(text, "Ella (EUR): no transactions from 2026-11-01 to 2026-11-14.")

    async def test_days_past_the_banks_90_days_that_are_not_stored_are_named(self):
        self.link()
        server = self.serve({TRANSACTIONS: page()})

        text = await tools.get_bank_transactions("2026-08-01")

        self.assertEqual(
            text,
            "Nothing is stored from 2026-08-01 to 2026-08-15, and the bank gives only the last 90 days.\n\n"
            "Ella (EUR): no transactions from 2026-08-01 to 2026-11-14.",
        )
        self.assertEqual(server.requests[0].url.params["date_from"], "2026-08-16")

    async def test_each_account_gets_its_own_block(self):
        self.link(ACCOUNT, {"uid": "acc-2", "name": "Savings", "currency": "EUR"})
        self.serve({TRANSACTIONS: page(entry("DBIT", "1.00")), "/accounts/acc-2/transactions": page()})

        text = await tools.get_bank_transactions("2026-11-13")

        self.assertEqual(
            text,
            "Ella (EUR), 1 transactions from 2026-11-13 to 2026-11-14, newest first:\n"
            "2026-11-13  -1.00 EUR  no description\n"
            "Total: in +0.00 EUR, out -1.00 EUR\n\n"
            "Savings (EUR): no transactions from 2026-11-13 to 2026-11-14.",
        )

    async def test_accounts_of_one_product_are_told_apart_by_their_number(self):
        product = "Rachunek oszczędnościowo – rozliczeniowy"
        self.link(
            *(
                {
                    "uid": uid,
                    "name": "JAN KOWALSKI",
                    "currency": "PLN",
                    "product": product,
                    "account_id": {"iban": iban},
                }
                for uid, iban in (("acc-1", "PL00 1240 0000 5017"), ("acc-2", "PL00 1240 0000 9792"))
            )
        )
        self.serve({TRANSACTIONS: page(), "/accounts/acc-2/transactions": page()})

        self.assertEqual(
            await tools.get_bank_transactions("2026-11-13"),
            f"{product} ••5017 (PLN): no transactions from 2026-11-13 to 2026-11-14.\n\n"
            f"{product} ••9792 (PLN): no transactions from 2026-11-13 to 2026-11-14.",
        )

    async def test_failures_tell_the_model_what_to_do(self):
        def damaged():
            config.banking_session_path.parent.mkdir(parents=True, exist_ok=True)
            config.banking_session_path.write_text('{"accounts": [}')

        cases = [
            (
                "unlinked",
                lambda: None,
                "No bank is linked: link one with `python -m tools.enable_banking_api` from src/",
            ),
            ("damaged", damaged, f"The saved bank link is damaged: {RELINK}"),
            (
                "no credentials",
                lambda: (self.link(), config.__setattr__("enable_banking_app_id", "")),
                "No Enable Banking production credentials: set ENABLE_BANKING_APP_ID and ENABLE_BANKING_KEY_PATH in .env",
            ),
        ]
        for name, setup, message in cases:
            with self.subTest(name):
                config.banking_session_path.unlink(missing_ok=True)
                setup()
                server = self.serve({TRANSACTIONS: page()})
                self.assertEqual(
                    await tools.run_tool("get_bank_transactions", {"date_from": "2026-11-01"}),
                    f"Tool get_bank_transactions failed: {message}",
                )
                self.assertEqual(server.requests, [])

    async def test_a_question_soon_after_is_answered_from_the_store(self):
        self.link()
        server = self.serve(
            {TRANSACTIONS: page(entry("DBIT", "1.00", "2026-11-02"), entry("DBIT", "2.00", "2026-11-13"))}
        )

        first = await tools.get_bank_transactions("2026-11-01")
        freeze(self, "2026-11-14 12:10")
        again = await tools.get_bank_transactions("2026-11-01")
        narrower = await tools.get_bank_transactions("2026-11-01", "2026-11-05")

        self.assertEqual(first, again)
        self.assertEqual(
            narrower,
            "Ella (EUR), 1 transactions from 2026-11-01 to 2026-11-05, newest first:\n"
            "2026-11-02  -1.00 EUR  no description\n"
            "Total: in +0.00 EUR, out -1.00 EUR",
        )
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(config.spending_path.stat().st_mode & 0o777, 0o600)

    async def test_after_15_minutes_the_last_7_days_are_read_again_in_place_of_what_was_stored(self):
        self.link()
        self.serve(
            {
                TRANSACTIONS: page(
                    entry("DBIT", "1.00", "2026-11-02"),
                    entry("DBIT", "2.00", "2026-11-13", creditor="Lidl", status="PDNG"),
                )
            }
        )
        await tools.get_bank_transactions("2026-11-01")
        freeze(self, "2026-11-14 12:16")
        server = self.serve({TRANSACTIONS: page(entry("DBIT", "2.50", "2026-11-14", creditor="Lidl"))})

        text = await tools.get_bank_transactions("2026-11-01")

        self.assertEqual(dict(server.requests[0].url.params), {"date_from": "2026-11-08", "date_to": "2026-11-14"})
        # The pending payment booked a day later, for a different amount: it's there once
        self.assertEqual(
            text,
            "Ella (EUR), 2 transactions from 2026-11-01 to 2026-11-14, newest first:\n"
            "2026-11-14  -2.50 EUR  Lidl\n"
            "2026-11-02  -1.00 EUR  no description\n"
            "Total: in +0.00 EUR, out -3.50 EUR",
        )

    async def test_stored_days_are_answered_past_the_banks_90_days_and_after_the_link_expires(self):
        self.link(valid_until="2026-12-01T10:00:00Z")
        self.serve({TRANSACTIONS: page(entry("DBIT", "1.00", "2026-11-02"))})
        await tools.get_bank_transactions("2026-11-01")
        freeze(self, "2027-03-01 12:00")
        server = self.serve({TRANSACTIONS: page()})

        stored = await tools.get_bank_transactions("2026-11-01", "2026-11-10")
        partly = await tools.get_bank_transactions("2026-11-10", "2026-11-20")

        self.assertEqual(
            stored,
            "Ella (EUR), 1 transactions from 2026-11-01 to 2026-11-10, newest first:\n"
            "2026-11-02  -1.00 EUR  no description\n"
            "Total: in +0.00 EUR, out -1.00 EUR",
        )
        self.assertEqual(
            partly,
            "The bank link expired on 2026-12-01, so nothing from 2026-11-15 to 2026-11-20 is "
            f"stored: {RELINK}.\n\n"
            "Ella (EUR): no transactions from 2026-11-10 to 2026-11-20.",
        )
        self.assertEqual(server.requests, [])

    async def test_when_the_bank_cannot_be_read_stored_days_are_answered_with_a_note(self):
        self.link()
        self.serve({TRANSACTIONS: page(entry("DBIT", "1.00", "2026-11-02"))})
        await tools.get_bank_transactions("2026-11-01")
        freeze(self, "2026-11-14 12:20")
        self.serve({TRANSACTIONS: httpx.Response(429, json={"error": "ASPSP_RATE_LIMIT_EXCEEDED"})})

        text = await tools.get_bank_transactions("2026-11-01")

        self.assertEqual(
            text,
            "Could not refresh from the bank (The bank refused: too many reads of the account "
            "today. Try again tomorrow), so payments since 2026-11-08 may be missing.\n\n"
            "Ella (EUR), 1 transactions from 2026-11-01 to 2026-11-14, newest first:\n"
            "2026-11-02  -1.00 EUR  no description\n"
            "Total: in +0.00 EUR, out -1.00 EUR",
        )

    async def test_a_read_cut_short_is_tried_again_next_time(self):
        self.link()
        server = self.serve({TRANSACTIONS: page(entry("DBIT", "1.00"), continuation_key="more")})

        await tools.get_bank_transactions("2026-11-01")
        reads = len(server.requests)
        await tools.get_bank_transactions("2026-11-01")

        self.assertEqual(len(server.requests), 2 * reads)

    async def test_relinking_keeps_an_accounts_history_by_its_iban(self):
        iban = {"account_id": {"iban": "PL00 1240 0000 5017"}, "currency": "PLN", "product": "Konto"}
        self.link({"uid": "acc-1", **iban})
        self.serve({TRANSACTIONS: page(entry("DBIT", "1.00", "2026-11-02"))})
        await tools.get_bank_transactions("2026-11-01")
        freeze(self, "2026-11-14 12:20")
        self.link({"uid": "acc-9", **iban})
        server = self.serve({"/accounts/acc-9/transactions": page()})

        text = await tools.get_bank_transactions("2026-11-01")

        self.assertEqual(dict(server.requests[0].url.params), {"date_from": "2026-11-08", "date_to": "2026-11-14"})
        self.assertIn("2026-11-02  -1.00 EUR  no description", text)

    async def run_linking(self, server, *, bank_number: str = "1", state: str | None = None) -> str:
        """Run `python -m tools.enable_banking_api`, typing a wrong bank number first, and return what it printed."""
        numbers = iter(["9", bank_number])

        def typed(prompt: str) -> str:
            if prompt.startswith("Country"):
                return "fi"
            if prompt.startswith("Bank number"):
                return next(numbers)
            sent_state = json.loads(server.requests[1].content)["state"]
            return f"{config.enable_banking_redirect_url}?state={state or sent_state}&code=abc"

        with mock.patch("builtins.input", typed), redirect_stdout(io.StringIO()) as printed:
            await banking._link()
        return printed.getvalue()

    def serve_bank(self, bank: dict):
        return self.serve(
            {
                "/aspsps": {"aspsps": [bank]},
                "/auth": {"url": "https://bank/login"},
                "/sessions": {
                    "session_id": "s-1",
                    "accounts": [ACCOUNT],
                    "access": {"valid_until": "2027-05-13T10:00:00Z"},
                },
            }
        )

    async def test_linking_asks_for_the_longest_access_the_bank_allows_and_saves_the_link(self):
        server = self.serve_bank({"name": "MockASPSP", "country": "FI", "maximum_consent_validity": 365 * 86400})

        printed = await self.run_linking(server)

        auth = json.loads(server.requests[1].content)
        # A year is more than PSD2's 180 days
        self.assertEqual(
            (auth["aspsp"], auth["access"]),
            ({"name": "MockASPSP", "country": "FI"}, {"valid_until": "2027-05-13T10:00:00+00:00"}),
        )
        self.assertEqual(json.loads(server.requests[2].content), {"code": "abc"})
        token = server.requests[2].headers["authorization"].removeprefix("Bearer ")
        self.assertEqual(jwt.get_unverified_header(token)["kid"], "app-1")
        self.assertIn("Type a number from 1 to 1\n", printed)
        self.assertTrue(
            printed.endswith(
                f"Linked 1 accounts, valid until 2027-05-13 12:00, saved to {config.banking_session_path}\n"
            ),
            printed,
        )
        self.assertEqual(config.banking_session_path.stat().st_mode & 0o777, 0o600)
        self.serve({TRANSACTIONS: page()})
        self.assertEqual(
            await tools.get_bank_transactions("2026-11-13"),
            "Ella (EUR): no transactions from 2026-11-13 to 2026-11-14.",
        )

    async def test_linking_a_bank_without_a_limit_asks_for_90_days(self):
        server = self.serve_bank({"name": "MockASPSP", "country": "FI"})

        await self.run_linking(server)

        self.assertEqual(json.loads(server.requests[1].content)["access"], {"valid_until": "2027-02-12T11:00:00+00:00"})

    async def test_the_banks_daily_limit_says_when_to_try_again(self):
        self.link()
        self.serve({TRANSACTIONS: httpx.Response(429, json={"error": "ASPSP_RATE_LIMIT_EXCEEDED"})})

        self.assertEqual(
            await tools.run_tool("get_bank_transactions", {"date_from": "2026-11-01"}),
            "Tool get_bank_transactions failed: The bank refused: too many reads of the account "
            "today. Try again tomorrow",
        )

    async def test_a_link_from_another_app_says_to_relink(self):
        self.link()
        self.serve({TRANSACTIONS: httpx.Response(404, json={"error": "ACCOUNT_DOES_NOT_EXIST"})})

        self.assertEqual(
            await tools.run_tool("get_bank_transactions", {"date_from": "2026-11-01"}),
            "Tool get_bank_transactions failed: Enable Banking doesn't know the saved bank link: it was "
            "made with another Enable Banking app or withdrawn at the bank. Relink with "
            "`python -m tools.enable_banking_api` from src/",
        )

    async def test_sandbox_mode_uses_the_sandbox_app_and_its_own_link(self):
        real = {"uid": "real-1", "name": "Real", "currency": "PLN"}
        self.link(real)
        server = self.serve_bank({"name": "Mock ASPSP", "country": "FI"})

        with mock.patch.object(config, "enable_banking_sandbox", True):
            printed = await self.run_linking(server)
            self.serve({TRANSACTIONS: page(entry("DBIT", "1.00"))})
            self.assertIn("1 transactions", await tools.get_bank_transactions("2026-11-13"))
            config.banking_sandbox_session_path.unlink()
            self.assertEqual(
                await tools.run_tool("get_bank_transactions", {"date_from": "2026-11-13"}),
                "Tool get_bank_transactions failed: No bank is linked: link one with "
                "`python -m tools.enable_banking_api --sandbox` from src/",
            )

        self.assertTrue(printed.startswith("Linking a bank to the Enable Banking sandbox app\n"), printed)
        token = server.requests[0].headers["authorization"].removeprefix("Bearer ")
        self.assertEqual(jwt.get_unverified_header(token)["kid"], "sandbox-1")
        # The production link is untouched
        self.assertEqual(json.loads(config.banking_session_path.read_text())["accounts"], [real])

    async def test_linking_refuses_a_redirect_from_another_authorisation(self):
        server = self.serve_bank({"name": "MockASPSP", "country": "FI"})

        with self.assertRaisesRegex(banking.BankingError, "its state differs"):
            await self.run_linking(server, state="someone-else")
        self.assertFalse(config.banking_session_path.exists())

    async def run_bank_job(self) -> list[str]:
        """Run the morning bank job; returns the Telegram messages it sent."""
        for name, value in (("telegram_token", "123:SECRET"), ("telegram_user_id", "42")):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # A digest of last week and last month is due, with no payments to report
        server = self.serve(
            {"/bot123:SECRET/sendMessage": {"ok": True, "result": {"message_id": 7}}, TRANSACTIONS: page()}
        )
        with self.assertLogs("deckartes.jobs", "INFO"):
            self.assertEqual(await bank_digest.run(), 0)
        return [
            json.loads(request.content)["text"]
            for request in server.requests
            if request.url.path.endswith("/sendMessage")
        ]

    async def test_bank_job_reminds_7_days_and_1_day_before_the_link_expires(self):
        for valid_until, expected in [
            (
                "2026-11-21T08:00:00Z",
                (
                    "Your bank link expires in 7 days (Saturday 21 November 09:00). "
                    f"To keep the bank tools working, {RELINK}."
                ),
            ),
            (
                "2026-11-15T08:00:00Z",
                (
                    "Your bank link expires tomorrow (Sunday 15 November 09:00). "
                    f"To keep the bank tools working, {RELINK}."
                ),
            ),
        ]:
            with self.subTest(valid_until):
                self.link(valid_until=valid_until)
                self.assertEqual(await self.run_bank_job(), [expected])

    async def test_bank_job_sends_nothing_on_other_days_or_without_a_link(self):
        self.assertEqual(await self.run_bank_job(), [])
        for valid_until in ("2026-11-20T08:00:00Z", "2026-11-14T08:00:00Z", "2026-11-01T08:00:00Z"):
            with self.subTest(valid_until):
                self.link(valid_until=valid_until)
                self.assertEqual(await self.run_bank_job(), [])


if __name__ == "__main__":
    unittest.main()
