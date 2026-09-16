"""Throttle, password-hash aur rate-limiter ke pehre.

Ye sab BINA DB ke chalte hain — yahan ka har function pure Python hai.

Kyun likhe gaye: teen bug the jo chupchaap baithe the aur kisi test ne kabhi
chhua nahi tha —

  1. `throttled(ip)` `defaultdict` par index karta tha, isliye sirf JAANCHNE
     se hi us IP ka record ban jaata tha aur kabhi hatta nahi tha. Normal
     traffic hi memory badhata rehta tha.
  2. `verify_password` scrypt ke n/r/p stored string se leta tha — ek
     planted/corrupt row ek login attempt ko GB-scale allocation bana sakti thi.
  3. Rate limiter ka bucket 4096 par capped tha jabki default limit 6000 —
     yaani default config par limiter kabhi trigger hi nahi hota tha. Suite
     ise isliye nahi pakad payi kyunki wo limit 1/5 par set karke chalti hai.

Isliye har test niche "purana code yahan fail hota" wali baat check karta hai,
sirf "chal raha hai" nahi.
"""

import time
from collections import deque

import pytest

from app.services.auth import (
    _ALLOWED_SCRYPT_PARAMS,
    hash_password,
    temp_password,
    verify_password,
)
from app.utils.throttle import IPThrottle


# --- throttle: memory ------------------------------------------------------


def test_checking_an_ip_does_not_remember_it() -> None:
    """Ye WO bug hai. Padhna free hona chahiye."""
    t = IPThrottle(max_failures=8, window_secs=600)
    for i in range(1000):
        t.throttled(f"10.0.0.{i}")
    assert t.tracked == 0, "sirf jaanchne se IP yaad nahi honi chahiye"


def test_one_ip_cannot_grow_unbounded() -> None:
    t = IPThrottle(max_failures=8, window_secs=600)
    for _ in range(10_000):
        t.note_failure("1.2.3.4")
    assert len(t._failed["1.2.3.4"]) <= 8


def test_stale_ips_are_swept() -> None:
    """Window guzar jaane par purani IP memory se nikal jaani chahiye."""
    t = IPThrottle(max_failures=3, window_secs=1, sweep_every_secs=0)
    for i in range(200):
        t.note_failure(f"9.9.9.{i}")
    assert t.tracked == 200
    time.sleep(1.1)
    t.note_failure("8.8.8.8")          # koi bhi write sweep chalata hai
    assert t.tracked == 1


def test_table_has_a_hard_cap() -> None:
    """Asli flood mein bhi dict cap se upar nahi jaata."""
    t = IPThrottle(max_failures=4, window_secs=600, sweep_every_secs=0, max_entries=50)
    for i in range(500):
        t.note_failure(f"7.7.{i // 256}.{i % 256}")
    assert t.tracked <= 50


# --- throttle: behaviour (purana jo karta tha wo abhi bhi ho) ---------------


def test_blocks_only_after_the_limit() -> None:
    t = IPThrottle(max_failures=8, window_secs=600)
    for _ in range(7):
        t.note_failure("1.2.3.4")
    assert t.throttled("1.2.3.4") is False
    t.note_failure("1.2.3.4")
    assert t.throttled("1.2.3.4") is True


def test_window_expiry_unblocks() -> None:
    t = IPThrottle(max_failures=2, window_secs=1)
    t.note_failure("1.2.3.4")
    t.note_failure("1.2.3.4")
    assert t.throttled("1.2.3.4") is True
    time.sleep(1.1)
    assert t.throttled("1.2.3.4") is False


def test_success_clears_the_ip() -> None:
    t = IPThrottle(max_failures=2, window_secs=600)
    t.note_failure("1.2.3.4")
    t.note_failure("1.2.3.4")
    assert t.throttled("1.2.3.4") is True
    t.clear("1.2.3.4")
    assert t.throttled("1.2.3.4") is False
    assert t.tracked == 0


def test_ips_are_independent() -> None:
    t = IPThrottle(max_failures=2, window_secs=600)
    t.note_failure("1.1.1.1")
    t.note_failure("1.1.1.1")
    assert t.throttled("1.1.1.1") is True
    assert t.throttled("2.2.2.2") is False


# --- password hashing ------------------------------------------------------


def test_password_round_trip() -> None:
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h) is True
    assert verify_password("wrong password", h) is False


@pytest.mark.parametrize("junk", ["", "not-a-hash", "scrypt$x$y$z", "md5$1$1$1$aa$bb"])
def test_broken_hashes_return_false_not_exceptions(junk: str) -> None:
    assert verify_password("anything", junk) is False


