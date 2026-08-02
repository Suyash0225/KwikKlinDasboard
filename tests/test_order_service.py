"""Order service tests: state machine, order numbers (incl. concurrency),
payments, dates. All rows created here are cleaned up afterwards.
"""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select, text as sqltext

from app.database import async_session_factory
from app.models import (
    Customer,
    Order,
    OrderStatus,
    OrderStatusHistory,
    PaymentMethod,
    PaymentStatus,
)
from app.services.order_service import (
    InvalidTransitionError,
    OrderError,
    OrderNotFoundError,
    can_transition,
    create_order,
    get_active_orders_for_phone,
    get_order,
    record_payment,
    set_expected_delivery,
    update_status,
)

TEST_PHONE = "+919999900022"
ITEMS = [{"type": "shirt", "qty": 3, "service": "wash_iron"}]

S = OrderStatus  # shorthand


@pytest.fixture(autouse=True)
def _mock_sends(sent):
    """Every test here creates orders -> notifications fire -> must be mocked.

    Reuses the shared `sent` recorder fixture (patches both import sites).
    """
    return sent


@pytest.fixture(autouse=True)
async def _cleanup_orders():
    yield
    async with async_session_factory() as s:
        await s.execute(
            sqltext(
                "DELETE FROM order_status_history WHERE order_id IN "
                "(SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id "
                f" WHERE c.phone = '{TEST_PHONE}')"
            )
        )
        await s.execute(
            sqltext(
                "DELETE FROM orders WHERE customer_id IN "
                f"(SELECT id FROM customers WHERE phone = '{TEST_PHONE}')"
            )
        )
        await s.execute(sqltext(f"DELETE FROM customers WHERE phone = '{TEST_PHONE}'"))
        await s.commit()


# --- state machine (pure function, no DB) ---

@pytest.mark.parametrize(
    "old,new",
    [
        (S.RECEIVED, S.IN_WASH),
        (S.IN_WASH, S.IN_DRY),
        (S.IN_IRON, S.READY),
        (S.READY, S.OUT_FOR_DELIVERY),
        (S.OUT_FOR_DELIVERY, S.DELIVERED),
        (S.RECEIVED, S.READY),          # skipping stages is allowed
        (S.IN_WASH, S.IN_IRON),         # skip drying
        (S.RECEIVED, S.CANCELLED),      # cancel any non-terminal
        (S.READY, S.ON_HOLD),           # hold any non-terminal
        (S.ON_HOLD, S.IN_DRY),          # resume anywhere in the sequence
        (S.ON_HOLD, S.CANCELLED),
    ],
)
def test_allowed_transitions(old: OrderStatus, new: OrderStatus) -> None:
    assert can_transition(old, new) is True


@pytest.mark.parametrize(
    "old,new",
    [
        (S.IN_DRY, S.IN_WASH),          # backward
        (S.READY, S.RECEIVED),          # backward
        (S.DELIVERED, S.IN_WASH),       # terminal
        (S.DELIVERED, S.CANCELLED),     # terminal stays terminal
        (S.CANCELLED, S.RECEIVED),      # terminal
        (S.CANCELLED, S.ON_HOLD),
        (S.RECEIVED, S.RECEIVED),       # no-op
    ],
)
def test_forbidden_transitions(old: OrderStatus, new: OrderStatus) -> None:
    assert can_transition(old, new) is False


# --- creation ---

async def test_create_order_full_shape() -> None:
    async with async_session_factory() as db:
        order = await create_order(
            db,
            customer_phone="9999900022",  # raw local format on purpose
            customer_name="Test Grahak",
            items=ITEMS,
            total_amount=Decimal("300.00"),
            created_by="test",
        )
        assert order.order_number.startswith("KK-")
        assert order.status is S.RECEIVED
        assert order.payment_status is PaymentStatus.UNPAID

        cust = (
            await db.execute(select(Customer).where(Customer.phone == TEST_PHONE))
        ).scalar_one()
        assert cust.name == "Test Grahak"

        history = (
            await db.execute(
                select(OrderStatusHistory).where(OrderStatusHistory.order_id == order.id)
            )
        ).scalars().all()
        assert len(history) == 1
        assert history[0].old_status is None and history[0].new_status is S.RECEIVED


