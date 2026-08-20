"""Runtime configuration.

Everything is environment driven so the same image runs in dev, CI and prod.
Defaults are deliberately the *safe* ones: stub providers, deterministic
orchestrator, no real money moved.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Scopes an API key may hold. Anything else in configuration is ignored.
KNOWN_SCOPES = frozenset({"agent", "admin"})

Orchestrator = Literal["deterministic", "crewai"]
ProviderMode = Literal["stub", "http"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SWARM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- app ---
    env: str = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    # --- storage ---
    database_url: str = "sqlite+aiosqlite:///./support_swarm.db"
    session_url: str = "memory://"  # or redis://localhost:6379/0
    session_ttl_seconds: int = 60 * 60 * 24

    # --- orchestration ---
    orchestrator: Orchestrator = "deterministic"
    max_agent_steps: int = 12
    resolution_timeout_seconds: int = 120

    # --- llm (via LiteLLM) ---
    llm_enabled: bool = False
    llm_model_supervisor: str = "claude-opus-5"
    llm_model_worker: str = "claude-sonnet-5"
    llm_model_qa: str = "claude-sonnet-5"
    llm_timeout_seconds: int = 45
    llm_max_retries: int = 2
    llm_max_output_tokens: int = 1024
    llm_redact_prompts: bool = True

    # --- downstream providers ---
    provider_mode: ProviderMode = "stub"
    identity_api_base: str = "http://localhost:9101"
    billing_api_base: str = "http://localhost:9102"
    entitlement_api_base: str = "http://localhost:9103"
    notification_api_base: str = "http://localhost:9104"
    provider_token: str = ""
    provider_timeout_seconds: float = 10.0

    # --- guardrails ---
    refund_auto_approve_cents: int = 20_000          # $200 hard autonomous ceiling
    refund_window_days: int = 90
    refund_daily_total_cap_cents: int = 200_000      # $2000/day across all tickets
    allow_autonomous_financial_actions: bool = True
    block_actions_on_injection: bool = True
    require_qa_pass_before_reply: bool = True

    # --- api auth ---
    #: "key:scope|scope,key2:scope". The two dev keys mirror the two privilege
    #: levels: filing tickets is not the same authority as approving a refund.
    api_keys: str = "dev-key:agent,dev-admin-key:agent|admin"
    escalation_queue: str = "support-eng-tier2"

    @field_validator("database_url")
    @classmethod
    def _no_empty_db(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("database_url must not be empty")
        return v

    def api_key_scopes(self) -> dict[str, set[str]]:
        """Parse ``api_keys`` into ``{key: {scope, ...}}``.

        Malformed entries are dropped, never defaulted. An entry with no
        explicit scope, or only unrecognised scopes, would otherwise turn a
        typo in configuration into a working credential -- a config error must
        fail closed.
        """
        out: dict[str, set[str]] = {}
        for entry in self.api_keys.split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            key, _, raw_scopes = entry.partition(":")
            key = key.strip()
            if not key:
                continue
            scopes = {
                s.strip() for s in raw_scopes.split("|") if s.strip() in KNOWN_SCOPES
            }
            if scopes:
                out[key] = scopes
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test hook — drop the memoised settings instance."""
    get_settings.cache_clear()
