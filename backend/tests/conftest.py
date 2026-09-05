from __future__ import annotations

import sys
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import create_app
from app.config import TestConfig


class StaticRates:
    name = "test-rates"

    def get_rates(self, _base: str, _targets: list[str]) -> dict[str, float]:
        return {"USD": 1.2, "INR": 100.0}


@pytest.fixture()
def app():
    application = create_app(TestConfig)
    application.extensions["exchange_rate_service"].provider = StaticRates()
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def authenticated(client):
    response = client.post("/api/auth/demo", json={})
    assert response.status_code == 200
    response = client.post("/api/companies/select", json={"company_id": "company-moneda-demo"})
    assert response.status_code == 200
    return client
