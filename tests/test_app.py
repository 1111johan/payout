from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from amazon_payout_api.amazon import AmazonApiError, resolve_marketplace
from amazon_payout_api.config import Settings
from amazon_payout_api.server import PayoutApplication, ScheduleRunner
from amazon_payout_api.store import PayoutStore


class FakeClient:
    calls = 0

    def get_payment_methods(self, marketplace, method_types=None):
        return {"marketplace": marketplace, "data": {"paymentMethods": []}}

    def initiate_payout(self, marketplace, account_type="Standard Orders"):
        self.calls += 1
        return {
            "marketplace": marketplace,
            "requestId": "request-123",
            "data": {"payoutReferenceId": "payout-123"},
        }

    def test_connection(self, marketplace="DE"):
        return {"mode": "sandbox", "lwa": "ok", "spApi": "ok", "sandboxFixture": True}

    def list_financial_event_groups(self, started_after, started_before=None):
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return {
            "groups": [
                {
                    "FinancialEventGroupId": "payout-123",
                    "ProcessingStatus": "Closed",
                    "FundTransferStatus": "Succeeded",
                    "OriginalTotal": {"CurrencyCode": "EUR", "CurrencyAmount": 10.10},
                    "ConvertedTotal": {"CurrencyCode": "EUR", "CurrencyAmount": 10.10},
                    "BeginningBalance": {"CurrencyCode": "EUR", "CurrencyAmount": 1.25},
                    "FundTransferDate": now,
                    "FinancialEventGroupStart": now,
                    "FinancialEventGroupEnd": now,
                    "TraceId": "trace-123",
                    "AccountTail": "7788",
                },
                {
                    "FinancialEventGroupId": "historic-456",
                    "ProcessingStatus": "Closed",
                    "FundTransferStatus": "Unknown",
                    "OriginalTotal": {"CurrencyCode": "GBP", "CurrencyAmount": 20.20},
                    "FinancialEventGroupStart": now,
                },
            ],
            "pageCount": 1,
            "requestIds": ["finance-request-1"],
            "truncated": False,
        }


def settings(path: Path, *, dry_run: bool = True, mode: str = "production", allow_sandbox_post: bool = False) -> Settings:
    return Settings(
        mode=mode,
        api_key="x" * 32,
        lwa_client_id="id",
        lwa_client_secret="secret",
        lwa_refresh_token="refresh",
        endpoint=(
            "https://sandbox.sellingpartnerapi-eu.amazon.com"
            if mode == "sandbox"
            else "https://sellingpartnerapi-eu.amazon.com"
        ),
        allow_production=True,
        allow_payout_post=True,
        dry_run=dry_run,
        database_path=path,
        host="127.0.0.1",
        port=8080,
        allow_sandbox_post=allow_sandbox_post,
    )


class PayoutApplicationTests(unittest.TestCase):
    def test_supported_marketplace(self):
        self.assertEqual(resolve_marketplace("de"), ("DE", "A1PA6795UKMFR9"))

    def test_uk_is_rejected(self):
        with self.assertRaises(AmazonApiError) as context:
            resolve_marketplace("UK")
        self.assertEqual(context.exception.code, "UNSUPPORTED_MARKETPLACE")

    def test_dry_run_never_calls_amazon(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            app = PayoutApplication(settings(Path(directory) / "test.sqlite3"), client=client)
            status, response = app.payout({"marketplace": "DE"}, "dry-run-0001", "")
            self.assertEqual(status, 200)
            self.assertEqual(response["status"], "PREVIEW_ONLY")
            self.assertEqual(client.calls, 0)

    def test_live_requires_marketplace_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PayoutApplication(settings(Path(directory) / "test.sqlite3", dry_run=False), client=FakeClient())
            with self.assertRaises(AmazonApiError) as context:
                app.payout({"marketplace": "DE"}, "live-key-0001", "CONFIRM:FR")
            self.assertEqual(context.exception.code, "CONFIRMATION_REQUIRED")

    def test_idempotent_replay_does_not_submit_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            client.calls = 0
            app = PayoutApplication(settings(Path(directory) / "test.sqlite3", dry_run=False), client=client)
            first = app.payout({"marketplace": "DE"}, "live-key-0002", "CONFIRM:DE")
            second = app.payout({"marketplace": "DE"}, "live-key-0002", "CONFIRM:DE")
            self.assertEqual(first[1]["status"], "SUBMITTED")
            self.assertTrue(second[1]["idempotentReplay"])
            self.assertEqual(client.calls, 1)

    def test_second_live_payout_is_blocked_for_24_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            client.calls = 0
            app = PayoutApplication(settings(Path(directory) / "test.sqlite3", dry_run=False), client=client)
            app.payout({"marketplace": "DE"}, "live-key-1001", "CONFIRM:DE")
            with self.assertRaises(AmazonApiError) as context:
                app.payout({"marketplace": "DE"}, "live-key-1002", "CONFIRM:DE")
            self.assertEqual(context.exception.code, "PAYOUT_INTERVAL_LIMIT")
            self.assertEqual(client.calls, 1)

    def test_sandbox_post_requires_separate_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PayoutApplication(
                settings(Path(directory) / "test.sqlite3", dry_run=False, mode="sandbox"),
                client=FakeClient(),
            )
            with self.assertRaises(AmazonApiError) as context:
                app.payout({"marketplace": "DE"}, "sandbox-1001", "CONFIRM:DE")
            self.assertEqual(context.exception.code, "SANDBOX_POST_DISABLED")

    def test_schedule_and_history_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.sqlite3"
            app = PayoutApplication(settings(database), client=FakeClient())
            app.save_schedule("DE", {"enabled": True, "runAt": "00:00"})
            runner = ScheduleRunner(app)
            now = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
            runner.run_due(now)
            runner.run_due(now)

            restarted = PayoutApplication(settings(database), client=FakeClient())
            self.assertEqual(restarted.schedules()[0]["marketplace"], "DE")
            history = restarted.history(100, None)[1]["items"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["status"], "PREVIEW_ONLY")
            self.assertEqual(history[0]["trigger"], "auto")

    def test_credential_test_is_saved_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PayoutApplication(settings(Path(directory) / "test.sqlite3", mode="sandbox"), client=FakeClient())
            status, response = app.test_credentials("DE")
            self.assertEqual(status, 200)
            self.assertEqual(response["status"], "ok")
            safe_status = str(app.status())
            self.assertNotIn("secret", safe_status)
            self.assertNotIn("refresh", safe_status)

    def test_finance_sync_links_amount_to_submitted_payout(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            client.calls = 0
            app = PayoutApplication(settings(Path(directory) / "test.sqlite3", dry_run=False), client=client)
            app.payout({"marketplace": "DE"}, "finance-live-1001", "CONFIRM:DE")
            status, response = app.sync_finance(180)

            self.assertEqual(status, 200)
            self.assertEqual(response["saved"], 2)
            summary = app.finance_summary(180)[1]
            self.assertEqual(summary["recordCount"], 2)
            self.assertEqual(summary["totals"][0]["succeeded"], "10.1")
            history = app.history(10, "DE")[1]["items"]
            self.assertEqual(history[0]["amount"], {"currency": "EUR", "value": "10.1"})
            self.assertEqual(history[0]["transferStatus"], "Succeeded")
            self.assertIn(b"payout-123", app.finance_csv(180))


if __name__ == "__main__":
    unittest.main()