async def test_create_order_rejects_bad_items() -> None:
    async with async_session_factory() as db:
        with pytest.raises(OrderError):
            await create_order(db, customer_phone=TEST_PHONE, items=[])
        with pytest.raises(OrderError):
            await create_order(db, customer_phone=TEST_PHONE, items=[{"qty": 2}])


async def test_order_numbers_unique_under_concurrency() -> None:
    """5 simultaneous creates must mint 5 distinct numbers (advisory lock)."""

    async def make_one() -> str:
        async with async_session_factory() as db:
            order = await create_order(
                db, customer_phone=TEST_PHONE, items=ITEMS, created_by="test"
            )
            return order.order_number

    numbers = await asyncio.gather(*(make_one() for _ in range(5)))
    assert len(set(numbers)) == 5, f"duplicate order numbers: {numbers}"
    # and they are consecutive for today
    seqs = sorted(int(n.rsplit("-", 1)[1]) for n in numbers)
    assert seqs == list(range(seqs[0], seqs[0] + 5))


# --- status updates ---

async def test_update_status_writes_history_and_blocks_backward() -> None:
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=TEST_PHONE, items=ITEMS, created_by="test")
        await update_status(db, order, S.IN_WASH, changed_by="staff:ravi")
        await update_status(db, order, S.READY, changed_by="staff:ravi")  # skip allowed

        with pytest.raises(InvalidTransitionError):
            await update_status(db, order, S.IN_WASH, changed_by="staff:ravi")

        history = (
            await db.execute(
                select(OrderStatusHistory)
                .where(OrderStatusHistory.order_id == order.id)
                .order_by(OrderStatusHistory.changed_at)
            )
        ).scalars().all()
        assert [h.new_status for h in history] == [S.RECEIVED, S.IN_WASH, S.READY]
        assert history[-1].changed_by == "staff:ravi"


async def test_delivered_sets_actual_delivery() -> None:
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=TEST_PHONE, items=ITEMS, created_by="test")
        await update_status(db, order, S.OUT_FOR_DELIVERY, changed_by="test")
        await update_status(db, order, S.DELIVERED, changed_by="staff:superman")
        assert order.actual_delivery is not None


# --- lookups ---

async def test_get_order_and_not_found() -> None:
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=TEST_PHONE, items=ITEMS, created_by="test")
        found = await get_order(db, order.order_number.lower())  # case-insensitive
        assert found.id == order.id
        with pytest.raises(OrderNotFoundError):
            await get_order(db, "KK-19990101-99")


async def test_active_orders_excludes_finished() -> None:
    async with async_session_factory() as db:
        o1 = await create_order(db, customer_phone=TEST_PHONE, items=ITEMS, created_by="test")
        o2 = await create_order(db, customer_phone=TEST_PHONE, items=ITEMS, created_by="test")
        await update_status(db, o2, S.CANCELLED, changed_by="test")

        active = await get_active_orders_for_phone(db, TEST_PHONE)
        numbers = [o.order_number for o in active]
        assert o1.order_number in numbers
        assert o2.order_number not in numbers


# --- payments & dates ---

async def test_payment_lifecycle() -> None:
    async with async_session_factory() as db:
        order = await create_order(
            db, customer_phone=TEST_PHONE, items=ITEMS,
            total_amount=Decimal("300.00"), created_by="test",
        )
        await record_payment(db, order, amount=Decimal("100.00"), method=PaymentMethod.UPI)
        assert order.payment_status is PaymentStatus.PARTIAL
        assert order.paid_at is None

        await record_payment(db, order, amount=Decimal("200.00"), method=PaymentMethod.CASH)
        assert order.payment_status is PaymentStatus.PAID
        assert order.paid_at is not None

        with pytest.raises(OrderError):
            await record_payment(db, order, amount=Decimal("-5"), method=PaymentMethod.CASH)


async def test_expected_delivery_reason_stays_internal() -> None:
    from datetime import date, timedelta

    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=TEST_PHONE, items=ITEMS, created_by="test")
        new_date = date.today() + timedelta(days=3)
        await set_expected_delivery(
            db, order, new_date,
            changed_by="staff:ravi", internal_reason="paani nahi aaya",
        )
        assert order.expected_delivery == new_date
        assert "paani nahi aaya" in (order.notes or "")  # manager-only column
