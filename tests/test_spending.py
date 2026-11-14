"""Spending by category, its saved totals and the weekly and monthly digests, against a fake Enable
Banking, a fake model and a fake Telegram."""

import datetime as dt
import json
import unittest
from decimal import Decimal
from unittest import mock

import httpx

from core.config import config
from frozen_clock import freeze
from http_fake import HttpTest
from jobs import bank_digest, runner
from model import mlx_client, mlx_server
from model.mlx_server import ModelServerError
from test_banking_tools import TRANSACTIONS, entry, link, page, use_fake_bank
from test_conversation import FakeClient
from tools import spending, tools

SEND = "/bot123:SECRET/sendMessage"
SEND_OK = {"ok": True, "result": {"message_id": 7}}

def says(text: str) -> list[mlx_client.Delta]:
    """One model reply."""
    return [mlx_client.Delta(content=text)]

def sure(category: str) -> dict[str, float]:
    """The decision model's answer for a payee, with the category above the labeling limit."""
    return {"Other": 0.1, category: 0.9}

def unsure(category: str) -> dict[str, float]:
    """An answer whose best category is below the limit, which leaves the payee undecided."""
    return {"Other": 0.35, "Shopping": 0.25, category: 0.4}

class FakeDecisions:
    """Stands in for mlx_client.decide: answers each payee with the next answer (an exception is
    raised), and records the payments' states and the options asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.states = []
        self.options = {}

    async def decide(self, state: str, question: str, options: dict[str, str]) -> dict[str, float]:
        self.states.append(state)
        self.options = options
        if not self.answers:
            raise ConnectionError("no more answers")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

def use_decisions(test: unittest.TestCase, *answers) -> FakeDecisions:
    """Fakes the decision model for the test, and records stopping its server (a mock)."""
    decisions = FakeDecisions(*answers)
    for target, name, value in ((mlx_client, "decide", decisions.decide),
                                (mlx_server, "stop_decisions", mock.Mock(return_value=True))):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        test.addCleanup(patcher.stop)
    return decisions

def saved(kind: str, start: str, **amounts: str) -> spending.Digest:
    """A digest already sent, with EUR totals per category: saved("month", "2026-09-01", Groceries="100")."""
    return spending.Digest(spending.Period(kind, dt.date.fromisoformat(start)),
                           {(category.replace("_", " "), "EUR"): spending.Total(Decimal(amount), 1)
                            for category, amount in amounts.items()})

WEEK = [
    entry("DBIT", "40.00", "2026-11-03", creditor="BIEDRONKA 1234 WARSZAWA"),
    entry("DBIT", "25.50", "2026-11-05", creditor="Biedronka 5678 Warszawa"),
    entry("DBIT", "200.00", "2026-11-06", creditor="ORLEN"),
    entry("DBIT", "100.00", "2026-11-07", creditor="Jan Kowalski", remittance=["Rent share"]),
    # Not spending: money in, a payment still pending, and one outside the week
    entry("CRDT", "4000.00", "2026-11-04", debtor="Employer"),
    entry("DBIT", "9.99", "2026-11-08", creditor="Streamly", status="PDNG"),
    entry("DBIT", "5.00", "2026-11-09", creditor="ORLEN"),
]

class SpendingTest(HttpTest):
    def setUp(self):
        freeze(self, "2026-11-14 12:00")
        use_fake_bank(self)
        link()

    async def test_a_week_is_totaled_by_category_and_payees_are_labeled_once(self):
        self.serve({TRANSACTIONS: page(*WEEK)})
        decisions = use_decisions(self, sure("Groceries"), sure("Fuel"), sure("Transfers"))

        text = await tools.get_spending_by_category("2026-11-02", "2026-11-08")

        self.assertEqual(text, "Spending in the week of Monday 2 – Sunday 8 November 2026:\n"
                               "- Fuel: 200.00 EUR (1 payment)\n"
                               "- Groceries: 65.50 EUR (2 payments)\n"
                               "Total spent: 265.50 EUR\n"
                               "Transfers, not counted as spending: 100.00 EUR (1 payment)\n"
                               "Not compared on the weeks before: no saved totals for the 4 weeks before.")
        # Both Biedronka shops are one payee, asked about once with every category as an option
        self.assertEqual(decisions.states, ["Odbiorca: BIEDRONKA 1234 WARSZAWA", "Odbiorca: ORLEN",
                                            "Odbiorca: Jan Kowalski\nOpis: Rent share"])
        self.assertEqual(list(decisions.options), list(spending.CATEGORIES))
        mlx_server.stop_decisions.assert_called_once_with()
        # Remembered: no second question to the model
        self.assertIn("Total spent: 265.50 EUR", await tools.get_spending_by_category("2026-11-02", "2026-11-08"))
        self.assertEqual(len(decisions.states), 3)

    async def test_a_payee_the_model_did_not_label_counts_as_other_and_is_asked_about_again(self):
        self.serve({TRANSACTIONS: page(entry("DBIT", "12.00", "2026-11-10", creditor="Apteka Zdrowie"))})
        decisions = use_decisions(self, unsure("Health"), ModelServerError("down"), sure("Health"))

        for _ in range(2):
            self.assertIn("- Other: 12.00 EUR (1 payment)\n",
                          await tools.get_spending_by_category("2026-11-10"))
        self.assertIn("- Health: 12.00 EUR (1 payment)\n", await tools.get_spending_by_category("2026-11-10"))
        self.assertEqual(len(decisions.states), 3)

    async def test_the_users_label_wins_over_the_models_for_every_matching_payee(self):
        self.serve({TRANSACTIONS: page(*WEEK)})
        use_decisions(self, sure("Shopping"), sure("Fuel"), sure("Transfers"))
        await tools.get_spending_by_category("2026-11-02", "2026-11-08")

        self.assertEqual(await tools.set_spending_category("Biedronka", "groceries"),
                         "Saved: payments to Biedronka now count as Groceries (1 payee seen so far matches).")
        self.assertIn("- Groceries: 65.50 EUR (2 payments)\n",
                      await tools.get_spending_by_category("2026-11-02", "2026-11-08"))
        self.assertEqual(await tools.run_tool("set_spending_category", {"payee": "Orlen", "category": "Pets"}),
                         "Tool set_spending_category failed: No category 'Pets'; use one of: Groceries, "
                         "Restaurants & cafés, Transport, Fuel, Housing & bills, Shopping, Health, "
                         "Entertainment & subscriptions, Travel, Cash, Transfers, Other")

    async def test_a_month_is_compared_month_on_month_and_year_on_year(self):
        spending.save([saved("month", "2026-09-01", Groceries="50.00", Fuel="250.00"),
                       saved("month", "2025-10-01", Groceries="40.00", Transfers="999.00")])
        self.serve({TRANSACTIONS: page(*(dict(payment, booking_date=payment["booking_date"].replace("-11-", "-10-"))
                                         for payment in WEEK))})
        use_decisions(self, sure("Groceries"), sure("Fuel"), sure("Transfers"))

        text = await tools.get_spending_by_category("2026-10-01", "2026-10-31")

        self.assertEqual(text, "Spending in October 2026:\n"
                               "- Fuel: 205.00 EUR (2 payments), down 18% month on month, new year on year\n"
                               "- Groceries: 65.50 EUR (2 payments), up 31% month on month, up 64% year on year\n"
                               "Total spent: 270.50 EUR, down 10% month on month, up 576% year on year\n"
                               "Transfers, not counted as spending: 100.00 EUR (1 payment)")

    async def test_a_range_that_is_not_a_week_or_month_is_not_compared(self):
        self.serve({TRANSACTIONS: page()})

        self.assertEqual(await tools.get_spending_by_category("2026-11-01"),
                         "No payments out in 2026-11-01 to 2026-11-14.")

    async def test_a_start_past_the_banks_90_days_is_moved_up_and_the_model_told(self):
        self.serve({TRANSACTIONS: page()})

        self.assertEqual(await tools.get_spending_by_category("2026-08-14"),
                         "Nothing is stored from 2026-08-14 to 2026-08-15, and the bank gives only the last 90 days.\n\n"
                         "No payments out in 2026-08-14 to 2026-11-14.")

class DigestJobTest(HttpTest):
    def setUp(self):
        # Monday: last week (26 October to 1 November) and October are both finished
        freeze(self, "2026-11-02 09:05")
        use_fake_bank(self)
        link()
        for target, name, value in ((config, "telegram_token", "123:SECRET"), (config, "telegram_user_id", "42"),
                                    (runner, "RETRY_SECONDS", 0)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = FakeClient()
        patcher = mock.patch.object(mlx_client, "complete", self.client.complete)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.decisions = use_decisions(self, sure("Groceries"))

    async def run_job(self, *payments: dict, send=SEND_OK) -> list[str]:
        """Run the morning bank job; returns the Telegram messages it sent."""
        server = self.server = self.serve({TRANSACTIONS: page(*payments), SEND: send})
        with self.assertLogs("deckartes.jobs", "INFO"):
            await bank_digest.run()
        return [json.loads(request.content)["text"] for request in server.requests if request.url.path == SEND]

    async def test_monday_after_a_month_end_sends_the_week_and_the_month_then_not_again(self):
        spending.save([saved("week", "2026-10-19", Groceries="40.00"), saved("week", "2026-10-12", Groceries="20.00"),
                       saved("month", "2026-09-01", Groceries="100.00")])
        self.client.rounds = [says("Groceries cost more than usual this week."),
                              says("You spent less on groceries than in September.")]
        shopping = [entry("DBIT", "45.00", "2026-10-31", creditor="Lidl"),
                    entry("DBIT", "30.00", "2026-10-05", creditor="Lidl")]

        self.assertEqual(await self.run_job(*shopping), [
            ("Spending in the week of Monday 26 October – Sunday 1 November 2026:\n"
            "- Groceries: 45.00 EUR (1 payment), up 50% on the average of the 2 weeks before\n"
            "Total spent: 45.00 EUR, up 50% on the average of the 2 weeks before\n\n"
            "Groceries cost more than usual this week.\n\n"
            "Spending in October 2026:\n"
            "- Groceries: 75.00 EUR (2 payments), down 25% month on month\n"
            "Total spent: 75.00 EUR, down 25% month on month\n\n"
            "You spent less on groceries than in September.")])
        # Run without the user, so it mustn't claim they're there
        self.assertNotIn("psu-ip-address", self.server.requests[0].headers)
        self.assertEqual(await self.run_job(*shopping), [])

    async def test_without_the_model_the_digest_is_the_table_alone(self):
        self.decisions.answers = [ModelServerError("down")] * 4
        self.client.rounds = [[ModelServerError("down")]] * 4

        self.assertEqual(await self.run_job(entry("DBIT", "45.00", "2026-10-31", creditor="Lidl")), [
            ("Spending in the week of Monday 26 October – Sunday 1 November 2026:\n"
            "- Other: 45.00 EUR (1 payment)\n"
            "Total spent: 45.00 EUR\n\n"
            "Spending in October 2026:\n"
            "- Other: 45.00 EUR (1 payment)\n"
            "Total spent: 45.00 EUR")])

    async def test_a_digest_that_could_not_be_sent_is_sent_the_next_morning(self):
        self.client.rounds = [says("Week."), says("Month."),
                              says("Week again."), says("Month again.")]
        payment = entry("DBIT", "45.00", "2026-10-31", creditor="Lidl")

        with mock.patch.object(runner, "MAX_ATTEMPTS", 1):
            await self.run_job(payment, send=httpx.Response(500, json={"ok": False}))
        freeze(self, "2026-11-03 09:05")
        sent = await self.run_job(payment)

        self.assertEqual(len(sent), 1)
        self.assertIn("Spending in the week of Monday 26 October", sent[0])
        self.assertTrue(sent[0].endswith("Month again."), sent[0])

if __name__ == "__main__":
    unittest.main()
