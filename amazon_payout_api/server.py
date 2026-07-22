from __future__ import annotations

from datetime import datetime, timedelta, timezone
import csv
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import io
import json
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .amazon import AmazonApiError, AmazonTransfersClient, SUPPORTED_MARKETPLACES, resolve_marketplace
from .config import Settings
from .store import PayoutStore, PayoutStoreError
from .ziniao import ZiniaoClient
from .ziniao_amazon import ZiniaoAmazonPayout


IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SCHEDULE_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SCHEDULE_PATH_PATTERN = re.compile(r"^/v1/schedules/([A-Za-z]{2})$")
ZINIAO_MARKETPLACES = ("US", "UK", "CA")
SCHEDULABLE_MARKETPLACES = (*ZINIAO_MARKETPLACES, *SUPPORTED_MARKETPLACES)
TRANSFER_SCHEDULE_SPACING_SECONDS = 65
ZINIAO_PAYOUT_INTERVAL = timedelta(hours=24, minutes=10)
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def resolve_scheduled_marketplace(value: str) -> str:
    normalized = value.strip().upper()
    if normalized == "GB":
        normalized = "UK"
    if normalized in ZINIAO_MARKETPLACES:
        return normalized
    code, _ = resolve_marketplace(normalized)
    return code


class PayoutApplication:
    def __init__(
        self,
        settings: Settings,
        client: AmazonTransfersClient | None = None,
        store: PayoutStore | None = None,
        ziniao: ZiniaoClient | None = None,
        ziniao_payout: ZiniaoAmazonPayout | None = None,
    ):
        self.settings = settings
        self.client = client or AmazonTransfersClient(settings)
        self.store = store or PayoutStore(settings.database_path)
        self.ziniao = ziniao or ZiniaoClient(settings)
        self.ziniao_payout = ziniao_payout or ZiniaoAmazonPayout(settings, self.ziniao)
        self.scheduler_running = False
        self.finance_sync_running = False
        self._payout_lock = threading.Lock()
        self._finance_sync_lock = threading.Lock()

    def authorize(self, supplied_key: str) -> bool:
        return bool(supplied_key) and hmac.compare_digest(supplied_key, self.settings.api_key)

    def status(self) -> dict[str, Any]:
        return {
            "service": "ok",
            "mode": self.settings.mode,
            "dryRun": self.settings.dry_run,
            "credentialsComplete": self.settings.credentials_complete,
            "allowProduction": self.settings.allow_production,
            "allowPayoutPost": self.settings.allow_payout_post,
            "allowSandboxPost": self.settings.allow_sandbox_post,
            "autoPayoutMarketplaces": sorted(self.settings.auto_payout_marketplaces),
            "timezone": self.settings.timezone,
            "schedulerEnabled": self.settings.scheduler_enabled,
            "schedulerRunning": self.scheduler_running,
            "financeSyncEnabled": self.settings.finance_sync_enabled,
            "financeSyncRunning": self.finance_sync_running,
            "lastConnectionTest": self.store.get_state("last_connection_test"),
            "lastFinanceSync": self.store.get_state("last_finance_sync"),
            "ziniao": self.ziniao.status(),
            "ziniaoPayoutEnabled": self.settings.allow_ziniao_payout,
        }

    def ziniao_stores(self) -> tuple[int, dict[str, Any]]:
        return 200, {"items": self.ziniao.list_stores()}

    def ziniao_running(self) -> tuple[int, dict[str, Any]]:
        return 200, {"items": self.ziniao.running_stores()}

    def ziniao_start_client(self) -> tuple[int, dict[str, Any]]:
        return 200, self.ziniao.start_client()

    def ziniao_update_core(self) -> tuple[int, dict[str, Any]]:
        return 200, self.ziniao.update_core()

    def ziniao_start_store(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return 200, self.ziniao.start_store(str(payload.get("controlType") or ""), str(payload.get("controlId") or ""))

    def ziniao_stop_store(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        duplicate = payload.get("duplicate", 0)
        if not isinstance(duplicate, int):
            raise AmazonApiError(422, "INVALID_ZINIAO_DUPLICATE", "duplicate must be an integer")
        return 200, self.ziniao.stop_store(
            str(payload.get("controlType") or ""),
            str(payload.get("controlId") or ""),
            duplicate,
        )

    def ziniao_exit_client(self) -> tuple[int, dict[str, Any]]:
        return 200, self.ziniao.exit_client()

    def ziniao_amazon_balance(self, control_type: str, control_id: str, marketplace: str) -> tuple[int, dict[str, Any]]:
        return 200, self.ziniao_payout.balance(control_type, control_id, marketplace)

    def ziniao_amazon_prepare(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        account_index = payload.get("accountIndex", 0)
        if isinstance(account_index, bool) or not isinstance(account_index, int) or account_index < 0:
            raise AmazonApiError(422, "INVALID_ZINIAO_ACCOUNT_INDEX", "accountIndex must be a non-negative integer")
        return 200, self.ziniao_payout.prepare(
            str(payload.get("controlType") or ""),
            str(payload.get("controlId") or ""),
            str(payload.get("marketplace") or ""),
            account_index,
        )

    def ziniao_amazon_submit(self, payload: dict[str, Any], confirmation: str) -> tuple[int, dict[str, Any]]:
        return 200, self.ziniao_payout.submit(
            str(payload.get("controlType") or ""),
            str(payload.get("controlId") or ""),
            str(payload.get("token") or ""),
            confirmation,
        )

    def test_credentials(self, marketplace: str = "DE") -> tuple[int, dict[str, Any]]:
        try:
            result = self.client.test_connection(marketplace)
        except (AmazonApiError, ValueError) as error:
            if isinstance(error, AmazonApiError):
                code, message = error.code, error.message
            else:
                code, message = "CONFIGURATION_ERROR", str(error)
            self.store.set_state("last_connection_test", {"status": "failed", "code": code, "message": message})
            if isinstance(error, AmazonApiError):
                raise
            raise AmazonApiError(500, code, message) from error
        response = {"status": "ok", **result}
        self.store.set_state("last_connection_test", response)
        return 200, response

    def payment_methods(self, marketplace: str, method_types: list[str]) -> tuple[int, dict[str, Any]]:
        return 200, self.client.get_payment_methods(marketplace, method_types or None)

    def payout(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
        confirmation: str,
        trigger_source: str = "manual",
    ) -> tuple[int, dict[str, Any]]:
        with self._payout_lock:
            return self._payout_locked(payload, idempotency_key, confirmation, trigger_source)

    def _payout_locked(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
        confirmation: str,
        trigger_source: str,
    ) -> tuple[int, dict[str, Any]]:
        marketplace = str(payload.get("marketplace") or "")
        account_type = str(payload.get("accountType") or "Standard Orders")
        code, marketplace_id = resolve_marketplace(marketplace)
        if account_type != "Standard Orders":
            raise AmazonApiError(422, "INVALID_ACCOUNT_TYPE", "Supported EU marketplaces only accept 'Standard Orders'")
        if not IDEMPOTENCY_PATTERN.fullmatch(idempotency_key):
            raise AmazonApiError(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be 8-128 safe characters")

        normalized = {"marketplace": code, "marketplaceId": marketplace_id, "accountType": account_type}
        request_hash = self.store.request_hash(normalized)
        preview = {
            "mode": self.settings.mode,
            "dryRun": self.settings.dry_run,
            "marketplace": code,
            "marketplaceId": marketplace_id,
            "accountType": account_type,
            "note": "Amazon chooses the eligible payout amount and default deposit method; the API does not accept an amount.",
        }

        if not self.settings.dry_run:
            safety_error = self._manual_submission_error(code, confirmation)
            if safety_error is not None:
                status = "SKIPPED" if trigger_source == "auto" else "FAILED"
                self.store.record_event(
                    idempotency_key,
                    code,
                    account_type,
                    trigger_source,
                    status,
                    safety_error.status,
                    error_code=safety_error.code,
                    error_message=safety_error.message,
                )
                raise safety_error

        try:
            claim = self.store.claim(
                idempotency_key,
                request_hash,
                code,
                account_type,
                trigger_source,
                enforce_interval=not self.settings.dry_run and self.settings.mode == "production",
            )
        except PayoutStoreError as error:
            status_code = 429 if error.code == "PAYOUT_INTERVAL_LIMIT" else 409
            status = "SKIPPED" if trigger_source == "auto" else "FAILED"
            self.store.record_event(
                idempotency_key,
                code,
                account_type,
                trigger_source,
                status,
                status_code,
                error_code=error.code,
                error_message=error.message,
            )
            raise AmazonApiError(status_code, error.code, error.message, error.details) from error

        if not claim["created"]:
            if claim["request_hash"] != request_hash:
                raise AmazonApiError(409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used for a different request")
            if claim["state"] in {"PENDING", "UNKNOWN"}:
                raise AmazonApiError(
                    409,
                    "IDEMPOTENCY_UNRESOLVED",
                    "The previous request is pending or has an unknown result and cannot be retried automatically",
                )
            response = dict(claim["response"] or {})
            error_payload = response.get("error")
            if isinstance(error_payload, dict):
                raise AmazonApiError(
                    int(claim["status_code"] or 500),
                    str(error_payload.get("code") or "PAYOUT_FAILED"),
                    str(error_payload.get("message") or "The previous payout attempt failed"),
                )
            response["idempotentReplay"] = True
            return int(claim["status_code"] or 200), response

        if self.settings.dry_run:
            response = {"status": "PREVIEW_ONLY", **preview}
            self.store.finish_claim(idempotency_key, "COMPLETED", 200, response)
            return 200, response

        try:
            result = self.client.initiate_payout(code, account_type)
        except AmazonApiError as error:
            response = {"error": {"code": error.code, "message": error.message}, **preview}
            claim_state = "UNKNOWN" if error.status >= 500 else "FAILED"
            run_status = "UNKNOWN" if claim_state == "UNKNOWN" else "FAILED"
            response["status"] = run_status
            self.store.finish_claim(
                idempotency_key,
                claim_state,
                error.status,
                response,
                error_code=error.code,
                error_message=error.message,
            )
            raise

        amazon_data = result["data"] if isinstance(result.get("data"), dict) else {}
        response = {"status": "SUBMITTED", **preview, "amazon": amazon_data}
        self.store.finish_claim(
            idempotency_key,
            "COMPLETED",
            200,
            response,
            amazon_request_id=result.get("requestId"),
            payout_reference_id=amazon_data.get("payoutReferenceId"),
        )
        return 200, response

    def _manual_submission_error(self, code: str, confirmation: str) -> AmazonApiError | None:
        if self.settings.mode == "sandbox":
            if not self.settings.allow_sandbox_post:
                return AmazonApiError(403, "SANDBOX_POST_DISABLED", "Sandbox payout POST is disabled by server safety settings")
        elif not self.settings.allow_production or not self.settings.allow_payout_post:
            return AmazonApiError(403, "PAYOUT_DISABLED", "Production payout is disabled by server safety settings")
        if confirmation != f"CONFIRM:{code}":
            return AmazonApiError(400, "CONFIRMATION_REQUIRED", f"X-Payout-Confirmation must equal CONFIRM:{code}")
        return None

    def auto_payout_blockers(self, marketplace: str) -> list[str]:
        marketplace = resolve_scheduled_marketplace(marketplace)
        blockers: list[str] = []
        if self.settings.mode != "production":
            blockers.append("AUTO_REQUIRES_PRODUCTION")
        if self.settings.dry_run:
            blockers.append("DRY_RUN_ENABLED")
        if not self.settings.allow_production:
            blockers.append("ALLOW_PRODUCTION_DISABLED")
        if not self.settings.allow_payout_post:
            blockers.append("ALLOW_PAYOUT_POST_DISABLED")
        if marketplace in ZINIAO_MARKETPLACES:
            if not self.settings.ziniao_enabled:
                blockers.append("ZINIAO_DISABLED")
            if not self.ziniao.configured:
                blockers.append("ZINIAO_CONFIGURATION_INCOMPLETE")
            if not self.settings.allow_ziniao_payout:
                blockers.append("ALLOW_ZINIAO_PAYOUT_DISABLED")
        elif not self.settings.credentials_complete:
            blockers.append("CREDENTIALS_INCOMPLETE")
        if marketplace not in self.settings.auto_payout_marketplaces:
            blockers.append("MARKETPLACE_NOT_ALLOWLISTED")
        return blockers

    def scheduled_payout(self, marketplace: str, idempotency_key: str) -> tuple[int, dict[str, Any]]:
        code = resolve_scheduled_marketplace(marketplace)
        if code not in ZINIAO_MARKETPLACES:
            return self.payout(
                {"marketplace": code, "accountType": "Standard Orders"},
                idempotency_key,
                f"CONFIRM:{code}",
                trigger_source="auto",
            )
        if self.settings.dry_run:
            response = {
                "status": "PREVIEW_ONLY",
                "marketplace": code,
                "channel": "ZINIAO_SELLER_CENTRAL",
            }
            self.store.record_event(idempotency_key, code, "Standard Orders", "auto", "PREVIEW_ONLY", 200)
            return 200, response
        with self._payout_lock:
            return self._scheduled_ziniao_payout_locked(code, idempotency_key)

    def _scheduled_ziniao_payout_locked(self, marketplace: str, idempotency_key: str) -> tuple[int, dict[str, Any]]:
        try:
            store = self._ensure_ziniao_store_running(self._configured_ziniao_store(marketplace))
            control_type = str(store.get("controlType") or "")
            control_id = str(store.get("controlId") or "")
            balance = self.ziniao_payout.balance(control_type, control_id, marketplace)
        except AmazonApiError as error:
            self.store.record_event(
                f"{idempotency_key}-scan",
                marketplace,
                "All Seller Central Accounts",
                "auto",
                "FAILED",
                error.status,
                error_code=error.code,
                error_message=error.message,
            )
            self.store.mark_schedule_run(idempotency_key, marketplace)
            raise

        accounts = [account for account in balance.get("accounts", []) if account.get("canRequest")]
        items: list[dict[str, Any]] = []
        errors: list[AmazonApiError] = []
        for account in accounts:
            item, error = self._scheduled_ziniao_account_locked(
                marketplace,
                idempotency_key,
                control_type,
                control_id,
                account,
            )
            items.append(item)
            if error is not None:
                errors.append(error)

        self.store.mark_schedule_run(idempotency_key, marketplace)
        statuses = {str(item.get("status") or "") for item in items}
        if "UNKNOWN" in statuses:
            aggregate_status = "UNKNOWN"
        elif "SUBMITTED" in statuses and "FAILED" in statuses:
            aggregate_status = "PARTIAL"
        elif "FAILED" in statuses:
            aggregate_status = "FAILED"
        elif "SUBMITTED" in statuses:
            aggregate_status = "SUBMITTED"
        else:
            aggregate_status = "SKIPPED"

        if "SUBMITTED" in statuses:
            self._reschedule_ziniao_after_success(marketplace)
        if errors and "SUBMITTED" not in statuses:
            raise errors[0]
        return 200, {
            "status": aggregate_status,
            "marketplace": marketplace,
            "channel": "ZINIAO_SELLER_CENTRAL",
            "items": items,
        }

    def _scheduled_ziniao_account_locked(
        self,
        marketplace: str,
        base_idempotency_key: str,
        control_type: str,
        control_id: str,
        account: dict[str, Any],
    ) -> tuple[dict[str, Any], AmazonApiError | None]:
        account_index = int(account["index"])
        account_type = str(account.get("accountType") or f"Account {account_index}")
        idempotency_key = f"{base_idempotency_key}-a{account_index}"
        normalized = {
            "marketplace": marketplace,
            "channel": "ZINIAO_SELLER_CENTRAL",
            "accountIndex": account_index,
            "accountType": account_type,
        }
        request_hash = self.store.request_hash(normalized)
        try:
            claim = self.store.claim(
                idempotency_key,
                request_hash,
                marketplace,
                account_type,
                "auto",
                enforce_interval=True,
            )
        except PayoutStoreError as error:
            status_code = 429 if error.code == "PAYOUT_INTERVAL_LIMIT" else 409
            self.store.record_event(
                idempotency_key,
                marketplace,
                account_type,
                "auto",
                "SKIPPED",
                status_code,
                error_code=error.code,
                error_message=error.message,
            )
            return {
                "status": "SKIPPED",
                "accountIndex": account_index,
                "accountType": account_type,
                "amount": account.get("amount"),
                "error": {"code": error.code, "message": error.message},
            }, None
        if not claim["created"]:
            return {
                "status": "SKIPPED",
                "accountIndex": account_index,
                "accountType": account_type,
                "amount": account.get("amount"),
                "error": {"code": "IDEMPOTENCY_UNRESOLVED", "message": "This account was already processed"},
            }, None

        try:
            prepared = self.ziniao_payout.prepare(control_type, control_id, marketplace, account_index)
            result = self.ziniao_payout.submit(
                control_type,
                control_id,
                str(prepared.get("token") or ""),
                f"CONFIRM:{prepared.get('token') or ''}",
            )
        except AmazonApiError as error:
            run_status = "UNKNOWN" if error.code == "ZINIAO_PAYOUT_RESULT_UNKNOWN" else "FAILED"
            response = {
                "status": run_status,
                "marketplace": marketplace,
                "channel": "ZINIAO_SELLER_CENTRAL",
                "accountIndex": account_index,
                "accountType": account_type,
                "error": {"code": error.code, "message": error.message},
            }
            self.store.finish_claim(
                idempotency_key,
                run_status,
                error.status,
                response,
                error_code=error.code,
                error_message=error.message,
            )
            return response, error

        submitted = str(result.get("status") or "").lower() == "submitted"
        response = {
            "status": "SUBMITTED" if submitted else "FAILED",
            "marketplace": marketplace,
            "channel": "ZINIAO_SELLER_CENTRAL",
            "accountIndex": account_index,
            "accountType": account_type,
            "amount": result.get("amount") or prepared.get("amount"),
            "accountTail": result.get("accountTail") or prepared.get("accountTail"),
            "message": result.get("message"),
        }
        self.store.finish_claim(idempotency_key, "COMPLETED" if submitted else "FAILED", 200, response)
        return response, None

    def _reschedule_ziniao_after_success(self, marketplace: str) -> None:
        local_next = datetime.now(timezone.utc).astimezone(ZoneInfo(self.settings.timezone)) + timedelta(minutes=10)
        if local_next.second or local_next.microsecond:
            local_next = local_next.replace(second=0, microsecond=0) + timedelta(minutes=1)
        self.store.save_schedule(marketplace, True, local_next.strftime("%H:%M"), self.settings.timezone)

    def _configured_ziniao_store(self, marketplace: str) -> dict[str, Any]:
        status = self.ziniao.status()
        if not status.get("serviceReachable"):
            self.ziniao.start_client()
        stores = [store for store in self.ziniao.list_stores() if not store.get("isExpired")]
        labels = {
            "US": ("UNITED STATES", "美国", "US"),
            "UK": ("UNITED KINGDOM", "英国", "UK", "GB"),
            "CA": ("CANADA", "加拿大", "CA"),
        }[marketplace]

        def source(store: dict[str, Any]) -> str:
            return f"{store.get('platformName') or ''} {store.get('browserName') or ''}".upper()

        exact = next(
            (
                store
                for store in stores
                if any(
                    label in source(store) if len(label) > 2 else re.search(rf"(^|\W){label}(\W|$)", source(store))
                    for label in labels
                )
            ),
            None,
        )
        if exact is not None:
            return exact
        shared = next((store for store in stores if "亚马逊" in source(store) or "AMAZON" in source(store)), None)
        if shared is not None:
            return shared
        raise AmazonApiError(409, "ZINIAO_STORE_NOT_CONFIGURED", f"No authorized Ziniao store is available for {marketplace}")

    @staticmethod
    def _ziniao_store_identifiers(store: dict[str, Any]) -> set[str]:
        return {
            str(store.get("browserId") or ""),
            str(store.get("browserOauth") or ""),
            str(store.get("controlId") or ""),
        } - {""}

    def _running_ziniao_store(self, configured_store: dict[str, Any]) -> dict[str, Any] | None:
        identifiers = self._ziniao_store_identifiers(configured_store)
        for running_store in self.ziniao.running_stores():
            if identifiers.intersection(self._ziniao_store_identifiers(running_store)):
                return running_store
        return None

    def _ensure_ziniao_store_running(self, configured_store: dict[str, Any]) -> dict[str, Any]:
        running_store = self._running_ziniao_store(configured_store)
        if running_store is not None and running_store.get("debuggingPort"):
            return running_store
        self.ziniao.start_store(
            str(configured_store.get("controlType") or ""),
            str(configured_store.get("controlId") or ""),
        )
        deadline = time.monotonic() + self.settings.ziniao_start_timeout_seconds
        while time.monotonic() < deadline:
            running_store = self._running_ziniao_store(configured_store)
            if running_store is not None and running_store.get("debuggingPort"):
                return running_store
            time.sleep(0.5)
        raise AmazonApiError(504, "ZINIAO_STORE_START_TIMEOUT", "Ziniao store did not expose a debugging port in time")

    def schedules(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        schedules = []
        for item in self.store.schedules():
            next_run = self._next_run(item, now)
            if item["marketplace"] in ZINIAO_MARKETPLACES:
                last_submitted = self.store.latest_submitted_at(item["marketplace"])
                if last_submitted is not None:
                    eligible_at = last_submitted + ZINIAO_PAYOUT_INTERVAL
                    while next_run < eligible_at:
                        next_run += timedelta(days=1)
            schedules.append({**item, "nextRunAt": next_run.isoformat()})
        return schedules

    def save_schedule(self, marketplace: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        code = resolve_scheduled_marketplace(marketplace)
        run_at = str(payload.get("runAt") or "")
        if not SCHEDULE_TIME_PATTERN.fullmatch(run_at):
            raise AmazonApiError(422, "INVALID_SCHEDULE_TIME", "runAt must use 24-hour HH:MM format")
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AmazonApiError(422, "INVALID_SCHEDULE_ENABLED", "enabled must be a boolean")
        schedule = self.store.save_schedule(code, enabled, run_at, self.settings.timezone)
        schedule["nextRunAt"] = self._next_run(schedule, datetime.now(timezone.utc)).isoformat()
        return 200, schedule

    def delete_schedule(self, marketplace: str) -> tuple[int, dict[str, Any]]:
        code = resolve_scheduled_marketplace(marketplace)
        deleted = self.store.delete_schedule(code)
        return 200, {"marketplace": code, "deleted": deleted}

    def history(self, limit: int, marketplace: str | None) -> tuple[int, dict[str, Any]]:
        code = None
        if marketplace:
            code = resolve_scheduled_marketplace(marketplace)
        return 200, {"items": self.store.history(limit, code)}

    def sync_finance(self, days: int | None = None) -> tuple[int, dict[str, Any]]:
        if self.settings.mode != "production":
            raise AmazonApiError(422, "FINANCE_SYNC_REQUIRES_PRODUCTION", "Financial records can only be synchronized in production mode")
        sync_days = self.settings.finance_sync_days if days is None else max(1, min(int(days), 180))
        with self._finance_sync_lock:
            now = datetime.now(timezone.utc)
            started_after = self._amazon_time(now - timedelta(days=sync_days))
            started_before = self._amazon_time(now - timedelta(minutes=3))
            try:
                result = self.client.list_financial_event_groups(started_after, started_before)
                saved = self.store.upsert_financial_event_groups(result["groups"])
            except (AmazonApiError, ValueError) as error:
                code = error.code if isinstance(error, AmazonApiError) else "CONFIGURATION_ERROR"
                message = error.message if isinstance(error, AmazonApiError) else str(error)
                self.store.set_state("last_finance_sync", {"status": "failed", "code": code, "message": message})
                if isinstance(error, AmazonApiError):
                    raise
                raise AmazonApiError(500, code, message) from error
            response = {
                "status": "ok",
                "days": sync_days,
                "startedAfter": started_after,
                "startedBefore": started_before,
                "received": len(result["groups"]),
                "saved": saved,
                "pageCount": result["pageCount"],
                "truncated": result["truncated"],
            }
            self.store.set_state("last_finance_sync", response)
            return 200, {**response, "summary": self.store.financial_summary(started_after=started_after)}

    def finance_records(
        self,
        days: int,
        limit: int,
        currency: str | None,
        transfer_status: str | None,
    ) -> tuple[int, dict[str, Any]]:
        started_after = self._amazon_time(datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 180))))
        return 200, {
            "startedAfter": started_after,
            "items": self.store.financial_records(
                limit,
                started_after=started_after,
                currency=currency,
                transfer_status=transfer_status,
            ),
        }

    def finance_summary(self, days: int) -> tuple[int, dict[str, Any]]:
        started_after = self._amazon_time(datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 180))))
        return 200, {
            "startedAfter": started_after,
            "lastSync": self.store.get_state("last_finance_sync"),
            **self.store.financial_summary(started_after=started_after),
        }

    def finance_csv(self, days: int) -> bytes:
        records = self.finance_records(days, 1000, None, None)[1]["items"]
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "financial_event_group_id",
                "marketplace",
                "processing_status",
                "transfer_status",
                "original_currency",
                "original_amount",
                "converted_currency",
                "converted_amount",
                "beginning_currency",
                "beginning_balance",
                "fund_transfer_date",
                "group_start",
                "group_end",
                "account_tail",
                "trace_id",
            ]
        )
        for item in records:
            writer.writerow(
                [
                    self._csv_value(item.get("groupId")),
                    item.get("marketplace") or "",
                    item.get("processingStatus") or "",
                    item.get("transferStatus") or "",
                    (item.get("originalAmount") or {}).get("currency") or "",
                    (item.get("originalAmount") or {}).get("value") or "",
                    (item.get("convertedAmount") or {}).get("currency") or "",
                    (item.get("convertedAmount") or {}).get("value") or "",
                    (item.get("beginningBalance") or {}).get("currency") or "",
                    (item.get("beginningBalance") or {}).get("value") or "",
                    item.get("fundTransferDate") or "",
                    item.get("groupStart") or "",
                    item.get("groupEnd") or "",
                    self._csv_value(item.get("accountTail")),
                    self._csv_value(item.get("traceId")),
                ]
            )
        return ("\ufeff" + output.getvalue()).encode("utf-8")

    @staticmethod
    def _csv_value(value: Any) -> str:
        text = str(value or "")
        return "'" + text if text.startswith(("=", "+", "-", "@")) else text

    @staticmethod
    def _amazon_time(value: datetime) -> str:
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _next_run(schedule: dict[str, Any], now_utc: datetime) -> datetime:
        local_zone = ZoneInfo(str(schedule["timezone"]))
        local_now = now_utc.astimezone(local_zone)
        hour, minute = (int(value) for value in str(schedule["runAt"]).split(":"))
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)


