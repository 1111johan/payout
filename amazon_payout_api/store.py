from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any


class PayoutStoreError(RuntimeError):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class PayoutStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initialize()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS payout_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    marketplace TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    status_code INTEGER NOT NULL,
                    response_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS payout_claims (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    marketplace TEXT NOT NULL,
                    account_type TEXT NOT NULL,
                    trigger_source TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    status_code INTEGER,
                    response_json TEXT
                );

                CREATE TABLE IF NOT EXISTS payout_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    marketplace TEXT NOT NULL,
                    account_type TEXT NOT NULL,
                    trigger_source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    status_code INTEGER,
                    amazon_request_id TEXT,
                    payout_reference_id TEXT,
                    error_code TEXT,
                    error_message TEXT
                );

                CREATE INDEX IF NOT EXISTS payout_runs_marketplace_time
                ON payout_runs (marketplace, completed_at DESC);

                CREATE TABLE IF NOT EXISTS schedules (
                    marketplace TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    run_at TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schedule_run_markers (
                    idempotency_key TEXT PRIMARY KEY,
                    marketplace TEXT NOT NULL,
                    completed_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS financial_event_groups (
                    group_id TEXT PRIMARY KEY,
                    marketplace TEXT,
                    processing_status TEXT,
                    transfer_status TEXT,
                    original_currency TEXT,
                    original_amount TEXT,
                    converted_currency TEXT,
                    converted_amount TEXT,
                    beginning_currency TEXT,
                    beginning_amount TEXT,
                    fund_transfer_date TEXT,
                    trace_id TEXT,
                    account_tail TEXT,
                    group_start TEXT,
                    group_end TEXT,
                    first_seen_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS financial_event_groups_date
                ON financial_event_groups (fund_transfer_date DESC, group_start DESC);

                CREATE INDEX IF NOT EXISTS financial_event_groups_currency
                ON financial_event_groups (original_currency, transfer_status);
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO payout_claims
                (idempotency_key, request_hash, marketplace, account_type, trigger_source,
                 state, created_at, updated_at, status_code, response_json)
                SELECT idempotency_key, request_hash, marketplace, 'Standard Orders', 'manual',
                       'COMPLETED', created_at, created_at, status_code, response_json
                FROM payout_requests
                """
            )
            legacy_rows = connection.execute(
                """
                SELECT idempotency_key, marketplace, created_at, status_code, response_json
                FROM payout_requests
                """
            ).fetchall()
            for row in legacy_rows:
                response = json.loads(row["response_json"])
                connection.execute(
                    """
                    INSERT OR IGNORE INTO payout_runs
                    (idempotency_key, marketplace, account_type, trigger_source, status,
                     started_at, completed_at, status_code, payout_reference_id)
                    VALUES (?, ?, 'Standard Orders', 'manual', ?, ?, ?, ?, ?)
                    """,
                    (
                        row["idempotency_key"],
                        row["marketplace"],
                        str(response.get("status") or "COMPLETED"),
                        row["created_at"],
                        row["created_at"],
                        row["status_code"],
                        (response.get("amazon") or {}).get("payoutReferenceId") if isinstance(response.get("amazon"), dict) else None,
                    ),
                )
            stale_before = int(time.time()) - 300
            connection.execute(
                "UPDATE payout_claims SET state = 'UNKNOWN', updated_at = ? WHERE state = 'PENDING' AND updated_at < ?",
                (int(time.time()), stale_before),
            )
            connection.execute(
                "UPDATE payout_runs SET status = 'UNKNOWN', completed_at = ? WHERE status = 'PENDING' AND started_at < ?",
                (int(time.time()), stale_before),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def request_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def claim(
        self,
        key: str,
        request_hash: str,
        marketplace: str,
        account_type: str,
        trigger_source: str,
        enforce_interval: bool,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM payout_claims WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                return self._claim_payload(existing, created=False)

            if enforce_interval:
                unresolved = connection.execute(
                    """
                    SELECT idempotency_key, state FROM payout_claims
                    WHERE marketplace = ? AND account_type = ? AND state IN ('PENDING', 'UNKNOWN')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (marketplace, account_type),
                ).fetchone()
                if unresolved is not None:
                    raise PayoutStoreError(
                        "PAYOUT_STATE_UNRESOLVED",
                        "A previous payout is pending or has an unknown result and must be checked manually",
                        {"idempotencyKey": unresolved["idempotency_key"], "state": unresolved["state"]},
                    )
                last_submitted = connection.execute(
                    """
                    SELECT completed_at FROM payout_runs
                    WHERE marketplace = ? AND account_type = ? AND status = 'SUBMITTED'
                    ORDER BY completed_at DESC LIMIT 1
                    """,
                    (marketplace, account_type),
                ).fetchone()
                if last_submitted is not None and now - int(last_submitted["completed_at"]) < 86_400:
                    remaining = 86_400 - (now - int(last_submitted["completed_at"]))
                    raise PayoutStoreError(
                        "PAYOUT_INTERVAL_LIMIT",
                        "A successful payout for this marketplace and account type is less than 24 hours old",
                        {"retryAfterSeconds": remaining},
                    )

            connection.execute(
                """
                INSERT INTO payout_claims
                (idempotency_key, request_hash, marketplace, account_type, trigger_source, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (key, request_hash, marketplace, account_type, trigger_source, now, now),
            )
            connection.execute(
                """
                INSERT INTO payout_runs
                (idempotency_key, marketplace, account_type, trigger_source, status, started_at)
                VALUES (?, ?, ?, ?, 'PENDING', ?)
                """,
                (key, marketplace, account_type, trigger_source, now),
            )
        return {
            "created": True,
            "request_hash": request_hash,
            "state": "PENDING",
            "status_code": None,
            "response": None,
        }

    @staticmethod
    def _claim_payload(row: sqlite3.Row, created: bool) -> dict[str, Any]:
        return {
            "created": created,
            "request_hash": row["request_hash"],
            "state": row["state"],
            "status_code": row["status_code"],
            "response": json.loads(row["response_json"]) if row["response_json"] else None,
        }

    def finish_claim(
        self,
        key: str,
        state: str,
        status_code: int,
        response: dict[str, Any],
        *,
        amazon_request_id: str | None = None,
        payout_reference_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = int(time.time())
        response_json = json.dumps(response, ensure_ascii=False)
        run_status = str(response.get("status") or state)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE payout_claims
                SET state = ?, updated_at = ?, status_code = ?, response_json = ?
                WHERE idempotency_key = ?
                """,
                (state, now, status_code, response_json, key),
            )
            connection.execute(
                """
                UPDATE payout_runs
                SET status = ?, completed_at = ?, status_code = ?, amazon_request_id = ?,
                    payout_reference_id = ?, error_code = ?, error_message = ?
                WHERE idempotency_key = ?
                """,
                (
                    run_status,
                    now,
                    status_code,
                    amazon_request_id,
                    payout_reference_id,
                    error_code,
                    (error_message or "")[:500] or None,
                    key,
                ),
            )
            if state == "COMPLETED":
                claim = connection.execute(
                    "SELECT request_hash, marketplace, created_at FROM payout_claims WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()
                if claim is not None:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO payout_requests
                        (idempotency_key, request_hash, marketplace, created_at, status_code, response_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (key, claim["request_hash"], claim["marketplace"], claim["created_at"], status_code, response_json),
                    )

    def record_event(
        self,
        key: str,
        marketplace: str,
        account_type: str,
        trigger_source: str,
        status: str,
        status_code: int,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        now = int(time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO payout_runs
                (idempotency_key, marketplace, account_type, trigger_source, status, started_at,
                 completed_at, status_code, error_code, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    marketplace,
                    account_type,
                    trigger_source,
                    status,
                    now,
                    now,
                    status_code,
                    error_code,
                    (error_message or "")[:500] or None,
                ),
            )
            return cursor.rowcount > 0

    def has_run(self, key: str) -> bool:
        with self._connect() as connection:
            payout_run = connection.execute(
                "SELECT 1 FROM payout_runs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if payout_run is not None:
                return True
            return connection.execute(
                "SELECT 1 FROM schedule_run_markers WHERE idempotency_key = ?",
                (key,),
            ).fetchone() is not None

    def mark_schedule_run(self, key: str, marketplace: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO schedule_run_markers
                (idempotency_key, marketplace, completed_at)
                VALUES (?, ?, ?)
                """,
                (key, marketplace, int(time.time())),
            )

    def last_submitted_at(self, marketplace: str, account_type: str = "Standard Orders") -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT completed_at FROM payout_runs
                WHERE marketplace = ? AND account_type = ? AND status = 'SUBMITTED'
                ORDER BY completed_at DESC LIMIT 1
                """,
                (marketplace, account_type),
            ).fetchone()
        if row is None or row["completed_at"] is None:
            return None
        return datetime.fromtimestamp(int(row["completed_at"]), timezone.utc)

    def latest_submitted_at(self, marketplace: str) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT completed_at FROM payout_runs
                WHERE marketplace = ? AND status = 'SUBMITTED'
                ORDER BY completed_at DESC LIMIT 1
                """,
                (marketplace,),
            ).fetchone()
        if row is None or row["completed_at"] is None:
            return None
        return datetime.fromtimestamp(int(row["completed_at"]), timezone.utc)

    def history(self, limit: int = 100, marketplace: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        query = """
            SELECT payout_runs.idempotency_key, payout_runs.marketplace, payout_runs.account_type,
                   payout_runs.trigger_source, payout_runs.status, payout_runs.started_at,
                   payout_runs.completed_at, payout_runs.status_code, payout_runs.amazon_request_id,
                   payout_runs.payout_reference_id, payout_runs.error_code, payout_runs.error_message,
                   financial_event_groups.original_currency AS ledger_original_currency,
                   financial_event_groups.original_amount AS ledger_original_amount,
                   financial_event_groups.converted_currency AS ledger_converted_currency,
                   financial_event_groups.converted_amount AS ledger_converted_amount,
                   financial_event_groups.transfer_status AS ledger_transfer_status
            FROM payout_runs
            LEFT JOIN financial_event_groups
              ON financial_event_groups.group_id = payout_runs.payout_reference_id
        """
        params: list[Any] = []
        if marketplace:
            query += " WHERE payout_runs.marketplace = ?"
            params.append(marketplace)
        query += " ORDER BY started_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._history_payload(row) for row in rows]

    @staticmethod
    def _history_payload(row: sqlite3.Row) -> dict[str, Any]:
        def iso(value: int | None) -> str | None:
            if value is None:
                return None
            return datetime.fromtimestamp(value, timezone.utc).isoformat()

        return {
            "idempotencyKey": row["idempotency_key"],
            "marketplace": row["marketplace"],
            "accountType": row["account_type"],
            "trigger": row["trigger_source"],
            "status": row["status"],
            "startedAt": iso(row["started_at"]),
            "completedAt": iso(row["completed_at"]),
            "statusCode": row["status_code"],
            "amazonRequestId": row["amazon_request_id"],
            "payoutReferenceId": row["payout_reference_id"],
            "errorCode": row["error_code"],
            "errorMessage": row["error_message"],
            "amount": {
                "currency": row["ledger_original_currency"],
                "value": row["ledger_original_amount"],
            } if row["ledger_original_currency"] else None,
            "convertedAmount": {
                "currency": row["ledger_converted_currency"],
                "value": row["ledger_converted_amount"],
            } if row["ledger_converted_currency"] else None,
            "transferStatus": row["ledger_transfer_status"],
        }

    def schedules(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT marketplace, enabled, run_at, timezone, updated_at FROM schedules ORDER BY marketplace"
            ).fetchall()
        return [
            {
                "marketplace": row["marketplace"],
                "enabled": bool(row["enabled"]),
                "runAt": row["run_at"],
                "timezone": row["timezone"],
                "updatedAt": datetime.fromtimestamp(row["updated_at"], timezone.utc).isoformat(),
            }
            for row in rows
        ]

    def save_schedule(self, marketplace: str, enabled: bool, run_at: str, timezone_name: str) -> dict[str, Any]:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO schedules (marketplace, enabled, run_at, timezone, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(marketplace) DO UPDATE SET
                    enabled = excluded.enabled,
                    run_at = excluded.run_at,
                    timezone = excluded.timezone,
                    updated_at = excluded.updated_at
                """,
                (marketplace, int(enabled), run_at, timezone_name, now),
            )
        return {
            "marketplace": marketplace,
            "enabled": enabled,
            "runAt": run_at,
            "timezone": timezone_name,
            "updatedAt": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        }

    def delete_schedule(self, marketplace: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM schedules WHERE marketplace = ?", (marketplace,))
            return cursor.rowcount > 0

    def set_state(self, key: str, value: dict[str, Any]) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO app_state (key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), now),
            )

    def get_state(self, key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value_json, updated_at FROM app_state WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["value_json"])
        value["updatedAt"] = datetime.fromtimestamp(row["updated_at"], timezone.utc).isoformat()
        return value

    @staticmethod
    def _amount(value: Any) -> str | None:
        if value is None or value == "":
            return None
        try:
            return format(Decimal(str(value)), "f")
        except InvalidOperation:
            return None

    def upsert_financial_event_groups(self, groups: list[dict[str, Any]]) -> int:
        now = int(time.time())
        saved = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            links = {
                row["payout_reference_id"]: row["marketplace"]
                for row in connection.execute(
                    "SELECT payout_reference_id, marketplace FROM payout_runs WHERE payout_reference_id IS NOT NULL"
                ).fetchall()
            }
            for group in groups:
                group_id = str(group.get("FinancialEventGroupId") or "")
                if not group_id:
                    continue
                original = group.get("OriginalTotal") if isinstance(group.get("OriginalTotal"), dict) else {}
                converted = group.get("ConvertedTotal") if isinstance(group.get("ConvertedTotal"), dict) else {}
                beginning = group.get("BeginningBalance") if isinstance(group.get("BeginningBalance"), dict) else {}
                connection.execute(
                    """
                    INSERT INTO financial_event_groups
                    (group_id, marketplace, processing_status, transfer_status,
                     original_currency, original_amount, converted_currency, converted_amount,
                     beginning_currency, beginning_amount, fund_transfer_date, trace_id,
                     account_tail, group_start, group_end, first_seen_at, updated_at, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET
                        marketplace = COALESCE(financial_event_groups.marketplace, excluded.marketplace),
                        processing_status = excluded.processing_status,
                        transfer_status = excluded.transfer_status,
                        original_currency = excluded.original_currency,
                        original_amount = excluded.original_amount,
                        converted_currency = excluded.converted_currency,
                        converted_amount = excluded.converted_amount,
                        beginning_currency = excluded.beginning_currency,
                        beginning_amount = excluded.beginning_amount,
                        fund_transfer_date = excluded.fund_transfer_date,
                        trace_id = excluded.trace_id,
                        account_tail = excluded.account_tail,
                        group_start = excluded.group_start,
                        group_end = excluded.group_end,
                        updated_at = excluded.updated_at,
                        raw_json = excluded.raw_json
                    """,
                    (
                        group_id,
                        links.get(group_id),
                        group.get("ProcessingStatus"),
                        group.get("FundTransferStatus"),
                        original.get("CurrencyCode"),
                        self._amount(original.get("CurrencyAmount")),
                        converted.get("CurrencyCode"),
                        self._amount(converted.get("CurrencyAmount")),
                        beginning.get("CurrencyCode"),
                        self._amount(beginning.get("CurrencyAmount")),
                        group.get("FundTransferDate"),
                        group.get("TraceId"),
                        group.get("AccountTail"),
                        group.get("FinancialEventGroupStart"),
                        group.get("FinancialEventGroupEnd"),
                        now,
                        now,
                        json.dumps(group, ensure_ascii=False),
                    ),
                )
                saved += 1
        return saved

    def financial_records(
        self,
        limit: int = 200,
        *,
        started_after: str | None = None,
        currency: str | None = None,
        transfer_status: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        where: list[str] = []
        params: list[Any] = []
        if started_after:
            where.append("COALESCE(fund_transfer_date, group_start, '') >= ?")
            params.append(started_after)
        if currency:
            where.append("original_currency = ?")
            params.append(currency.upper())
        if transfer_status:
            where.append("transfer_status = ?")
            params.append(transfer_status)
        query = "SELECT * FROM financial_event_groups"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY COALESCE(fund_transfer_date, group_start) DESC, group_id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._financial_payload(row) for row in rows]

    @staticmethod
    def _financial_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "groupId": row["group_id"],
            "marketplace": row["marketplace"],
            "processingStatus": row["processing_status"],
            "transferStatus": row["transfer_status"],
            "originalAmount": {"currency": row["original_currency"], "value": row["original_amount"]} if row["original_currency"] else None,
            "convertedAmount": {"currency": row["converted_currency"], "value": row["converted_amount"]} if row["converted_currency"] else None,
            "beginningBalance": {"currency": row["beginning_currency"], "value": row["beginning_amount"]} if row["beginning_currency"] else None,
            "fundTransferDate": row["fund_transfer_date"],
            "traceId": row["trace_id"],
            "accountTail": row["account_tail"],
            "groupStart": row["group_start"],
            "groupEnd": row["group_end"],
            "updatedAt": datetime.fromtimestamp(row["updated_at"], timezone.utc).isoformat(),
        }

    def financial_summary(self, *, started_after: str | None = None) -> dict[str, Any]:
        records = self.financial_records(1000, started_after=started_after)
        currency_totals: dict[str, dict[str, Any]] = {}
        status_totals: dict[tuple[str, str], Decimal] = {}
        for record in records:
            amount = record.get("originalAmount") or {}
            currency = str(amount.get("currency") or "UNKNOWN")
            value = Decimal(str(amount.get("value") or "0"))
            bucket = currency_totals.setdefault(
                currency,
                {"currency": currency, "groupCount": 0, "total": Decimal("0"), "succeeded": Decimal("0"), "pending": Decimal("0")},
            )
            bucket["groupCount"] += 1
            bucket["total"] += value
            normalized_status = str(record.get("transferStatus") or "Pending")
            status_totals[(currency, normalized_status)] = status_totals.get((currency, normalized_status), Decimal("0")) + value
            if normalized_status.lower() in {"succeeded", "transferred", "transfered"}:
                bucket["succeeded"] += value
            else:
                bucket["pending"] += value
        totals = [
            {
                "currency": bucket["currency"],
                "groupCount": bucket["groupCount"],
                "total": format(bucket["total"], "f"),
                "succeeded": format(bucket["succeeded"], "f"),
                "pending": format(bucket["pending"], "f"),
            }
            for bucket in sorted(currency_totals.values(), key=lambda item: item["currency"])
        ]
        statuses = [
            {"currency": currency, "status": status, "amount": format(amount, "f")}
            for (currency, status), amount in sorted(status_totals.items())
        ]
        return {"recordCount": len(records), "totals": totals, "statuses": statuses}

    # Backwards-compatible helpers for existing callers and databases.
    def get(self, key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_hash, status_code, response_json FROM payout_requests WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return {"request_hash": row["request_hash"], "status_code": row["status_code"], "response": json.loads(row["response_json"])}

    def save(self, key: str, request_hash: str, marketplace: str, status_code: int, response: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO payout_requests VALUES (?, ?, ?, ?, ?, ?)",
                (key, request_hash, marketplace, int(time.time()), status_code, json.dumps(response, ensure_ascii=False)),
            )
