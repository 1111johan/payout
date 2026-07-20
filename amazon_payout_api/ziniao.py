from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import time
from typing import Any
import urllib.error
import urllib.request
import uuid

from .amazon import AmazonApiError
from .config import Settings


SECRET_FIELDS = {"company", "username", "password"}


class ZiniaoClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return all(
            (
                self.settings.ziniao_client_path,
                self.settings.ziniao_company,
                self.settings.ziniao_username,
                self.settings.ziniao_password,
            )
        )

    def status(self) -> dict[str, Any]:
        client_path = self.settings.ziniao_client_path
        return {
            "enabled": self.settings.ziniao_enabled,
            "configured": self.configured,
            "clientInstalled": bool(client_path and client_path.is_file()),
            "clientPath": str(client_path) if client_path else None,
            "clientVersion": self.settings.ziniao_version,
            "serviceReachable": self._port_open(),
            "serviceHost": self.settings.ziniao_host,
            "servicePort": self.settings.ziniao_port,
        }

    def start_client(self) -> dict[str, Any]:
        self._require_enabled()
        client_path = self.settings.ziniao_client_path
        if client_path is None or not client_path.is_file():
            raise AmazonApiError(503, "ZINIAO_CLIENT_NOT_FOUND", "Ziniao client executable was not found")
        if self._port_open():
            return {"status": "running", "alreadyRunning": True, **self.status()}

        command = [
            str(client_path),
            "--run_type=web_driver",
            "--ipc_type=http",
            f"--port={self.settings.ziniao_port}",
        ]
        options: dict[str, Any] = {"cwd": str(client_path.parent), "close_fds": True}
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **options)
        except OSError as error:
            raise AmazonApiError(502, "ZINIAO_CLIENT_START_FAILED", "Could not start the Ziniao client") from error

        deadline = time.monotonic() + self.settings.ziniao_start_timeout_seconds
        while time.monotonic() < deadline:
            if self._port_open():
                return {"status": "running", "alreadyRunning": False, "pid": process.pid, **self.status()}
            time.sleep(0.5)
        if process.poll() is not None:
            raise AmazonApiError(
                502,
                "ZINIAO_CLIENT_EXITED",
                "Ziniao client exited before its WebDriver service became available",
            )
        raise AmazonApiError(504, "ZINIAO_CLIENT_START_TIMEOUT", "Timed out waiting for the Ziniao WebDriver service")

    def update_core(self) -> dict[str, Any]:
        response = self._request("updateCore", allow_progress=True)
        status_code = self._status_code(response)
        return {
            "status": "ready" if status_code == 0 else "updating",
            "statusCode": status_code,
            "message": self._message(response),
            "requestId": response.get("requestId"),
        }

    def list_stores(self) -> list[dict[str, Any]]:
        response = self._request("getBrowserList")
        stores = response.get("browserList")
        if not isinstance(stores, list):
            return []
        return [self._store(item) for item in stores if isinstance(item, dict)]

    def running_stores(self) -> list[dict[str, Any]]:
        response = self._request("getRunningInfo", require_credentials=False)
        stores = response.get("browsers")
        if not isinstance(stores, list):
            return []
        return [self._store(item) for item in stores if isinstance(item, dict)]

    def start_store(self, control_type: str, control_id: str) -> dict[str, Any]:
        control_type, control_id = self._control(control_type, control_id)
        response = self._request(
            "startBrowser",
            **{
                control_type: control_id,
                "isWaitPluginUpdate": True,
                "isHeadless": False,
                "privacyMode": False,
                "cookieTypeLoad": 0,
                "cookieTypeSave": 0,
                "runMode": "2",
                "isLoadUserPlugin": True,
                "notPromptForDownload": 1,
            },
        )
        return {
            "status": "running",
            "controlType": control_type,
            "controlId": control_id,
            "debuggingPort": response.get("debuggingPort"),
            "launcherPage": response.get("launcherPage"),
            "browserPath": response.get("browserPath"),
            "mainHandle": response.get("mainHandle"),
            "duplicate": response.get("duplicate", 0),
            "coreVersion": response.get("coreVersion") or response.get("core_version"),
            "coreType": response.get("coreType") or response.get("core_type"),
        }

    def stop_store(self, control_type: str, control_id: str, duplicate: int = 0) -> dict[str, Any]:
        control_type, control_id = self._control(control_type, control_id)
        self._request("stopBrowser", **{control_type: control_id, "duplicate": int(duplicate)})
        return {"status": "stopped", "controlType": control_type, "controlId": control_id}

    def exit_client(self) -> dict[str, Any]:
        self._request("exit", require_credentials=False)
        return {"status": "stopped"}

    def _request(
        self,
        action: str,
        *,
        require_credentials: bool = True,
        allow_progress: bool = False,
        **parameters: Any,
    ) -> dict[str, Any]:
        self._require_enabled()
        if require_credentials and not self.configured:
            raise AmazonApiError(
                503,
                "ZINIAO_CONFIGURATION_INCOMPLETE",
                "Ziniao company, username and password must be configured locally",
            )
        payload: dict[str, Any] = {"action": action, "requestId": str(uuid.uuid4()), **parameters}
        if self.configured:
            payload.update(
                {
                    "company": self.settings.ziniao_company,
                    "username": self.settings.ziniao_username,
                    "password": self.settings.ziniao_password,
                }
            )
        request = urllib.request.Request(
            f"http://{self.settings.ziniao_host}:{self.settings.ziniao_port}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.ziniao_request_timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AmazonApiError(
                502,
                "ZINIAO_UNREACHABLE",
                "Could not reach the local Ziniao WebDriver service",
                {"errorType": type(error).__name__, "reason": str(error)},
            ) from error
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AmazonApiError(502, "ZINIAO_INVALID_RESPONSE", "Ziniao returned an invalid JSON response") from error
        if not isinstance(result, dict):
            raise AmazonApiError(502, "ZINIAO_INVALID_RESPONSE", "Ziniao returned an invalid response")
        status_code = self._status_code(result)
        if status_code != 0 and not (allow_progress and status_code > 0):
            code = {
                -10003: "ZINIAO_LOGIN_FAILED",
                -10004: "ZINIAO_STORE_ID_MISSING",
                -10006: "ZINIAO_STORE_START_PENDING",
                -10013: "ZINIAO_DEVICE_AUTH_REQUIRED",
            }.get(status_code, "ZINIAO_REQUEST_FAILED")
            raise AmazonApiError(502, code, self._message(result) or f"Ziniao request failed ({status_code})")
        return self._safe(result)

    def _require_enabled(self) -> None:
        if not self.settings.ziniao_enabled:
            raise AmazonApiError(403, "ZINIAO_DISABLED", "Ziniao control is disabled by local configuration")

    def _port_open(self) -> bool:
        try:
            with socket.create_connection((self.settings.ziniao_host, self.settings.ziniao_port), timeout=0.35):
                return True
        except OSError:
            return False

    @staticmethod
    def _status_code(payload: dict[str, Any]) -> int:
        try:
            return int(payload.get("statusCode", -10000))
        except (TypeError, ValueError):
            return -10000

    @staticmethod
    def _message(payload: dict[str, Any]) -> str:
        for name in ("err", "statusMsg", "msg", "LastError"):
            value = payload.get(name)
            if value:
                return str(value)
        return ""

    @staticmethod
    def _control(control_type: str, control_id: str) -> tuple[str, str]:
        if control_type not in {"browserId", "browserOauth"}:
            raise AmazonApiError(422, "INVALID_ZINIAO_CONTROL_TYPE", "controlType must be browserId or browserOauth")
        normalized = str(control_id).strip()
        if not normalized or len(normalized) > 512:
            raise AmazonApiError(422, "INVALID_ZINIAO_CONTROL_ID", "A valid Ziniao store control ID is required")
        return control_type, normalized

    @classmethod
    def _store(cls, item: dict[str, Any]) -> dict[str, Any]:
        browser_id = item.get("browserId")
        browser_oauth = item.get("browserOauth")
        control_type = "browserId" if browser_id not in (None, "") else "browserOauth"
        control_id = browser_id if control_type == "browserId" else browser_oauth
        return {
            "controlType": control_type,
            "controlId": str(control_id or ""),
            "browserId": str(browser_id or ""),
            "browserOauth": str(browser_oauth or ""),
            "browserName": item.get("browserName") or item.get("name") or "-",
            "siteId": item.get("siteId"),
            "platformId": item.get("platform_id") or item.get("platformId"),
            "platformName": item.get("platform_name") or item.get("platformName"),
            "browserIp": item.get("browserIp") or item.get("ip"),
            "isExpired": bool(item.get("isExpired", False)),
            "debuggingPort": item.get("debuggingPort"),
            "duplicate": item.get("duplicate", 0),
            "launcherPage": item.get("launcherPage"),
            "browserPath": item.get("browserPath"),
            "coreVersion": item.get("coreVersion") or item.get("core_version"),
            "coreType": item.get("coreType") or item.get("core_type"),
        }

    @classmethod
    def _safe(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._safe(item) for key, item in value.items() if key.lower() not in SECRET_FIELDS}
        if isinstance(value, list):
            return [cls._safe(item) for item in value]
        return value
