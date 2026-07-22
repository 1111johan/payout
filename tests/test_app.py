from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from amazon_payout_api.amazon import AmazonApiError, resolve_marketplace
from amazon_payout_api.config import Settings
from amazon_payout_api.server import ApiHandler, PayoutApplication, ScheduleRunner, ZINIAO_PAYOUT_INTERVAL
from amazon_payout_api.store import PayoutStore
from amazon_payout_api.ziniao import ZiniaoClient
from amazon_payout_api.ziniao_amazon import PAYMENTS_DASHBOARD_URLS, ZiniaoAmazonPayout


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


class FakeElement:
    def __init__(self, text="", *, disabled=False, on_click=None):
        self.text = text
        self.disabled = disabled
        self.on_click = on_click

    def click(self):
        if self.on_click:
            self.on_click()

    def get_attribute(self, name):
        if name == "disabled" and self.disabled:
            return "true"
        return None


class FakeMarketplaceDriver:
    def __init__(self):
        self.current_url = "about:blank"
        self.option = FakeElement("Canada")
        self.confirm = FakeElement(on_click=self._confirm)

    def get(self, url):
        self.current_url = url

    def find_elements(self, by, selector):
        if selector == "button.full-page-account-switcher-account-details":
            return [self.option]
        if selector == "kat-button[data-test='confirm-selection']":
            return [self.confirm]
        return []

    def execute_script(self, script, *arguments):
        if arguments:
            arguments[0].click()
        return None

    def _confirm(self):
        self.current_url = "https://sellercentral.amazon.com/payments/dashboard/index.html"


class FakeAuthenticatedMarketplaceDriver(FakeMarketplaceDriver):
    def __init__(self):
        super().__init__()
        self.option = FakeElement("United States")
        self.authenticated = False

    def get(self, url):
        self.current_url = url if self.authenticated else "https://sellercentral.amazon.com/ap/signin"

    def find_elements(self, by, selector):
        if not self.authenticated:
            return []
        return super().find_elements(by, selector)

    def execute_script(self, script, *arguments):
        if "cvf-widget-btn-verify-account-switcher" in script:
            self.authenticated = True
            self.current_url = "https://sellercentral.amazon.com/home"
            return "account"
        return super().execute_script(script, *arguments)


class FakeDashboardDriver:
    def __init__(self, state):
        self.state = state
        self.current_url = "https://sellercentral.amazon.co.uk/payments/dashboard/index.html"

    def execute_script(self, script, *arguments):
        return self.state


class FakeReauthDriver:
    current_url = "https://sellercentral.amazon.co.uk/ap/signin"

    def execute_script(self, script, *arguments):
        return False


class FakeAutofillBody:
    text = "Current settlement amount\nGBP 458.87\naccount ending\n819"


class FakeAutofillDriver:
    current_url = "https://sellercentral.amazon.co.uk/ap/signin"

    def execute_script(self, script, *arguments):
        if "ineligibility-alert-section" in script:
            return ""
        self.current_url = "https://sellercentral.amazon.co.uk/payments/disburse/details"
        return True

    def find_element(self, by, selector):
        return FakeAutofillBody()


class FakeMfaDriver:
    current_url = "https://sellercentral.amazon.co.uk/ap/mfa"

    def execute_script(self, script, *arguments):
        self.current_url = "https://sellercentral.amazon.co.uk/payments/dashboard/index.html"
        return True


class FakeResultBody:
    text = "unchanged confirmation page"


class FakeResultDriver:
    current_url = "https://sellercentral.amazon.com/payments/disburse/details"

    def execute_script(self, script, *arguments):
        return {
            "successVisible": False,
            "errorVisible": False,
            "intervalLimited": True,
            "intervalMessage": "Only one request is allowed within 24 hours.",
        }

    def find_element(self, by, selector):
        return FakeResultBody()


class FakeDriverService:
    def stop(self):
        return None


class FakePreparedPayoutDriver:
    def __init__(self):
        self.current_url = "https://sellercentral.amazon.com/payments/disburse/details"
        self.service = FakeDriverService()

    def execute_script(self, script, *arguments):
        return True

    def find_element(self, by, selector):
        return FakeResultBody()


class FakeScheduledZiniaoClient:
    configured = True

    def status(self):
        return {"serviceReachable": True}

    def list_stores(self):
        return [
            {
                "controlType": "browserId",
                "controlId": "store-1",
                "browserId": "store-1",
                "browserOauth": "oauth-1",
                "browserName": "Carkee-US",
                "platformName": "Amazon-US",
                "isExpired": False,
            }
        ]

    def running_stores(self):
        return [
            {
                "controlType": "browserOauth",
                "controlId": "oauth-1",
                "browserOauth": "oauth-1",
                "debuggingPort": 20112,
            }
        ]