def test_planted_scrypt_params_are_refused_instantly() -> None:
    """Memory bomb ka pehra.

    scrypt ~128*n*r bytes maangta hai. n=2**22, r=8 matlab ~4 GB — ek
    login attempt poore process ko le dubta. Params ab whitelist se bahar
    hain to hum scrypt ko haath hi nahi lagate.
    """
    good = hash_password("hello world")
    parts = good.split("$")
    parts[1] = str(2**22)
    planted = "$".join(parts)

    started = time.monotonic()
    assert verify_password("hello world", planted) is False
    assert time.monotonic() - started < 0.1, "scrypt ko chalne hi nahi dena tha"


def test_oversized_dklen_is_refused() -> None:
    parts = hash_password("hello world").split("$")
    parts[5] = "ab" * 200          # 400-byte \"hash\"
    assert verify_password("hello world", "$".join(parts)) is False


def test_issued_hashes_use_whitelisted_params() -> None:
    """hash_password aur verify_password ek doosre se alag na ho jaayen."""
    n, r, p = hash_password("hello world").split("$")[1:4]
    assert (int(n), int(r), int(p)) in _ALLOWED_SCRYPT_PARAMS


def test_short_passwords_are_rejected() -> None:
    with pytest.raises(ValueError):
        hash_password("short")


# --- temp passwords --------------------------------------------------------


def test_temp_password_length_is_fixed_and_hashable() -> None:
    """Purana `[:8]` wala rasta kabhi-kabhi chhota password banata tha."""
    for _ in range(500):
        pw = temp_password()
        assert len(pw) == 13
        assert len(pw) >= 8          # hash_password ka minimum
        hash_password(pw)            # phat-na nahi chahiye


def test_temp_password_has_no_confusable_characters() -> None:
    """WhatsApp par jaata hai, banda haath se type karta hai."""
    joined = "".join(temp_password()[3:] for _ in range(300))
    assert not (set(joined) & set("0O1lI"))


def test_temp_passwords_do_not_repeat() -> None:
    assert len({temp_password() for _ in range(2000)}) == 2000


# --- rate limiter ----------------------------------------------------------


def test_rate_limiter_actually_fires_at_the_default_limit(monkeypatch) -> None:
    """Purana bucket 4096 par capped tha, default limit 6000 — yaani
    `len(dq) >= limit` kabhi sach hi nahi hota tha."""
    from app.config import settings
    import app.main as main_mod

    main_mod._RL_BUCKETS.clear()
    limit = settings.RATE_LIMIT_PER_MIN
    assert limit > 0, "default par limiter chalu hona chahiye"

    allowed = sum(
        0 if main_mod._rate_limited("tenant-x", "/admin/api/orders") else 1
        for _ in range(limit + 50)
    )
    assert allowed == limit
    assert main_mod._rate_limited("tenant-x", "/admin/api/orders") is True
    main_mod._RL_BUCKETS.clear()


def test_rate_limiter_bucket_is_bounded(monkeypatch) -> None:
    from app.config import settings
    import app.main as main_mod

    main_mod._RL_BUCKETS.clear()
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MIN", 10)
    for _ in range(5000):
        main_mod._rate_limited("tenant-y", "/admin/api/orders")
    assert len(main_mod._RL_BUCKETS["tenant-y"]) <= 11
    main_mod._RL_BUCKETS.clear()


def test_rate_limiter_follows_a_changed_limit(monkeypatch) -> None:
    """Suite limit runtime par badalti hai — bucket ko saath chalna chahiye."""
    from app.config import settings
    import app.main as main_mod

    main_mod._RL_BUCKETS.clear()
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MIN", 5)
    results = [main_mod._rate_limited("tenant-z", "/admin/api/orders") for _ in range(8)]
    assert results == [False] * 5 + [True] * 3
    main_mod._RL_BUCKETS.clear()


def test_unlisted_paths_are_not_limited(monkeypatch) -> None:
    from app.config import settings
    import app.main as main_mod

    main_mod._RL_BUCKETS.clear()
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MIN", 1)
    for _ in range(100):
        assert main_mod._rate_limited("tenant-w", "/health") is False
    main_mod._RL_BUCKETS.clear()


def test_zero_disables_the_limiter(monkeypatch) -> None:
    from app.config import settings
    import app.main as main_mod

    main_mod._RL_BUCKETS.clear()
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MIN", 0)
    for _ in range(1000):
        assert main_mod._rate_limited("tenant-v", "/admin/api/orders") is False
    main_mod._RL_BUCKETS.clear()
