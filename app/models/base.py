"""Declarative base shared by all models.

The naming convention matters: without it, Postgres auto-generates constraint
names, and Alembic migrations then can't reliably refer to them (e.g. when a
later migration drops a constraint). With it, every index/constraint gets a
predictable name from day one.
"""

import uuid

from sqlalchemy import ForeignKey, MetaData
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TenantScoped:
    """Mixin: kis laundry business ka row hai (multi-tenant isolation).

    Ye mixin teen cheezein deta hai:
    - `tenant_id` column (FK -> tenants.id, indexed) — declarative mixin se
      har subclass table par copy hota hai, naming convention ke saath
      (ix_<table>_tenant_id / fk_<table>_tenant_id_tenants).
    - app/database.py ke ORM events ka target: har SELECT par automatic
      tenant filter, har naye row par automatic tenant stamp.
    - Postgres RLS policies (migration d4c8e2f7a915) isi column par hain.

    Nullable until the API layer writes it explicitly — naye rows events se
    stamp hote hain; purane rows migration f0a7b3c9d1e4 ne backfill kiye.
    NOTE: settings_kv (PK=key) aur coupons (PK=code) ke natural PKs abhi
    globally unique hain — per-tenant PK/unique API-scoping phase mein.
    """

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), index=True
    )
