"""Regression tests for the WAHA-only WhatsApp stats endpoint.

The application intentionally uses WAHA/NOWEB. The stats endpoint reports
traffic from the conversation database and must not call Meta Graph APIs or
expose stale Meta template/quality data.
"""

import pytest

from app.config import settings

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
STATS = "/admin/api/whatsapp/stats"


async def test_stats_reports_database_traffic_and_waha_provider(client) -> None:
    response = await client.get(STATS, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert set(body["today"]) == {"sent", "received", "customers_talked"}
    assert all(isinstance(value, int) and value >= 0 for value in body["today"].values())
    assert body["provider"] == "waha"
    assert body["templates"] == {"approved": 0, "pending": 0, "rejected": 0}
    assert body["quality"] is None
    assert body["meta_ok"] is None
    assert body["meta_state"] == "disabled"


async def test_stats_does_not_depend_on_meta_credentials(client, monkeypatch) -> None:
    # Meta credentials are not used by the configured WAHA/NOWEB transport.
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN", "")
    monkeypatch.setattr(settings, "WHATSAPP_WABA_ID", "")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "")

    response = await client.get(STATS, headers=AUTH)

    assert response.status_code == 200
    assert response.json()["provider"] == "waha"
    assert response.json()["meta_state"] == "disabled"


async def test_stats_is_available_without_a_meta_graph_call(client, monkeypatch) -> None:
    import app.services.wa_templates as wa_templates

    async def unexpected_graph_call(*args, **kwargs):
        pytest.fail("WAHA stats must not call the Meta Graph API")

    monkeypatch.setattr(wa_templates, "graph", unexpected_graph_call)

    response = await client.get(STATS, headers=AUTH)

    assert response.status_code == 200
