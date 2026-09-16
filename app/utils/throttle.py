"""Per-IP sliding-window throttle — bounded memory.

Ye do jagah alag-alag likha hua tha (`services/auth.py` ka login throttle
aur `routers/orders.py` ka API-key throttle) aur dono mein EK HI rog tha:

    _FAILED: dict[str, deque] = defaultdict(deque)
    ...
    q = _FAILED[ip]          # <- sirf PADHNE se entry ban jaati hai

`defaultdict` par index karte hi row ban jaata hai, aur row kabhi hatta
nahi tha — sirf successful login par (`clear_failures`). Matlab:

  * har wo IP jo kabhi login page tak aayi, hamesha ke liye memory mein;
  * `orders.py` wala to HAR admin request par chalta hai, isliye wahan har
    visitor ka record jama hota tha, galat key wale ka hi nahi;
  * IP badal-badal kar maarne wala process ki memory jitni chahe bada leta —
    request 401 khaati rehti aur phir bhi nuksaan ho jaata.

Yahan teen cheezein theek hain:
  1. `throttled()` sirf padhta hai — entry nahi banata.
  2. Har IP ka deque `maxlen` par bandha hai (window ke andar itni hi
     failures matter karti hain, usse zyada ginne ka koi matlab nahi).
  3. Purane IP samay-samay par saaf ho jaate hain, aur dict bada ho jaye to
     sweep har write par.

SEEMA — ye per-PROCESS hai. Do uvicorn worker matlab attacker ko do guna
attempts (load balancer jis worker par bhejta hai). Ek shop ke liye theek
hai; jab tak assli sharing na chahiye, ye jaan-boojh kar simple hai.
Zaroorat pade to isi class ka DB/Redis-backed version banega — call sites
(`throttled` / `note_failure` / `clear`) waise ke waise rahenge.
"""

from __future__ import annotations

import time
from collections import deque

import structlog

log = structlog.get_logger()


class IPThrottle:
    """`max_failures` galat koshishein `window_secs` mein -> throttled."""

    def __init__(
        self,
        *,
        max_failures: int,
        window_secs: int,
        name: str = "throttle",
        sweep_every_secs: int = 300,
        max_entries: int = 10_000,
    ) -> None:
        self.max_failures = max_failures
        self.window_secs = window_secs
        self.name = name
        self._sweep_every = sweep_every_secs
        self._max_entries = max_entries
        self._failed: dict[str, deque[float]] = {}
        self._last_sweep = 0.0

    # -- internal ----------------------------------------------------------

    def _prune(self, q: deque[float], now: float) -> None:
        while q and now - q[0] > self.window_secs:
            q.popleft()

    def _sweep(self, now: float) -> None:
        """Jin IP ki saari failures purani ho chuki hain, unhe hata do.

        Normally har `sweep_every_secs` par. Dict cap se upar chala jaye to
        har write par — taaki ye kabhi bhi unbounded na ho.
        """
        if now - self._last_sweep < self._sweep_every and len(self._failed) < self._max_entries:
            return
        self._last_sweep = now
        stale = [ip for ip, q in self._failed.items() if not q or now - q[-1] > self.window_secs]
        for ip in stale:
            self._failed.pop(ip, None)
        if len(self._failed) >= self._max_entries:
            # Sweep ke baad bhi cap par — matlab abhi asli flood chal rahi
            # hai. Sabse purani entries girao aur bata do.
            log.warning(
                "throttle_table_full", throttle=self.name, entries=len(self._failed)
            )
            for ip in sorted(self._failed, key=lambda i: self._failed[i][-1])[
                : len(self._failed) - self._max_entries + 1
            ]:
                self._failed.pop(ip, None)

    # -- public ------------------------------------------------------------

    def throttled(self, ip: str) -> bool:
        """Read-only — is IP ko abhi rokna chahiye? Entry nahi banata."""
        q = self._failed.get(ip)
        if not q:
            return False
        self._prune(q, time.monotonic())
        if not q:
            self._failed.pop(ip, None)
            return False
        return len(q) >= self.max_failures

    def note_failure(self, ip: str) -> None:
        now = time.monotonic()
        self._sweep(now)
        q = self._failed.get(ip)
        if q is None:
            q = self._failed[ip] = deque(maxlen=self.max_failures)
        self._prune(q, now)
        q.append(now)

    def clear(self, ip: str) -> None:
        """Sahi login ho gaya — is IP ka record bhool jao."""
        self._failed.pop(ip, None)

    def reset(self) -> None:
        """Tests ke liye."""
        self._failed.clear()
        self._last_sweep = 0.0

    @property
    def tracked(self) -> int:
        """Abhi kitni IP yaad hain — monitoring/tests ke liye."""
        return len(self._failed)
