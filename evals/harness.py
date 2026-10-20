"""Behaviour evals for the AI agents: build the real prompt, get an output, check it.

Four suites, one golden JSONL file each under evals/data/:

    customer  — the WhatsApp reply agent (ai_agent): intent, action, grounded
                prices, script mirroring, pickup intake, injection resistance
    owner     — the owner's assistant (bill_agent): which tool it reaches for
                first, and that customer text cannot trigger a write
    bill      — staff bill extraction (bill_agent): items, qty, phone, advance
    campaign  — marketing copy (marketing_agent): passes copy_guard

Prompts are built with the production builders (system prompt, FACTS
formatting, tool router, model choice), so an eval measures what customers
get. Only the inputs are synthetic: the database reads are replaced with the
case's own facts, and nothing is sent or written.

An output comes from either a live Gemini call (needs GEMINI_API_KEY) or a
saved JSONL file of earlier outputs (replay), so the same checks run in CI.
"""

from __future__ import annotations

import json
import re
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
SUITES = ("customer", "owner", "bill", "campaign")
# Synthetic only: the 99999 00xxx range is blocked from real sends.
EVAL_PHONE = "+919999900290"
EVAL_SHOP = {"name": "Kwik Klin", "city": "Varanasi"}


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise ValueError(f"{path}:{number}: each row must be an object with a string id")
            rows.append(row)
    return rows


def load_cases(suite: str) -> list[dict[str, Any]]:
    cases = read_jsonl(DATA_DIR / f"{suite}_cases.jsonl")
    seen: set[str] = set()
    for case in cases:
        if case["id"] in seen:
            raise ValueError(f"{suite}: duplicate case id {case['id']}")
        seen.add(case["id"])
        if not isinstance(case.get("expect"), dict):
            raise ValueError(f"{suite}/{case['id']}: 'expect' object is required")
    return cases


def _shop():
    from app.services.shop_identity import ShopIdentity

    return ShopIdentity(**EVAL_SHOP)


@contextmanager
def _patched(*triples):
    """Temporarily replace module attributes (the DB reads) with case data."""
    with ExitStack() as stack:
        for module, name, value in triples:
            original = getattr(module, name)
            setattr(module, name, value)
            stack.callback(setattr, module, name, original)
        yield


# --------------------------------------------------------------------------
# request builders — production prompt code, synthetic inputs
# --------------------------------------------------------------------------


async def build_request(suite: str, case: dict) -> dict:
    """{system, user_text, schema, model, max_tokens, history, context}."""
    return await _BUILDERS[suite](case)


async def _customer_request(case: dict) -> dict:
    from app.models import Customer
    from app.services import ai_agent, app_settings, customer_rhythm, llm_client, shop_identity
    from app.services.knowledge import knowledge_block

    ctx = case.get("context") or {}
    message = case["message"]
    customer = Customer(
        phone=EVAL_PHONE, name=ctx.get("name"), address=ctx.get("address"),
        ai_memory=ctx.get("memory"),
    )
    rates = ctx.get("rates") or []
    tool_data = {
        "get_customer_profile": {
            "name": ctx.get("name") or "", "address": ctx.get("address") or "",
            "phone": EVAL_PHONE,
        },
        "get_customer_orders": ctx.get("orders") or [],
        "get_customer_bills": ctx.get("bills") or [],
        "get_shop_rate_card": rates,
    }

    async def fake_tool(db, cust, name, **kwargs):
        return tool_data.get(name, [])

    async def fake_settings(db, *keys):
        shop_info = {
            "turnaround_days": 4, "shop_hours": "", "shop_address": "",
            "shop_contact_phone": "",
        }
        shop_info.update(ctx.get("settings") or {})
        return {key: shop_info.get(key, "") for key in keys}

    async def fake_rhythm(db, customer_id):
        return ctx.get("rhythm")

    with _patched(
        (ai_agent, "run_customer_tool", fake_tool),
        (app_settings, "get_many", fake_settings),
        (customer_rhythm, "load_customer_rhythm", fake_rhythm),
    ):
        tools = ai_agent._select_customer_tools_deterministic(message)
        facts = await ai_agent._build_facts(None, customer, tool_names=tools)

    faqs = [SimpleNamespace(question=k["q"], answer=k["a"]) for k in ctx.get("knowledge") or []]
    kb = knowledge_block(faqs, [], [])
    notes = ai_agent.customer_notes_block(customer)
    if notes:
        kb = f"{kb}\n\n{notes}" if kb else notes
    turns = [
        {"role": "model" if t["from"] == "shop" else "user", "text": t["text"]}
        for t in case.get("history") or []
    ]
    return {
        "system": shop_identity.render(ai_agent._COMPOSE_SYSTEM, _shop()),
        "user_text": ai_agent._build_prompt((facts, kb, turns), message, "auto"),
        "schema": ai_agent._REPLY_SCHEMA,
        "model": (
            llm_client.MODEL_SMART if ai_agent._needs_smart_customer_reply(message)
            else llm_client.MODEL_CHEAP
        ),
        "max_tokens": 1024,
        "history": turns,
        "context": {"facts": facts, "rates": rates, "turns": turns},
    }


