# Engine Rework Phase 1 Reconciliation

This document reconciles the pre-rework architecture with the implementation on
5 August 2026. It is the evidence record for the target design in
`doc/arc42-auspex.md`; it is not an implementation plan for preserving the old
engine.

Status terms:

- **Real** means the suspected behavior exists and is wrong for the target design.
- **Misreading** means the code does something materially different from the suspicion.
- **Already fixed** means the desired behavior is present before Phase 2.

## Changed constraints

The following constraints supersede the pre-rework document and old ADR assumptions:

- `e6b_v2` and `e6b_balanced_v1` are replaced outright. Phase 2 must not add a
  compatibility path, feature flag, or dual publication.
- Bronze NDJSON is retained. Silver and Gold are regenerable and may be dropped and
  rebuilt without migration scripts.
- Historical score manifests, replay-identical score facts, immutable model ledgers,
  and longitudinal cache retention are not requirements.
- Cosmos owner-scoped ledger data is not regenerable and must not be changed or lost.
- Point-in-time `event_date`/`knowledge_date`, owner isolation, deterministic policy,
  connector watermarking, and guaranteed Fabric suspension remain requirements.

## Suspected defects a-i

### a. Manual classification suppresses all score-driven recommendations

**Status: Real.**

**Phase 2 resolution: Implemented.** Cohort assignment is now independent from
theme/VTI linkage, and manual or LLM provenance can be `READY` when all leg contracts
are met.

The old document says that an explicit classification replaces ETF memberships.
Notebook 04 implements that rule with a `left_anti` join in
`classified_theme_memberships`, then emits the manual or LLM row with
`membership_weight = NULL`. `engine/thesis.py` uses `membership_weight` as the only
thesis-linkage input, marks it missing, and returns `PARTIAL` when the cohort is large
enough. `api/auspex_api/recommender/policy.py` suppresses every non-`READY` score with
reason `coverage`.

The current document and code agree, but the design is wrong. All ten portfolio
holdings have manual classifications effective 4 August 2026, so none can become
`READY`; RGTI is additionally `WITHHELD` because its cohort is too small.

**Resolution:** classification chooses the cohort only. Thesis linkage is looked up
independently from the assigned theme's proxy blend for every provenance. Preserve
classification provenance on the score fact and in the UI. A classified security is
`PARTIAL` for linkage only when no quantitative theme or reference weight exists.

**Tradeoff:** a manual label no longer suppresses a valid quantitative observation,
but classification and linkage can disagree. The UI must show both rather than imply
that classification confidence is economic exposure.

### b. Thesis linkage is a size proxy

**Status: Real and empirically confirmed.**

**Phase 2 resolution: Implemented.** Linkage is now the log ratio of assigned-theme
proxy weight to VTI weight; both observations remain PIT-bound and independently
nullable.

`engine/thesis.py` consumes raw `membership_weight`, and Notebook 04 passes the
blended ETF constituent weight without a broad-market control. A read-only Warehouse
check on the 5 August 2026 score date joined each cohort's latest eligible membership
snapshot to the latest PIT market cap. Across 327 rows, Spearman $\rho$ between raw
membership weight and market cap was `0.8334`. Per-theme results were `0.9323` for
AI compute (25 rows), `0.7045` for data-center buildout (148), `0.9792` for energy
security (21), `0.9912` for enterprise technology (74), and `0.9912` for healthcare
(59). The leg is strongly size-loaded in every measured cohort.

**Resolution:** define linkage as the log of theme-blend weight relative to the same
security's weight in a configured broad-market reference. Missing either side makes
the leg unavailable; it is not replaced with zero.

**Tradeoff:** size neutrality is more economically meaningful, but it adds a reference
holdings dependency and excludes securities absent from the reference until another
defensible exposure measure exists.

### c. Attention and crowding are the same variable inverted

**Status: Misreading in the strict form; the overlap is real.**

**Phase 2 resolution: Implemented.** Attention retains current-versus-prior 30-day
change, while crowding uses inverse quarter-over-quarter institutional holder-count
change and no longer reads news volume or holder-count level.

Notebook 04 limits news to 60 days and calculates
`(current_30d - previous_30d) / sqrt(previous_30d)`, with a current-count fallback when
the previous window is zero. `engine/thesis.py` then standardizes that change
cross-sectionally. Attention is therefore a within-security change followed by a
cohort standardization, not a cross-sectional z-score of the 30-day level.

Crowding does use the inverse current 30-day news-count level. The two legs are not
identical inverses, but they reuse the same observation stream and are likely
correlated. No correlation telemetry currently tests that dependence.

**Resolution:** retain attention as an explicitly named within-security change measure.
Remove news volume from crowding. Crowding uses quarter-over-quarter change in distinct
active institutional holder count from PIT 13F facts; if two comparable periods are not
available, the leg is missing rather than synthesized from news.

**Tradeoff:** the legs become economically distinct at the cost of a new market-data
dependency and more honest partial coverage during rollout.

### d. Smart-money zero fill is not neutral

