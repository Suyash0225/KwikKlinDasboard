"""Stand-ins for Gemini function calling in owner-agent tests."""

import json

from app.services.llm_client import ToolTurn


def tool_turn(*calls, text: str = "") -> ToolTurn:
    """A model turn: function calls as (name, args) pairs, or a text answer."""
    parts = [{"functionCall": {"name": n, "args": a}, "thoughtSignature": f"sig-{n}"} for n, a in calls]
    if text:
        parts.append({"text": text})
    return ToolTurn({"role": "model", "parts": parts})


def patch_tool_turns(monkeypatch, module, turns, *, seen=None, forced_text="", modes=None):
    """Feed `turns` to ask_with_tools in order; record each request.

    seen gets the conversation as JSON text per call, so a test can assert
    that a tool result was fed back. A forced (mode=NONE) call returns
    forced_text.
    """
    queue = list(turns)

    async def fake(*, system, contents, declarations, model=None, max_tokens=0, mode="AUTO"):
        if seen is not None:
            seen.append(json.dumps(contents, ensure_ascii=False))
        if modes is not None:
            modes.append(mode)
        if mode == "NONE":
            return tool_turn(text=forced_text)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(module.llm_client, "ask_with_tools", fake)
