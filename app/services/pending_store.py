"""Per-sender drafts awaiting "haan" (bills, payments, relays, agent writes).

Staff phones are unique per shop, not globally: one person can work for
two shops on this SaaS. Keyed by phone alone, a bill drafted for shop A
could be confirmed while the request runs as shop B. This store keys every
entry by (tenant, phone) underneath while keeping the plain dict interface
the call sites use.

In-process memory: correct for the documented single uvicorn worker
(docs/DEPLOY.md). Running several workers needs a shared store — drafts
would otherwise land on whichever worker handled the message.
"""

from __future__ import annotations

from typing import Any


def _tenant():
    from app.services import tenant_context

    return tenant_context.effective_tenant_id()


class TenantPending(dict):
    """dict keyed by phone for callers; (tenant, phone) in storage."""

    def _k(self, phone: str):
        return (_tenant(), phone)

    def __getitem__(self, phone: str) -> Any:
        return super().__getitem__(self._k(phone))

    def __setitem__(self, phone: str, value: Any) -> None:
        super().__setitem__(self._k(phone), value)

    def __delitem__(self, phone: str) -> None:
        super().__delitem__(self._k(phone))

    def __contains__(self, phone: object) -> bool:
        return super().__contains__(self._k(phone))  # type: ignore[arg-type]

    def get(self, phone: str, default: Any = None) -> Any:
        return super().get(self._k(phone), default)

    def pop(self, phone: str, *default: Any) -> Any:
        return super().pop(self._k(phone), *default)

    def setdefault(self, phone: str, default: Any = None) -> Any:
        return super().setdefault(self._k(phone), default)
