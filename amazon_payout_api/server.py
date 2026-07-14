from __future__ import annotations

from datetime import datetime, timedelta, timezone
import csv
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .amazon import AmazonApiError, AmazonTransfersClient, SUPPORTED_MARKETPLACES, resolve_marketplace
from .config import Settings
from .store import PayoutStore, PayoutStoreError


IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SCHEDULE_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SCHEDULE_PATH_PATTERN = re.compile(r"^/v1/schedules/([A-Za-z]{2})$")
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


class PayoutApplication:
    def __init__(self, settings: Settings, client: AmazonTransfersClient | None = None, store: PayoutStore | None = None):
        self.settings = settings
        self.client = client or AmazonTransfersClient(settings)
        self.store = store or PayoutStore(settings.database_path)
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
        }

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
        blockers: list[str] = []
        if self.settings.mode != "production":
            blockers.append("AUTO_REQUIRES_PRODUCTION")
        if self.settings.dry_run:
            blockers.append("DRY_RUN_ENABLED")
        if not self.settings.credentials_complete:
            blockers.append("CREDENTIALS_INCOMPLETE")
        if not self.settings.allow_production:
            blockers.append("ALLOW_PRODUCTION_DISABLED")
        if not self.settings.allow_payout_post:
            blockers.append("ALLOW_PAYOUT_POST_DISABLED")
        if marketplace not in self.settings.auto_payout_marketplaces:
            blockers.append("MARKETPLACE_NOT_ALLOWLISTED")
        return blockers

    def schedules(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        return [{**item, "nextRunAt": self._next_run(item, now).isoformat()} for item in self.store.schedules()]

    def save_schedule(self, marketplace: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        code, _ = resolve_marketplace(marketplace)
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
        code, _ = resolve_marketplace(marketplace)
        deleted = self.store.delete_schedule(code)
        return 200, {"marketplace": code, "deleted": deleted}

    def history(self, limit: int, marketplace: str | None) -> tuple[int, dict[str, Any]]:
        code = None
        if marketplace:
            code, _ = resolve_marketplace(marketplace)
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
        for schedule in self.app.store.schedules():
            if not schedule["enabled"]:
                continue
            zone = ZoneInfo(str(schedule["timezone"]))
            local_now = now_utc.astimezone(zone)
            if local_now.strftime("%H:%M") < schedule["runAt"]:
                continue
            marketplace = schedule["marketplace"]
            key = f"auto-{marketplace}-{local_now.strftime('%Y%m%d')}"
            if self.app.store.has_run(key):
                continue
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
                self.app.payout(
                    {"marketplace": marketplace, "accountType": "Standard Orders"},
                    key,
                    f"CONFIRM:{marketplace}",
                    trigger_source="auto",
                )
            except (AmazonApiError, ValueError) as error:
                print(f"Scheduled payout {key} did not complete: {error}")


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
