from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Settings


SUPPORTED_MARKETPLACES = {
    "BE": "AMEN7PMS3EDWL",
    "DE": "A1PA6795UKMFR9",
    "ES": "A1RKKUPIHCS9HS",
    "FR": "A13V1IB3VIYZZH",
    "IT": "APJ6JRA9NG5V4",
    "NL": "A1805IZSGTT6HS",
    "PL": "A1C3SOZRARQ6R3",
    "SE": "A2NODRKZP88ZB9",
}

KNOWN_UNSUPPORTED_MARKETPLACES = {
    "UK": "A1F83G8C2ARO7",
    "GB": "A1F83G8C2ARO7",
    "US": "ATVPDKIKX0DER",
    "CA": "A2EUQ1WTGCTBG2",
    "MX": "A1AM78C64UM0Y8",
}


class AmazonApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def resolve_marketplace(value: str) -> tuple[str, str]:
    normalized = value.strip().upper()
    if normalized in SUPPORTED_MARKETPLACES:
        return normalized, SUPPORTED_MARKETPLACES[normalized]
    for code, marketplace_id in SUPPORTED_MARKETPLACES.items():
        if normalized == marketplace_id:
            return code, marketplace_id
    if normalized in KNOWN_UNSUPPORTED_MARKETPLACES or normalized in KNOWN_UNSUPPORTED_MARKETPLACES.values():
        raise AmazonApiError(
            422,
            "UNSUPPORTED_MARKETPLACE",
            "Amazon Transfers API currently supports only BE, DE, ES, FR, IT, NL, PL and SE; UK/GB is not supported.",
        )
    raise AmazonApiError(422, "INVALID_MARKETPLACE", "Unknown or unsupported marketplace code/ID")


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]
    payload: Any


class AmazonTransfersClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0

    def _send(self, request: urllib.request.Request) -> HttpResult:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    payload = {"message": raw[:500]}
                return HttpResult(response.status, dict(response.headers), payload)
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"message": raw[:500]}
            return HttpResult(error.code, dict(error.headers), payload)
        except urllib.error.URLError as error:
            raise AmazonApiError(502, "AMAZON_UNREACHABLE", "Could not reach Amazon SP-API") from error

    def _token(self) -> str:
        if self._access_token and time.monotonic() < self._access_token_expires_at:
            return self._access_token
        self.settings.validate_amazon_credentials()
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.settings.lwa_refresh_token,
                "client_id": self.settings.lwa_client_id,
                "client_secret": self.settings.lwa_client_secret,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://api.amazon.com/auth/o2/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            method="POST",
        )
        result = self._send(request)
        if result.status != 200 or not isinstance(result.payload, dict) or not result.payload.get("access_token"):
            raise AmazonApiError(502, "LWA_AUTH_FAILED", "Amazon LWA authorization failed")
        self._access_token = str(result.payload["access_token"])
        expires_in = max(60, int(result.payload.get("expires_in", 3600)))
        self._access_token_expires_at = time.monotonic() + expires_in - 60
        return self._access_token

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> HttpResult:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Accept": "application/json",
            "User-Agent": "amazon-payout-api/1.0",
            "x-amz-access-token": self._token(),
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.settings.endpoint + path,
            data=data,
            headers=headers,
            method=method,
        )
        result = self._send(request)
        if not 200 <= result.status < 300:
            code = "AMAZON_API_ERROR"
            message = "Amazon SP-API rejected the request"
            if isinstance(result.payload, dict) and result.payload.get("errors"):
                first = result.payload["errors"][0]
                code = str(first.get("code") or code)
                message = str(first.get("message") or message)
            raise AmazonApiError(result.status, code, message, result.payload)
        return result

    @staticmethod
    def _request_id(result: HttpResult) -> str | None:
        for name, value in result.headers.items():
            if name.lower() == "x-amzn-requestid":
                return value
        return None

    def test_connection(self, marketplace: str = "DE") -> dict[str, Any]:
        self._token()
        if self.settings.mode == "sandbox":
            # Amazon's static Transfers sandbox only matches this documented fixture.
            path = "/finances/transfers/2024-06-01/paymentMethods?marketplaceId=ATVPDKIKX0DER"
            result = self._request("GET", path)
            return {
                "mode": "sandbox",
                "lwa": "ok",
                "spApi": "ok",
                "sandboxFixture": True,
                "requestId": self._request_id(result),
            }
        response = self.get_payment_methods(marketplace, ["BANK_ACCOUNT"])
        return {
            "mode": "production",
            "lwa": "ok",
            "spApi": "ok",
            "marketplace": response["marketplace"],
            "requestId": response.get("requestId"),
        }

    def get_payment_methods(self, marketplace: str, method_types: list[str] | None = None) -> dict[str, Any]:
        code, marketplace_id = resolve_marketplace(marketplace)
        query_marketplace_id = "ATVPDKIKX0DER" if self.settings.mode == "sandbox" else marketplace_id
        query: dict[str, str] = {"marketplaceId": query_marketplace_id}
        if method_types:
            allowed = {"BANK_ACCOUNT", "CARD", "SELLER_WALLET"}
            invalid = sorted(set(method_types) - allowed)
            if invalid:
                raise AmazonApiError(422, "INVALID_PAYMENT_METHOD_TYPE", "Invalid payment method type", invalid)
            if self.settings.mode != "sandbox":
                query["paymentMethodTypes"] = ",".join(method_types)
        path = "/finances/transfers/2024-06-01/paymentMethods?" + urllib.parse.urlencode(query)
        result = self._request("GET", path)
        return {
            "marketplace": code,
            "marketplaceId": marketplace_id,
            "sandboxFixture": self.settings.mode == "sandbox",
            "requestId": self._request_id(result),
            "data": result.payload,
        }

    def initiate_payout(self, marketplace: str, account_type: str = "Standard Orders") -> dict[str, Any]:
        code, marketplace_id = resolve_marketplace(marketplace)
        if account_type != "Standard Orders":
            raise AmazonApiError(422, "INVALID_ACCOUNT_TYPE", "Supported EU marketplaces only accept 'Standard Orders'")
        result = self._request(
            "POST",
            "/finances/transfers/2024-06-01/payouts",
            {"marketplaceId": marketplace_id, "accountType": account_type},
        )
        return {
            "marketplace": code,
            "marketplaceId": marketplace_id,
            "requestId": self._request_id(result),
            "data": result.payload,
        }

    def list_financial_event_groups(
        self,
        started_after: str,
        started_before: str | None = None,
        *,
        max_results: int = 100,
        max_pages: int = 20,
    ) -> dict[str, Any]:
        groups: list[dict[str, Any]] = []
        request_ids: list[str] = []
        next_token: str | None = None
        page_count = 0
        while page_count < max_pages:
            query: dict[str, str] = {
                "MaxResultsPerPage": str(max(1, min(max_results, 100))),
                "FinancialEventGroupStartedAfter": started_after,
            }
            if started_before:
                query["FinancialEventGroupStartedBefore"] = started_before
            if next_token:
                query["NextToken"] = next_token
            path = "/finances/v0/financialEventGroups?" + urllib.parse.urlencode(query)
            result = self._request("GET", path)
            page_count += 1
            request_id = self._request_id(result)
            if request_id:
                request_ids.append(request_id)
            payload = result.payload.get("payload", {}) if isinstance(result.payload, dict) else {}
            page_groups = payload.get("FinancialEventGroupList", []) if isinstance(payload, dict) else []
            if isinstance(page_groups, list):
                groups.extend(item for item in page_groups if isinstance(item, dict))
            next_token = str(payload.get("NextToken") or "") if isinstance(payload, dict) else ""
            if not next_token:
                break
            time.sleep(2)
        return {
            "groups": groups,
            "pageCount": page_count,
            "requestIds": request_ids,
            "truncated": bool(next_token),
        }
