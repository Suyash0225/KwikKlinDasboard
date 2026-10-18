"""Single-shop mode defaults keep optional SaaS/Instagram publishing off."""

from app.config import settings
from app.services.app_settings import DEFAULTS


def test_saas_subscription_billing_is_off_by_default() -> None:
    assert settings.SAAS_SUBSCRIPTIONS_ENABLED is False


def test_instagram_auto_publish_is_off_by_default() -> None:
    assert DEFAULTS["instagram_auto_publish_enabled"] is False
    # The daily social feature remains enabled to generate posters and owner packs.
    assert DEFAULTS["social_daily_enabled"] is True
