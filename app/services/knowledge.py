"""Owner-taught knowledge + conversation memory for the agent.

Retrieval is deliberately boring: lowercase token overlap between the
incoming message and stored FAQ/correction questions. No embeddings, no
extra infrastructure — at a few hundred entries this is instant and good
enough; pgvector can replace the scorer later without touching callers.

Retrieval is also the first thing to blame when a reply is wrong, so every
lookup logs WHICH rows it picked and at what score — and a lookup that picks
nothing logs the best score it rejected, which is the difference between a
floor set too high and an answer the owner never wrote. rank_knowledge()
exposes the same scoring with the losers included, for measuring recall.

Two things keep "boring" from becoming "slow" as the knowledge base grows:
- The corpus is CACHED per tenant, not re-SELECTed on every message. A
  doc-heavy shop was pulling every 4 KB chunk out of Postgres just to
  answer 'kitna lagega'.
- Each entry's token set is computed ONCE, when it enters the cache, not
  re-tokenised against every incoming message.
Writers call invalidate(); the TTL is only a safety net for a missed one.
"""

import re
import time

import structlog
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import Conversation, Correction, DocChunk, FaqEntry

log = structlog.get_logger()

# How long a cached corpus may be stale if some writer forgets invalidate().
# Short enough that "Teach me" still feels live, long enough that a busy
# thread doesn't re-read the tables message after message.
_CACHE_TTL_SECONDS = 60.0

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


def _score(query_tokens: set[str], cand_tokens: set[str]) -> float:
    """Jaccard overlap. Both sides are pre-tokenised — see _corpus()."""
    if not cand_tokens or not query_tokens:
        return 0.0
    return len(query_tokens & cand_tokens) / len(cand_tokens | query_tokens)


def _rank(
    entries: list[tuple[object, set[str]]], query: set[str], top_k: int, floor: float
) -> list[tuple[object, float]]:
    """Top-k (row, score) pairs above the floor, best first.

    A partial sort: we only ever want 2-3 winners out of possibly thousands
    of entries, so scoring is O(n) and only the survivors get ordered.

    Scores come back with the rows because retrieval quality can only be
    measured — or tuned — against the number that decided each row's fate.
    """
    hits = [(row, s) for row, toks in entries if (s := _score(query, toks)) > floor]
    hits.sort(key=lambda t: t[1], reverse=True)
    return hits[:top_k]


def _top_score(entries: list[tuple[object, set[str]]], query: set[str]) -> float:
    """Best score in the corpus, floor ignored.

    Only computed when a lookup found NOTHING: 'nothing matched, and the
    closest entry scored 0.14 against a 0.15 floor' is a floor that needs
    lowering, while 'the closest scored 0.01' is a knowledge base that never
    had the answer. Same empty reply, opposite fix.
    """
    return max((_score(query, toks) for _, toks in entries), default=0.0)


# tenant_id -> (expires_at, faqs, corrections, chunks); each list holds
# (row, token_set) pairs so tokenisation happens once per WRITE, not per read.
_corpus_cache: dict[object, tuple] = {}


def invalidate() -> None:
    """Owner taught/edited/deleted something — drop the cached corpus.

    Cheap and global on purpose: teaching is rare, reading is constant, and
    a wrongly-kept cache is worse than a wrongly-dropped one.
    """
    _corpus_cache.clear()


_CACHED_MODELS = (FaqEntry, Correction, DocChunk)


@event.listens_for(Session, "after_flush")
def _drop_cache_on_knowledge_write(session, _flush_context) -> None:
    """Any ORM write to a knowledge table busts the cache — automatically.

    Deliberately an event and not a call at each write site: teaching
    happens from the dashboard, from 'sikha do' on WhatsApp, and from the
    Teach-me queue. One forgotten call there would mean the owner teaches
    the agent and the agent keeps giving the old answer.
    """
    for bucket in (session.new, session.dirty, session.deleted):
        if any(isinstance(obj, _CACHED_MODELS) for obj in bucket):
            _corpus_cache.clear()
            return


def _cache_key():
    from app.services import tenant_context

    return tenant_context.current_tenant_id.get() or tenant_context.cached_home_tenant_id()


async def _corpus(db: AsyncSession) -> tuple[list, list, list]:
    """Every enabled knowledge row, tokenised, for the current tenant."""
    key = _cache_key()
    cached = _corpus_cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1], cached[2], cached[3]

    faqs = (await db.execute(select(FaqEntry).where(FaqEntry.enabled))).scalars().all()
    corr = (await db.execute(select(Correction).where(Correction.enabled))).scalars().all()
    chunks = (await db.execute(select(DocChunk).where(DocChunk.enabled))).scalars().all()

    entry = (
        time.monotonic() + _CACHE_TTL_SECONDS,
        [(f, _tokens(f.question)) for f in faqs],
        [(c, _tokens(c.question)) for c in corr],
        [(d, _tokens(d.content)) for d in chunks],
    )
    _corpus_cache[key] = entry
    log.info(
        "knowledge_corpus_loaded",
        faqs=len(faqs), corrections=len(corr), doc_chunks=len(chunks),
    )
    return entry[1], entry[2], entry[3]


# Retrieval knobs, named because they are the two numbers anyone tuning
# recall-vs-precision actually reaches for. A chunk is long, so its overlap
# ratios run far smaller than a one-line question's — hence its own floor.
FAQ_SCORE_FLOOR = 0.15
DOC_SCORE_FLOOR = 0.04
DOC_TOP_K = 2