**Status: Real, and the implementation is less honest than the old document says.**

**Phase 2 resolution: Implemented.** Nulls now reach the engine, observed
subcomponent weights renormalize above the `0.50` gate, and no second leg
standardization moves an imputed zero.

The old document says missing smart-money subcomponents receive component z-score zero,
make coverage partial, and can move after leg re-standardization. Notebook 04 actually
coalesces all six inputs to numeric zero or `false` before constructing
`OpportunityObservation`. `_coverage_reasons` therefore never sees them as missing and
can mark the row `READY`. The same zero-fill pattern affects news count, attention, and
institutional holder count.

Even if missingness reached the engine, `_winsorized_z` substitutes zero before the
second leg standardization. A nominally neutral imputation does not remain neutral once
the leg is re-ranked against peers.

**Resolution:** preserve nulls. Standardize each observed subcomponent, combine only
available subcomponents with renormalized configured weights, and require at least
`0.50` of a leg's configured subcomponent weight. Below that threshold, drop the leg
and name it in coverage reasons. Do not apply a second cross-sectional standardization
that lets peer missingness move an observed security's imputed contribution.

**Tradeoff:** masks differ across securities and leg variance is no longer forced to
one by a second ranking pass. That loss of cosmetic scale uniformity is preferable to
distribution-dependent imputation.

### e. Percentile-only policy always promotes a cohort fraction

**Status: Real.**

**Phase 2 resolution: Implemented.** Raw composite is served to deterministic policy;
non-positive raw composite suppresses score-driven increases with `absolute_floor`.

The raw composite is stored in `fact_theme_opportunity_score`, but
`dbo.v_opportunity_score` does not serve it and `CandidateSignal` contains only the
percentile. Policy thresholds at 60, 70, and 80 therefore operate without any absolute
quality floor. In every non-degenerate eligible cohort, some securities qualify for an
increase even when every raw composite is negative.

**Resolution:** serve the raw composite to policy. A score-driven increase requires
both the existing percentile threshold and `raw_composite > 0`. A non-positive raw
composite adds suppression reason `absolute_floor`. Threshold calibration remains a
separate backtest task; zero is the model's natural signed boundary, not a tuned value.

**Tradeoff:** weak cohorts can produce no increases, which is intentional. Raw
composites are not comparable across dates or themes and must not be displayed as a
second consumer score.

### f. `quantum_computing` has no ETF-derived cohort

**Status: Real.**

**Phase 2 resolution: Implemented.** QTUM is a governed weekly ETF component and VTI
is a separate governed broad-market reference.

Notebook 05 adds `quantum_computing` to `dim_theme` with benchmark `QTUM`, but
`theme_component_df` and `bridge_theme_etf` contain no QTUM component. QTUM is reference
metadata only, so `fact_theme_membership` receives no quantum constituents. The manual
RGTI row is consequently a one-security cohort and the eight-security gate returns
`WITHHELD`.

**Resolution:** register QTUM as the `quantum_computing` TRS proxy and ingest its
validated holdings like every other proxy. RGTI's manual classification still chooses
the cohort; its linkage is independently read from QTUM and the broad-market reference.

**Tradeoff:** QTUM's methodology is an imperfect taxonomy, but a governed proxy cohort
is preferable to a permanently unrankable single-security label.

### g. The percentile transform pins 0 and 100

**Status: Real design defect; the implementation correctly follows the old document.**

**Phase 2 resolution: Implemented.** Scores use average-rank Blom positions and the UI
chooses whole-point or one-decimal precision from cohort granularity.

`engine/thesis.py` implements `100 * firstRank / (N - 1)` exactly. Every non-tied
cohort has a 0.0 and 100.0, first-rank tie handling biases ties downward, and a fixed UI
decimal can imply precision finer than the cohort supports.

**Resolution:** use average ranks and the Blom plotting position
`100 * (rank - 3/8) / (N + 1/4)`. It never emits exact endpoints. Display whole points
when adjacent cohort positions are at least one point apart and one decimal otherwise;
always show cohort size or rank context.

**Tradeoff:** familiar round endpoints disappear, while tie treatment and statistical
interpretation improve.

### h. No financing or dilution measurement

**Status: Real gap.**

**Phase 2 resolution: Implemented fail-closed.** PIT financing facts now expose diluted
share growth, cash runway/burn, and S-3/S-3ASR/424B evidence. Policy has no embedded
thresholds; missing external calibration or incomplete records suppress increases with
`financing`.

The six-leg inputs and policy contain no financing-risk field or suppression reason.
Fundamentals already land `shares_outstanding`, and filing ingestion recognizes 424B
forms, but Notebook 04 derives neither diluted-share growth, cash runway against TTM
operating burn, nor shelf-registration evidence. S-3/424B evidence is not joined to the
score.

**Resolution:** publish all three PIT-safe financing signals. Financing is a pre-policy
suppression filter with reason `financing`, not a seventh leg. Phase 2 adds the filter
contract and telemetry; activation thresholds require the separate backtest/calibration
work and must be versioned policy configuration rather than hidden constants.

