"""Kai nakli dukaanein, har ek apne data ke saath — multi-tenant testing.

`seed_demo_data.py` ek dukaan bharta hai (home tenant). Ye uske alawa N
POORI dukaanein banata hai: tenant, owner, staff, rate card, customers,
orders aur tasks — taaki ye dekha ja sake ki ek dukaan ka data doosri ko
dikhta to nahi, aur bhari DB par panel kaisa chalta hai.

    python -m scripts.seed_demo_shops                 # 5 dukaan, 300/300/150 har ek
    python -m scripts.seed_demo_shops 6 1000 1000 500 # shops, customers, orders, tasks
    python -m scripts.seed_demo_shops --clear         # sab nakli dukaanein hatao

KYUN ALAG SCRIPT: seed_demo_data home tenant mein likhta hai aur usi ka
`--clear` chalata hai. Dono ko ek script mein milane par "sab hataao" ka
matlab do cheezein ho jaata — aur galat waali chalne par asli dukaan ka
tenant uda dena mumkin ho jaata. Yahan sab kuch DEMO_SLUG se shuru hota
hai, aur clear sirf usi upsarg par chalti hai.

CHETAVNI — TEST SUITE ISI DB PAR CHALTI HAI:

Demo data bharne ke baad `pytest` mein lagbhag 10 test fail honge. Wo
tootey nahi hain: kai test ginti par asserts karte hain ("customers
endpoint N lautaye", "standup mein itne pending"), aur 6000 nakli rows
un gintiyon ko badal dete hain. Testing se pehle `--clear` chala lo,
phir baseline wapas aa jaata hai. (Ye khud jaanch kar likha hai:
clear karte hi wo dason failures gayab ho jaate hain.)

TENANT SCOPING (sabse zaroori hissa):

Naye rows par tenant_id `before_flush` event lagata hai, jo
`tenant_context.current_tenant_id` se padhta hai. Isliye har dukaan ka
data likhne se pehle wo context SET karna padta hai. Bina iske sab rows
home tenant mein chali jaayengi aur "multi-tenant test" ka koi matlab hi
nahi rahega.
"""

import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import structlog
from sqlalchemy import select, text

from app.database import async_session_factory, engine
from app.models import (
    ROLE_OWNER,
    TENANT_ACTIVE,
    Customer,
    Order,
    OrderStatus,
    PaymentStatus,
    Rate,
    Staff,
    StaffRole,
    Task,
    Tenant,
    User,
)
from app.services import auth, tenant_context
from app.utils.logger import configure_logging

configure_logging()
log = structlog.get_logger()

# Har nakli cheez par yahi nishaan. `--clear` sirf isi upsarg par chalti
# hai, isliye asli dukaan kabhi galti se nahi udti.
DEMO_SLUG = "demo-shop-"

SHOPS = [
    ("Sparkle Dry Clean", "Lucknow", "pro"),
    ("Fresh Fold Laundry", "Kanpur", "starter"),
    ("Royal Wash House", "Jaipur", "pro"),
    ("QuickPress Laundry", "Indore", "starter"),
    ("City Laundry Co", "Bhopal", "growth"),
    ("Urban Steam Care", "Nagpur", "pro"),
]

FIRST = ["Ramesh", "Suresh", "Anita", "Priya", "Vikas", "Neha", "Amit", "Kavita",
         "Rahul", "Pooja", "Sunil", "Meena", "Arjun", "Divya", "Manoj", "Shweta"]
LAST = ["Sharma", "Verma", "Gupta", "Yadav", "Singh", "Mishra", "Pandey", "Tiwari"]
AREAS = ["Civil Lines", "Gomti Nagar", "Mall Road", "Station Road", "Model Town"]

GOODS = [("Shirt", 45), ("Trouser", 60), ("Saree", 120), ("Kurta", 55),
         ("Jeans", 70), ("Jacket", 150), ("Bedsheet", 90), ("Towel", 25)]
SERVICES = ["Wash & Iron", "Dry Clean", "Steam Iron", "Wash & Fold"]
STAGES = [OrderStatus.RECEIVED, OrderStatus.IN_WASH, OrderStatus.IN_IRON,
          OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED]