async def _owner_request(case: dict) -> dict:
    from app.services import bill_agent, llm_client

    ctx = case.get("context") or {}
    lines = list(ctx.get("snapshot") or [])
    messages = ctx.get("customer_messages") or []
    lines.append(
        f"CUSTOMER MESSAGES (pichhle 24 ghante, {len(messages)}):"
        if messages else "CUSTOMER MESSAGES (pichhle 24 ghante): ek bhi nahi"
    )
    for m in messages:
        lines.append(f"- [{m['at']}] {m['who']} ({EVAL_PHONE}): {bill_agent._untrusted(m['text'], 140)}")
    staff = ctx.get("staff_chat") or []
    if staff:
        lines.append("STAFF CHAT (aakhri messages, IST time ke saath):")
    for m in staff:
        lines.append(f"- [{m['at']}] {m['who']}: {bill_agent._untrusted(m['text'], 110)}")
    convo = f"SNAPSHOT:\n" + "\n".join(lines) + "\n\n"
    if ctx.get("previous"):
        prev = ctx["previous"]
        convo += f"PICHHLI BAAT:\nOwner: {prev['owner']}\nTum: {prev['assistant']}\n\n"
    convo += f"OWNER'S MESSAGE:\n{case['message'][:500]}"
    from app.services import agent_tools

    return {
        "system": bill_agent._manager_system(_shop()),
        "user_text": convo,
        "schema": None,
        # production runs the owner agent on native function calling
        "declarations": agent_tools.function_declarations(),
        "model": llm_client.MODEL_SMART,
        "max_tokens": 700,
        "history": None,
        "context": {},
    }


async def _bill_request(case: dict) -> dict:
    from app.services import bill_agent, llm_client
    from app.utils.dates import today_ist

    ctx = case.get("context") or {}
    card = "\n".join(
        f"{r['service']}|{r['garment']}|{r['rate']}/{r.get('unit', 'pc')}"
        for r in ctx.get("rates") or []
    )
    if not (ctx.get("draft") is not None or bill_agent._BILL_CONTEXT_RE.search(case["message"])):
        card = ""  # production drops the card for non-bill commands
    return {
        "system": bill_agent._EXTRACT_SYSTEM,
        "user_text": bill_agent._extract_prompt(
            case["message"], card, ctx.get("draft"), ctx.get("history", ""),
            today_ist().isoformat(),
        ),
        "schema": bill_agent._EXTRACT_SCHEMA,
        "model": llm_client.MODEL_CHEAP,
        "max_tokens": 500,
        "history": None,
        "context": {},
    }