**Tradeoff:** keeping financing outside the weighted score avoids re-deriving all six
weights and makes the veto explicit. Until thresholds are approved, the signals can be
observed without silently changing recommendations.

### i. `RAGS` is orphaned

**Status: Real documentation defect.**

**Phase 2 resolution: Implemented.** The orphan term is removed; `opportunity_v1` is
the sole score-engine contract and `balanced_v1` the current weight configuration.

`RAGS` appears only in the old glossary. No engine symbol, schema, API contract, or
other architecture section uses it.

**Resolution:** delete it. Opportunity Score is the only score term in the target
architecture.

## Additional incongruences

### Multi-theme serving is not the documented maximum

**Phase 2 resolution: Implemented.** Classification assigns one effective cohort and
Notebook/Warehouse publication rejects duplicate security/date score rows.

The old document says serving selects the highest same-date score outside explicit
classifications. `dbo.v_opportunity_score` returns every theme row. The recommendation
service builds a dictionary keyed only by `security_sk`, so the last row wins without
an ordering guarantee, while candidate construction can still process duplicate theme
rows. The document describes behavior the implementation does not enforce.

**Target resolution:** one effective classification assigns one cohort per security and
date. Serving rejects duplicate effective rows instead of selecting the most favorable
score. This removes max-of-themes selection bias rather than making it deterministic.

### Raw score is calculated but absent from the policy contract

**Phase 2 resolution: Implemented.** Raw composite is carried through Warehouse and
Cosmos into policy but remains excluded from consumer score display.

The fact stores `opportunity_score_raw`; the serving view and `CandidateSignal` omit it.
This prevents the absolute-floor policy required by the target design.

**Target resolution:** publish raw composite to the deterministic policy boundary but
not as a consumer-facing score.

### No leg-dependence telemetry

**Phase 2 resolution: Implemented.** The current Gold release stores the full 6x6
matrix, pair counts, and PC1 share; completion telemetry emits release summaries.

The old risk section acknowledges correlated legs, but the build emits no 6x6
correlation matrix or principal-component concentration measure. Fixed weights do not
diagnose double counting.

**Target resolution:** emit the pairwise-complete six-leg correlation matrix, sample
counts, and PC1 variance share per cohort/date with `DailyBuildCompleted`. Telemetry is
diagnostic and does not dynamically alter weights.

### No score-movement attribution

**Phase 2 resolution: Implemented.** The current release stores the prior-distribution
counterfactual split into own-composite and cohort effects, with reconciliation checks.

Facts contain a point-in-time score but no split between a security's own composite
change and movement of the cohort distribution.

**Target resolution:** decompose day-over-day percentile movement with a counterfactual:
apply the prior cohort distribution to the current raw composite for the own-composite
effect; the residual from switching to the current distribution is the cohort effect.

### Historical machinery exceeds the changed requirements

**Phase 2 resolution: Implemented for the score path.** Regenerable score and
diagnostic tables are rebuilt in place and retired engine versions are rejected. Bronze
and owner-scoped Cosmos ledger data retain their separate preservation contracts.

The old architecture treats immutable score history, content-addressed manifests,
model-version ledgers, and audited release retention as quality requirements. Notebook
04 deletes and rebuilds the bounded current model projection, so the Lakehouse already
does not provide the claimed longitudinal ledger without external Warehouse retention.

**Target resolution:** keep only validation needed for fail-closed current publication:
row counts, current-generation completeness, PIT checks, and atomic replacement.
Historical Silver/Gold model data may be rebuilt from Bronze. Content-addressed caches
may remain as cost/idempotency optimizations, not as immutable records. Existing Cosmos
ledger behavior remains untouched because it protects unrecoverable owner data.

## Target decisions for Phase 2

1. Replace the old score path in place; do not preserve `e6b_v2` behavior.
2. Assign one cohort independently of linkage and retain classification provenance.
3. Add QTUM and a configured broad-market reference to validated ETF holdings.
4. Use size-neutral relative linkage.
5. Keep attention as within-security change; move crowding to quarter-over-quarter
  institutional holder-count change.
6. Renormalize available subcomponents with a `0.50` minimum available-weight gate.
7. Use Blom plotting positions and cohort-aware display precision.
8. Require percentile threshold and positive raw composite for score-driven increases.
9. Publish financing signals and a versioned `financing` suppression boundary without
   calibrating activation thresholds in this phase.
10. Emit leg-correlation/PC1 telemetry and own-versus-cohort score movement attribution.
11. Preserve PIT, owner isolation, watermarking, deterministic policy, and capacity
    suspension; remove score-history machinery that exists only for audit or immutability.

## Rebuild and preservation boundary

Phase 2 may drop and rebuild Silver and Gold analytical tables from retained Bronze
NDJSON. It must not write data migrations. It must not modify the Cosmos ledger schema,
owner partitioning, owner identity resolution, or `knowledge_date` enforcement. The
rebuild must complete before Warehouse/Search/Cosmos analytical projections are
published, and current publication remains fail-closed.