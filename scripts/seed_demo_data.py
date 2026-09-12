"""Bahut saara nakli data — pagination, speed aur "bhari dukaan kaisi
dikhti hai" dekhne ke liye. Sirf dev/staging par.

Har row par `KK-DEMO-` / `TD-` ka nishaan hai, isliye --clear se poora
kachra ek command mein nikal jaata hai. Asli data kabhi nahi chhuta.

Run:
    python -m scripts.seed_demo_data            # 400 orders, 120 tasks
    python -m scripts.seed_demo_data 1200 300   # apni ginti
    python -m scripts.seed_demo_data --clear    # sab hataao
"""

import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import structlog
from sqlalchemy import select, text

from app.database import async_session_factory
from app.models import Customer, Order, OrderStatus, PaymentStatus, Staff, StaffRole, Task
from app.models.order import OrderStatusHistory
from app.utils.logger import configure_logging

configure_logging()
log = structlog.get_logger()

ORDER_TAG = "KK-DEMO-"
TASK_TAG = "TD-"
PHONE_TAG = "+9177"          # demo customers ka apna range

FIRST = ["Ramesh", "Suresh", "Anita", "Priya", "Vikas", "Neha", "Amit", "Kavita",
         "Rahul", "Pooja", "Sunil", "Meena"]
LAST = ["Sharma", "Verma", "Gupta", "Singh", "Yadav", "Mishra", "Pandey", "Tiwari",
        "Dubey", "Jha", "Rai", "Chauhan"]
GOODS = [("Shirt", 30), ("Pant", 40), ("Saree", 120), ("Blazer", 150),
         ("Kurta", 35), ("Bedsheet", 80), ("Blanket", 200)]
STAGES = [OrderStatus.RECEIVED, OrderStatus.PICKUP_ASSIGNED, OrderStatus.PICKED_UP,
          OrderStatus.IN_WASH, OrderStatus.IN_IRON, OrderStatus.READY,
          OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED]


async def clear() -> None:
    async with async_session_factory() as db:
        for stmt, arg in (
            ("DELETE FROM task_messages WHERE task_id IN "
             "(SELECT id FROM tasks WHERE code LIKE :t)", {"t": f"{TASK_TAG}%"}),
            ("DELETE FROM tasks WHERE code LIKE :t", {"t": f"{TASK_TAG}%"}),
            ("DELETE FROM payments WHERE order_id IN "
             "(SELECT id FROM orders WHERE order_number LIKE :o)", {"o": f"{ORDER_TAG}%"}),
            ("DELETE FROM order_status_history WHERE order_id IN "
             "(SELECT id FROM orders WHERE order_number LIKE :o)", {"o": f"{ORDER_TAG}%"}),
            ("DELETE FROM orders WHERE order_number LIKE :o", {"o": f"{ORDER_TAG}%"}),
            ("DELETE FROM conversations WHERE customer_id IN "
             "(SELECT id FROM customers WHERE phone LIKE :p)", {"p": f"{PHONE_TAG}%"}),
            ("DELETE FROM customers WHERE phone LIKE :p", {"p": f"{PHONE_TAG}%"}),
        ):
            await db.execute(text(stmt), arg)
        await db.commit()
    print("demo data cleared")


async def seed(n_orders: int, n_tasks: int) -> None:
    from scripts._bootstrap import prime

    await prime()   # warna sab rows tenant_id=NULL ke saath jayengi
    async with async_session_factory() as db:
        staff = (await db.execute(select(Staff))).scalars().all()
        if not staff:
            raise SystemExit("Pehle staff banao: python -m scripts.seed_staff")
        deliv = next((s for s in staff if s.role is StaffRole.DELIVERY), None)
        wash = next((s for s in staff if s.role is StaffRole.WASHER), None)

        custs = []
        for i in range(max(20, n_orders // 4)):
            c = Customer(
                phone=f"{PHONE_TAG}{i:08d}",
                name=f"{random.choice(FIRST)} {random.choice(LAST)}",
                address=f"{random.randint(1, 99)}, {random.choice(['Lanka', 'Assi', 'Sigra', 'Kamachha'])}, Varanasi",
            )
            db.add(c)
            custs.append(c)
        await db.flush()

        now = datetime.now(timezone.utc)
        for i in range(n_orders):
            c = random.choice(custs)
            items = []
            for name, rate in random.sample(GOODS, random.randint(1, 3)):
                qty = random.randint(1, 6)
                items.append({"type": name, "qty": qty, "rate": rate,
                              "service": "Wash & Iron", "amount": rate * qty, "unit": "pc"})
            total = Decimal(sum(it["amount"] for it in items))
            roll = random.random()
            paid = total if roll < 0.55 else (total / 2 if roll < 0.75 else Decimal(0))
            db.add(
                Order(
                    order_number=f"{ORDER_TAG}{i:05d}", customer_id=c.id, items=items,
                    total_amount=total, amount_paid=paid,
                    payment_status=(
                        PaymentStatus.PAID if paid >= total
                        else PaymentStatus.PARTIAL if paid > 0 else PaymentStatus.UNPAID
                    ),
                    status=random.choice(STAGES),
                    expected_delivery=(now + timedelta(days=random.randint(-8, 6))).date(),
                    created_at=now - timedelta(days=random.randint(0, 40), hours=random.randint(0, 23)),
                    priority="urgent" if random.random() < 0.08 else "normal",
                    assigned_delivery_id=deliv.id if (deliv and random.random() < 0.5) else None,
                    assigned_washer_id=wash.id if (wash and random.random() < 0.5) else None,
                )
            )
            if i % 200 == 199:
                await db.commit()
        await db.commit()

        orders = (
            await db.execute(
                select(Order).where(Order.order_number.like(f"{ORDER_TAG}%")).limit(n_tasks)
            )
        ).scalars().all()
        for i, o in enumerate(orders):
            db.add(
                OrderStatusHistory(order_id=o.id, old_status=None,
                                   new_status=OrderStatus.RECEIVED,
                                   changed_by=random.choice(staff).name)
            )
            db.add(
                Task(
                    code=f"{TASK_TAG}{i:05d}",
                    title=f"{random.choice(['Pickup', 'Dhulai', 'Delivery'])}: {o.order_number}",
                    assigned_staff_id=(deliv or wash).id, order_id=o.id, status="OPEN",
                    urgent=random.random() < 0.1,
                    created_at=now - timedelta(hours=random.randint(1, 200)),
                )
            )
        await db.commit()
    print(f"seeded {n_orders} orders, {len(orders)} tasks, {len(custs)} customers")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--clear" in sys.argv:
        asyncio.run(clear())
        return
    n_orders = int(args[0]) if args else 400
    n_tasks = int(args[1]) if len(args) > 1 else 120
    asyncio.run(seed(n_orders, n_tasks))


if __name__ == "__main__":
    main()
