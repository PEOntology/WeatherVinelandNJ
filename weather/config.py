"""Central configuration.

Everything secret comes from environment variables (GitHub Actions secrets in
production). Nothing in this file, or anywhere in the repository, may contain a
credential.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"
SITE_DATA = SITE_DIR / "data"
STATE_DIR = ROOT / "state"
PRIVATE_DIR = ROOT / "private"  # gitignored; raw licensed records never go in git
BOUNDARY_FILE = ROOT / "data" / "vineland_boundary.geojson"

TZ = ZoneInfo("America/New_York")
START_DATE = date(2026, 5, 1)

AREA_NAME = "Vineland, NJ"
AREA_SCOPE = "City of Vineland municipal boundary (Census place 3476070)"
CENTER_LAT = 39.4864
CENTER_LON = -75.0260

# Nearby measured reference. It is NOT in Vineland and must never be labelled so.
REF_STATION = "MIV"
REF_STATION_ICAO = "KMIV"
REF_STATION_NAME = "Millville Municipal Airport"
REF_NETWORK = "NJ_ASOS"

SITE_URL = os.environ.get("SITE_URL", "https://constructionweather.us")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _flag(name: str, default: bool = False) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Xweather (Lightning Enterprise add-on required for anything older than 5 minutes)
    xweather_client_id: str | None = field(default_factory=lambda: _env("XWEATHER_CLIENT_ID"))
    xweather_client_secret: str | None = field(default_factory=lambda: _env("XWEATHER_CLIENT_SECRET"))
    xweather_base: str = field(default_factory=lambda: _env("XWEATHER_BASE", "https://data.api.xweather.com"))
    xweather_lightning_path: str = field(default_factory=lambda: _env("XWEATHER_LIGHTNING_PATH", "lightning/within"))
    # Historical/polygon lightning needs the Lightning Enterprise add-on; off until purchased.
    xweather_enterprise: bool = field(default_factory=lambda: _flag("XWEATHER_ENTERPRISE"))
    glm_enabled: bool = field(default_factory=lambda: _flag("GLM_ENABLED", True))
    xweather_filter: str = field(default_factory=lambda: _env("XWEATHER_LIGHTNING_FILTER", "all"))
    # Individual event coordinates are published only if the license allows it.
    publish_lightning_events: bool = field(default_factory=lambda: _flag("PUBLISH_LIGHTNING_EVENTS"))

    # National Weather Service asks for an identifying User-Agent.
    nws_user_agent: str = field(
        default_factory=lambda: _env("NWS_USER_AGENT", "(constructionweather.us, weather-log)")
    )

    # Resend e-mail
    resend_api_key: str | None = field(default_factory=lambda: _env("RESEND_API_KEY"))
    report_from: str = field(
        default_factory=lambda: _env("REPORT_FROM", "Vineland Weather Log <reports@reports.constructionweather.us>")
    )
    report_recipients: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            r.strip() for r in (_env("REPORT_RECIPIENTS", "") or "").split(",") if r.strip()
        )
    )
    alert_email: str | None = field(default_factory=lambda: _env("ALERT_EMAIL"))

    # Procore: off | dry-run | live
    procore_mode: str = field(default_factory=lambda: (_env("PROCORE_MODE", "off") or "off").lower())
    procore_env: str = field(default_factory=lambda: (_env("PROCORE_ENV", "sandbox") or "sandbox").lower())
    procore_client_id: str | None = field(default_factory=lambda: _env("PROCORE_CLIENT_ID"))
    procore_client_secret: str | None = field(default_factory=lambda: _env("PROCORE_CLIENT_SECRET"))
    procore_company_id: str | None = field(default_factory=lambda: _env("PROCORE_COMPANY_ID"))
    procore_project_id: str | None = field(default_factory=lambda: _env("PROCORE_PROJECT_ID"))
    procore_folder_id: str | None = field(default_factory=lambda: _env("PROCORE_FOLDER_ID"))

    # Xano (private archive). Uses the Metadata API content endpoints.
    xano_meta_url: str | None = field(default_factory=lambda: _env("XANO_META_URL"))
    xano_token: str | None = field(default_factory=lambda: _env("XANO_API_TOKEN"))
    xano_workspace_id: str | None = field(default_factory=lambda: _env("XANO_WORKSPACE_ID"))
    xano_table_id: str | None = field(default_factory=lambda: _env("XANO_TABLE_ID"))

    @property
    def xweather_configured(self) -> bool:
        return bool(self.xweather_client_id and self.xweather_client_secret)

    @property
    def resend_configured(self) -> bool:
        return bool(self.resend_api_key and self.report_recipients)

    @property
    def xano_configured(self) -> bool:
        return bool(self.xano_meta_url and self.xano_token and self.xano_workspace_id and self.xano_table_id)

    @property
    def procore_login_base(self) -> str:
        return "https://login.procore.com" if self.procore_env == "production" else "https://login-sandbox.procore.com"

    @property
    def procore_api_base(self) -> str:
        return "https://api.procore.com" if self.procore_env == "production" else "https://sandbox.procore.com"


def settings() -> Settings:
    return Settings()
