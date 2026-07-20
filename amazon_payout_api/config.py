from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Load a small .env file without overwriting existing environment values."""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    value = default if raw is None else int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    mode: str
    api_key: str
    lwa_client_id: str
    lwa_client_secret: str
    lwa_refresh_token: str
    endpoint: str
    allow_production: bool
    allow_payout_post: bool
    dry_run: bool
    database_path: Path
    host: str
    port: int
    timezone: str = "Asia/Shanghai"
    scheduler_enabled: bool = True
    scheduler_poll_seconds: int = 60
    auto_payout_marketplaces: frozenset[str] = frozenset()
    allow_sandbox_post: bool = False
    finance_sync_enabled: bool = True
    finance_sync_interval_seconds: int = 21_600
    finance_sync_days: int = 180
    ziniao_enabled: bool = False
    ziniao_client_path: Path | None = None
    ziniao_version: str = "v6"
    ziniao_host: str = "127.0.0.1"
    ziniao_port: int = 16851
    ziniao_company: str = ""
    ziniao_username: str = ""
    ziniao_password: str = ""
    ziniao_request_timeout_seconds: int = 120
    ziniao_start_timeout_seconds: int = 30
    ziniao_webdriver_path: Path | None = None
    ziniao_amazon_page_timeout_seconds: int = 90
    ziniao_prepare_ttl_seconds: int = 300
    allow_ziniao_payout: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        mode = os.environ.get("SP_API_MODE", "sandbox").strip().lower()
        if mode not in {"sandbox", "production"}:
            raise ValueError("SP_API_MODE must be sandbox or production")

        prefix = mode.upper()
        endpoint_default = (
            "https://sandbox.sellingpartnerapi-eu.amazon.com"
            if mode == "sandbox"
            else "https://sellingpartnerapi-eu.amazon.com"
        )
        endpoint = os.environ.get(f"SP_API_{prefix}_ENDPOINT_EU", endpoint_default).rstrip("/")
        database_path = Path(os.environ.get("DATABASE_PATH", "data/payouts.sqlite3"))
        if not database_path.is_absolute():
            database_path = PROJECT_ROOT / database_path
        timezone = os.environ.get("TIMEZONE", "Asia/Shanghai").strip()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Unknown TIMEZONE: {timezone}") from error
        auto_payout_marketplaces = frozenset(
            item.strip().upper()
            for item in os.environ.get("AUTO_PAYOUT_MARKETPLACES", "").split(",")
            if item.strip()
        )
        ziniao_client_path_raw = os.environ.get("ZINIAO_CLIENT_PATH", "").strip()
        ziniao_client_path = Path(ziniao_client_path_raw).expanduser() if ziniao_client_path_raw else None
        ziniao_webdriver_path_raw = os.environ.get("ZINIAO_WEBDRIVER_PATH", "").strip()
        ziniao_webdriver_path = Path(ziniao_webdriver_path_raw).expanduser() if ziniao_webdriver_path_raw else None
        ziniao_version = os.environ.get("ZINIAO_VERSION", "v6").strip().lower()
        if ziniao_version not in {"v5", "v6"}:
            raise ValueError("ZINIAO_VERSION must be v5 or v6")
        return cls(
            mode=mode,
            api_key=os.environ.get("API_KEY", ""),
            lwa_client_id=os.environ.get(f"{prefix}_LWA_CLIENT_ID", ""),
            lwa_client_secret=os.environ.get(f"{prefix}_LWA_CLIENT_SECRET", ""),
            lwa_refresh_token=os.environ.get(f"{prefix}_LWA_REFRESH_TOKEN", ""),
            endpoint=endpoint,
            allow_production=env_bool("ALLOW_PRODUCTION"),
            allow_payout_post=env_bool("ALLOW_PAYOUT_POST"),
            dry_run=env_bool("DRY_RUN", True),
            database_path=database_path,
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8080")),
            timezone=timezone,
            scheduler_enabled=env_bool("SCHEDULER_ENABLED", True),
            scheduler_poll_seconds=env_int("SCHEDULER_POLL_SECONDS", 60, 10, 3600),
            auto_payout_marketplaces=auto_payout_marketplaces,
            allow_sandbox_post=env_bool("ALLOW_SANDBOX_POST"),
            finance_sync_enabled=env_bool("FINANCE_SYNC_ENABLED", True),
            finance_sync_interval_seconds=env_int("FINANCE_SYNC_INTERVAL_SECONDS", 21_600, 300, 86_400),
            finance_sync_days=env_int("FINANCE_SYNC_DAYS", 180, 1, 180),
            ziniao_enabled=env_bool("ZINIAO_ENABLED"),
            ziniao_client_path=ziniao_client_path,
            ziniao_version=ziniao_version,
            ziniao_host=os.environ.get("ZINIAO_HOST", "127.0.0.1").strip(),
            ziniao_port=env_int("ZINIAO_PORT", 16851, 1024, 65535),
            ziniao_company=os.environ.get("ZINIAO_COMPANY", ""),
            ziniao_username=os.environ.get("ZINIAO_USERNAME", ""),
            ziniao_password=os.environ.get("ZINIAO_PASSWORD", ""),
            ziniao_request_timeout_seconds=env_int("ZINIAO_REQUEST_TIMEOUT_SECONDS", 120, 10, 600),
            ziniao_start_timeout_seconds=env_int("ZINIAO_START_TIMEOUT_SECONDS", 30, 5, 180),
            ziniao_webdriver_path=ziniao_webdriver_path,
            ziniao_amazon_page_timeout_seconds=env_int("ZINIAO_AMAZON_PAGE_TIMEOUT_SECONDS", 90, 15, 300),
            ziniao_prepare_ttl_seconds=env_int("ZINIAO_PREPARE_TTL_SECONDS", 300, 60, 900),
            allow_ziniao_payout=env_bool("ALLOW_ZINIAO_PAYOUT"),
        )

    def validate_for_server(self) -> None:
        if not self.api_key or len(self.api_key) < 24:
            raise ValueError("API_KEY is required and must contain at least 24 characters")

    def validate_amazon_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                (f"{self.mode.upper()}_LWA_CLIENT_ID", self.lwa_client_id),
                (f"{self.mode.upper()}_LWA_CLIENT_SECRET", self.lwa_client_secret),
                (f"{self.mode.upper()}_LWA_REFRESH_TOKEN", self.lwa_refresh_token),
            )
            if not value
        ]
        if missing:
            raise ValueError("Missing Amazon credentials: " + ", ".join(missing))

    @property
    def credentials_complete(self) -> bool:
        return all((self.lwa_client_id, self.lwa_client_secret, self.lwa_refresh_token))
