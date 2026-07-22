from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
import threading
import time
from typing import Any

from .amazon import AmazonApiError
from .config import Settings
from .ziniao import ZiniaoClient


PAYMENTS_DASHBOARD_URLS = {
    "US": "https://sellercentral.amazon.com/payments/dashboard/index.html",
    "UK": "https://sellercentral.amazon.co.uk/payments/dashboard/index.html",
    "GB": "https://sellercentral.amazon.co.uk/payments/dashboard/index.html",
    "CA": "https://sellercentral.amazon.ca/payments/dashboard/index.html",
}
ACCOUNT_SWITCHER_URL = (
    "https://sellercentral.amazon.com/account-switcher/default/merchantMarketplace"
    "?returnTo=%2Fpayments%2Fdashboard%2Findex.html"
)
MARKETPLACE_SWITCH_LABELS = {
    "US": ("United States", "\u7f8e\u56fd"),
    "UK": ("United Kingdom", "\u82f1\u56fd"),
    "GB": ("United Kingdom", "\u82f1\u56fd"),
    "CA": ("Canada", "\u52a0\u62ff\u5927"),
}


@dataclass
class PreparedPayout:
    token: str
    control_type: str
    control_id: str
    marketplace: str
    account_index: int
    account_type: str
    amount: str
    account_tail: str
    expires_at: datetime


