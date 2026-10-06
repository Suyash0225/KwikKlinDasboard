"""Application settings, loaded from environment variables / .env file.

Every secret and every tunable lives here. No other module should read
os.environ directly — import `settings` from this module instead.
"""

import os
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
        env_file=os.getenv("KWIKKLIN_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database ---
    # Must use the asyncpg driver, e.g.
    # postgresql+asyncpg://laundry:laundry@localhost:5432/laundry
    DATABASE_URL: str
    # App ka apna DB role — NOSUPERUSER, taaki RLS uspar sach mein lage.
    # KHALI ho to DATABASE_URL hi chalta hai (yaani kuch todta nahi), par
    # tab RLS bypass hoti hai aur startup hardening uspar chillata hai.
    # Banane ke liye: python -m scripts.create_app_role
    APP_DATABASE_URL: str = ""

    # --- Provider switch ---
    # "meta"  = direct Meta Cloud API (test number, development)
    # "dotpe" = DotPe BSP (the real business number lives there)
    WHATSAPP_PROVIDER: Literal["waha", "meta", "dotpe"] = "waha"

    # --- WhatsApp Cloud API (Meta Graph API) ---
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    # Meta webhook credentials (unused by WAHA).
    WHATSAPP_VERIFY_TOKEN: str = ""
    WHATSAPP_APP_SECRET: str = ""

    # --- WAHA (self-hosted WhatsApp Web) ---
    # Example: http://waha:3000 when both containers share a Docker network.
    WAHA_BASE_URL: str = ""
    WAHA_API_KEY: str = ""
    WAHA_SESSION: str = "default"
    WAHA_WEBHOOK_SECRET: str = ""
    # Backward-compatible name used by the existing EC2 WAHA container.
    # New deployments should prefer WAHA_WEBHOOK_SECRET.
    WAHA_WEBHOOK_HMAC_KEY: str = ""

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
    LLM_PROVIDER: Literal["anthropic", "gemini", "openrouter"] = "openrouter"
    # Provider used automatically when the primary provider is unavailable.
    # "none" disables cross-provider failover.
    LLM_FALLBACK_PROVIDER: Literal["none", "anthropic", "gemini", "openrouter"] = "anthropic"
    LLM_SECONDARY_FALLBACK_PROVIDER: Literal["none", "anthropic", "gemini", "openrouter"] = "openrouter"
    ANTHROPIC_API_KEY: str
    GEMINI_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "nvidia/nemotron-3.5-lightning:free"

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
    # Fernet key — tenants.wa_token (har dukaan ka WhatsApp token) isi se
    # DB mein encrypted rehta hai. Khali = plaintext (purana single-shop
    # deploy chalta rahe), par startup par WARNING. Banane ke liye:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    TOKEN_ENCRYPTION_KEY: str = ""
    # Vendor (platform) ka apna master key — /control ke liye. Pehle
    # ADMIN_API_KEY hi dukaan ka bhi tha aur vendor ka bhi: ek string leak =
    # home dukaan ka dashboard AUR sab 60 dukaanon ka control, dono khule.
    # Khali = ADMIN_API_KEY hi chalta hai (purana deploy), par WARNING.
    VENDOR_API_KEY: str = ""
    # Browser login for the vendor Control Room.
    # NOTE: this is intentionally configurable via the production .env;
    # never commit the actual password to Git.
    CONTROL_LOGIN_ID: str = ""
    CONTROL_LOGIN_PASSWORD: str = ""

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
    # --- Selling the software (Razorpay) ---
    # Khali chhodne par billing OFF rehta hai: signup phir bhi chalta hai,
    # tenant trial mein baith jaata hai. Isse local dev bina keys ke chalta hai.
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    # Razorpay dashboard mein webhook banate waqt jo secret set karo, wahi.
    RAZORPAY_WEBHOOK_SECRET: str = ""
    # Public URL jahan checkout/callback wapas aayega (tunnel ya domain).
    APP_BASE_URL: str = "http://127.0.0.1:8000"
    # Public website ka pakka domain (canonical, sitemap, OG, JSON-LD) —
    # production: https://kwikklin.online. Khali = request ke host se (dev).
    SITE_URL: str = ""

    # --- Google se login ---
    # Google Cloud Console -> APIs & Services -> Credentials -> OAuth client ID
    # (type: Web application). Redirect URI wahan EXACTLY ye daalein:
    #   <APP_BASE_URL>/api/auth/google/callback
    # Khali chhodne par Google button page par dikhta hi nahi.
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # --- Website (/laundry) par live Google reviews ---
    # Google Cloud Console -> "Places API (New)" enable -> API key banao.
    # Khali chhodne par site par reviews ki jagah "Read reviews on Google"
    # button dikhta hai. PLACE_ID khali ho to dukaan ka naam se dhoondh lete hain.
    GOOGLE_PLACES_API_KEY: str = ""
    GOOGLE_PLACE_ID: str = ""

    # --- Google Analytics 4 Data API (optional dashboard reporting) ---
    # Property id is the numeric GA4 property id, e.g. 123456789.
    # The refresh token must be granted analytics.readonly by the same OAuth client.
    GA4_PROPERTY_ID: str = ""
    GA4_REFRESH_TOKEN: str = ""

    ENVIRONMENT: Literal["development", "production"] = "production"

    # Per-tenant API rate limit (requests/min). Ek runaway client (loop mein
    # fansa script, scraper) sabko slow na kare. DDoS-scale ke liye upar
    # CDN/proxy hai. 0 = off.
    #
    # Pehle 6000 tha — "itna ooncha ki kabhi nahi chhuega". Wo sach tha, aur
    # isi liye ye pehra kabhi laga hi nahi; upar se limiter ka bucket 4096 par
    # capped tha, to 6000 tak pahunchna possible hi nahi tha (dekhein
    # main.py `_rate_limited`). 600/min = 10 req/sec per tenant — asli
    # dashboard use isse bahut neeche rehta hai, par loop mein fansi script
    # ab sach mein rukti hai.
    RATE_LIMIT_PER_MIN: int = 600
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


# Import this, not the class:  from app.config import settings
settings = Settings()
