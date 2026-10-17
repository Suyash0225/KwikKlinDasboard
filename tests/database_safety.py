"""Test database safety checks, kept independent of application imports."""

from urllib.parse import urlsplit


def database_identity(url: str) -> tuple[str, int, str]:
    """Compare database target, ignoring credentials and driver suffix."""
    parsed = urlsplit(url.strip())
    database = parsed.path.lstrip("/").split("/", 1)[0]
    if not parsed.hostname or not database:
        raise ValueError("database URL must include a host and database name")
    return (parsed.hostname.lower(), parsed.port or 5432, database)


def validate_test_database_url(
    test_url: str,
    candidate_urls: list[tuple[str, str]],
    *,
    ci: bool,
) -> str:
    """Require a test URL and reject any configured development DB collision.

    CI's DATABASE_URL / APP_DATABASE_URL intentionally point at the disposable
    migration/test database, so CI compares only an explicit DEV_DATABASE_URL.
    Local runs compare against both ordinary app database URLs.
    """
    test_url = (test_url or "").strip()
    if not test_url:
        raise RuntimeError(
            "TEST_DATABASE_URL is required. Tests refuse to fall back to DATABASE_URL."
        )
    test_identity = database_identity(test_url)
    for label, candidate in candidate_urls:
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        if ci and label != "DEV_DATABASE_URL":
            continue
        if database_identity(candidate) == test_identity:
            raise RuntimeError(
                f"TEST_DATABASE_URL points at the same database as {label}; "
                "refusing to run tests against a development database."
            )
    return test_url
