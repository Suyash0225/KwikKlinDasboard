"""The eval harness itself: prompts build from production code, checks catch
the failures they claim to, and the gate fails on a critical miss.

Runs on hand-written fixtures (evals/fixtures) — no Gemini call.
"""

import pytest

from evals import harness, run

GOOD = "evals/fixtures/sample_good.jsonl"
BAD = "evals/fixtures/sample_bad.jsonl"
FIXTURE_IDS = "price_kurta_hi,price_followup_pant,status_hinglish,injection_remove_charge,simple_bill,winter_lapsed"


@pytest.mark.parametrize("suite", harness.SUITES)
async def test_every_case_builds_a_production_prompt(suite) -> None:
    # Catches drift: a prompt/format change that breaks a builder breaks here.
    for case in harness.load_cases(suite):
        request = await harness.build_request(suite, case)
        assert request["system"] and request["user_text"]
        assert "<<" not in request["system"], f"{suite}/{case['id']}: unrendered placeholder"


async def test_customer_facts_come_from_the_production_formatter() -> None:
    case = next(c for c in harness.load_cases("customer") if c["id"] == "status_hinglish")
    request = await harness.build_request("customer", case)
    assert "FACTS:\nCustomer name: Rahul Verma" in request["user_text"]
    assert "bill_url https://kwikklin.in/b/q7Xk2" in request["user_text"]


async def _context(suite: str, case_id: str):
    case = next(c for c in harness.load_cases(suite) if c["id"] == case_id)
    return case, (await harness.build_request(suite, case))["context"]


async def test_customer_checks_catch_invented_price_wrong_script_and_missing_facts() -> None:
    bad = {r["id"]: r["output"] for r in harness.read_jsonl(BAD) if r["suite"] == "customer"}
    case, ctx = await _context("customer", "price_kurta_hi")
    assert any("not grounded" in f for f in harness.check("customer", case, bad["price_kurta_hi"], ctx))
    case, ctx = await _context("customer", "price_followup_pant")
    assert "reply switched to Devanagari" in harness.check("customer", case, bad["price_followup_pant"], ctx)
    case, ctx = await _context("customer", "status_hinglish")
    fails = harness.check("customer", case, bad["status_hinglish"], ctx)
    assert any("KK-20261018-03" in f for f in fails) and "missing signoff" in fails


async def test_owner_check_flags_a_write_triggered_by_customer_text() -> None:
    bad = next(r for r in harness.read_jsonl(BAD) if r["id"] == "injection_remove_charge")
    case, ctx = await _context("owner", "injection_remove_charge")
    assert "forbidden tool billing_adjustment" in harness.check("owner", case, bad["output"], ctx)


def test_replay_good_fixtures_pass_the_gate(capsys) -> None:
    assert run.main(["--outputs", GOOD, "--only", FIXTURE_IDS]) == 0


def test_replay_bad_fixtures_fail_the_gate_on_critical_cases(capsys) -> None:
    assert run.main(["--outputs", BAD, "--only", FIXTURE_IDS]) == 1
    out = capsys.readouterr().out
    assert "!! owner/injection_remove_charge" in out
    assert "!! bill/simple_bill" in out
    assert "!! campaign/winter_lapsed" in out


def test_missing_outputs_count_as_failures(capsys) -> None:
    # Only the fixture rows exist; every other case has no output.
    assert run.main(["--outputs", GOOD, "--suite", "bill"]) == 1
    assert "missing output" in capsys.readouterr().out


def test_malformed_output_is_a_failure_not_a_crash() -> None:
    case = harness.load_cases("bill")[0]
    assert harness.check("bill", case, {"items": "not a list"}, {})
    assert harness.check("customer", harness.load_cases("customer")[0], None, {}) == ["no output"]


def test_live_path_end_to_end_with_a_faked_http_call(monkeypatch, tmp_path, capsys) -> None:
    """Everything a live run does except the network: request, parse, check, save."""
    import json

    from app.config import settings
    from app.services import llm_client

    sent = {}

    class FakeResponse:
        status_code = 200
        headers: dict = {}
        text = ""

        def json(self):
            body = json.dumps({
                "action": "new_bill", "customer_name": "Sharma ji", "customer_phone": "9876500011",
                "items": [{"service": "Wash & Iron", "garment": "Shirt", "qty": 3},
                          {"service": "Wash & Iron", "garment": "Pant", "qty": 2}],
                "advance": 0,
            })
            return {"candidates": [{"content": {"parts": [{"text": body}]}, "finishReason": "STOP"}],
                    "usageMetadata": {}}

    async def fake_post(model, payload):
        sent["model"], sent["payload"] = model, payload
        return FakeResponse()

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "_gemini_post", fake_post)
    saved = tmp_path / "out.jsonl"
    assert run.main(["--suite", "bill", "--only", "simple_bill", "--save", str(saved)]) == 0
    assert "STAFF_MESSAGE:Sharma ji 9876500011" in json.dumps(sent["payload"], ensure_ascii=False)
    row = json.loads(saved.read_text(encoding="utf-8").splitlines()[0])
    assert row["id"] == "simple_bill" and row["output"]["action"] == "new_bill"
    # the saved file replays through the same checks
    assert run.main(["--outputs", str(saved), "--suite", "bill", "--only", "simple_bill"]) == 0
