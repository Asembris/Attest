"""Configuration for Attest.

Model selection is per-step and never hardcoded. Each pipeline step resolves its
model through `settings.model_for(step)`, which falls back to `model_default`
when that step has no explicit override. This exists so a single step (most
likely semantic entailment) can be moved to a stronger model without touching
code or dragging the cheap steps up with it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Step(StrEnum):
    """Pipeline steps that make an LLM call. Each resolves its own model."""

    CLAIM_EXTRACTION = "claim_extraction"
    EVIDENCE_SELECTION = "evidence_selection"
    ENTAILMENT = "entailment"
    VERDICT = "verdict"


class Settings(BaseSettings):
    # protected_namespaces=() so our `model_*` fields don't collide with
    # pydantic's reserved `model_` prefix.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")

    datahub_gms_url: str = Field(default="http://localhost:8080", alias="DATAHUB_GMS_URL")
    datahub_token: str = Field(default="", alias="DATAHUB_TOKEN")

    model_default: str = Field(default="gpt-4o-mini", alias="ATTEST_MODEL_DEFAULT")
    model_claim_extraction: str | None = Field(
        default=None, alias="ATTEST_MODEL_CLAIM_EXTRACTION"
    )
    model_evidence_selection: str | None = Field(
        default=None, alias="ATTEST_MODEL_EVIDENCE_SELECTION"
    )
    model_entailment: str | None = Field(default=None, alias="ATTEST_MODEL_ENTAILMENT")
    model_verdict: str | None = Field(default=None, alias="ATTEST_MODEL_VERDICT")

    def model_for(self, step: Step) -> str:
        """Resolve the model for a step, falling back to the default."""
        override = getattr(self, f"model_{step.value}", None)
        return override or self.model_default


settings = Settings()
