"""Configuration: a YAML file with ${ENV_VAR} substitution, validated by pydantic."""

from __future__ import annotations

import os
import re
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class SyrveConfig(BaseModel):
    base_url: str = "https://api-eu.syrve.live"
    api_login: str
    organization_id: str
    terminal_group_id: str
    # Empty list = every reservable section of the terminal group.
    restaurant_section_ids: list[str] = Field(default_factory=list)
    # Treat tables with open (unpaid) orders as busy right now (walk-in guests).
    consider_open_orders: bool = False
    # "regular" links the reserve to a CRM customer by phone, "one-time" does not.
    customer_type: str = "regular"
    should_remind: bool = True
    request_timeout_seconds: float = 30.0
    # How long to wait for Syrve to confirm a reserve was created on the POS.
    creation_poll_attempts: int = 10
    creation_poll_interval_seconds: float = 1.0


class OntopoConfig(BaseModel):
    base_url: str
    api_key: str = ""
    venue_id: str
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    # Paths are templates; {venue_id} and {reservation_id} are substituted.
    availability_path: str = "/venues/{venue_id}/availability"
    reservations_path: str = "/venues/{venue_id}/reservations"
    # Optional: where to report the POS outcome of a reservation. Empty = disabled.
    reservation_status_path: str = "/venues/{venue_id}/reservations/{reservation_id}/pos-status"
    webhook_secret: str = ""
    webhook_signature_header: str = "X-Ontopo-Signature"
    # Fallback polling of reservations, in case webhooks are missed.
    poll_reservations: bool = False
    poll_interval_seconds: int = 60
    request_timeout_seconds: float = 30.0


class DayHours(BaseModel):
    open: time
    close: time  # close <= open means the venue closes after midnight

    @field_validator("open", "close", mode="before")
    @classmethod
    def _yaml_sexagesimal(cls, value):
        # YAML 1.1 parses an unquoted 12:30 as the base-60 integer 750.
        if isinstance(value, int):
            hours, minutes = divmod(value, 60)
            return time(hours % 24, minutes)
        return value


class AvailabilityConfig(BaseModel):
    horizon_days: int = Field(14, ge=1, le=90)
    slot_minutes: int = Field(15, ge=5, le=120)
    default_duration_minutes: int = Field(120, ge=15)
    # Cleaning / turnover buffer kept around existing reservations.
    buffer_minutes: int = Field(15, ge=0)
    # Last seating is this many minutes before closing.
    last_seating_offset_minutes: int = Field(60, ge=0)
    sync_interval_seconds: int = Field(120, ge=10)
    # Keys: "default" and/or mon..sun. A null value means closed that day.
    opening_hours: dict[str, DayHours | None] = Field(
        default_factory=lambda: {"default": DayHours(open=time(12), close=time(23))}
    )

    @field_validator("opening_hours")
    @classmethod
    def _check_keys(cls, value: dict[str, DayHours | None]) -> dict[str, DayHours | None]:
        unknown = set(value) - set(WEEKDAYS) - {"default"}
        if unknown:
            raise ValueError(f"unknown opening_hours keys: {sorted(unknown)}")
        return value

    def hours_for(self, weekday: int) -> DayHours | None:
        key = WEEKDAYS[weekday]
        if key in self.opening_hours:
            return self.opening_hours[key]
        return self.opening_hours.get("default")


class ReservationsConfig(BaseModel):
    default_duration_minutes: int = Field(120, ge=15)
    # Ontopo table id -> Syrve table id. Tables can also be matched by number/name.
    table_map: dict[str, str] = Field(default_factory=dict)
    auto_assign_tables: bool = True
    allow_table_combining: bool = False
    max_attempts: int = Field(8, ge=1)
    retry_base_seconds: int = Field(15, ge=1)


class StorageConfig(BaseModel):
    path: str = "data/connector.db"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    # If set, /admin/* endpoints require the X-Admin-Token header.
    admin_token: str = ""


class Settings(BaseModel):
    timezone: str = "Asia/Jerusalem"
    syrve: SyrveConfig
    ontopo: OntopoConfig
    availability: AvailabilityConfig = Field(default_factory=AvailabilityConfig)
    reservations: ReservationsConfig = Field(default_factory=ReservationsConfig)
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
