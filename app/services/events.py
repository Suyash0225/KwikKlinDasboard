"""Live updates: jo abhi hua, wo abhi dikhe — kahin bhi.

Ye wahi dikkat ka ilaaj hai jahan Ajit ne phone se bill banaya aur owner ke
khule hue dashboard par wo nazar hi nahi aaya. Data bilkul theek tha; page
ne bas dobara poocha hi nahi tha. Ab server khud bata deta hai.

**Rasta: SSE (Server-Sent Events), WebSocket nahi.** Yahan khabar sirf ek
taraf jaati hai — server se browser tak. SSE usi ek kaam ke liye bana hai:
saada HTTP hai (cloudflared tunnel, proxy, sab jagah bina setup ke chalta
hai), aur connection tootne par browser KHUD dobara jud jaata hai. WebSocket
do-tarfa hota hai, jiski yahan zaroorat hi nahi, aur uske badle mein
reconnect, ping/pong aur proxy ki jhanjhat khud sambhalni padti.

**Khabar mein data nahi jaata — sirf ishara.** Message bas itna kehta hai
"task badla" ya "naya order aaya"; page phir apne hisaab se maangta hai.
Isse do faayde hain: kisi ka data galat connection par nikal hi nahi sakta,
aur har screen wahi maangti hai jo wo dikha rahi hai.

**Tenant ki deewar yahan bhi.** Har subscriber apne tenant se bandha hai.
Ek dukaan ki halchal doosri dukaan ke connection par kabhi nahi jaati —
publish karte waqt tenant milaya jaata hai, subscribe karte waqt bhi.

**Dheema sunne wala poore server ko nahi rokta.** Har connection ki apni
chhoti queue hai. Kisi ka phone tunnel mein chala gaya aur queue bhar gayi
to uske purane ishare gir jaate hain — server ruकta nahi. Wo phone wapas
aakar ek baar poora refresh kar lega, jo waise bhi sahi jawab hai.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger()

# Ek connection ki queue kitni gehri. Chhoti jaan-boojh kar: ye khabar ka
# godam nahi hai. Bhar jaye to sabse purana ishara girta hai — kyunki naya
# ishara purane se zyada kaam ka hai.
QUEUE_DEPTH = 32

# Har kitne second par ek dhadkan, do kaam ke liye: connection zinda rahe
# (proxy chup pade connection ko 30-60s mein kaat dete hain), aur browser ko
# SABOOT mile ki stream sach mein guzar rahi hai.
HEARTBEAT_SECONDS = 20

# tenant_id -> us dukaan ke khule hue connections ki queues
_subscribers: dict[str, set[asyncio.Queue]] = {}


def _key(tenant_id: UUID | str | None) -> str:
    return str(tenant_id) if tenant_id else "-"


def publish(tenant_id: UUID | str | None, kind: str, **fields: Any) -> int:
    """Ek ishara us dukaan ke sabhi khule screens par. Kitne par gaya, wo lautao.

    Ye kabhi raise nahi karta aur kabhi rukta nahi. Live update ek suvidha
    hai — uski wajah se koi bill, koi task, koi payment nahi girna chahiye.
    """
    subs = _subscribers.get(_key(tenant_id))
    if not subs:
        return 0
    payload = json.dumps({"kind": kind, **fields}, default=str)
    sent = 0
    for q in list(subs):
        try:
            q.put_nowait(payload)
            sent += 1
        except asyncio.QueueFull:
            # Dheema sunne wala: sabse purana giraakar naya daalo
            try:
                q.get_nowait()
                q.put_nowait(payload)
                sent += 1
            except Exception:
                pass
        except Exception:
            log.debug("event_publish_skipped", kind=kind)
    return sent


async def stream(tenant_id: UUID | str | None, is_disconnected) -> AsyncIterator[str]:
    """Ek connection ka SSE stream. `is_disconnected` async callable hai.

    Browser band hua, tab band hui, ya network gaya — generator khatam hota
    hai aur queue register se nikal jaati hai. Bina iske har reconnect ek
    marí hui queue chhod jaata aur memory chupchaap badhti rehti.
    """
    key = _key(tenant_id)
    q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_DEPTH)
    _subscribers.setdefault(key, set()).add(q)
    log.info("events_subscribed", tenant=key, open=len(_subscribers[key]))
    try:
        # Pehla message turant: isse browser ko pata chal jaata hai ki
        # connection sach mein khula hai (kuch proxy pehle byte tak rokte
        # hain), aur EventSource ka onopen chalta hai.
        yield "event: ready\ndata: {}\n\n"
        while True:
            if await is_disconnected():
                break
            try:
                payload = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # Dhadkan ek NAAM wale event ki tarah, comment (`: ping`) ki
                # tarah nahi. Comment line browser JS tak pahunchti hi nahi —
                # yani page ke paas ye jaanne ka koi tarika nahi hota tha ki
                # stream zinda hai ya beech mein kisi proxy ne kaat di. Naam
                # hone se page sun sakta hai, aur chup pad jaane par khud
                # poochhne (polling) par laut sakta hai.
                yield "event: ping\ndata: {}\n\n"
                continue
            yield f"data: {payload}\n\n"
    except asyncio.CancelledError:
        raise
    finally:
        subs = _subscribers.get(key)
        if subs is not None:
            subs.discard(q)
            if not subs:
                _subscribers.pop(key, None)
        log.info("events_unsubscribed", tenant=key)


def open_connections(tenant_id: UUID | str | None = None) -> int:
    """Kitne screen abhi jude hain. Health/debug ke liye."""
    if tenant_id is None:
        return sum(len(s) for s in _subscribers.values())
    return len(_subscribers.get(_key(tenant_id), ()))