class ScheduleRunner(threading.Thread):
    def __init__(self, app: PayoutApplication):
        super().__init__(name="payout-scheduler", daemon=True)
        self.app = app
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self.app.scheduler_running = True
        try:
            while not self._stop_event.is_set():
                self.run_due()
                self._stop_event.wait(self.app.settings.scheduler_poll_seconds)
        finally:
            self.app.scheduler_running = False

    def run_due(self, now_utc: datetime | None = None) -> None:
        now_utc = now_utc or datetime.now(timezone.utc)
        rank = {marketplace: index for index, marketplace in enumerate(SCHEDULABLE_MARKETPLACES)}
        schedules = sorted(self.app.store.schedules(), key=lambda item: rank.get(item["marketplace"], len(rank)))
        due_schedules = []
        for schedule in schedules:
            if not schedule["enabled"]:
                continue
            zone = ZoneInfo(str(schedule["timezone"]))
            local_now = now_utc.astimezone(zone)
            hour, minute = (int(value) for value in str(schedule["runAt"]).split(":"))
            scheduled_today = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if local_now < scheduled_today:
                continue
            updated_at = datetime.fromisoformat(str(schedule["updatedAt"])).astimezone(zone)
            if updated_at > scheduled_today:
                continue
            marketplace = schedule["marketplace"]
            if marketplace in ZINIAO_MARKETPLACES:
                last_submitted = self.app.store.latest_submitted_at(marketplace)
                if last_submitted is not None and now_utc < last_submitted + ZINIAO_PAYOUT_INTERVAL:
                    continue
                key = f"auto-{marketplace}-{local_now.strftime('%Y%m%d')}-{schedule['runAt'].replace(':', '')}"
            else:
                key = f"auto-{marketplace}-{local_now.strftime('%Y%m%d')}"
            if self.app.store.has_run(key):
                continue
            due_schedules.append((marketplace, key))

        for index, (marketplace, key) in enumerate(due_schedules):
            if not self.app.settings.dry_run:
                blockers = self.app.auto_payout_blockers(marketplace)
                if blockers:
                    self.app.store.record_event(
                        key,
                        marketplace,
                        "Standard Orders",
                        "auto",
                        "SKIPPED",
                        403,
                        error_code="AUTO_PAYOUT_BLOCKED",
                        error_message=", ".join(blockers),
                    )
                    continue
            try:
                self.app.scheduled_payout(marketplace, key)
            except (AmazonApiError, ValueError) as error:
                print(f"Scheduled payout {key} did not complete: {error}")
            if (
                not self.app.settings.dry_run
                and marketplace in SUPPORTED_MARKETPLACES
                and any(item[0] in SUPPORTED_MARKETPLACES for item in due_schedules[index + 1 :])
            ):
                self._stop_event.wait(TRANSFER_SCHEDULE_SPACING_SECONDS)


