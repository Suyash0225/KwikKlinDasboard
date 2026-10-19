"""Regression tests for campaign admin endpoints' runtime dependencies."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.routers import agent_admin


@pytest.mark.asyncio
async def test_campaign_detail_resolves_inbound_reply_lookup(monkeypatch):
    campaign_id = uuid4()
    campaign = SimpleNamespace(
        id=campaign_id, name="Test", segment="all_active", stats={},
        campaign_type="promotion", rule_flags={}, status="sent", message_text="Hi",
        created_by="owner", created_at=datetime.now(timezone.utc),
        scheduled_at=None, sent_at=None,
    )
    recipient = SimpleNamespace(
        id=uuid4(), customer_id=uuid4(), status="replied", detail=None,
        wa_message_id="wamid.test", updated_at=datetime.now(timezone.utc),
    )
    customer = SimpleNamespace(id=recipient.customer_id, name="Customer", phone="+919999999999")

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class DB:
        def __init__(self):
            self.results = [Result([(recipient, customer)]), Result([
                ("wamid.test", "Thanks", datetime.now(timezone.utc))
            ])]

        async def get(self, model, key):
            return campaign

        async def execute(self, statement):
            return self.results.pop(0)

    monkeypatch.setattr(agent_admin, "campaign_stats", AsyncMock(return_value={"replied": 1}))
    result = await agent_admin.campaign_detail(str(campaign_id), DB())
    assert result["recipients"][0]["reply"]["text"] == "Thanks"


@pytest.mark.asyncio
async def test_delete_campaign_builds_related_cleanup_statements(monkeypatch):
    campaign_id = uuid4()
    campaign = SimpleNamespace(id=campaign_id, status="draft", stats={}, name="Draft")

    class DB:
        def __init__(self):
            self.statements = []
            self.deleted = None
            self.committed = False

        async def get(self, model, key):
            return campaign

        async def execute(self, statement):
            self.statements.append(statement)

        async def delete(self, obj):
            self.deleted = obj

        async def commit(self):
            self.committed = True

    db = DB()
    monkeypatch.setattr(agent_admin.audit, "record", AsyncMock())
    result = await agent_admin.delete_campaign(str(campaign_id), db)
    assert result == {"ok": True, "id": str(campaign_id)}
    assert len(db.statements) == 2
    assert db.deleted is campaign
    assert db.committed
