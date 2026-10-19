"""Regression tests for symbols referenced by campaign admin endpoints."""

from app.routers import agent_admin


def test_campaign_admin_route_dependencies_are_imported():
    # campaign_detail uses Conversation/Direction when recipients have replies;
    # delete_campaign uses SQLAlchemy delete/update statements.
    assert agent_admin.Conversation is not None
    assert agent_admin.Direction is not None
    assert callable(agent_admin.delete)
    assert callable(agent_admin.update)