class FakeScheduledZiniaoPayout:
    def __init__(self):
        self.prepared = []
        self.submitted = []

    def balance(self, control_type, control_id, marketplace):
        return {
            "marketplace": marketplace,
            "accounts": [
                {"index": 0, "accountType": "Standard Orders", "amount": "10.00", "canRequest": True},
                {"index": 1, "accountType": "Invoice Payment Orders", "amount": "5.00", "canRequest": True},
                {"index": 2, "accountType": "Deferred Transactions", "amount": "0.00", "canRequest": False},
            ],
        }

    def prepare(self, control_type, control_id, marketplace, account_index=0):
        self.prepared.append((marketplace, account_index))
        return {
            "token": f"token-{marketplace}-{account_index}",
            "accountIndex": account_index,
            "accountType": "Standard Orders" if account_index == 0 else "Invoice Payment Orders",
            "amount": f"{marketplace} {10 - account_index * 5}.00",
            "accountTail": "819",
        }

    def submit(self, control_type, control_id, token, confirmation):
        parts = token.removeprefix("token-").rsplit("-", 1)
        self.submitted.append((parts[0], int(parts[1])))
        return {
            "status": "submitted",
            "amount": "10.00",
            "accountTail": "819",
            "message": "accepted",
        }


class FakeScheduledZiniaoFailure:
    def balance(self, control_type, control_id, marketplace):
        return {
            "marketplace": marketplace,
            "accounts": [
                {"index": 0, "accountType": "Standard Orders", "amount": "10.00", "canRequest": True},
            ],
        }

    def prepare(self, control_type, control_id, marketplace, account_index=0):
        raise AmazonApiError(
            504,
            "ZINIAO_MARKETPLACE_SWITCH_UNAVAILABLE",
            "marketplace switcher unavailable before submission",
        )


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
    @staticmethod
    def auth_handler(peer_host: str, request_host: str, supplied_key: str = ""):
        handler = object.__new__(ApiHandler)
        handler.client_address = (peer_host, 12345)
        handler.headers = {"Host": request_host, "X-API-Key": supplied_key}
        handler.server = type(
            "Server",
            (),
            {"app": type("App", (), {"authorize": lambda self, key: key == "valid-key"})()},
        )()
        handler.auth_errors = []
        handler._json = lambda status, payload: handler.auth_errors.append((status, payload))
        return handler

    def test_loopback_request_does_not_require_api_key(self):
        handler = self.auth_handler("127.0.0.1", "127.0.0.1:8080")
        self.assertTrue(handler._require_auth())
        self.assertEqual(handler.auth_errors, [])

    def test_loopback_request_with_untrusted_host_still_requires_api_key(self):
        handler = self.auth_handler("127.0.0.1", "attacker.example")
        self.assertFalse(handler._require_auth())
        self.assertEqual(handler.auth_errors[0][0], 401)

    def test_remote_request_still_accepts_valid_api_key(self):
        handler = self.auth_handler("192.0.2.10", "payout.example", "valid-key")
        self.assertTrue(handler._require_auth())
        self.assertEqual(handler.auth_errors, [])

    def test_ziniao_status_does_not_expose_credentials(self):
        configured = replace(
            settings(Path("unused.sqlite3")),
            ziniao_enabled=True,
            ziniao_client_path=Path("C:/Program Files/ziniao/ziniao.exe"),
            ziniao_company="company-secret",
            ziniao_username="robot-secret",
            ziniao_password="password-secret",
        )
        status = str(ZiniaoClient(configured).status())
        self.assertNotIn("company-secret", status)
        self.assertNotIn("robot-secret", status)
        self.assertNotIn("password-secret", status)

    def test_ziniao_store_control_requires_known_id_type(self):
        with self.assertRaises(AmazonApiError) as context:
            ZiniaoClient._control("unknown", "123")
        self.assertEqual(context.exception.code, "INVALID_ZINIAO_CONTROL_TYPE")

    def test_ziniao_confirmation_value_parser(self):
        lines = ["当前结算金额", "US$712.79", "转入以下尾号的账户:", "819"]
        self.assertEqual(ZiniaoAmazonPayout._value_after(lines, ("当前结算金额",)), "US$712.79")
        self.assertEqual(ZiniaoAmazonPayout._value_after(lines, ("转入以下尾号的账户:",)), "819")

    def test_ziniao_supports_us_uk_and_ca_payment_dashboards(self):
        self.assertEqual(
            {marketplace: PAYMENTS_DASHBOARD_URLS[marketplace] for marketplace in ("US", "UK", "CA")},
            {
                "US": "https://sellercentral.amazon.com/payments/dashboard/index.html",
                "UK": "https://sellercentral.amazon.co.uk/payments/dashboard/index.html",
                "CA": "https://sellercentral.amazon.ca/payments/dashboard/index.html",
            },
        )

    def test_ziniao_switches_marketplace_through_shared_store(self):
        payout = ZiniaoAmazonPayout(settings(Path("unused.sqlite3")), ZiniaoClient(settings(Path("unused.sqlite3"))))
        driver = FakeMarketplaceDriver()
        payout._switch_marketplace(driver, "CA")
        self.assertEqual(driver.current_url, "https://sellercentral.amazon.com/payments/dashboard/index.html")

    def test_ziniao_authenticates_before_opening_marketplace_switcher(self):
        payout = ZiniaoAmazonPayout(settings(Path("unused.sqlite3")), ZiniaoClient(settings(Path("unused.sqlite3"))))
        driver = FakeAuthenticatedMarketplaceDriver()
        payout._switch_marketplace(driver, "US")
        self.assertTrue(driver.authenticated)
        self.assertEqual(driver.current_url, "https://sellercentral.amazon.com/payments/dashboard/index.html")

    def test_ziniao_dashboard_exposes_all_accounts_and_skips_disabled_zero_balance(self):
        payout = ZiniaoAmazonPayout(settings(Path("unused.sqlite3")), ZiniaoClient(settings(Path("unused.sqlite3"))))
        payout._switch_marketplace = lambda driver, code: None
        driver = FakeDashboardDriver(
            {
                "labels": ["标准订单", "发票支付订单", "延迟交易"],
                "amounts": ["GBP 458.87", "GBP 42.50", "GBP 0.00"],
                "total": "GBP 458.87",
                "buttons": [{"disabled": False}, {"disabled": False}, {"disabled": False}],
            }
        )
        result = payout._dashboard(driver, "UK")
        self.assertEqual(len(result["accounts"]), 3)
        self.assertEqual(result["accounts"][0]["accountType"], "Standard Orders")
        self.assertTrue(result["accounts"][0]["canRequest"])
        self.assertEqual(result["accounts"][1]["accountType"], "Invoice Payment Orders")
        self.assertTrue(result["accounts"][1]["canRequest"])
        self.assertEqual(result["accounts"][2]["accountType"], "Deferred Transactions")
        self.assertFalse(result["accounts"][2]["canRequest"])

    def test_ziniao_prepare_and_submit_preserve_invoice_account(self):
        configured = replace(settings(Path("unused.sqlite3")), allow_ziniao_payout=True)
        payout = ZiniaoAmazonPayout(configured, ZiniaoClient(configured))
        driver = FakePreparedPayoutDriver()
        state = {
            "marketplace": "US",
            "accounts": [
                {"index": 0, "accountType": "Standard Orders", "amount": "US$100.00", "canRequest": True},
                {"index": 1, "accountType": "Invoice Payment Orders", "amount": "US$42.50", "canRequest": True},
            ],
        }
        clicked = []
        payout._running_store = lambda control_type, control_id: {}
        payout._driver = lambda store: driver
        payout._dashboard = lambda current_driver, marketplace: state
        payout._click_request_payment = lambda current_driver, account_index: clicked.append(account_index)
        payout._confirmation_state = lambda current_driver: {"amount": "US$42.50", "accountTail": "819"}
        payout._wait_for_result = lambda current_driver, before_url, before_text: {
            "status": "submitted",
            "message": "accepted",
        }

        prepared = payout.prepare("browserOauth", "store-1", "US", 1)
        result = payout.submit(
            "browserOauth",
            "store-1",
            prepared["token"],
            f"CONFIRM:{prepared['token']}",
        )

        self.assertEqual(prepared["accountIndex"], 1)
        self.assertEqual(prepared["accountType"], "Invoice Payment Orders")
        self.assertEqual(result["accountIndex"], 1)
        self.assertEqual(result["accountType"], "Invoice Payment Orders")
        self.assertEqual(clicked, [1, 1])

    def test_ziniao_confirmation_reports_amazon_reauthentication(self):
        configured = replace(settings(Path("unused.sqlite3")), ziniao_amazon_page_timeout_seconds=1)
        payout = ZiniaoAmazonPayout(configured, ZiniaoClient(configured))
        with self.assertRaises(AmazonApiError) as context:
            payout._confirmation_state(FakeReauthDriver())
        self.assertEqual(context.exception.code, "ZINIAO_AMAZON_REAUTH_REQUIRED")

    def test_ziniao_confirmation_uses_autofilled_amazon_password(self):
        payout = ZiniaoAmazonPayout(settings(Path("unused.sqlite3")), ZiniaoClient(settings(Path("unused.sqlite3"))))
        result = payout._confirmation_state(FakeAutofillDriver())
        self.assertEqual(result, {"amount": "GBP 458.87", "accountTail": "819"})

    def test_ziniao_authentication_uses_autofilled_mfa(self):
        payout = ZiniaoAmazonPayout(settings(Path("unused.sqlite3")), ZiniaoClient(settings(Path("unused.sqlite3"))))
        driver = FakeMfaDriver()
        payout._complete_autofilled_authentication(driver)
        self.assertEqual(driver.current_url, "https://sellercentral.amazon.co.uk/payments/dashboard/index.html")

    def test_ziniao_result_reports_visible_24_hour_limit(self):
        payout = ZiniaoAmazonPayout(settings(Path("unused.sqlite3")), ZiniaoClient(settings(Path("unused.sqlite3"))))
        with self.assertRaises(AmazonApiError) as context:
            payout._wait_for_result(
                FakeResultDriver(),
                FakeResultDriver.current_url,
                FakeResultBody.text,
            )
        self.assertEqual(context.exception.code, "ZINIAO_PAYOUT_INTERVAL_LIMIT")

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
            now = datetime.now(timezone.utc) + timedelta(days=1)
            runner.run_due(now)
            runner.run_due(now)

            restarted = PayoutApplication(settings(database), client=FakeClient())
            self.assertEqual(restarted.schedules()[0]["marketplace"], "DE")
            history = restarted.history(100, None)[1]["items"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["status"], "PREVIEW_ONLY")
            self.assertEqual(history[0]["trigger"], "auto")

    def test_schedule_runs_us_uk_and_ca_through_ziniao(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = Path(directory) / "ziniao-schedules.sqlite3"
            configured = replace(
                settings(database, dry_run=False),
                auto_payout_marketplaces=frozenset({"US", "UK", "CA"}),
                ziniao_enabled=True,
                ziniao_client_path=Path("C:/Program Files/ziniao/ziniao.exe"),
                ziniao_company="company",
                ziniao_username="robot",
                ziniao_password="password",
                allow_ziniao_payout=True,
            )
            payout = FakeScheduledZiniaoPayout()
            app = PayoutApplication(
                configured,
                client=FakeClient(),
                ziniao=FakeScheduledZiniaoClient(),
                ziniao_payout=payout,
            )
            for marketplace in ("US", "UK", "CA"):
                app.save_schedule(marketplace, {"enabled": True, "runAt": "00:00"})

            runner = ScheduleRunner(app)
            runner.run_due(datetime.now(timezone.utc))
            self.assertEqual(payout.prepared, [])

            now = datetime.now(timezone.utc) + timedelta(days=1)
            runner.run_due(now)
            runner.run_due(now)

            expected_accounts = [
                ("US", 0),
                ("US", 1),
                ("UK", 0),
                ("UK", 1),
                ("CA", 0),
                ("CA", 1),
            ]
            self.assertEqual(payout.prepared, expected_accounts)
            self.assertEqual(payout.submitted, expected_accounts)
            schedules = app.schedules()
            self.assertEqual({item["marketplace"] for item in schedules}, {"US", "UK", "CA"})
            for schedule in schedules:
                last_submitted = app.store.last_submitted_at(schedule["marketplace"])
                self.assertIsNotNone(last_submitted)
                self.assertGreaterEqual(datetime.fromisoformat(schedule["nextRunAt"]), last_submitted + ZINIAO_PAYOUT_INTERVAL)
            history = app.history(100, None)[1]["items"]
            self.assertEqual(len(history), 6)
            self.assertTrue(all(item["status"] == "SUBMITTED" for item in history))
            self.assertEqual(
                {item["accountType"] for item in history},
                {"Standard Orders", "Invoice Payment Orders"},
            )
            self.assertFalse(any(item["accountType"] == "Deferred Transactions" for item in history))
            self.assertEqual(app.history(10, "UK")[1]["items"][0]["marketplace"], "UK")

    def test_ziniao_pre_submission_timeout_is_failed_not_unknown(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = Path(directory) / "ziniao-pre-submit.sqlite3"
            configured = replace(
                settings(database, dry_run=False),
                auto_payout_marketplaces=frozenset({"US"}),
                ziniao_enabled=True,
                ziniao_client_path=Path("C:/Program Files/ziniao/ziniao.exe"),
                ziniao_company="company",
                ziniao_username="robot",
                ziniao_password="password",
                allow_ziniao_payout=True,
            )
            app = PayoutApplication(
                configured,
                client=FakeClient(),
                ziniao=FakeScheduledZiniaoClient(),
                ziniao_payout=FakeScheduledZiniaoFailure(),
            )
            with self.assertRaises(AmazonApiError):
                app.scheduled_payout("US", "auto-US-20260721-1524")

            history = app.history(10, "US")[1]["items"]
            self.assertEqual(history[0]["status"], "FAILED")
            self.assertEqual(history[0]["errorCode"], "ZINIAO_MARKETPLACE_SWITCH_UNAVAILABLE")

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
