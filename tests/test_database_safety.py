"""Guard against accidentally running the test suite on a development DB."""

import pytest

from tests.database_safety import validate_test_database_url


def test_guard_rejects_same_database_even_with_different_credentials() -> None:
    test_url = "postgresql+asyncpg://test_role:secret@localhost:5432/laundry"
    dev_url = "postgresql+asyncpg://owner:other@localhost:5432/laundry"
    with pytest.raises(RuntimeError, match="same database as DATABASE_URL"):
        validate_test_database_url(test_url, [("DATABASE_URL", dev_url)], ci=False)


def test_guard_accepts_a_separate_test_database() -> None:
    test_url = "postgresql+asyncpg://test_role:secret@localhost:5432/laundry_test"
    dev_url = "postgresql+asyncpg://owner:other@localhost:5432/laundry"
    assert validate_test_database_url(
        test_url, [("DATABASE_URL", dev_url)], ci=False
    ) == test_url


def test_guard_requires_explicit_test_database() -> None:
    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL is required"):
        validate_test_database_url("", [("DATABASE_URL", "postgresql://localhost/laundry")], ci=False)


def test_ci_ignores_migration_url_but_still_checks_explicit_dev_url() -> None:
    test_url = "postgresql+asyncpg://app:secret@localhost:5432/kwikklin_test"
    migration_url = "postgresql+asyncpg://owner:secret@localhost:5432/kwikklin_test"
    assert validate_test_database_url(
        test_url, [("DATABASE_URL", migration_url)], ci=True
    ) == test_url
    with pytest.raises(RuntimeError, match="same database as DEV_DATABASE_URL"):
        validate_test_database_url(
            test_url, [("DEV_DATABASE_URL", migration_url)], ci=True
        )