async def _campaign_request(case: dict) -> dict:
    from app.services import llm_client, marketing_agent

    ctx = case.get("context") or {}
    return {
        "system": marketing_agent._copy_system(_shop(), ctx.get("style", "")),
        "user_text": f"Segment: {ctx['segment']}. Offer: {ctx['offer']}. Draft the message.",
        "schema": None,
        "model": llm_client.MODEL_SMART,
        "max_tokens": 900,
        "history": None,
        "context": {"offer": ctx["offer"]},
    }


_BUILDERS = {
    "customer": _customer_request,
    "owner": _owner_request,
    "bill": _bill_request,
    "campaign": _campaign_request,
}


async def call_live(request: dict) -> Any:
    """One real Gemini call, bypassing the budget/usage DB (nothing persisted)."""
    from app.services import llm_client

    async def no_usage(*args, **kwargs):
        return None

    with _patched((llm_client, "_record_usage", no_usage)):
        if request.get("declarations"):
            return tool_turn_output(await llm_client._gemini_generate(
                request["system"], request["user_text"], request["model"],
                request["max_tokens"], None, None, None,
                tools={
                    "declarations": request["declarations"], "mode": "AUTO",
                    "contents": [{"role": "user", "parts": [{"text": request["user_text"]}]}],
                },
            ))
        text = await llm_client._gemini_generate(
            request["system"], request["user_text"], request["model"],
            request["max_tokens"], request["schema"], None, request["history"],
        )
    return json.loads(text) if request["schema"] is not None else text


def tool_turn_output(raw: str) -> dict:
    """A function-calling turn as {tool, args, answer} — the first call is
    what the checks judge; every call is kept for the report."""
    from app.services import agent_tools
    from app.services.llm_client import ToolTurn

    turn = ToolTurn(json.loads(raw))
    calls = [
        {"tool": c["name"], "args": agent_tools.args_from_call(c["name"], c["args"])}
        for c in turn.calls
    ]
    first = calls[0] if calls else {"tool": "", "args": ""}
    return {**first, "answer": turn.text, "calls": calls}


# --------------------------------------------------------------------------
# checks — each returns a list of failure reasons (empty = pass)
# --------------------------------------------------------------------------

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def _has_any(text: str, needles: list[str]) -> bool:
    lowered = (text or "").casefold()
    return any(n.casefold() in lowered for n in needles)


def _relative_day(label: str):
    from app.utils.dates import today_ist

    offsets = {"aaj": 0, "kal": 1, "parso": 2}
    return (today_ist() + timedelta(days=offsets[label])).isoformat()


def check(suite: str, case: dict, output: Any, context: dict | None = None) -> list[str]:
    if output is None:
        return ["no output"]
    try:
        return _CHECKS[suite](case, output, context or {})
    except Exception as exc:  # a malformed output is a failure, not a crash
        return [f"check crashed on output: {type(exc).__name__}: {exc}"]


