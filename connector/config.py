"""Configuration: a YAML file with ${ENV_VAR} substitution, validated by pydantic."""

from __future__ import annotations

import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, model_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

ONTOPO_STAGING_URL = "https://top-openapi.staging-01.ontopo.cz"


class SyrveConfig(BaseModel):
    base_url: str = "https://api-eu.syrve.live"
    api_login: str
    organization_id: str
    terminal_group_id: str
    # Empty list = every reservable section of the terminal group.
    restaurant_section_ids: list[str] = Field(default_factory=list)
    # "regular" links the reserve to a CRM customer by phone, "one-time" does not.
    customer_type: str = "regular"
    should_remind: bool = True
    request_timeout_seconds: float = 30.0
    # How long to wait for Syrve to confirm a reserve was created on the POS.
    creation_poll_attempts: int = 10
    creation_poll_interval_seconds: float = 1.0


class OntopoConfig(BaseModel):
    base_url: str = ONTOPO_STAGING_URL
    # Venue API key issued by Ontopo, sent as the x-api-key header.
    api_key: str
    # Optional shared token for incoming webhooks: accepted as ?token=... or the x-api-key header.
    webhook_token: str = ""
    # Safety net for missed webhooks: POST /pos/reservationsForToday.
    poll_reservations: bool = True
    poll_interval_seconds: int = Field(300, ge=30)
    request_timeout_seconds: float = 30.0


class ReservationsConfig(BaseModel):
    default_duration_minutes: int = Field(120, ge=15)
    # Turnover buffer kept between reserves on the same table when auto-assigning.
    buffer_minutes: int = Field(15, ge=0)
    # Ontopo table label -> Syrve table (id, number or name). Unmapped labels are
    # matched to Syrve tables by number, then by name.
    table_map: dict[str, str] = Field(default_factory=dict)
    auto_assign_tables: bool = True
    allow_table_combining: bool = False
    # Send the table chosen in Syrve back to Ontopo when it differs from Ontopo's.
    report_assigned_tables: bool = True
    max_attempts: int = Field(8, ge=1)
    retry_base_seconds: int = Field(15, ge=1)


class OrdersConfig(BaseModel):
    """Syrve table orders -> Ontopo (this is how Ontopo learns which tables are taken)."""

    enabled: bool = True
    sync_interval_seconds: int = Field(30, ge=5)
    # An order opened on a reserved table from N minutes before the reservation
    # until its end is treated as that reservation's guests arriving.
    early_arrival_minutes: int = Field(45, ge=0)
    # Orders that match no Ontopo reservation are sent as POS-created (walk-in) reservations.
    create_walk_ins: bool = True
    include_items: bool = True


class StorageConfig(BaseModel):
    path: str = "data/connector.db"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    # If set, /status and /admin/* require the X-Admin-Token header.
    admin_token: str = ""


class Settings(BaseModel):
    timezone: str = "Asia/Jerusalem"
    syrve: SyrveConfig
    ontopo: OntopoConfig
    reservations: ReservationsConfig = Field(default_factory=ReservationsConfig)
    orders: OrdersConfig = Field(default_factory=OrdersConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @model_validator(mode="after")
    def _check_timezone(self) -> "Settings":
        ZoneInfo(self.timezone)  # raises for unknown zones
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _expand_env(value):
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ValueError(f"environment variable {name} is not set")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.environ.get("CONNECTOR_CONFIG", "config.yaml"))
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Settings.model_validate(_expand_env(raw))