async def clear() -> None:
    """Sirf demo dukaanein. Har DELETE slug ke upsarg se bandha hua hai."""
    async with async_session_factory() as db:
        ids = (
            await db.execute(
                select(Tenant.id).where(Tenant.slug.like(f"{DEMO_SLUG}%"))
            )
        ).scalars().all()
        if not ids:
            print("koi demo dukaan nahi mili")
            return
        # Bachchon se shuru, phir tenant — warna foreign keys rok dengi.
        for stmt in (
            "DELETE FROM task_messages WHERE task_id IN (SELECT id FROM tasks WHERE tenant_id = ANY(:t))",
            "DELETE FROM tasks WHERE tenant_id = ANY(:t)",
            "DELETE FROM payments WHERE order_id IN (SELECT id FROM orders WHERE tenant_id = ANY(:t))",
            "DELETE FROM order_status_history WHERE order_id IN (SELECT id FROM orders WHERE tenant_id = ANY(:t))",
            "DELETE FROM orders WHERE tenant_id = ANY(:t)",
            "DELETE FROM conversations WHERE tenant_id = ANY(:t)",
            "DELETE FROM customers WHERE tenant_id = ANY(:t)",
            "DELETE FROM rate_card WHERE tenant_id = ANY(:t)",
            "DELETE FROM staff WHERE tenant_id = ANY(:t)",
            "DELETE FROM users WHERE tenant_id = ANY(:t)",
            "DELETE FROM tenants WHERE id = ANY(:t)",
        ):
            await db.execute(text(stmt), {"t": list(ids)})
        await db.commit()
    print(f"{len(ids)} demo dukaanein aur unka saara data hataya")