def _check_customer(case: dict, out: dict, ctx: dict) -> list[str]:
    from app.services.ai_agent import amounts_are_grounded
    from app.services.copy_guard import copy_problems

    exp = case["expect"]
    fails: list[str] = []
    reply = str(out.get("reply") or "")
    if "intent_any" in exp and out.get("intent") not in exp["intent_any"]:
        fails.append(f"intent {out.get('intent')} not in {exp['intent_any']}")
    if "language" in exp and out.get("language") != exp["language"]:
        fails.append(f"language {out.get('language')} != {exp['language']}")
    if "action_any" in exp and out.get("action") not in exp["action_any"]:
        fails.append(f"action {out.get('action')} not in {exp['action_any']}")
    if out.get("action") in exp.get("action_not", []):
        fails.append(f"forbidden action {out.get('action')}")
    if "escalate" in exp and bool(out.get("escalate")) != exp["escalate"]:
        fails.append(f"escalate {bool(out.get('escalate'))} != {exp['escalate']}")
    if exp.get("reply_any") and not _has_any(reply, exp["reply_any"]):
        fails.append(f"reply has none of {exp['reply_any']}")
    for needle in exp.get("reply_all", []):
        if needle.casefold() not in reply.casefold():
            fails.append(f"reply missing '{needle}'")
    for needle in exp.get("reply_none", []):
        if needle.casefold() in reply.casefold():
            fails.append(f"reply must not contain '{needle}'")
    # Rule 11: mirror the customer's script. Default from the message itself.
    script = exp.get("script") or (
        "devanagari" if _DEVANAGARI.search(case["message"]) else "latin"
    )
    if script == "devanagari" and not _DEVANAGARI.search(reply):
        fails.append("reply not in Devanagari")
    if script == "latin" and _DEVANAGARI.search(reply):
        fails.append("reply switched to Devanagari")
    if exp.get("signoff", True) and _shop().signoff.casefold() not in reply.casefold():
        fails.append("missing signoff")
    if exp.get("grounded", True) and ctx:
        recent = " ".join(t["text"] for t in ctx.get("turns", []) if t["role"] == "user")
        if not amounts_are_grounded(
            ctx.get("rates", []), f"{recent} {case['message']}", reply, facts=ctx.get("facts", ""),
        ):
            fails.append("reply quotes an amount not grounded in FACTS")
    invented = [
        p for p in copy_problems(reply, allowed_text=(ctx or {}).get("facts", ""))
        if p.startswith(("link", "phone"))
    ]
    fails += [f"invented contact: {p}" for p in invented]
    intake = out.get("intake") or {}
    if "intake_ready" in exp and bool(intake.get("ready")) != exp["intake_ready"]:
        fails.append(f"intake.ready {bool(intake.get('ready'))} != {exp['intake_ready']}")
    if exp.get("intake_items_any") and not _has_any(
        str(intake.get("items_text") or ""), exp["intake_items_any"]
    ):
        fails.append(f"intake.items_text has none of {exp['intake_items_any']}")
    if "intake_pickup" in exp and intake.get("pickup_date") != _relative_day(exp["intake_pickup"]):
        fails.append(
            f"intake.pickup_date {intake.get('pickup_date')!r} != {exp['intake_pickup']}"
        )
    return fails


def _check_owner(case: dict, out: dict, ctx: dict) -> list[str]:
    exp = case["expect"]
    fails: list[str] = []
    tool = str(out.get("tool") or "").strip()
    answer = str(out.get("answer") or "")
    if "tool_any" in exp and tool not in exp["tool_any"]:
        fails.append(f"tool {tool or '(answer)'} not in {exp['tool_any']}")
    for called in [c["tool"] for c in out.get("calls") or []] or [tool]:
        if called in exp.get("tool_not", []):
            fails.append(f"forbidden tool {called}")
    if exp.get("args_any") and not _has_any(str(out.get("args") or ""), exp["args_any"]):
        fails.append(f"args have none of {exp['args_any']}")
    if not tool:
        if exp.get("answer_any") and not _has_any(answer, exp["answer_any"]):
            fails.append(f"answer has none of {exp['answer_any']}")
        for needle in exp.get("answer_none", []):
            if needle.casefold() in answer.casefold():
                fails.append(f"answer must not contain '{needle}'")
    return fails


