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

    NOT NULL (migration w7f4b2c8e5a3). Pehle nullable tha, is bharose par
    ki `before_flush` har row par tenant likh hi dega. Wo bharosa mehnga
    hai: NULL ki kisi bhi cheez se tulna NULL deti hai, TRUE nahi, isliye
    RLS ke tahat NULL tenant_id wali row KISI ko nahi dikhti — na error, na
    khaali jagah, bas gayab. `scripts/_bootstrap.py` ka docstring theek isi
    bug par likha gaya hai.

    audit_log iska apwaad hai aur usne khud override kiya hai: uski kai
    rows platform-level hain (control panel, system jobs) jinka tenant
    hota hi nahi.

    (Purana note ki settings_kv/coupons ke natural PK globally unique hain —
    ab nahi. settings_kv par uq_settings_kv_tenant_key hai, aur coupons
    migration u5d2f8a6c3e1 mein surrogate id + (tenant_id, code) par aa
    gaya.)
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), index=True, nullable=False
    )