async def make_shop(idx: int, name: str, city: str, plan: str) -> tuple:
    """Tenant + owner + staff + rate card. Data alag function mein."""
    slug = f"{DEMO_SLUG}{idx}"
    async with async_session_factory() as db:
        if (
            await db.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none() is not None:
            print(f"  {slug} pehle se hai — chhod raha hoon")
            return None, None
        t = Tenant(
            slug=slug, shop_name=name, owner_name=f"{random.choice(FIRST)} {random.choice(LAST)}",
            owner_phone=f"+9188{idx:08d}", owner_email=f"owner{idx}@demo.test",
            city=city, plan=plan, status=TENANT_ACTIVE,
            current_period_end=datetime.now(timezone.utc) + timedelta(days=365),
            setup_fee_paid=True, onboarding_done=True,
            notes="NAKLI dukaan — scripts/seed_demo_shops.py se.",
        )
        db.add(t)
        await db.flush()
        tid = t.id

        # Owner ka login. Password sabka ek — ye demo data hai, aur alag-alag
        # yaad rakhna testing ko dushwar banata hai.
        db.add(User(
            tenant_id=tid, name=t.owner_name, email=t.owner_email,
            phone=t.owner_phone, password_hash=auth.hash_password("demo12345"),
            role=ROLE_OWNER,
        ))
        await db.commit()

    # Staff aur rates us dukaan ke CONTEXT mein — warna before_flush inhe
    # home tenant par chipka dega.
    token = tenant_context.current_tenant_id.set(tid)
    try:
        async with async_session_factory() as db:
            for n, (nm, role) in enumerate((
                ("Manager", StaffRole.MANAGER),
                ("Washerman", StaffRole.WASHER),
                ("Delivery", StaffRole.DELIVERY),
            )):
                db.add(Staff(
                    phone=f"+9189{idx:04d}{n:04d}", name=f"{nm} {idx}", role=role,
                ))
            for svc in SERVICES:
                for g, base in GOODS:
                    db.add(Rate(
                        service=svc, garment=g, unit="pc",
                        rate=Decimal(base + random.randint(-10, 25)),
                    ))
            await db.commit()
    finally:
        tenant_context.current_tenant_id.reset(token)
    return tid, name


async def fill_shop(tid, n_cust: int, n_orders: int, n_tasks: int) -> None:
    """Us dukaan ka customers/orders/tasks — sab uske context mein."""
    token = tenant_context.current_tenant_id.set(tid)
    try:
        async with async_session_factory() as db:
            staff = (await db.execute(select(Staff))).scalars().all()
            deliv = next((s for s in staff if s.role is StaffRole.DELIVERY), None)
            wash = next((s for s in staff if s.role is StaffRole.WASHER), None)

            custs = []
            for i in range(n_cust):
                c = Customer(
                    # Phone PER-TENANT unique hai, isliye har dukaan wahi
                    # range dobara use kar sakti hai — aur asli duniya mein
                    # bhi do dukaanon ka ek hi grahak ho sakta hai.
                    phone=f"+9177{i:08d}",
                    name=f"{random.choice(FIRST)} {random.choice(LAST)}",
                    address=f"{random.randint(1, 99)}, {random.choice(AREAS)}",
                )
                db.add(c)
                custs.append(c)
                if i % 500 == 499:
                    await db.flush()
            await db.flush()

            now = datetime.now(timezone.utc)
            for i in range(n_orders):
                c = random.choice(custs)
                items = []
                for nm, rate in random.sample(GOODS, random.randint(1, 3)):
                    qty = random.randint(1, 6)
                    items.append({"type": nm, "qty": qty, "rate": rate,
                                  "service": random.choice(SERVICES),
                                  "amount": rate * qty, "unit": "pc"})
                total = Decimal(sum(it["amount"] for it in items))
                roll = random.random()
                paid = total if roll < 0.55 else (total / 2 if roll < 0.75 else Decimal(0))
                db.add(Order(
                    order_number=f"D{tid.hex[:4].upper()}-{i:05d}",
                    customer_id=c.id, items=items,
                    total_amount=total, amount_paid=paid,
                    payment_status=(
                        PaymentStatus.PAID if paid >= total
                        else PaymentStatus.PARTIAL if paid > 0 else PaymentStatus.UNPAID
                    ),
                    status=random.choice(STAGES),
                    expected_delivery=(now + timedelta(days=random.randint(-8, 6))).date(),
                    created_at=now - timedelta(days=random.randint(0, 60), hours=random.randint(0, 23)),
                    priority="urgent" if random.random() < 0.08 else "normal",
                    assigned_delivery_id=deliv.id if (deliv and random.random() < 0.5) else None,
                    assigned_washer_id=wash.id if (wash and random.random() < 0.5) else None,
                ))
                if i % 300 == 299:
                    await db.commit()
            await db.commit()

            orders = (await db.execute(select(Order).limit(n_tasks))).scalars().all()
            for i, o in enumerate(orders):
                db.add(Task(
                    code=f"TD{tid.hex[:4].upper()}-{i:05d}",
                    title=f"{random.choice(['Pickup', 'Dhulai', 'Delivery'])}: {o.order_number}",
                    assigned_staff_id=(deliv or wash).id if (deliv or wash) else None,
                    order_id=o.id, status="OPEN",
                    urgent=random.random() < 0.1,
                    created_at=now - timedelta(hours=random.randint(1, 300)),
                ))
                if i % 300 == 299:
                    await db.commit()
            await db.commit()
    finally:
        tenant_context.current_tenant_id.reset(token)


async def seed(n_shops: int, n_cust: int, n_orders: int, n_tasks: int) -> None:
    made = []
    for i in range(min(n_shops, len(SHOPS))):
        name, city, plan = SHOPS[i]
        tid, nm = await make_shop(i + 1, name, city, plan)
        if tid is None:
            continue
        print(f"  {nm} ({city}, {plan}) — bhar raha hoon...")
        await fill_shop(tid, n_cust, n_orders, n_tasks)
        made.append((nm, f"owner{i + 1}@demo.test"))
    print(f"\n  {len(made)} nakli dukaanein taiyar "
          f"({n_cust} customers, {n_orders} orders, {n_tasks} tasks har ek)\n")
    for nm, email in made:
        print(f"    {nm:24} {email}  /  demo12345")
    print()


async def _run() -> None:
    """Sab kuch EK hi event loop mein, dispose bhi.

    Pehle dispose alag asyncio.run() mein tha, aur asyncpg apne connections
    band karte waqt "RuntimeError: Event loop is closed" ka dher ugal deta
    tha — kaam ho chuka hota tha, par output aisa dikhta tha jaise sab
    phat gaya ho.
    """
    if "--clear" in sys.argv:
        await clear()
    else:
        a = [x for x in sys.argv[1:] if not x.startswith("-")]
        await seed(
            int(a[0]) if len(a) > 0 else 5,
            int(a[1]) if len(a) > 1 else 300,
            int(a[2]) if len(a) > 2 else 300,
            int(a[3]) if len(a) > 3 else 150,
        )
    await engine.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
