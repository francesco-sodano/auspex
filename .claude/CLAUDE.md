# Project Context

**Product:** Auspex (codename: FIP — Financial Insight Platform). MVP SaaS personal financial assistant: ingests market/regulatory/macro data daily, ranks equities by growth potential, tracks a user portfolio, and suggests buy/sell/hold actions. Advisory only — never executes trades or moves money.

### Hard constraints
- **Azure first-party services only** — no Databricks, Snowflake, Confluent.
- **Microsoft Fabric is the data platform** — OneLake (storage), Notebooks/PySpark (transforms), Warehouse (T-SQL serving).
- **Region: Switzerland North.** All resources pinned here.
- **Batch/scheduled only (v1).** Daily cadence; no streaming.
- **IaC: Bicep** for Azure resources; Fabric items via Fabric Git integration.

### Languages & formats
| Layer | Tech |
|-------|------|
| Source connectors | Python (Azure Functions, Flex Consumption) |
| Fabric transforms | PySpark (Fabric Notebooks) |
| Gold serving | T-SQL (Fabric Warehouse) |
| Frontend | React SPA (Azure Static Web Apps) |
| Web API | Python (Azure Functions) |
| IaC | Bicep |

- Raw data: JSON/NDJSON in bronze; Delta/Parquet in silver; Warehouse tables in gold.
- All timestamps **UTC**. Money normalized to **USD** via `fact_fx_rate` at `event_date`.
- Portfolio valued in **CHF** by default (configurable in `app_config`).

### Medallion architecture (OneLake)
```
bronze (raw NDJSON) -> silver (Delta, parsed/deduped/resolved) -> gold (Warehouse star schema)
```
- **Bronze path:** `bronze/{source_id}/{yyyy}/{mm}/{dd}/{batch_id}.ndjson`
- **Silver tables:** `insider_txn`, `news`, `prices`, `holdings_13f`, `ownership_events`, `contracts`, `macro`, `fundamentals`, `fx`, `portfolio_position`
- **Gold:** `dim_security` (SCD2), `dim_date`, `dim_entity`, `dim_source` + fact tables + metric views + `recommendation`

### Building blocks
| Block | Service | Responsibility |
|-------|---------|---------------|
| Source connectors | Azure Functions | Fetch -> bronze -> advance watermark |
| Control plane | Cosmos DB serverless | Source registry, watermarks, run log, dedup |
| Orchestration | Fabric Data Factory | Daily: connectors -> notebooks -> indexing -> agent |
| Transforms | Fabric Notebooks (PySpark) | Bronze->silver, entity resolution, metrics |
| Vector serving | Azure AI Search + Azure OpenAI | Hybrid+vector index over news/filings |
| Capacity scheduler | Timer Function | Resume Fabric -> trigger -> suspend (cost guard) |
| Web API | Azure Functions | Only public data surface; enforces per-user isolation |
| Frontend | React SPA (SWA) | Candidates, portfolio, evidence, grounded chat |
| Auth | Entra External ID | Federated sign-in (Microsoft/Google/GitHub); no Auspex passwords |
| Secrets | Key Vault | Source API keys via managed identity; no secrets in code |

### Critical correctness rules
- **Point-in-time (PIT):** every fact carries `event_date` (when it happened) + `knowledge_date` (when Auspex first knew it). All queries filter `knowledge_date <= @asof`. Zero look-ahead.
- **Idempotency:** bronze uses deterministic `batch_id`; silver uses Delta `MERGE` on natural keys. Replays must converge.
- **Watermarks advance only after** a successful bronze write.
- **13F caution:** `knowledge_date` = filing date (up to 45 days after quarter end), not the quarter-end date.

### Connector contract (every source implements)
Every connector extends `BaseConnector` (Python ABC): `fetch(since)` for source-specific REST, `run()` orchestrates read watermark -> fetch -> dedup check -> write bronze -> advance watermark. Bronze envelope wraps the raw record untouched with `ingest_ts`, `source_id`, `schema_version`, `batch_id`, `watermark_from`. One Function per source; secrets via Key Vault managed identity.

### Per-user data isolation
Every per-user row carries `owner_user_sk`. The web API resolves the Entra principal and filters **every** query and mutation by it. No un-scoped data-access method exists — cross-user access is structurally impossible. Shared signal data (prices, filings, RAGS features) is not per-user.

### Naming conventions
- Azure resources: `fip-{env}-{component}` (e.g., `fip-prod-func`)
- Cosmos containers: `lower_snake`
- Warehouse: `dim_*`, `fact_*`, `v_*` (views), `metric_weights` (config table)

### Repository layout
```
fip/
  infra/        # Bicep: main.bicep + modules/ + params/{dev,prod}.json
  connectors/   # shared/base_connector.py + one folder per source
  fabric/       # notebooks/  pipelines/  warehouse/
  web/          # React SPA
  api/          # Azure Functions web API
  search/       # AI Search schema + indexing job
  tests/        # PIT, idempotency, DQ, contract tests
  .github/workflows/
```

### Data sources (v1)
`sec_form4`, `sec_13f`, `sec_13dg`, `sec_8k` (SEC EDGAR — free/official), `prices_eod` (Finnhub free), `prices_yf` (Yahoo fallback — **unofficial, not licensable**), `fundamentals` (FMP free), `news` (Finnhub/RSS), `macro_fred` (FRED), `macro_snb_ecb` (ECB/SNB FX), `contracts` (USASpending.gov), `portfolio` (manual entry). MVP runs on free/public tiers; commercial licensing is Phase 2.

### Implementation order (epics)
E1 Foundation -> E2 Control plane -> E3 Connector framework -> E4 Silver + entity resolution -> E5 Gold -> E6 Metrics -> E7 Vector serving -> E8 Remaining connectors -> E9 Web app -> E19 Identity/isolation -> E10 Hardening -> E11 Web API -> E12 Portfolio management -> E13 Backtesting -> E14 Valuation/quality -> E15 Cost/tax-aware recommender -> E16 Agent + guardrails -> E17 Explainability/UX -> E18 Chat/i18n/notifications. DoD per epic: code + Bicep merged; idempotent re-run verified; PIT tests pass; observability emitting.

### MVP scope
**In:** all planned data sources (free tiers), full gold star schema, RAGS score + backtest harness, manual portfolio entry, multi-user with per-user isolation, stocks/ETFs only, English only.
**Out (accepted, not defects):** trade execution, bank integration, broker CSV import, localization, bonds/funds, Private Endpoints/WAF, per-bank tenant isolation, advanced book-level risk analytics.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" -> "Write tests for invalid inputs, then make them pass"
- "Fix the bug" -> "Write a test that reproduces it, then make it pass"
- "Refactor X" -> "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] -> verify: [check]
2. [Step] -> verify: [check]
3. [Step] -> verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.