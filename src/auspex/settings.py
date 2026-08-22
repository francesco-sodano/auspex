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
    aoai_tokens_per_minute: float = 450_000.0
    # The full GPT-4.1 deployment has a separate 30K TPM quota. Narrative and
    # answer calls must be paced against it rather than the mini deployment's
    # larger extraction budget.
    aoai_narrative_tokens_per_minute: float = 30_000.0
    extraction_concurrency: int = 16

    # --- Entra External ID (federated auth, arc42 F-16) --------------------------------
    #
    # Auspex authenticates against a Microsoft Entra tenant, which may be either:
    #
    # * a **workforce** tenant (`login.microsoftonline.com`), where every principal is
    #   an organisational member or B2B guest; or
    # * an **external** tenant (Microsoft Entra External ID / CIAM,
    #   `<subdomain>.ciamlogin.com`), which is what lets people sign up with a personal
    #   Gmail/Outlook address or a local email + password through a sign-up/sign-in
    #   user flow. Consumer identities are the whole point of an external tenant.
    #
    # Nothing here is derived from a naming convention, because the two tenant types
    # disagree on the exact issuer string and an external tenant may legitimately issue
    # either `https://<sub>.ciamlogin.com/<tenant-id>/v2.0` or
    # `https://<sub>.ciamlogin.com/<sub>.onmicrosoft.com/v2.0` depending on the
    # authority the app was registered with. Guessing wrong rejects every token, so the
    # values below are supplied explicitly by infrastructure and/or discovered from the
    # tenant's own OpenID Connect metadata (`entra_openid_configuration_url`), which is
    # authoritative.
    entra_tenant_id: str = ""
    entra_audience: str = ""
    entra_authority: str = ""
    entra_issuer: str = ""
    entra_jwks_url: str = ""

    # OpenID Connect metadata document. When set, `issuer` and `jwks_uri` are read from
    # it at runtime and take precedence over the static values above — the tenant itself
    # is the only source that cannot be misconfigured.
    entra_openid_configuration_url: str = ""

    # Host that MSAL must be told to trust (`knownAuthorities`). Required for external
    # tenants: MSAL only trusts `login.microsoftonline.com` implicitly, so a
    # `*.ciamlogin.com` authority is rejected unless it is declared. Served to the SPA
    # by `/auth-config.json`.
    entra_known_authority: str = ""

    # Optional scope the SPA must request for the API audience, e.g.
    # `api://<client-id>/Auspex.Access`. Served to the SPA so the token request is not
    # hard-coded in the frontend.
    entra_api_scope: str = ""

    # Migration window. While a deployment moves from one tenant to another (typically
    # workforce -> external), tokens from the previous tenant stay acceptable so the
    # existing owner is not locked out mid-cutover. Both must be set to take effect;
    # clear them once every user has re-authenticated against the new tenant.
    entra_legacy_issuer: str = ""
    entra_legacy_jwks_url: str = ""
    entra_legacy_audience: str = ""

    # The owner's fixed Entra `oid` claim (arc42 §5.7, §11 "user_id derives from
    # the token"). Configuring this lets
    # nightly/bootstrap (which have no live bearer token to read a claim from) derive
    # the same user_id/owner_user_sk an authenticated API request resolves. Existing
    # deployments may instead resolve the owner from `app_users`.
    #
    # With multi-user enabled this no longer *restricts* who may authenticate: it
    # only pins the pre-existing production owner's ledger partition so that
    # owner's historical events stay readable under their own account.
    owner_provider_user_id: str = ""

    # During an issuer cutover, the pre-existing owner's old token carries a
    # different immutable object ID. When all three legacy token settings are
    # enabled, map only this old principal to `owner_provider_user_id`; no other
    # identity receives an alias.
    owner_legacy_provider_user_id: str = ""

    # Multi-user migration safety valve. A deployment whose pre-existing ledger
    # was addressed by a partition value that is *not* the derived user_id (for
    # example one resolved dynamically through the source account's `app_users`
    # container) can pin that exact value here. It is applied only to the
    # principal named by `owner_provider_user_id`, and only at registration, so
    # that owner's historical events stay readable under their own account
    # while every other user is partitioned by their own user_id.
    owner_ledger_partition_key: str = ""

    # --- Multi-user administration (arc42 §5.7 "App user lifecycle") ------------------
    # Names the first administrator by email so a brand-new deployment has someone
    # who can approve everybody else. Consulted *only* while no administrator exists
    # yet; the moment the first admin registers, authority binds permanently to that
    # principal's immutable Entra object ID (`AdminAuthorityBinding`) and changing
    # this value grants nothing.
    initial_admin_email: str = ""

    # Maximum number of ACTIVE users whose per-user nightly stage (portfolio
    # projection, policy, recommendations) runs concurrently. Bounded so a large
    # roster cannot exhaust Cosmos RU or the source ledger's connection budget.
    nightly_user_concurrency: int = 4

    # A DEFERRED recommendation disposition suppresses the same decision signature
    # for this many days, after which the recommendation reappears unchanged.
    deferred_disposition_days: int = 7

    # Maximum age of the token's `auth_time` claim for irreversible account
    # operations (account deletion). Tokens without the claim fall back to the
    # explicit typed-confirmation contract.
    fresh_auth_max_age_seconds: int = 600

    # --- HTTP boundary --------------------------------------------------------------
    # Same-origin production needs no CORS entry. Local Vite development is allowed
    # by default; deployments may replace this comma-separated list.
    cors_allowed_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173"
    )
    jwt_clock_skew_seconds: int = 60
    rate_limit_window_seconds: int = 60
    # Disabled by default for local/tests; infrastructure sets bounded production
    # values explicitly.
    registration_rate_limit: int = 0
    chat_rate_limit: int = 0

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
    pipeline_hard_timeout_minutes: int = 60
    pipeline_target_minutes: int = 25
    #: Ceiling on any *single* pipeline step. Bounds a step that hangs on a
    #: provider or model call well inside the whole-run deadline, instead of
    #: letting one step consume the entire budget on its own.
    pipeline_step_timeout_minutes: int = 30

    # --- reporting currency ------------------------------------------------------------
    book_currency: str = "USD"
    reporting_currency: str = "CHF"

    @property
    def cors_origins(self) -> list[str]:
        return [
            value.strip()
            for value in self.cors_allowed_origins.split(",")
            if value.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