def _check_bill(case: dict, out: dict, ctx: dict) -> list[str]:
    from app.services.bill_agent import _key

    exp = case["expect"]
    fails: list[str] = []
    if "action" in exp and out.get("action") != exp["action"]:
        fails.append(f"action {out.get('action')} != {exp['action']}")
    if "items" in exp:
        got = sorted(
            (_key(i.get("garment")), _key(i.get("service")), float(i.get("qty") or 0))
            for i in out.get("items") or []
        )
        want = sorted(
            (_key(i["garment"]), _key(i.get("service", "")), float(i["qty"]))
            for i in exp["items"]
        )
        # service is compared only when the case names one
        if len(got) != len(want):
            fails.append(f"items {got} != {want}")
        else:
            for (gg, gs, gq), (wg, ws, wq) in zip(got, want):
                if gg != wg or gq != wq or (ws and gs != ws):
                    fails.append(f"items {got} != {want}")
                    break
    if "phone_digits" in exp:
        digits = re.sub(r"\D", "", str(out.get("customer_phone") or ""))
        if not digits.endswith(exp["phone_digits"]):
            fails.append(f"phone {out.get('customer_phone')!r} != {exp['phone_digits']}")
    if "advance" in exp and float(out.get("advance") or 0) != float(exp["advance"]):
        fails.append(f"advance {out.get('advance')} != {exp['advance']}")
    if "name_any" in exp and not _has_any(str(out.get("customer_name") or ""), exp["name_any"]):
        fails.append(f"customer_name {out.get('customer_name')!r} not in {exp['name_any']}")
    for field, value in (exp.get("fields") or {}).items():
        if out.get(field) != value:
            fails.append(f"{field} {out.get(field)!r} != {value!r}")
    return fails


def _check_campaign(case: dict, out: str, ctx: dict) -> list[str]:
    from app.services.action_policy import business_policy_text
    from app.services.copy_guard import copy_problems

    exp = case["expect"]
    offer = (case.get("context") or {}).get("offer", "")
    fails = copy_problems(
        str(out), allowed_text=f"{offer} {business_policy_text()}",
        must_include=_shop().name, min_chars=80,
    )
    for needle in exp.get("copy_all", []):
        if needle.casefold() not in str(out).casefold():
            fails.append(f"copy missing '{needle}'")
    return fails


_CHECKS = {
    "customer": _check_customer,
    "owner": _check_owner,
    "bill": _check_bill,
    "campaign": _check_campaign,
}


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def score(results: list[dict]) -> dict:
    """results: {suite, id, tags, critical, failures} per attempt."""
    by_case: dict[tuple, list[dict]] = {}
    for r in results:
        by_case.setdefault((r["suite"], r["id"]), []).append(r)
    report: dict[str, Any] = {"suites": {}, "failures": []}
    for (suite, case_id), rows in sorted(by_case.items()):
        s = report["suites"].setdefault(suite, {
            "cases": 0, "attempts": 0, "passed_attempts": 0, "stable_cases": 0,
            "flaky_cases": 0, "critical_failures": 0, "per_tag": {},
        })
        oks = [not r["failures"] for r in rows]
        s["cases"] += 1
        s["attempts"] += len(oks)
        s["passed_attempts"] += sum(oks)
        s["stable_cases"] += all(oks)
        s["flaky_cases"] += any(oks) and not all(oks)
        if rows[0].get("critical") and not all(oks):
            s["critical_failures"] += 1
        for tag in rows[0].get("tags") or []:
            t = s["per_tag"].setdefault(tag, {"passed": 0, "total": 0})
            t["passed"] += sum(oks)
            t["total"] += len(oks)
        for r in rows:
            if r["failures"]:
                report["failures"].append(
                    {"suite": suite, "id": case_id, "attempt": r.get("attempt", 0),
                     "critical": bool(r.get("critical")), "why": r["failures"]}
                )
    for s in report["suites"].values():
        s["pass_rate"] = s["passed_attempts"] / s["attempts"] if s["attempts"] else 0.0
        s["stable_rate"] = s["stable_cases"] / s["cases"] if s["cases"] else 0.0
    return report


def gate(report: dict, min_pass: float, min_stable: float) -> tuple[bool, list[str]]:
    """Critical cases (safety) must pass every attempt; the rest by threshold."""
    reasons = []
    for suite, s in report["suites"].items():
        if s["critical_failures"]:
            reasons.append(f"{suite}: {s['critical_failures']} critical case(s) failed")
        if s["pass_rate"] < min_pass:
            reasons.append(f"{suite}: pass rate {s['pass_rate']:.2f} < {min_pass:.2f}")
        if s["stable_rate"] < min_stable:
            reasons.append(f"{suite}: stable rate {s['stable_rate']:.2f} < {min_stable:.2f}")
    return not reasons, reasons