class FinanceSyncRunner(threading.Thread):
    def __init__(self, app: PayoutApplication):
        super().__init__(name="finance-sync", daemon=True)
        self.app = app
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self.app.finance_sync_running = True
        try:
            while not self._stop_event.is_set():
                try:
                    self.app.sync_finance()
                except (AmazonApiError, ValueError) as error:
                    print(f"Financial records sync did not complete: {error}")
                self._stop_event.wait(self.app.settings.finance_sync_interval_seconds)
        finally:
            self.app.finance_sync_running = False


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AmazonPayoutAPI/1.1"

    @property
    def app(self) -> PayoutApplication:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'")

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._json(404, {"error": {"code": "NOT_FOUND", "message": "Asset not found"}})
            return
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _download(self, raw: bytes, filename: str, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(raw)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, error: AmazonApiError) -> None:
        self._json(error.status, {"error": {"code": error.code, "message": error.message, "details": error.details}})

    def _require_auth(self) -> bool:
        peer_host = self.client_address[0]
        request_host = self.headers.get("Host", "")
        try:
            peer_is_loopback = ipaddress.ip_address(peer_host).is_loopback
            parsed_host = urlparse(f"//{request_host}").hostname or ""
            host_is_loopback = parsed_host.lower() == "localhost" or ipaddress.ip_address(parsed_host).is_loopback
        except ValueError:
            peer_is_loopback = False
            host_is_loopback = False
        if peer_is_loopback and host_is_loopback:
            return True
        if self.app.authorize(self.headers.get("X-API-Key", "")):
            return True
        self._json(401, {"error": {"code": "UNAUTHORIZED", "message": "Valid X-API-Key required"}})
        return False

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise AmazonApiError(400, "INVALID_CONTENT_LENGTH", "Invalid Content-Length") from error
        if length <= 0 or length > 16_384:
            raise AmazonApiError(400, "INVALID_BODY_SIZE", "JSON body is required and must be at most 16 KiB")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AmazonApiError(400, "INVALID_JSON", "Request body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise AmazonApiError(400, "INVALID_JSON", "Request body must be a JSON object")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/assets/styles.css":
            self._file(WEB_ROOT / "styles.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/assets/app.js":
            self._file(WEB_ROOT / "app.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/health":
            self._json(200, {"status": "ok", "mode": self.app.settings.mode, "dryRun": self.app.settings.dry_run})
            return
        if not self._require_auth():
            return
        try:
            if parsed.path == "/v1/status":
                self._json(200, self.app.status())
                return
            if parsed.path == "/v1/marketplaces":
                self._json(200, {"supported": SUPPORTED_MARKETPLACES, "unsupportedNote": "UK/GB, US and CA are not supported by Amazon Transfers API."})
                return
            if parsed.path == "/v1/ziniao/status":
                self._json(200, self.app.ziniao.status())
                return
            if parsed.path == "/v1/ziniao/stores":
                status, response = self.app.ziniao_stores()
                self._json(status, response)
                return
            if parsed.path == "/v1/ziniao/running":
                status, response = self.app.ziniao_running()
                self._json(status, response)
                return
            if parsed.path == "/v1/ziniao/amazon/balance":
                query = parse_qs(parsed.query)
                status, response = self.app.ziniao_amazon_balance(
                    (query.get("controlType") or [""])[0],
                    (query.get("controlId") or [""])[0],
                    (query.get("marketplace") or [""])[0],
                )
                self._json(status, response)
                return
            if parsed.path == "/v1/payment-methods":
                query = parse_qs(parsed.query)
                marketplace = (query.get("marketplace") or [""])[0]
                method_types = [item for value in query.get("type", []) for item in value.split(",") if item]
                status, response = self.app.payment_methods(marketplace, method_types)
                self._json(status, response)
                return
            if parsed.path == "/v1/schedules":
                self._json(200, {"items": self.app.schedules()})
                return
            if parsed.path == "/v1/payouts/history":
                query = parse_qs(parsed.query)
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError as error:
                    raise AmazonApiError(422, "INVALID_LIMIT", "limit must be an integer") from error
                marketplace = (query.get("marketplace") or [None])[0]
                status, response = self.app.history(limit, marketplace)
                self._json(status, response)
                return
            if parsed.path in {"/v1/finance/records", "/v1/finance/summary", "/v1/finance/export.csv"}:
                query = parse_qs(parsed.query)
                try:
                    days = int((query.get("days") or ["180"])[0])
                    limit = int((query.get("limit") or ["200"])[0])
                except ValueError as error:
                    raise AmazonApiError(422, "INVALID_FINANCE_FILTER", "days and limit must be integers") from error
                if parsed.path == "/v1/finance/records":
                    currency = (query.get("currency") or [None])[0]
                    transfer_status = (query.get("status") or [None])[0]
                    status, response = self.app.finance_records(days, limit, currency, transfer_status)
                    self._json(status, response)
                    return
                if parsed.path == "/v1/finance/summary":
                    status, response = self.app.finance_summary(days)
                    self._json(status, response)
                    return
                filename = f"amazon-payout-ledger-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
                self._download(self.app.finance_csv(days), filename, "text/csv; charset=utf-8")
                return
            self._json(404, {"error": {"code": "NOT_FOUND", "message": "Route not found"}})
        except AmazonApiError as error:
            self._error(error)
        except ValueError as error:
            self._error(AmazonApiError(500, "CONFIGURATION_ERROR", str(error)))

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        try:
            path = urlparse(self.path).path
            payload = self._read_body()
            if path == "/v1/credentials/test":
                status, response = self.app.test_credentials(str(payload.get("marketplace") or "DE"))
                self._json(status, response)
                return
            if path == "/v1/payouts":
                status, response = self.app.payout(
                    payload,
                    self.headers.get("Idempotency-Key", ""),
                    self.headers.get("X-Payout-Confirmation", ""),
                )
                self._json(status, response)
                return
            if path == "/v1/finance/sync":
                raw_days = payload.get("days")
                if raw_days is not None and not isinstance(raw_days, int):
                    raise AmazonApiError(422, "INVALID_FINANCE_DAYS", "days must be an integer")
                status, response = self.app.sync_finance(raw_days)
                self._json(status, response)
                return
            if path == "/v1/ziniao/client/start":
                status, response = self.app.ziniao_start_client()
                self._json(status, response)
                return
            if path == "/v1/ziniao/client/exit":
                status, response = self.app.ziniao_exit_client()
                self._json(status, response)
                return
            if path == "/v1/ziniao/core/update":
                status, response = self.app.ziniao_update_core()
                self._json(status, response)
                return
            if path == "/v1/ziniao/stores/start":
                status, response = self.app.ziniao_start_store(payload)
                self._json(status, response)
                return
            if path == "/v1/ziniao/stores/stop":
                status, response = self.app.ziniao_stop_store(payload)
                self._json(status, response)
                return
            if path == "/v1/ziniao/amazon/prepare":
                status, response = self.app.ziniao_amazon_prepare(payload)
                self._json(status, response)
                return
            if path == "/v1/ziniao/amazon/submit":
                status, response = self.app.ziniao_amazon_submit(
                    payload,
                    self.headers.get("X-Ziniao-Payout-Confirmation", ""),
                )
                self._json(status, response)
                return
            self._json(404, {"error": {"code": "NOT_FOUND", "message": "Route not found"}})
        except AmazonApiError as error:
            self._error(error)
        except ValueError as error:
            self._error(AmazonApiError(500, "CONFIGURATION_ERROR", str(error)))

    def do_PUT(self) -> None:
        if not self._require_auth():
            return
        try:
            match = SCHEDULE_PATH_PATTERN.fullmatch(urlparse(self.path).path)
            if match is None:
                self._json(404, {"error": {"code": "NOT_FOUND", "message": "Route not found"}})
                return
            status, response = self.app.save_schedule(match.group(1), self._read_body())
            self._json(status, response)
        except AmazonApiError as error:
            self._error(error)
        except ValueError as error:
            self._error(AmazonApiError(500, "CONFIGURATION_ERROR", str(error)))

    def do_DELETE(self) -> None:
        if not self._require_auth():
            return
        try:
            match = SCHEDULE_PATH_PATTERN.fullmatch(urlparse(self.path).path)
            if match is None:
                self._json(404, {"error": {"code": "NOT_FOUND", "message": "Route not found"}})
                return
            status, response = self.app.delete_schedule(match.group(1))
            self._json(status, response)
        except AmazonApiError as error:
            self._error(error)


def run() -> None:
    settings = Settings.from_env()
    settings.validate_for_server()
    app = PayoutApplication(settings)
    scheduler = ScheduleRunner(app) if settings.scheduler_enabled else None
    finance_sync = FinanceSyncRunner(app) if settings.finance_sync_enabled and settings.mode == "production" else None
    if scheduler is not None:
        scheduler.start()
    if finance_sync is not None:
        finance_sync.start()
    server = ThreadingHTTPServer((settings.host, settings.port), ApiHandler)
    server.app = app  # type: ignore[attr-defined]
    print(f"Amazon payout console listening on http://{settings.host}:{settings.port} (mode={settings.mode}, dry_run={settings.dry_run})")
    try:
        server.serve_forever()
    finally:
        if scheduler is not None:
            scheduler.stop()
            scheduler.join(timeout=5)
        if finance_sync is not None:
            finance_sync.stop()
            finance_sync.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    run()
