"""Har service ke apne stages — sirf-press wala kapda dhulai se nahi guzarta.

Owner rate card mein service banate waqt tay karta hai ki us service mein
kaun se stage hain (Wash / Dry / Iron). Order ke stages = uske saare
kapdon ki services ka jod, hamesha isi kram mein. Order agle stage par
jaata hai; koi stage na bache to seedha READY.

Jis service ka owner ne kuch tay nahi kiya, uska andaaza naam se: "Steam
Iron", "Press", "Polish" -> sirf Iron; "Wash & Fold" -> Wash + Dry; baaki
sab pehle jaisa poora Wash -> Dry -> Iron. Isliye purani dukaanon par
kuch nahi badalta jab tak naam saaf na kahe.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderStatus

STAGES = (OrderStatus.IN_WASH, OrderStatus.IN_DRY, OrderStatus.IN_IRON)
FULL = list(STAGES)
# Task.kind aur WhatsApp menu ka action — dono isi naam se chalte hain
KIND = {OrderStatus.IN_WASH: "wash", OrderStatus.IN_DRY: "dry", OrderStatus.IN_IRON: "iron"}
ACTION = {**KIND, OrderStatus.READY: "ready"}
# Dhulai shuru hone se pehle ke status
BEFORE = (OrderStatus.RECEIVED, OrderStatus.PICKED_UP)

_WASHY = ("wash", "dhulai", "laundry", "dry clean", "dryclean", "clean")
_PRESSY = ("iron", "press", "istri", "isteri", "steam", "polish", "roll")


def default_stages(service: str) -> list[OrderStatus]:
    name = (service or "").lower()
    washy = any(w in name for w in _WASHY)
    if not washy and any(w in name for w in _PRESSY):
        return [OrderStatus.IN_IRON]
    if "wash" in name and "fold" in name and not any(w in name for w in _PRESSY):
        return [OrderStatus.IN_WASH, OrderStatus.IN_DRY]
    return list(FULL)


def clean_config(value) -> dict[str, list[str]]:
    """Settings mein jo aaya use saaf karo — galat stage naam chupchaap hat jaate hain."""
    if not isinstance(value, dict):
        raise ValueError("service_stages must be an object: {service: [stages]}")
    allowed = {s.name for s in STAGES}
    out: dict[str, list[str]] = {}
    for service, stages in value.items():
        if not isinstance(service, str) or not service.strip() or not isinstance(stages, list):
            raise ValueError("service_stages: har service ke liye stages ki list chahiye")
        picked = {str(s) for s in stages} & allowed
        out[service.strip()] = [s.name for s in STAGES if s.name in picked]
    return out


def stages_for_services(services, config: dict | None) -> list[OrderStatus]:
    """Kapdon ki services -> order ke stages (jod, sahi kram mein)."""
    config = config or {}
    wanted: set[OrderStatus] = set()
    any_service = False
    for svc in services:
        if not svc:
            continue
        any_service = True
        if svc in config:
            wanted.update(OrderStatus[s] for s in config[svc] if s in OrderStatus.__members__)
        else:
            wanted.update(default_stages(svc))
    if not any_service:
        return list(FULL)
    return [s for s in STAGES if s in wanted]


async def config(db: AsyncSession) -> dict:
    from app.services import app_settings

    value = await app_settings.get(db, "service_stages")
    return value if isinstance(value, dict) else {}


def _services(order: Order) -> list[str]:
    return [str(i.get("service") or "").strip() for i in (order.items or []) if isinstance(i, dict)]


async def stages_for(db: AsyncSession, order: Order, cfg: dict | None = None) -> list[OrderStatus]:
    if cfg is None:
        cfg = await config(db)
    return stages_for_services(_services(order), cfg)


def next_after(status: OrderStatus, stages: list[OrderStatus]) -> OrderStatus | None:
    """Is status ke baad agla kadam. Dhulai ki kataar se bahar ho to None."""
    if status in BEFORE:
        return stages[0] if stages else OrderStatus.READY
    if status not in STAGES:
        return None
    later = [s for s in stages if STAGES.index(s) > STAGES.index(status)]
    return later[0] if later else OrderStatus.READY


async def next_stage(db: AsyncSession, order: Order, cfg: dict | None = None) -> OrderStatus | None:
    return next_after(order.status, await stages_for(db, order, cfg))


def next_name(order: Order, cfg: dict) -> str | None:
    """List/serializer ke liye — config pehle se padha hua (ek query, N orders)."""
    nxt = next_after(order.status, stages_for_services(_services(order), cfg))
    return nxt.name if nxt else None


def task_kind(status: OrderStatus, stages: list[OrderStatus]) -> str | None:
    """Is status par khula kaam kis kind ka hona chahiye."""
    if status in BEFORE:
        return KIND[stages[0]] if stages else "wash"
    return KIND.get(status)


_WORD = {
    OrderStatus.IN_WASH: ("🧼", "washing"), OrderStatus.IN_DRY: ("💨", "drying"),
    OrderStatus.IN_IRON: ("👔", "ironing"), OrderStatus.READY: ("✅", "ready"),
}


def menu_step(status: OrderStatus, stages: list[OrderStatus]) -> tuple[str, str, str] | None:
    """WhatsApp menu ka agla kadam: (action, title, description)."""
    nxt = next_after(status, stages)
    if nxt is None:
        return None
    emoji, word = _WORD[nxt]
    desc = "Mark order ready for delivery" if nxt is OrderStatus.READY else f"Move clothes to {word}"
    if status in BEFORE:
        title = "✅ Mark ready" if nxt is OrderStatus.READY else f"{emoji} Start {word}"
    else:
        title = f"{emoji} {_WORD[status][1].capitalize()} done"
    return ACTION[nxt], title, desc


def task_matches(task_kind_: str, status: OrderStatus, stages: list[OrderStatus]) -> bool:
    """Kya ye kaam is stage ka hai? Dhulai se pehle koi bhi processing kaam
    chalega — deploy se pehle bane "wash" kaam sirf-press order par bhi."""
    if status in BEFORE:
        return task_kind_ in KIND.values()
    return task_kind_ == KIND.get(status)