class ZiniaoAmazonPayout:
    def __init__(self, settings: Settings, ziniao: ZiniaoClient):
        self.settings = settings
        self.ziniao = ziniao
        self._lock = threading.Lock()
        self._prepared: dict[str, PreparedPayout] = {}

    def balance(self, control_type: str, control_id: str, marketplace: str) -> dict[str, Any]:
        with self._lock:
            store = self._running_store(control_type, control_id)
            driver = self._driver(store)
            try:
                state = self._dashboard(driver, marketplace)
                return {"status": "ready", **state}
            finally:
                driver.service.stop()

    def prepare(
        self,
        control_type: str,
        control_id: str,
        marketplace: str,
        account_index: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            store = self._running_store(control_type, control_id)
            driver = self._driver(store)
            try:
                state = self._dashboard(driver, marketplace)
                account = self._require_account_request(state, account_index)
                self._click_request_payment(driver, account_index)
                confirmation = self._confirmation_state(driver)
                token = secrets.token_urlsafe(32)
                prepared = PreparedPayout(
                    token=token,
                    control_type=control_type,
                    control_id=control_id,
                    marketplace=marketplace.strip().upper(),
                    account_index=account_index,
                    account_type=str(account["accountType"]),
                    amount=confirmation["amount"],
                    account_tail=confirmation["accountTail"],
                    expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.settings.ziniao_prepare_ttl_seconds),
                )
                self._prepared[token] = prepared
                self._purge_expired()
                return {
                    "status": "confirmation_required",
                    "token": token,
                    "marketplace": marketplace.upper(),
                    "accountIndex": prepared.account_index,
                    "accountType": prepared.account_type,
                    "amount": prepared.amount,
                    "accountTail": prepared.account_tail,
                    "expiresAt": prepared.expires_at.isoformat(),
                    "pageUrl": driver.current_url,
                }
            finally:
                driver.service.stop()

    def submit(
        self,
        control_type: str,
        control_id: str,
        token: str,
        confirmation_header: str,
    ) -> dict[str, Any]:
        if not self.settings.allow_ziniao_payout:
            raise AmazonApiError(403, "ZINIAO_PAYOUT_DISABLED", "Ziniao payment requests are disabled by local configuration")
        if not token or confirmation_header != f"CONFIRM:{token}":
            raise AmazonApiError(400, "ZINIAO_PAYOUT_CONFIRMATION_REQUIRED", "A matching payment confirmation is required")
        with self._lock:
            self._purge_expired()
            prepared = self._prepared.pop(token, None)
            if prepared is None:
                raise AmazonApiError(409, "ZINIAO_PREPARATION_EXPIRED", "The prepared payment request is missing or expired")
            if prepared.control_type != control_type or prepared.control_id != control_id:
                raise AmazonApiError(409, "ZINIAO_PREPARATION_MISMATCH", "The prepared payment request belongs to another store")
            store = self._running_store(control_type, control_id)
            driver = self._driver(store)
            try:
                state = self._dashboard(driver, prepared.marketplace)
                self._require_account_request(
                    state,
                    prepared.account_index,
                    expected_type=prepared.account_type,
                )
                self._click_request_payment(driver, prepared.account_index)
                current = self._confirmation_state(driver)
                if current["amount"] != prepared.amount or current["accountTail"] != prepared.account_tail:
                    raise AmazonApiError(
                        409,
                        "ZINIAO_PAYMENT_DETAILS_CHANGED",
                        "Amazon payment amount or destination account changed after preparation",
                    )
                before_url = driver.current_url
                before_text = driver.find_element("tag name", "body").text
                driver.execute_script(
                    "const candidates=[...document.querySelectorAll('kat-button,button,input[type=submit],input[type=button]')].filter(x=>"
                    "(x.getAttribute('label')||x.innerText||x.value||'').trim().toLowerCase()==='请求付款'||"
                    "(x.getAttribute('label')||x.innerText||x.value||'').trim().toLowerCase()==='request payment');"
                    "const enabled=candidates.filter(x=>!x.hasAttribute('disabled'));"
                    "if(enabled.length!==1){throw new Error('Expected one final request-payment button');}"
                    "enabled[0].click();"
                )
                result = self._wait_for_result(driver, before_url, before_text)
                return {
                    "status": result["status"],
                    "marketplace": prepared.marketplace,
                    "accountIndex": prepared.account_index,
                    "accountType": prepared.account_type,
                    "amount": prepared.amount,
                    "accountTail": prepared.account_tail,
                    "pageUrl": driver.current_url,
                    "message": result["message"],
                }
            except AmazonApiError:
                raise
            except Exception as error:
                raise AmazonApiError(
                    502,
                    "ZINIAO_PAYOUT_RESULT_UNKNOWN",
                    "The final Amazon payment result could not be confirmed; do not retry automatically",
                ) from error
            finally:
                driver.service.stop()

    def _dashboard(self, driver: Any, marketplace: str) -> dict[str, Any]:
        code = marketplace.strip().upper()
        if code not in MARKETPLACE_SWITCH_LABELS:
            raise AmazonApiError(422, "ZINIAO_UNSUPPORTED_MARKETPLACE", "Ziniao payment-page control supports US, UK and CA")
        self._switch_marketplace(driver, code)
        self._complete_autofilled_authentication(driver)
        deadline = time.monotonic() + self.settings.ziniao_amazon_page_timeout_seconds
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_state = driver.execute_script(
                "const labels=[...document.querySelectorAll('.marketplace-name-text')].map(x=>(x.innerText||x.textContent||'').trim());"
                "const amounts=[...document.querySelectorAll('.available-currency-amount')].map(x=>(x.innerText||x.textContent||'').trim());"
                "const total=(document.querySelector('.available-currency-total-amount')?.innerText||'').trim();"
                "const buttons=[...document.querySelectorAll('kat-button')].filter(x=>['请求付款','Request payment'].includes(x.getAttribute('label'))).map(x=>({disabled:x.hasAttribute('disabled')}));"
                "return {labels,amounts,total,buttons};"
            )
            labels = last_state.get("labels", [])
            amounts = last_state.get("amounts", [])
            buttons = last_state.get("buttons", [])
            if labels and len(amounts) >= len(labels) and len(buttons) >= len(labels):
                break
            time.sleep(1)
        else:
            raise AmazonApiError(
                504,
                "ZINIAO_AMAZON_PAGE_NOT_READY",
                "Amazon payment dashboard did not finish loading before the timeout",
                last_state,
            )
        accounts = []
        for index, label in enumerate(last_state["labels"]):
            amount = str(last_state["amounts"][index])
            accounts.append(
                {
                    "index": index,
                    "accountType": self._canonical_account_type(str(label)),
                    "amount": amount,
                    "canRequest": (
                        not bool(last_state["buttons"][index]["disabled"])
                        and self._amount_is_positive(amount)
                    ),
                }
            )
        return {
            "marketplace": code,
            "pageUrl": driver.current_url,
            "totalAvailable": last_state.get("total") or None,
            "accounts": accounts,
        }

    def _switch_marketplace(self, driver: Any, code: str) -> None:
        driver.get(ACCOUNT_SWITCHER_URL)
        deadline = time.monotonic() + self.settings.ziniao_amazon_page_timeout_seconds
        option = None
        authentication_completed = False
        while time.monotonic() < deadline:
            if any(path in driver.current_url for path in ("/ap/signin", "/ap/mfa", "/ap/cvf", "/ap/challenge")):
                if authentication_completed:
                    raise AmazonApiError(
                        409,
                        "ZINIAO_AMAZON_REAUTH_FAILED",
                        "Amazon returned to authentication while opening the marketplace switcher",
                    )
                self._complete_autofilled_authentication(driver)
                authentication_completed = True
                driver.get(ACCOUNT_SWITCHER_URL)
                continue
            options = driver.find_elements("css selector", "button.full-page-account-switcher-account-details")
            labels = MARKETPLACE_SWITCH_LABELS[code]
            option = next(
                (item for item in options if any(item.text.strip().startswith(label) for label in labels)),
                None,
            )
            if option is not None:
                break
            time.sleep(0.5)
        if option is None:
            raise AmazonApiError(
                504,
                "ZINIAO_MARKETPLACE_SWITCH_UNAVAILABLE",
                f"Amazon Seller Central did not expose the {code} marketplace in the account switcher",
            )
        option.click()

        confirm = None
        while time.monotonic() < deadline:
            matches = driver.find_elements("css selector", "kat-button[data-test='confirm-selection']")
            if matches:
                disabled = str(matches[0].get_attribute("disabled") or "").strip().lower()
                if disabled not in {"1", "true", "disabled"}:
                    confirm = matches[0]
                    break
            time.sleep(0.25)
        if confirm is None:
            raise AmazonApiError(
                504,
                "ZINIAO_MARKETPLACE_CONFIRM_UNAVAILABLE",
                "Amazon Seller Central did not expose the marketplace confirmation control",
            )
        driver.execute_script("arguments[0].click();", confirm)
        while time.monotonic() < deadline:
            if "/account-switcher/" not in driver.current_url:
                return
            time.sleep(0.5)
        raise AmazonApiError(
            504,
            "ZINIAO_MARKETPLACE_SWITCH_TIMEOUT",
            f"Amazon Seller Central did not finish switching to {code}",
        )

    @staticmethod
    def _canonical_account_type(label: str) -> str:
        known_types = {
            "standard orders": "Standard Orders",
            "标准订单": "Standard Orders",
            "invoice payment orders": "Invoice Payment Orders",
            "发票支付订单": "Invoice Payment Orders",
            "deferred transactions": "Deferred Transactions",
            "延迟交易": "Deferred Transactions",
        }
        normalized = " ".join(label.split()).casefold()
        return known_types.get(normalized, " ".join(label.split()))

    @staticmethod
    def _amount_is_positive(amount: str) -> bool:
        return any(character in "123456789" for character in amount)

    @staticmethod
    def _require_account_request(
        state: dict[str, Any],
        account_index: int,
        *,
        expected_type: str | None = None,
    ) -> dict[str, Any]:
        account = next((item for item in state.get("accounts", []) if item.get("index") == account_index), None)
        if account is None:
            raise AmazonApiError(
                422,
                "ZINIAO_ACCOUNT_NOT_FOUND",
                "Amazon no longer exposes the selected payment account",
            )
        if expected_type is not None and account.get("accountType") != expected_type:
            raise AmazonApiError(
                409,
                "ZINIAO_ACCOUNT_CHANGED",
                "The selected Amazon payment account changed after preparation",
            )
        if not account.get("canRequest"):
            raise AmazonApiError(
                422,
                "ZINIAO_ACCOUNT_UNAVAILABLE",
                f"The {account.get('accountType') or 'selected'} account does not currently allow a payment request",
            )
        return account

    @staticmethod
    def _click_request_payment(driver: Any, index: int) -> None:
        clicked = driver.execute_script(
            "const labels=['请求付款','Request payment'];"
            "const buttons=[...document.querySelectorAll('kat-button')].filter(x=>labels.includes((x.getAttribute('label')||'').trim()));"
            "const button=buttons[arguments[0]];"
            "if(!button||button.hasAttribute('disabled')){return false;}"
            "button.click();return true;",
            index,
        )
        if not clicked:
            raise AmazonApiError(
                409,
                "ZINIAO_REQUEST_BUTTON_UNAVAILABLE",
                "Amazon no longer exposes an enabled request-payment button for the selected account",
            )

    def _confirmation_state(self, driver: Any) -> dict[str, str]:
        deadline = time.monotonic() + self.settings.ziniao_amazon_page_timeout_seconds
        body = ""
        self._complete_autofilled_authentication(driver)
        while time.monotonic() < deadline:
            interval_limit = self._interval_limit_message(driver)
            if interval_limit:
                raise AmazonApiError(
                    409,
                    "ZINIAO_PAYOUT_INTERVAL_LIMIT",
                    interval_limit,
                )
            body = driver.find_element("tag name", "body").text
            if ("当前结算金额" in body or "Current settlement amount" in body) and (
                "转入以下尾号的账户" in body or "account ending" in body.lower()
            ):
                break
            time.sleep(0.5)
        else:
            raise AmazonApiError(
                504,
                "ZINIAO_CONFIRMATION_PAGE_NOT_READY",
                "Amazon payment confirmation page did not finish loading",
            )
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        amount = self._value_after(lines, ("当前结算金额", "Current settlement amount"))
        account_tail = self._value_after(lines, ("转入以下尾号的账户:", "转入以下尾号的账户：", "account ending"))
        if not amount or not account_tail:
            raise AmazonApiError(502, "ZINIAO_CONFIRMATION_PARSE_FAILED", "Could not read payment amount or account tail")
        return {"amount": amount, "accountTail": account_tail}

    @staticmethod
    def _interval_limit_message(driver: Any) -> str:
        return str(
            driver.execute_script(
                "const alert=document.querySelector('#ineligibility-alert-section');"
                "const buttons=[...document.querySelectorAll('kat-button')].filter(x=>['请求付款','Request payment'].includes((x.getAttribute('label')||'').trim()));"
                "if(!alert||!buttons.length||!buttons.every(x=>x.hasAttribute('disabled'))){return '';}"
                "const text=(alert.innerText||alert.textContent||'').replace(/\\s+/g,' ').trim();"
                "return (/24\\s*(hours|小时)/i.test(text)||/once.{0,20}24/i.test(text))?text:'';"
            )
            or ""
        )

    def _complete_autofilled_authentication(self, driver: Any) -> None:
        completed: set[str] = set()
        while True:
            url = driver.current_url
            if "/ap/signin" in url:
                if "signin" in completed:
                    raise AmazonApiError(
                        409,
                        "ZINIAO_AMAZON_REAUTH_FAILED",
                        "Amazon returned to the Seller Central sign-in page after automatic authentication",
                    )
                completed.add("signin")
                self._complete_autofilled_signin(driver)
                continue
            if "/ap/mfa" in url:
                if "mfa" in completed:
                    raise AmazonApiError(
                        409,
                        "ZINIAO_AMAZON_MFA_FAILED",
                        "Amazon returned to the two-step verification page after automatic authentication",
                    )
                completed.add("mfa")
                self._complete_autofilled_mfa(driver)
                continue
            if any(path in url for path in ("/ap/cvf", "/ap/challenge")):
                raise AmazonApiError(
                    409,
                    "ZINIAO_AMAZON_MFA_REQUIRED",
                    "Amazon requires a CAPTCHA or another manual verification step",
                )
            return

    def _complete_autofilled_signin(self, driver: Any) -> None:
        deadline = time.monotonic() + min(30, self.settings.ziniao_amazon_page_timeout_seconds)
        clicked_account = False
        clicked_password = False
        while time.monotonic() < deadline:
            if "/ap/signin" not in driver.current_url:
                return
            action = str(
                driver.execute_script(
                    "const password=document.querySelector('input[type=password],#ap_password');"
                    "if(!password&&!arguments[0]){"
                    "const accounts=[...document.querySelectorAll('a.cvf-widget-btn-verify-account-switcher')].filter(x=>x.getClientRects().length);"
                    "if(accounts.length===1){accounts[0].click();return 'account';}"
                    "}"
                    "if(password&&!arguments[1]){"
                    "const username=document.querySelector('input[type=email],input[name=email],input[name=username],#ap_email');"
                    "const submit=document.querySelector('#signInSubmit,input[type=submit],button[type=submit]');"
                    "if(password.value&&submit&&(!username||username.value)){submit.click();return 'password';}"
                    "}"
                    "return '';",
                    clicked_account,
                    clicked_password,
                )
                or ""
            )
            if action == "account":
                clicked_account = True
            elif action == "password":
                clicked_password = True
            time.sleep(0.5)
        if not clicked_account and not clicked_password:
            raise AmazonApiError(
                409,
                "ZINIAO_AMAZON_REAUTH_REQUIRED",
                "Amazon requires a fresh Seller Central sign-in and Ziniao did not autofill the password",
            )
        raise AmazonApiError(
            504,
            "ZINIAO_AMAZON_REAUTH_TIMEOUT",
            "Amazon did not finish the autofilled Seller Central sign-in before the timeout",
        )

    def _complete_autofilled_mfa(self, driver: Any) -> None:
        deadline = time.monotonic() + min(30, self.settings.ziniao_amazon_page_timeout_seconds)
        clicked = False
        while time.monotonic() < deadline:
            if "/ap/mfa" not in driver.current_url:
                return
            if not clicked:
                clicked = bool(
                    driver.execute_script(
                        "const otp=document.querySelector('#auth-mfa-otpcode,input[name=otpCode]');"
                        "const submit=document.querySelector('#auth-signin-button,input[name=mfaSubmit],button[type=submit]');"
                        "if(!otp||!otp.value||!submit){return false;}"
                        "submit.click();return true;"
                    )
                )
            time.sleep(0.5)
        if not clicked:
            raise AmazonApiError(
                409,
                "ZINIAO_AMAZON_MFA_REQUIRED",
                "Amazon requires two-step verification and Ziniao did not autofill the OTP",
            )
        raise AmazonApiError(
            504,
            "ZINIAO_AMAZON_MFA_TIMEOUT",
            "Amazon did not finish the autofilled two-step verification before the timeout",
        )

    def _wait_for_result(self, driver: Any, before_url: str, before_text: str) -> dict[str, str]:
        deadline = time.monotonic() + self.settings.ziniao_amazon_page_timeout_seconds
        while time.monotonic() < deadline:
            state = driver.execute_script(
                "const visible=(element)=>Boolean(element&&element.getClientRects().length);"
                "const success=document.querySelector('#disburse-now-submit-success-alert');"
                "const error=document.querySelector('#disburse-now-submit-error-alert');"
                "const ineligible=document.querySelector('#ineligibility-alert-section');"
                "const requestButtons=[...document.querySelectorAll('kat-button')].filter(x=>['请求付款','Request payment'].includes((x.getAttribute('label')||'').trim()));"
                "const ineligibleText=(ineligible?.innerText||ineligible?.textContent||'').trim();"
                "return {"
                "successVisible:visible(success),"
                "errorVisible:visible(error),"
                "intervalLimited:visible(ineligible)&&requestButtons.length>0&&requestButtons.every(x=>x.hasAttribute('disabled'))&&(/24\\s*(hours|小时)/i.test(ineligibleText)||/once.{0,20}24/i.test(ineligibleText)),"
                "intervalMessage:ineligibleText"
                "};"
            )
            if state.get("successVisible"):
                return {"status": "submitted", "message": "Amazon accepted the payment request"}
            if state.get("errorVisible"):
                return {"status": "failed", "message": "Amazon rejected the payment request"}
            if state.get("intervalLimited"):
                raise AmazonApiError(
                    409,
                    "ZINIAO_PAYOUT_INTERVAL_LIMIT",
                    str(state.get("intervalMessage") or "Amazon allows only one on-demand payment request within 24 hours"),
                )
            body = driver.find_element("tag name", "body").text
            if driver.current_url != before_url or body != before_text:
                lowered = body.lower()
                if any(term in lowered for term in ("success", "submitted", "已提交", "成功", "请求已收到")):
                    return {"status": "submitted", "message": "Amazon accepted the payment request"}
                if any(term in lowered for term in ("error", "failed", "无法", "失败")):
                    return {"status": "failed", "message": "Amazon rejected the payment request"}
            time.sleep(0.5)
        raise AmazonApiError(
            504,
            "ZINIAO_PAYOUT_RESULT_UNKNOWN",
            "Amazon did not expose a confirmed result before the timeout; do not retry automatically",
        )

    def _running_store(self, control_type: str, control_id: str) -> dict[str, Any]:
        control_type, control_id = self.ziniao._control(control_type, control_id)
        running_stores = self.ziniao.running_stores()
        for store in running_stores:
            if str(store.get(control_type) or "") == control_id or (
                store.get("controlType") == control_type and store.get("controlId") == control_id
            ):
                return store
        for configured_store in self.ziniao.list_stores():
            if str(configured_store.get(control_type) or "") != control_id and not (
                configured_store.get("controlType") == control_type and configured_store.get("controlId") == control_id
            ):
                continue
            identifiers = {
                str(configured_store.get("browserId") or ""),
                str(configured_store.get("browserOauth") or ""),
            }
            for store in running_stores:
                if identifiers.intersection(
                    {
                        str(store.get("browserId") or ""),
                        str(store.get("browserOauth") or ""),
                    }
                ) - {""}:
                    return store
        raise AmazonApiError(409, "ZINIAO_STORE_NOT_RUNNING", "Start the Ziniao store environment before reading payments")

    def _driver(self, store: dict[str, Any]) -> Any:
        driver_path = self.settings.ziniao_webdriver_path
        if driver_path is None or not driver_path.is_file():
            raise AmazonApiError(503, "ZINIAO_WEBDRIVER_NOT_FOUND", "A matching Ziniao ChromeDriver is required")
        port = store.get("debuggingPort")
        if not port:
            raise AmazonApiError(409, "ZINIAO_DEBUGGING_PORT_MISSING", "The running Ziniao store has no debugging port")
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except ImportError as error:
            raise AmazonApiError(503, "SELENIUM_NOT_INSTALLED", "Install the selenium Python package") from error
        options = Options()
        options.debugger_address = f"127.0.0.1:{int(port)}"
        try:
            return webdriver.Chrome(service=Service(str(driver_path)), options=options)
        except Exception as error:
            raise AmazonApiError(502, "ZINIAO_WEBDRIVER_CONNECT_FAILED", "Could not connect Selenium to the Ziniao store") from error

    def _purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        self._prepared = {token: item for token, item in self._prepared.items() if item.expires_at > now}

    @staticmethod
    def _value_after(lines: list[str], labels: tuple[str, ...]) -> str:
        for index, line in enumerate(lines):
            if any(label.lower() in line.lower() for label in labels):
                if index + 1 < len(lines):
                    return lines[index + 1]
                for label in labels:
                    if label.lower() in line.lower():
                        return line[len(label) :].strip(" :：")
        return ""
