"""Owner AI must expose real analytics and a safe marketing draft workflow."""

from app.services import agent_tools


def test_owner_tools_expose_analytics_and_safe_marketing_workflow():
    names = {tool["name"] for tool in agent_tools.TOOL_SPECS}
    assert "garment_demand" in names
    assert "marketing_followup" in names
    assert "garment_demand" in agent_tools._TOOLS
    assert "marketing_followup" in agent_tools._TOOLS
    assert "marketing_followup" in agent_tools.WRITE_TOOLS
