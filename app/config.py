"""Application settings, loaded from environment variables / .env file.

Every secret and every tunable lives here. No other module should read
os.environ directly — import `settings` from this module instead.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration for the bot.

    Fields without a default are REQUIRED. If one is missing from the
    environment, importing this module raises a ValidationError that names
    the missing field. That is deliberate — we would rather fail loudly at
    startup than send a message with a broken token.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database ---
    # Must use the asyncpg driver, e.g.
    # postgresql+asyncpg://laundry:laundry@localhost:5432/laundry
    DATABASE_URL: str

    # --- Provider switch ---
    # "meta"  = direct Meta Cloud API (test number, development)
    # "dotpe" = DotPe BSP (the real business number lives there)
    WHATSAPP_PROVIDER: Literal["meta", "dotpe"] = "meta"

    # --- WhatsApp Cloud API (Meta Graph API) ---
    WHATSAPP_TOKEN: str
    WHATSAPP_PHONE_NUMBER_ID: str
    # Shared secret we echo back during Meta's GET webhook verification.
    WHATSAPP_VERIFY_TOKEN: str
    # Used to verify the X-Hub-Signature-256 header on incoming webhooks.
    WHATSAPP_APP_SECRET: str

    # --- Backups ---
    # Where pg_dump lives on this machine; nightly backups need it.
    PG_DUMP_PATH: str = r"C:\Program Files\PostgreSQL\15\bin\pg_dump.exe"

    # --- DotPe BSP (only used when WHATSAPP_PROVIDER=dotpe) ---
    # From the DotPe merchant panel -> API section. Empty = dotpe disabled.
    DOTPE_API_KEY: str = ""
    # The WABA number registered with DotPe, digits with country code,
    # e.g. "917644020285".
    DOTPE_WABA_NUMBER: str = ""
    # We set this as the auth header value when configuring DotPe's webhook
    # (their panel lets you add a custom header). Requests without it -> 403.
    DOTPE_WEBHOOK_TOKEN: str = ""

    # --- LLM (Phase 4 AI agent) ---
    # anthropic = Claude (sk-ant-... key) | gemini = Google (AIza... key).
    # Only app/services/llm_client.py reads these.
    LLM_PROVIDER: Literal["anthropic", "gemini"] = "anthropic"
    ANTHROPIC_API_KEY: str
    GEMINI_API_KEY: str = ""

    # --- People ---
    # Manager's WhatsApp number in E.164 form, e.g. +919876543210.
    MANAGER_PHONE: str
    # WhatsApp Business Account id — template management via Graph API
    WHATSAPP_WABA_ID: str = ""
    # Meta app id — webhook subscription self-healing needs it
    WHATSAPP_APP_ID: str = ""
    # Optional second number CC'd on every escalation alert (Ravi).
    ESCALATION_CC_PHONE: str = ""

    # --- Internal admin API ---
    # Sent as the X-API-Key header on /admin and /orders endpoints.
    ADMIN_API_KEY: str

    # --- Shop / locale ---
    SHOP_NAME: str
    # Country code (no '+') assumed when a customer sends a local number.
    DEFAULT_COUNTRY_CODE: str = "91"

    # --- Scheduling behaviour ---
    # No proactive messages between QUIET_HOURS_START and QUIET_HOURS_END.
    # Both are hours in 24h local time. Default: quiet from 21:00 to 09:00.
    QUIET_HOURS_START: int = Field(default=21, ge=0, le=23)
    QUIET_HOURS_END: int = Field(default=9, ge=0, le=23)
    # Days of silence before a customer gets a "we miss you" follow-up.
    FOLLOWUP_DAYS: int = Field(default=14, ge=1, le=365)

    # --- Runtime ---
    ENVIRONMENT: Literal["development", "production"] = "development"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


# Import this, not the class:  from app.config import settings
settings = Settings()
