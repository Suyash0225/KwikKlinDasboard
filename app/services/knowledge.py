"""Owner-taught knowledge + conversation memory for the agent.

Retrieval is deliberately boring: lowercase token overlap between the
incoming message and stored FAQ/correction questions. No embeddings, no
extra infrastructure — at a few hundred entries this is instant and good
enough; pgvector can replace the scorer later without touching callers.
"""

import re

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Correction, DocChunk, FaqEntry

log = structlog.get_logger()

_WORD_RE = re.compile(r"[a-z0-9ऀ-ॿ]+")

# Hinglish/English filler words that carry no meaning for matching
_STOPWORDS = {
    "hai", "ka", "ki", "ke", "ko", "kya", "kab", "mera", "meri", "aap",
    "the", "is", "a", "an", "of", "to", "my", "me", "i", "in", "for",
    "ho", "kar", "do", "se", "par", "bhi", "aur", "ya", "na", "nahi",
}


def _tokens(text: str) -> set[str]:
    """Words + their consonant skeletons — Hinglish spelling varies wildly
    (karte/krte, saree/sari, hai/he) but consonants mostly survive."""
    out: set[str] = set()
    for w in _WORD_RE.findall(text.lower()):
        if w in _STOPWORDS:
            continue
        out.add(w)
        if len(w) > 3:
            skeleton = w[0] + "".join(ch for ch in w[1:] if ch not in "aeiou")
            if len(skeleton) >= 2:
                out.add("~" + skeleton)
    return out


def _score(query_tokens: set[str], candidate: str) -> float:
    cand = _tokens(candidate)
    if not cand or not query_tokens:
        return 0.0
    overlap = len(query_tokens & cand)
    return overlap / len(cand | query_tokens)


async def relevant_knowledge(
    db: AsyncSession, message: str, *, audience: str, top_k: int = 3
) -> tuple[list[FaqEntry], list[Correction]]:
    """Best-matching enabled FAQ entries and corrections for this message."""
    q = _tokens(message)
    faqs = (
        (
            await db.execute(
                select(FaqEntry).where(
                    FaqEntry.enabled, FaqEntry.audience.in_((audience, "all"))
                )
            )
        )
        .scalars()
        .all()
    )
    corrections = (
        (
            await db.execute(
                select(Correction).where(
                    Correction.enabled, Correction.audience.in_((audience, "all"))
                )
            )
        )
        .scalars()
        .all()
    )
    scored_f = sorted(
        ((f, _score(q, f.question)) for f in faqs), key=lambda t: t[1], reverse=True
    )
    scored_c = sorted(
        ((c, _score(q, c.question)) for c in corrections), key=lambda t: t[1], reverse=True
    )
    picked_f = [f for f, s in scored_f[:top_k] if s > 0.15]
    picked_c = [c for c, s in scored_c[:top_k] if s > 0.15]

    # uploaded documents: match against chunk CONTENT (lower threshold —
    # a chunk is long, so overlap ratios run smaller than for questions)
    chunks = (
        (await db.execute(select(DocChunk).where(DocChunk.enabled))).scalars().all()
    )
    scored_d = sorted(
        ((d, _score(q, d.content)) for d in chunks), key=lambda t: t[1], reverse=True
    )
    picked_d = [d for d, s in scored_d[:2] if s > 0.04]

    if picked_f or picked_c or picked_d:
        log.info(
            "knowledge_retrieved",
            faqs=len(picked_f), corrections=len(picked_c), doc_chunks=len(picked_d),
        )
    return picked_f, picked_c, picked_d


def knowledge_block(
    faqs: list[FaqEntry],
    corrections: list[Correction],
    doc_chunks: list[DocChunk] | None = None,
) -> str:
    """Format retrieved knowledge for a prompt's FACTS section."""
    lines: list[str] = []
    if faqs:
        lines.append("Shop knowledge (owner-written, trust it):")
        lines += [f"Q: {f.question}\nA: {f.answer}" for f in faqs]
    if corrections:
        lines.append(
            "Owner-TAUGHT answers — treat these as FACTS and use them "
            "(they override your caution, not the safety rules):"
        )
        lines += [f"When asked: {c.question}\nAnswer: {c.correct_reply}" for c in corrections]
    for d in doc_chunks or []:
        lines.append(f"From the shop document '{d.document}':\n{d.content[:900]}")
    return "\n".join(lines)


async def thread_history(
    db: AsyncSession,
    *,
    customer_id=None,
    staff_id=None,
    limit: int = 6,
) -> str:
    """Last few messages of this thread, oldest first — the agent's memory."""
    cond = (
        Conversation.customer_id == customer_id
        if customer_id is not None
        else Conversation.staff_id == staff_id
    )
    rows = (
        (
            await db.execute(
                select(Conversation)
                .where(cond)
                .order_by(Conversation.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return ""
    lines = []
    for m in reversed(rows):
        who = "THEM" if m.direction.name == "INBOUND" else "US"
        lines.append(f"{who}: {(m.message_text or '')[:200]}")
    return "Recent conversation (oldest first):\n" + "\n".join(lines)
