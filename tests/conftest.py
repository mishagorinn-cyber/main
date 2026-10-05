from __future__ import annotations

import httpx
import pytest

from connector.ontopo.client import OntopoClient
from connector.service import Connector
from connector.storage import Store
from connector.syrve.client import SyrveClient

from .fakes import FakeOntopo, FakeSyrve, make_settings


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def fake_syrve():
    return FakeSyrve()


@pytest.fixture
def fake_ontopo():
    return FakeOntopo()


@pytest.fixture
def connector(settings, fake_syrve, fake_ontopo):
    syrve = SyrveClient(
        settings.syrve, http=httpx.AsyncClient(base_url="https://syrve.test", transport=fake_syrve.transport())
    )
    ontopo = OntopoClient(
        settings.ontopo, http=httpx.AsyncClient(base_url="https://ontopo.test", transport=fake_ontopo.transport())
    )
    return Connector(settings, syrve=syrve, ontopo=ontopo, store=Store(":memory:"))
