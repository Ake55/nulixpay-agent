"""Load PayPal Sandbox settings from the environment.

The live PayPal host is intentionally absent. Credentials are read here and
passed to the PayPal client. Nothing in this module prints them.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = ROOT / ".env"
SANDBOX_API_ORIGIN = "https://api-m.sandbox.paypal.com"
DEFAULT_RETURN_URL = "http://127.0.0.1:8000/payments/return"
DEFAULT_CANCEL_URL = "http://127.0.0.1:8000/payments/cancel"
DEFAULT_DATABASE = ROOT / "data" / "orders.sqlite"
REQUEST_TIMEOUT_SECONDS = 10.0


class SettingsError(Exception):
    """Raised when sandbox checkout is not configured safely."""


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    mode: str
    return_url: str
    cancel_url: str
    database_path: Path
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS

    @property
    def api_origin(self) -> str:
        if self.mode != "sandbox":
            raise SettingsError(
                "PAYPAL_MODE must be sandbox. This server never calls the live PayPal API."
            )
        return SANDBOX_API_ORIGIN


def load_settings(env_file: Path | None = None) -> Settings:
    """Read sandbox settings.

    Existing environment variables win over the .env file. Values are not logged.
    """
    load_dotenv(DEFAULT_ENV_FILE if env_file is None else env_file, override=False)
    mode = os.environ.get("PAYPAL_MODE", "").strip().lower()
    client_id = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()
    if mode == "":
        raise SettingsError(
            "PAYPAL_MODE is missing. Add PAYPAL_MODE=sandbox to the .env file in the project root, then restart the API."
        )
    if mode != "sandbox":
        raise SettingsError(
            "PAYPAL_MODE must be sandbox. This server never calls the live PayPal API."
        )
    if not client_id or not client_secret:
        raise SettingsError(
            "PayPal Sandbox credentials are missing. Set PAYPAL_CLIENT_ID and "
            "PAYPAL_CLIENT_SECRET on the server."
        )
    database = os.environ.get("PAYPAL_DB_PATH", "").strip()
    database_path = Path(database) if database else DEFAULT_DATABASE
    if not database_path.is_absolute():
        database_path = ROOT / database_path
    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        mode=mode,
        return_url=_public_url("PAYPAL_RETURN_URL", os.environ.get("PAYPAL_RETURN_URL", ""), DEFAULT_RETURN_URL),
        cancel_url=_public_url("PAYPAL_CANCEL_URL", os.environ.get("PAYPAL_CANCEL_URL", ""), DEFAULT_CANCEL_URL),
        database_path=database_path,
    )


def _public_url(name: str, value: str, default: str) -> str:
    raw = value.strip() or default
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SettingsError(f"{name} must be an http or https URL.")
    if parsed.username or parsed.password:
        raise SettingsError(f"{name} must not include a username or password.")
    return raw
