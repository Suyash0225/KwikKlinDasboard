"""Non-blocking Gemini quality judge for customer-facing AI replies.

This agent is deliberately outside the customer reply path. It reads the
customer message + facts/knowledge/history + the production agent decision
and reply, then records PASS/FAIL and the concrete reason in AuditLog.
"""
import json
import time

import httpx
import structlog

from app.database import async_session_factory
from app.services import app_settings, audit

log = structlog.get_logger()

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_QA_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["PASS", "FAIL", "REVIEW"]},
        "decision_correct": {"type": "boolean"},
        "reply_correct": {"type": "boolean"},
        "knowledge_correct": {"type": "boolean"},
        "severity": {"type": "string", "enum": ["none", "low", "medium", "high", "critical"]},
        "issue": {"type": "string"},
        "reason": {"type": "string"},
        "suggested_correction": {"type": "string"},
        "agent_to_fix": {"type": "string", "enum": ["none", "service", "decision", "task", "knowledge", "provider"]},
    },
    "required": [
        "status", "decision_correct", "reply_correct", "knowledge_correct",
        "severity", "issue", "reason", "suggested_correction", "agent_to_fix",
    ],
    "additionalProperties": False,
}


async def _call_gemini(*, api_key: str, model: str, prompt: str) -> dict:
    payload = {
        "system_instruction": {
            "parts": [{
                "text": (
                    "You are Kwik Klin's AI Quality Judge. Review production AI behavior, "
                    "not the customer. Be strict and factual. Use ONLY the supplied facts, "
                    "knowledge and conversation. Do not invent business rules. A response "
                    "is FAIL if it states unsupported prices/dates/policies, misunderstands "
                    "the customer's intent, misses a clear lead, or contains a material "
                    "customer-support error. If evidence is insufficient, use REVIEW."
                )
            }]
        },
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _QA_SCHEMA,
            "maxOutputTokens": 2048,
        },
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_GEMINI_BASE}/{model}:generateContent",
                params={"key": api_key},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Gemini connection error: {str(exc)[:180]}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    candidates = data.get("candidates") or []
    parts = ((candidates[0].get("content") or {}).get("parts") if candidates else None) or []
    text = "".join(str(p.get("text") or "") for p in parts).strip()
    if not text:
        raise RuntimeError("Gemini returned empty QA response")
    result = json.loads(text)
    log.info(
        "ai_qa_completed",
        provider="gemini",
        model=model,
        status=result.get("status"),
        severity=result.get("severity"),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return result


async def judge_customer_turn(
    *,
    customer_phone: str,
    customer_text: str,
    facts: str,
    knowledge: str,
    history: str,
    agent_output: dict,
    purpose: str = "customer_reply",
) -> None:
    """Review one turn. Never raises into the customer-response path."""
    try:
        async with async_session_factory() as db:
            api_key = str(await app_settings.get(db, "ai_qa_api_key") or "").strip()
            model = str(await app_settings.get(db, "ai_qa_model") or "gemini-3.5-flash-lite").strip()
            if not api_key:
                return

        prompt = (
            f"PURPOSE: {purpose}\n"
            f"CUSTOMER MESSAGE:\n{customer_text[:2000]}\n\n"
            f"FACTS FROM DATABASE:\n{facts[:7000]}\n\n"
            f"BUSINESS KNOWLEDGE:\n{knowledge[:7000]}\n\n"
            f"RECENT CONVERSATION:\n{history[:5000]}\n\n"
            f"PRODUCTION AGENT DECISION:\n{json.dumps(agent_output, ensure_ascii=False)[:5000]}\n\n"
            "Judge whether the decision and customer-facing reply are correct."
        )
        result = await _call_gemini(api_key=api_key, model=model, prompt=prompt)

        await audit.record(
                actor_role="system",
                actor="gemini-qa",
                action="ai_qa",
                args={
                    "phone": customer_phone,
                    "purpose": purpose,
                    "status": result.get("status"),
                    "severity": result.get("severity"),
                    "decision_correct": result.get("decision_correct"),
                    "reply_correct": result.get("reply_correct"),
                    "knowledge_correct": result.get("knowledge_correct"),
                    "agent_to_fix": result.get("agent_to_fix"),
                    "issue": str(result.get("issue") or "")[:500],
                },
                result=str(result.get("reason") or "")[:1000],
            )
    except Exception:
        # QA can never make the customer's actual reply fail.
        log.exception("ai_qa_failed", phone=customer_phone)
