"""All enums used by the models.

These become native Postgres ENUM types. SQLAlchemy stores the member NAME
(e.g. "RECEIVED"), not the value, so keep names stable — renaming a member
later requires a migration.
"""

import enum


class OrderStatus(enum.Enum):
    """Order lifecycle, in this exact order (plus two terminal states)."""

    RECEIVED = "RECEIVED"
    # home-pickup flow (owner's Order Agent spec, Aug 2026)
    PICKUP_ASSIGNED = "PICKUP_ASSIGNED"
    PICKED_UP = "PICKED_UP"
    IN_WASH = "IN_WASH"
    IN_DRY = "IN_DRY"
    IN_IRON = "IN_IRON"
    READY = "READY"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    # Terminal states outside the normal flow
    CANCELLED = "CANCELLED"
    ON_HOLD = "ON_HOLD"


class StaffRole(enum.Enum):
    WASHER = "WASHER"
    DELIVERY = "DELIVERY"
    # The shop's owner/manager side. An ADMIN is not a worker: they get every
    # escalation, every new order and every payment, and their WhatsApp
    # messages carry manager powers.
    ADMIN = "ADMIN"


class PaymentStatus(enum.Enum):
    UNPAID = "UNPAID"
    PARTIAL = "PARTIAL"
    PAID = "PAID"


class PaymentMethod(enum.Enum):
    CASH = "CASH"
    UPI = "UPI"
    OTHER = "OTHER"


class Direction(enum.Enum):
    """Message direction relative to us: INBOUND = they wrote to the bot."""

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class EscalationStatus(enum.Enum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    CLOSED = "CLOSED"
