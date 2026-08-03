"""Document upload training: upload -> chunks -> retrieval -> delete."""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.services.knowledge import knowledge_block, relevant_knowledge

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
DOC = "test-pricelist.txt"


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    async with async_session_factory() as s:
        await s.execute(sqltext(f"DELETE FROM doc_chunks WHERE document = '{DOC}'"))
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


async def test_upload_requires_auth(client) -> None:
    r = await client.post(
        "/admin/api/training/upload",
        files={"file": (DOC, b"hello", "text/plain")},
    )
    assert r.status_code == 401
