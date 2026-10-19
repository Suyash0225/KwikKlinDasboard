from app.services import agent_tools


def test_growth_review_is_registered_as_read_only_owner_tool() -> None:
    spec = next((item for item in agent_tools.TOOL_SPECS if item["name"] == "growth_review"), None)
    assert spec is not None
    assert "read-only" in spec["when"]
    assert "growth_review" in agent_tools._TOOLS
    assert "growth_review" not in agent_tools.WRITE_TOOLS