def _preview(row: object, n: int = 80) -> str:
    """One short line identifying a retrieved row in a log."""
    text = (getattr(row, "question", None) or getattr(row, "content", "") or "")
    text = " ".join(text.split())[:n]
    doc = getattr(row, "document", None)
    return f"{doc}: {text}" if doc else text


def _hits_out(hits: list[tuple[object, float]]) -> list[dict]:
    return [
        {"id": str(row.id), "score": round(score, 3), "text": _preview(row)}
        for row, score in hits
    ]


async def relevant_knowledge(
    db: AsyncSession, message: str, *, audience: str, top_k: int = 3
) -> tuple[list[FaqEntry], list[Correction], list[DocChunk]]:
    """Best-matching enabled FAQs, corrections and document chunks."""
    q = _tokens(message)
    if not q:
        log.info("knowledge_skipped", reason="no_query_tokens", query=message[:80])
        return [], [], []
    faqs, corrections, chunks = await _corpus(db)

    aud = (audience, "all")
    pool_f = [e for e in faqs if e[0].audience in aud]
    pool_c = [e for e in corrections if e[0].audience in aud]

    hits_f = _rank(pool_f, q, top_k, FAQ_SCORE_FLOOR)
    hits_c = _rank(pool_c, q, top_k, FAQ_SCORE_FLOOR)
    hits_d = _rank(chunks, q, DOC_TOP_K, DOC_SCORE_FLOOR)

    if hits_f or hits_c or hits_d:
        # WHICH rows, not how many. A count says the agent read something;
        # only the ids say whether it read the RIGHT something, and only the
        # scores say how close the runner-up was.
        log.info(
            "knowledge_retrieved",
            audience=audience,
            query=message[:80],
            faqs=_hits_out(hits_f),
            corrections=_hits_out(hits_c),
            doc_chunks=_hits_out(hits_d),
        )
    else:
        # A miss used to log nothing at all, which made "the agent had no
        # facts" and "the agent was never asked" indistinguishable after the
        # fact. The near-miss scores separate the two fixes: 0.14 against a
        # 0.15 floor is a floor to lower, 0.01 is an answer nobody wrote.
        log.info(
            "knowledge_miss",
            audience=audience,
            query=message[:80],
            corpus={
                "faqs": len(pool_f),
                "corrections": len(pool_c),
                "doc_chunks": len(chunks),
            },
            best_below_floor={
                "faqs": round(_top_score(pool_f, q), 3),
                "corrections": round(_top_score(pool_c, q), 3),
                "doc_chunks": round(_top_score(chunks, q), 3),
            },
            floors={"faq": FAQ_SCORE_FLOOR, "doc": DOC_SCORE_FLOOR},
        )
    return (
        [r for r, _ in hits_f],
        [r for r, _ in hits_c],
        [r for r, _ in hits_d],
    )


def _candidates(
    entries: list[tuple[object, set[str]]],
    query: set[str],
    *,
    floor: float,
    top_k: int,
    limit: int,
) -> list[dict]:
    scored = sorted(
        ((row, _score(query, toks)) for row, toks in entries),
        key=lambda t: t[1],
        reverse=True,
    )
    kept = {row.id for row, s in scored[:top_k] if s > floor}
    return [
        {
            "id": str(row.id),
            "text": _preview(row, 120),
            "score": round(s, 4),
            "above_floor": s > floor,
            "retrieved": row.id in kept,
        }
        for row, s in scored[:limit]
    ]


async def rank_knowledge(
    db: AsyncSession,
    message: str,
    *,
    audience: str = "customer",
    top_k: int = 3,
    limit: int = 10,
) -> dict:
    """Every candidate ranked, floors reported rather than applied — for evals.

    relevant_knowledge() answers "what did the agent get to read". This
    answers "what was in the running, and by how much did each one miss",
    which is what Hit-Rate / Recall / Precision / MRR are computed from —
    a metric needs the rows that lost, and those never reach the reply path.

    Deliberately NOT used to answer customers: it sorts the entire corpus,
    which relevant_knowledge() avoids on purpose.
    """
    q = _tokens(message)
    faqs, corrections, chunks = await _corpus(db)
    aud = (audience, "all")
    pool_f = [e for e in faqs if e[0].audience in aud]
    pool_c = [e for e in corrections if e[0].audience in aud]

    out = {
        "query": message,
        "query_tokens": sorted(q),
        "audience": audience,
        "params": {
            "faq_floor": FAQ_SCORE_FLOOR,
            "doc_floor": DOC_SCORE_FLOOR,
            "faq_top_k": top_k,
            "doc_top_k": DOC_TOP_K,
        },
        "faqs": _candidates(pool_f, q, floor=FAQ_SCORE_FLOOR, top_k=top_k, limit=limit),
        "corrections": _candidates(
            pool_c, q, floor=FAQ_SCORE_FLOOR, top_k=top_k, limit=limit
        ),
        "doc_chunks": _candidates(
            chunks, q, floor=DOC_SCORE_FLOOR, top_k=DOC_TOP_K, limit=limit
        ),
    }
    # The flat list a Hit-Rate assertion actually wants.
    out["retrieved_ids"] = [
        c["id"]
        for key in ("faqs", "corrections", "doc_chunks")
        for c in out[key]
        if c["retrieved"]
    ]
    return out


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
