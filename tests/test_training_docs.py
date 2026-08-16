"""Document upload training: upload -> chunks -> retrieval -> delete."""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.services.knowledge import (
    DOC_SCORE_FLOOR,
    DOC_TOP_K,
    knowledge_block,
    rank_knowledge,
    relevant_knowledge,
)

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
DOC = "test-pricelist.txt"
RANK_DOC = "test-ranking.txt"


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    async with async_session_factory() as s:
        await s.execute(
            sqltext("DELETE FROM doc_chunks WHERE document IN (:a, :b)"),
            {"a": DOC, "b": RANK_DOC},
        )
        await s.commit()


async def test_upload_retrieve_and_delete(client) -> None:
    body = (
        "Kwik Klin premium services.\n\n"
        "Sofa cover dry clean: 500 rupees per seat, 3 din ka time.\n\n"
        "Curtain wash and press: 100 rupees per panel.\n\n"
        "Woollen blanket / razai dry clean: 350 rupees, winter special."
    )
    r = await client.post(
        "/admin/api/training/upload",
        files={"file": (DOC, body.encode(), "text/plain")},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    assert r.json()["chunks"] >= 1

    # appears in the list
    docs = (await client.get("/admin/api/training/docs", headers=AUTH)).json()
    assert any(d["document"] == DOC for d in docs)

    # the agent's retrieval finds the matching chunk for a real question
    async with async_session_factory() as db:
        faqs, corr, chunks = await relevant_knowledge(
            db, "sofa cover dry clean ka kya rate hai?", audience="customer"
        )
    assert chunks and "500" in chunks[0].content
    kb = knowledge_block(faqs, corr, chunks)
    assert DOC in kb and "Sofa cover" in kb

    # bad extension refused
    r = await client.post(
        "/admin/api/training/upload",
        files={"file": ("x.exe", b"nope", "application/octet-stream")},
        headers=AUTH,
    )
    assert r.status_code == 400

    # delete removes it
    r = await client.delete(f"/admin/api/training/docs/{DOC}", headers=AUTH)
    assert r.status_code == 200
    async with async_session_factory() as db:
        _, _, chunks = await relevant_knowledge(
            db, "sofa cover dry clean rate", audience="customer"
        )
    assert not any(c.document == DOC for c in chunks)


# Each paragraph has to clear the ~700-char packer on its own, otherwise the
# uploader merges all three into one chunk and there is nothing to lose.
_PAD = " Kwik Klin Varanasi ki dukaan par yahi seva di jaati hai roz." * 13


async def test_rank_knowledge_keeps_the_losing_rows(client) -> None:
    """Retrieval metrics are computed from the rows that did NOT make it.

    relevant_knowledge() only ever returns winners, so Hit-Rate and MRR
    cannot be derived from it — a query that retrieved nothing and a query
    whose right answer scored 0.14 against a 0.15 floor look identical.
    rank_knowledge() exists to keep the losers, and must never disagree with
    relevant_knowledge() about which rows actually reached the model.
    """
    body = "\n\n".join(
        f"{topic}.{_PAD}"
        for topic in (
            "Sofa cover dry clean 500 rupaye per seat",
            "Curtain wash and press 100 rupaye per panel",
            "Woollen blanket razai dry clean 350 rupaye",
        )
    )
    r = await client.post(
        "/admin/api/training/upload",
        files={"file": (RANK_DOC, body.encode(), "text/plain")},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    assert r.json()["chunks"] > DOC_TOP_K, "need more chunks than top_k to have losers"

    question = "sofa cover dry clean ka kya rate hai?"
    async with async_session_factory() as db:
        faqs, corr, chunks = await relevant_knowledge(db, question, audience="customer")
        ranked = await rank_knowledge(db, question, audience="customer", limit=20)

    # the contract an eval harness leans on: same query, same winners
    assert set(ranked["retrieved_ids"]) == {
        str(row.id) for row in (*faqs, *corr, *chunks)
    }

    for key in ("faqs", "corrections", "doc_chunks"):
        cands = ranked[key]
        scores = [c["score"] for c in cands]
        assert scores == sorted(scores, reverse=True), f"{key} not ranked best-first"
        for c in cands:
            assert not (c["retrieved"] and not c["above_floor"])

    # losers survive, with the score that decided them — this is the number
    # you tune the floor against
    docs = ranked["doc_chunks"]
    lost = [c for c in docs if not c["retrieved"]]
    assert lost, "rank_knowledge dropped the rows it exists to report"
    assert all(isinstance(c["score"], float) for c in lost)
    assert ranked["params"]["doc_floor"] == DOC_SCORE_FLOOR


async def test_upload_requires_auth(client) -> None:
    r = await client.post(
        "/admin/api/training/upload",
        files={"file": (DOC, b"hello", "text/plain")},
    )
    assert r.status_code == 401
