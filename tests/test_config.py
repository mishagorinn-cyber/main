from pathlib import Path

import pytest

from connector.config import load_settings

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"
REQUIRED = ("SYRVE_API_LOGIN", "SYRVE_ORGANIZATION_ID", "SYRVE_TERMINAL_GROUP_ID", "ONTOPO_API_KEY")


def test_example_config_loads(monkeypatch):
    for name in REQUIRED:
        monkeypatch.setenv(name, f"value-{name}")
    for name in ("ONTOPO_BASE_URL", "ONTOPO_WEBHOOK_TOKEN", "ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    s = load_settings(EXAMPLE)
    assert s.syrve.api_login == "value-SYRVE_API_LOGIN"
    assert s.ontopo.api_key == "value-ONTOPO_API_KEY"
    assert s.ontopo.base_url == "https://top-openapi.staging-01.ontopo.cz"
    assert s.ontopo.webhook_token == ""


def test_missing_env_var_is_an_error(monkeypatch):
    monkeypatch.delenv("SYRVE_API_LOGIN", raising=False)
    with pytest.raises(ValueError, match="SYRVE_API_LOGIN"):
        load_settings(EXAMPLE)
