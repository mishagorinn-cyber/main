from datetime import time
from pathlib import Path

import pytest

from connector.config import load_settings

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def test_example_config_loads(monkeypatch):
    for name in ("SYRVE_API_LOGIN", "SYRVE_ORGANIZATION_ID", "SYRVE_TERMINAL_GROUP_ID",
                 "ONTOPO_BASE_URL", "ONTOPO_API_KEY", "ONTOPO_VENUE_ID"):
        monkeypatch.setenv(name, f"value-{name}")
    monkeypatch.delenv("ONTOPO_WEBHOOK_SECRET", raising=False)
    s = load_settings(EXAMPLE)
    assert s.syrve.api_login == "value-SYRVE_API_LOGIN"
    assert s.ontopo.webhook_secret == ""
    assert s.availability.hours_for(4).open == time(11)      # Friday
    assert s.availability.hours_for(5).close == time(1)      # Saturday
    assert s.availability.hours_for(0).open == time(12)      # default


def test_missing_env_var_is_an_error(monkeypatch):
    monkeypatch.delenv("SYRVE_API_LOGIN", raising=False)
    with pytest.raises(ValueError, match="SYRVE_API_LOGIN"):
        load_settings(EXAMPLE)
