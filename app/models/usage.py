"""Every LLM call, recorded — so "kitna use hua, kitne me khatam hoga" has
an answer instead of a guess.

Tokens are stored raw; money is computed at read time from the rate card in
settings. That way a price change re-prices the whole history correctly and
the owner can put in his own numbers without a migration.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    provider: Mapped[str] = mapped_column(String(20), index=True)  # gemini | anthropic
    model: Mapped[str] = mapped_column(String(60), index=True)
    # what the call was for: reply | extract | vision | query | social ...
    purpose: Mapped[str] = mapped_column(String(24), default="other", index=True)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ok: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<LlmUsage {self.model} in={self.input_tokens} out={self.output_tokens}>"
