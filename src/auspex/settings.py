"""Application settings.

No secrets or connection strings live here or anywhere else in this codebase
(arc42 TC-04 / TC-06). Every field is either a public endpoint URL/name that
managed identity or Key Vault-backed API keys authenticate against at
runtime, or a tuning knob. Third-party API keys are read from Key Vault by
:mod:`auspex.providers` at call time via ``azure-identity``; they are never
stored, logged, or passed through this settings object.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration.

    All fields have safe defaults for local/test execution. Production values
    are supplied purely via environment variables / container app settings —
    never via files checked into source control.
    """

    model_config = SettingsConfigDict(env_prefix="AUSPEX_", extra="ignore")

    environment: str = "local"

    # --- config & prompt asset roots -------------------------------------------------
    config_dir: Path = Field(default=Path(__file__).resolve().parents[2] / "config")
    prompts_dir: Path = Field(default=Path(__file__).resolve().parents[2] / "prompts")

    # --- Cosmos DB (managed identity; no connection string, arc42 TC-04) --------------
    cosmos_account_endpoint: str = "https://cosmos-auspex.documents.azure.com:443/"
    cosmos_database_name: str = "auspex"

    # --- Live portfolio ledger (arc42 §5.7) --------------------------------------------
    # Valid pre-existing events remain in their Cosmos account. The nightly job has
    # Data Reader; the authenticated API has container-scoped Data Contributor for
    # validated append-only transaction CRUD.
    # Matches AUSPEX_PORTFOLIO_COSMOS_ENDPOINT / AUSPEX_PORTFOLIO_COSMOS_DATABASE /
    # AUSPEX_PORTFOLIO_MAPPING in infra/modules/containerapps.bicep.
    portfolio_cosmos_endpoint: str = ""
    portfolio_cosmos_database: str = ""
    portfolio_mapping: str = ""  # optional absolute path override for portfolio_mapping.yaml

    # Bootstrap step 11 (arc42 §6.3) prints the mapped sample document and a binding
    # summary (lot_level, cash_chf, holdings count, any unmapped tickers) for owner
    # review, then REQUIRES this to be explicitly `true` before proceeding — bootstrap
    # runs unattended for 2.5-5 hours against the real source ledger, so it must never
    # silently continue on an unreviewed binding. There is deliberately no environment
    # or code path that defaults this to `true`: an operator sets
    # `AUSPEX_CONFIRM_PORTFOLIO_BINDING=true` only after reading the logged mapping for
    # *this* run.
    confirm_portfolio_binding: bool = False

    # --- Blob Storage (managed identity) ----------------------------------------------
    blob_account_url: str = "https://stauspex.blob.core.windows.net"
    blob_container_documents: str = "documents"
    blob_container_sections: str = "sections"
    blob_container_exports: str = "exports"

    # --- Key Vault (managed identity) -------------------------------------------------
    key_vault_url: str = "https://kv-auspex.vault.azure.net/"

    # --- Azure OpenAI (managed identity / Cognitive Services OpenAI User) -------------
    aoai_endpoint: str = "https://aoai-auspex.openai.azure.com/"
    aoai_api_version: str = "2024-10-21"
    aoai_deployment_extraction: str = "gpt-4.1-mini"
    aoai_deployment_narrative: str = "gpt-4.1"
    aoai_deployment_planner: str = "gpt-4.1-mini"
    aoai_deployment_answer: str = "gpt-4.1"
    # Confirmed live `gpt-4.1-mini` deployment quota (arc42 §6.3 "Runtime budget":
    # the deployment's tokens-per-minute quota is "the single lever on bootstrap
    # duration"). Sweden Central's regional TPM ceiling is 5,000,000 — well above
    # this, so a further quota increase is available without a region change if
    # bootstrap duration ever needs to shrink below the ~75 min this implies.
    aoai_tokens_per_minute: float = 200_000.0
    # The full GPT-4.1 deployment has a separate 30K TPM quota. Narrative and
    # answer calls must be paced against it rather than the mini deployment's
    # larger extraction budget.
    aoai_narrative_tokens_per_minute: float = 30_000.0

    # --- Entra External ID (federated auth, arc42 F-16) --------------------------------
    entra_tenant_id: str = ""
    entra_audience: str = ""
    entra_authority: str = ""
    entra_issuer: str = ""
    entra_jwks_url: str = ""

    # The owner's fixed Entra `oid` claim (arc42 §5.7, §11 "user_id derives from
    # the token"). Configuring this lets
    # nightly/bootstrap (which have no live bearer token to read a claim from) derive
    # the same user_id/owner_user_sk an authenticated API request resolves. Existing
    # deployments may instead resolve the owner from `app_users`.
    owner_provider_user_id: str = ""

    # --- Provider endpoints (API keys resolved from Key Vault at call time) ----------
    # Alpha Vantage is the default price/FX provider. Tiingo remains available
    # behind the same interfaces when configured.
    alpha_vantage_base_url: str = "https://www.alphavantage.co"
    tiingo_base_url: str = "https://api.tiingo.com"
    finnhub_base_url: str = "https://finnhub.io/api/v1"
    finnhub_rate_limit_per_second: float = 1.0
    fx_base_url: str = "https://api.exchangerate.host"
    edgar_base_url: str = "https://data.sec.gov"
    edgar_www_base_url: str = "https://www.sec.gov"
    edgar_user_agent: str = "Auspex/1.0 (contact@example.com)"
    edgar_rate_limit_per_second: float = 8.0

    # Key Vault secret *names* (never the key values themselves) for the price/FX
    # and news providers, matching infra/modules/containerapps.bicep's
    # AUSPEX_PRICE_API_KEY_SECRET / AUSPEX_NEWS_API_KEY_SECRET env vars.
    price_api_key_secret: str = "ALPHAVANTAGE-API-KEY"
    news_api_key_secret: str = "FINNHUB-API-KEY"

    # --- pipeline tuning ---------------------------------------------------------------
    pipeline_hard_timeout_minutes: int = 45
    pipeline_target_minutes: int = 25

    # --- reporting currency ------------------------------------------------------------
    book_currency: str = "USD"
    reporting_currency: str = "CHF"


@lru_cache
def get_settings() -> Settings:
    return Settings()
