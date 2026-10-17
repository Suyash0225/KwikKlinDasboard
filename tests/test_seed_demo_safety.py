"""Production guard tests for demo data seeders."""

import sys

import pytest

from app.config import settings
from scripts import seed_demo_data, seed_demo_shops


@pytest.mark.parametrize("operation", ["seed", "clear"])
async def test_seed_demo_data_operations_refuse_production(monkeypatch, operation):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(SystemExit, match="ENVIRONMENT=production"):
        if operation == "seed":
            await seed_demo_data.seed(1, 1)
        else:
            await seed_demo_data.clear()


@pytest.mark.parametrize("operation", ["seed", "clear"])
async def test_seed_demo_shops_operations_refuse_production(monkeypatch, operation):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(SystemExit, match="ENVIRONMENT=production"):
        if operation == "seed":
            await seed_demo_shops.seed(1, 1, 1, 1)
        else:
            await seed_demo_shops.clear()


def test_seed_demo_data_entrypoint_refuses_production(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(sys, "argv", ["seed_demo_data", "--clear"])
    with pytest.raises(SystemExit, match="ENVIRONMENT=production"):
        seed_demo_data.main()


def test_seed_demo_shops_entrypoint_refuses_production(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(sys, "argv", ["seed_demo_shops", "--clear"])
    with pytest.raises(SystemExit, match="ENVIRONMENT=production"):
        seed_demo_shops.main()
