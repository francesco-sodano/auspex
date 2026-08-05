# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "00000000-0000-4000-8000-000000000002",
# META       "default_lakehouse_name": "auspex_bronze",
# META       "default_lakehouse_workspace_id": "00000000-0000-4000-8000-000000000001",
# META       "known_lakehouses": [
# META         {
# META           "id": "00000000-0000-4000-8000-000000000002"
# META         }
# META       ]
# META     }
# META   }
# META }

# CELL ********************

# Fabric Notebook: nb_04_metrics
# Builds the E6a metric layer and stable security feature contract from E5 gold facts.
# Attaches to: auspex_bronze (default lakehouse)
#
# Current implemented source coverage:
# - fact_market_daily -> momentum, realized risk, risk-adjusted context
# - fact_insider_txn  -> smart-money insider metrics
#
# E6b final Opportunity Score is intentionally not published here; it depends on
# E8 source completion and the E14 valuation-brake leg.

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
from math import sqrt
import os
import sys
import tempfile
from delta.tables import DeltaTable
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql.types import (
    BooleanType, DateType, DecimalType, DoubleType, IntegerType, LongType,
    StringType, StructField, StructType, TimestampType,
)

OPPORTUNITY_ENGINE_LAKEHOUSE_PATH = "Files/config/e14/d861397782bbb25849db40eb22bb76c574bf76c8be344d8d1328d3c18b559ad2.py"
OPPORTUNITY_ENGINE_SHA256 = "d861397782bbb25849db40eb22bb76c574bf76c8be344d8d1328d3c18b559ad2"

opportunity_engine_source = mssparkutils.fs.head(OPPORTUNITY_ENGINE_LAKEHOUSE_PATH, 1024 * 1024)
opportunity_engine_bytes = opportunity_engine_source.encode("utf-8")
if hashlib.sha256(opportunity_engine_bytes).hexdigest() != OPPORTUNITY_ENGINE_SHA256:
    raise RuntimeError("E14/E6b engine resource hash mismatch")
opportunity_engine_path = os.path.join(tempfile.gettempdir(), "thesis_e6b_v2.py")
with open(opportunity_engine_path, "wb") as opportunity_engine_file:
    opportunity_engine_file.write(opportunity_engine_bytes)
opportunity_engine_spec = importlib.util.spec_from_file_location("thesis", opportunity_engine_path)
if opportunity_engine_spec is None or opportunity_engine_spec.loader is None:
    raise RuntimeError(f"Could not load E14/E6b engine resource: {opportunity_engine_path}")
opportunity_engine = importlib.util.module_from_spec(opportunity_engine_spec)
sys.modules[opportunity_engine_spec.name] = opportunity_engine
opportunity_engine_spec.loader.exec_module(opportunity_engine)
os.remove(opportunity_engine_path)
OPPORTUNITY_MODEL_VERSION = opportunity_engine.MODEL_VERSION
OPPORTUNITY_WEIGHT_VERSION = opportunity_engine.WEIGHT_VERSION
OPPORTUNITY_LEG_WEIGHTS = opportunity_engine.LEG_WEIGHTS
OpportunityObservation = opportunity_engine.OpportunityObservation
score_theme = opportunity_engine.score_theme

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# PARAMETERS CELL ********************

priority_as_of_date = ""

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Helpers ---
_RF_ANNUAL_DEFAULT = 0.02
spark.conf.set("spark.advise.divisionExprConvertRule.enable", "true")

parsed_priority_as_of_date = None
if priority_as_of_date:
    try:
        parsed_priority_as_of_date = date.fromisoformat(priority_as_of_date)
    except ValueError as exc:
        raise ValueError("priority_as_of_date must use YYYY-MM-DD") from exc


def _require_table(table_name: str) -> None:
    if not spark.catalog.tableExists(table_name):
        raise RuntimeError(f"Required E5 table is missing: {table_name}")


def _ensure_columns(table_name: str, column_specs: dict[str, str]) -> None:
    existing = set(spark.table(table_name).columns)
    for column_name, ddl in column_specs.items():
        if column_name not in existing:
            spark.sql(f"ALTER TABLE {table_name} ADD COLUMNS ({ddl})")


def _merge_all(table_name: str, source_df, condition: str) -> None:
    target = DeltaTable.forName(spark, table_name)
    (
        target
        .alias("t")
        .merge(source_df.alias("s"), condition)
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    print(f"Merged source_rows={metrics.get('numSourceRows', 'unknown')} into {table_name}")


def _replace_delta_projection(table_name: str, select_sql: str) -> None:
    for drop_sql in (f"DROP VIEW IF EXISTS {table_name}", f"DROP TABLE IF EXISTS {table_name}"):
        try:
            spark.sql(drop_sql)
        except Exception:
            pass

    spark.sql(f"CREATE TABLE {table_name} USING DELTA AS {select_sql}")
    print(f"Materialized {table_name}")


for required in [
    "dim_security", "dim_date", "fact_market_daily", "fact_insider_txn",
    "silver_theme_membership", "fact_theme_membership",
    "security_theme_classification",
    "fact_fundamental_anchor", "fact_narrative_intensity", "narrative_snapshot_manifest",
    "fundamental_anchor_snapshot_manifest", "fact_narrative_premium",
    "narrative_premium_snapshot_manifest",
]:
    _require_table(required)

if parsed_priority_as_of_date is not None:
    resolved_priority_as_of_date = (
        spark.table("fact_market_daily")
        .filter(F.col("event_date") <= F.lit(parsed_priority_as_of_date))
        .agg(F.max("event_date").alias("event_date"))
        .first()
        .event_date
    )
    if resolved_priority_as_of_date is None:
        raise ValueError(
            "priority_as_of_date has no market session on or before it"
        )
    parsed_priority_as_of_date = resolved_priority_as_of_date

_ensure_columns("fact_theme_membership", {
    "snapshot_batch_id": "snapshot_batch_id STRING",
    "snapshot_ingest_ts": "snapshot_ingest_ts TIMESTAMP",
})
theme_snapshot_provenance = (
    spark.table("silver_theme_membership")
    .select(
        "theme_id", "security_sk", "event_date", "theme_revision_hash",
        F.col("batch_id").alias("snapshot_batch_id"),
        F.col("ingest_ts").alias("snapshot_ingest_ts"),
    )
)
theme_snapshot_provenance_earliest = (
    theme_snapshot_provenance
    .withColumn(
        "compatibility_row_number",
        F.row_number().over(
            Window.partitionBy(
                "theme_id", "security_sk", "event_date", "theme_revision_hash"
            ).orderBy(
                F.col("snapshot_ingest_ts").asc(),
                F.col("snapshot_batch_id").asc(),
            )
        ),
    )
    .filter(F.col("compatibility_row_number") == 1)
    .drop("compatibility_row_number")
)
theme_snapshot_provenance_by_batch = (
    theme_snapshot_provenance
    .withColumn(
        "compatibility_row_number",
        F.row_number().over(
            Window.partitionBy(
                "theme_id", "security_sk", "event_date", "theme_revision_hash",
                "snapshot_batch_id",
            ).orderBy(F.col("snapshot_ingest_ts").asc())
        ),
    )
    .filter(F.col("compatibility_row_number") == 1)
    .drop("compatibility_row_number")
)
theme_snapshot_provenance_by_ingest = (
    theme_snapshot_provenance
    .withColumn(
        "compatibility_row_number",
        F.row_number().over(
            Window.partitionBy(
                "theme_id", "security_sk", "event_date", "theme_revision_hash",
                "snapshot_ingest_ts",
            ).orderBy(F.col("snapshot_batch_id").asc())
        ),
    )
    .filter(F.col("compatibility_row_number") == 1)
    .drop("compatibility_row_number")
)
(
    DeltaTable.forName(spark, "fact_theme_membership")
    .alias("t")
    .merge(
        theme_snapshot_provenance_earliest.alias("s"),
        "t.theme_id = s.theme_id AND t.security_sk = s.security_sk "
        "AND t.event_date = s.event_date AND t.theme_revision_hash = s.theme_revision_hash",
    )
    .whenMatchedUpdate(
        condition="t.snapshot_batch_id IS NULL AND t.snapshot_ingest_ts IS NULL",
        set={
            "snapshot_batch_id": "s.snapshot_batch_id",
            "snapshot_ingest_ts": "s.snapshot_ingest_ts",
        },
    )
    .execute()
)
(
    DeltaTable.forName(spark, "fact_theme_membership")
    .alias("t")
    .merge(
        theme_snapshot_provenance_by_batch.alias("s"),
        "t.theme_id = s.theme_id AND t.security_sk = s.security_sk "
        "AND t.event_date = s.event_date AND t.theme_revision_hash = s.theme_revision_hash "
        "AND t.snapshot_batch_id = s.snapshot_batch_id",
    )
    .whenMatchedUpdate(
        condition="t.snapshot_ingest_ts IS NULL",
        set={"snapshot_ingest_ts": "s.snapshot_ingest_ts"},
    )
    .execute()
)
(
    DeltaTable.forName(spark, "fact_theme_membership")
    .alias("t")
    .merge(
        theme_snapshot_provenance_by_ingest.alias("s"),
        "t.theme_id = s.theme_id AND t.security_sk = s.security_sk "
        "AND t.event_date = s.event_date AND t.theme_revision_hash = s.theme_revision_hash "
        "AND t.snapshot_ingest_ts = s.snapshot_ingest_ts",
    )
    .whenMatchedUpdate(
        condition="t.snapshot_batch_id IS NULL",
        set={"snapshot_batch_id": "s.snapshot_batch_id"},
    )
    .execute()
)
missing_theme_snapshot_provenance = spark.table("fact_theme_membership").filter(
    F.col("snapshot_batch_id").isNull() | F.col("snapshot_ingest_ts").isNull()
).count()
if missing_theme_snapshot_provenance:
    raise RuntimeError(
        "Theme Gold rows are missing atomic snapshot provenance: "
        f"{missing_theme_snapshot_provenance}"
    )

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Metric config ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS metric_weights (
        metric_name    STRING        NOT NULL,
        metric_group   STRING        NOT NULL,
        weight         DECIMAL(9,6)  NOT NULL,
        direction      INT           NOT NULL,
        is_active      BOOLEAN       NOT NULL,
        required_epic  STRING,
        version        STRING        NOT NULL,
        effective_from DATE          NOT NULL,
        effective_to   DATE          NOT NULL,
        updated_at     TIMESTAMP
    )
    USING DELTA
""")

_ensure_columns("metric_weights", {
    "required_epic": "required_epic STRING",
    "version": "version STRING",
    "effective_from": "effective_from DATE",
    "effective_to": "effective_to DATE",
    "updated_at": "updated_at TIMESTAMP",
})

weights_schema = StructType([
    StructField("metric_name", StringType(), False),
    StructField("metric_group", StringType(), False),
    StructField("weight", DecimalType(9, 6), False),
    StructField("direction", IntegerType(), False),
    StructField("is_active", BooleanType(), False),
    StructField("required_epic", StringType(), True),
    StructField("version", StringType(), False),
    StructField("effective_from", DateType(), False),
    StructField("effective_to", DateType(), False),
    StructField("updated_at", TimestampType(), True),
])

weights_rows = [
    ("momentum_3m", "composite_growth_score", Decimal("0.250000"), 1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
    ("momentum_6m", "composite_growth_score", Decimal("0.150000"), 1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
    ("momentum_12m", "composite_growth_score", Decimal("0.100000"), 1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
    ("realized_vol_30d", "composite_growth_score", Decimal("0.100000"), -1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
    ("insider_net_buy_ratio_90d", "composite_growth_score", Decimal("0.250000"), 1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
    ("insider_cluster_buy_30d", "composite_growth_score", Decimal("0.150000"), 1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
    *[
        (
            leg_name,
            "opportunity_score",
            Decimal(f"{weight:.6f}"),
            1,
            True,
            "E14/E6b",
            OPPORTUNITY_WEIGHT_VERSION,
            date(1900, 1, 1),
            date(9999, 12, 31),
            None,
        )
        for leg_name, weight in OPPORTUNITY_LEG_WEIGHTS.items()
    ],
]
weights_df = spark.createDataFrame(weights_rows, weights_schema).withColumn("updated_at", F.current_timestamp())
(
    DeltaTable.forName(spark, "metric_weights")
    .alias("t")
    .merge(
        weights_df.alias("s"),
        "t.metric_name = s.metric_name AND t.version = s.version "
        "AND t.effective_from = s.effective_from",
    )
    .whenNotMatchedInsertAll()
    .execute()
)

weight_sum = (
    spark.table("metric_weights")
    .filter((F.col("metric_group") == "composite_growth_score") & (F.col("is_active") == True) & (F.col("version") == "e6a_v1"))
    .agg(F.round(F.sum("weight"), 6).alias("weight_sum"))
    .collect()[0]
    .weight_sum
)
if weight_sum != Decimal("1.000000"):
    raise RuntimeError(f"metric_weights for composite_growth_score must sum to 1.000000, got {weight_sum}")
opportunity_active_weights = {
    row.metric_name: float(row.weight)
    for row in spark.table("metric_weights")
    .filter(
        (F.col("metric_group") == "opportunity_score")
        & (F.col("is_active") == True)
        & (F.col("version") == OPPORTUNITY_WEIGHT_VERSION)
    )
    .select("metric_name", "weight")
    .collect()
}
opportunity_weight_sum = (
    spark.table("metric_weights")
    .filter(
        (F.col("metric_group") == "opportunity_score")
        & (F.col("is_active") == True)
        & (F.col("version") == OPPORTUNITY_WEIGHT_VERSION)
    )
    .agg(F.round(F.sum("weight"), 6).alias("weight_sum"))
    .collect()[0]
    .weight_sum
)
if opportunity_weight_sum != Decimal("1.000000"):
    raise RuntimeError(
        f"metric_weights for opportunity_score must sum to 1.000000, got {opportunity_weight_sum}"
    )

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Feature contract table ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS security_daily_features (
        security_sk                   BIGINT       NOT NULL,
        date_sk                       INT          NOT NULL,
        ticker                        STRING,
        company_name                  STRING,
        gics_sector                   STRING,
        country                       STRING,
        as_of                         DATE         NOT NULL,
        close                         DECIMAL(18,6),
        ret_1d                        DECIMAL(12,8),
        momentum_3m                   DOUBLE,
        momentum_6m                   DOUBLE,
        momentum_12m                  DOUBLE,
        rel_strength_sector           DOUBLE,
        realized_vol_30d              DOUBLE,
        realized_vol_90d              DOUBLE,
        realized_vol_252d             DOUBLE,
        downside_deviation_252d       DOUBLE,
        max_drawdown_252d             DOUBLE,
        beta_252d                     DOUBLE,
        illiquidity                   DOUBLE,
        ann_return_252d               DOUBLE,
        sharpe_252d                   DOUBLE,
        sortino_252d                  DOUBLE,
        calmar_252d                   DOUBLE,
        info_ratio_252d               DOUBLE,
        insider_net_buy_ratio_90d     DOUBLE,
        insider_cluster_buy_30d       INT,
        inst_net_flow_qoq             DOUBLE,
        inst_new_initiations          INT,
        activist_13d_flag             BOOLEAN,
        news_sentiment_ewma_14d       DOUBLE,
        news_count_30d                DOUBLE,
        news_volume_z_30d             DOUBLE,
        contract_award_usd_trailing_90d DOUBLE,
        pe_ratio                      DOUBLE,
        peg_ratio                     DOUBLE,
        ps_ratio                      DOUBLE,
        ev_ebitda                     DOUBLE,
        profit_margin                 DOUBLE,
        rev_growth_yoy                DOUBLE,
        fcf_yield                     DOUBLE,
        net_debt_to_ebitda            DOUBLE,
        fundamental_anchor_z          DOUBLE,
        fundamental_anchor_method     STRING,
        fundamental_anchor_imputed_flags STRING,
        institutional_holder_count_120d INT,
        narrative_intensity           DOUBLE,
        narrative_coverage_status      STRING,
        narrative_coverage_reasons_json STRING,
        narrative_premium             DOUBLE,
        narrative_premium_coverage_status STRING,
        narrative_premium_coverage_reasons_json STRING,
        narrative_decision_id         STRING,
        anchor_support_z              DOUBLE,
        divergence_state              STRING,
        narrative_is_converging       BOOLEAN,
        composite_growth_score        DOUBLE,
        opportunity_score             DOUBLE,
        score_status                  STRING,
        max_knowledge_date            DATE,
        stale_sources_json            STRING,
        feature_built_at              TIMESTAMP NOT NULL
    )
    USING DELTA
""")

_ensure_columns("security_daily_features", {
    "realized_vol_30d": "realized_vol_30d DOUBLE",
    "realized_vol_90d": "realized_vol_90d DOUBLE",
    "score_status": "score_status STRING",
    "max_knowledge_date": "max_knowledge_date DATE",
    "stale_sources_json": "stale_sources_json STRING",
    "news_count_30d": "news_count_30d DOUBLE",
    "pe_ratio": "pe_ratio DOUBLE",
    "peg_ratio": "peg_ratio DOUBLE",
    "ps_ratio": "ps_ratio DOUBLE",
    "ev_ebitda": "ev_ebitda DOUBLE",
    "profit_margin": "profit_margin DOUBLE",
    "rev_growth_yoy": "rev_growth_yoy DOUBLE",
    "fcf_yield": "fcf_yield DOUBLE",
    "net_debt_to_ebitda": "net_debt_to_ebitda DOUBLE",
    "fundamental_anchor_method": "fundamental_anchor_method STRING",
    "fundamental_anchor_imputed_flags": "fundamental_anchor_imputed_flags STRING",
    "institutional_holder_count_120d": "institutional_holder_count_120d INT",
    "narrative_coverage_status": "narrative_coverage_status STRING",
    "narrative_coverage_reasons_json": "narrative_coverage_reasons_json STRING",
    "narrative_premium_coverage_status": "narrative_premium_coverage_status STRING",
    "narrative_premium_coverage_reasons_json": "narrative_premium_coverage_reasons_json STRING",
    "narrative_decision_id": "narrative_decision_id STRING",
    "anchor_support_z": "anchor_support_z DOUBLE",
    "narrative_is_converging": "narrative_is_converging BOOLEAN",
    "feature_built_at": "feature_built_at TIMESTAMP",
})
spark.sql("""
    UPDATE security_daily_features
    SET opportunity_score = NULL,
        score_status = 'THEME_CONTEXT_REQUIRED'
    WHERE opportunity_score IS NOT NULL
       OR score_status IS NULL
       OR score_status <> 'THEME_CONTEXT_REQUIRED'
""")

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_theme_opportunity_score (
        score_id STRING NOT NULL,
        generation STRING NOT NULL,
        cohort_snapshot_hash STRING NOT NULL,
        theme_id STRING NOT NULL,
        security_sk BIGINT NOT NULL,
        date_sk INT NOT NULL,
        as_of DATE NOT NULL,
        candidate_source STRING NOT NULL,
        candidate_snapshot_id STRING,
        candidate_snapshot_ingest_ts TIMESTAMP,
        candidate_count INT NOT NULL,
        thesis_linkage_z DOUBLE,
        attention_acceleration_z DOUBLE,
        smart_money_z DOUBLE,
        fundamental_health_z DOUBLE,
        valuation_brake_z DOUBLE,
        crowding_positioning_z DOUBLE,
        thesis_linkage_contribution DOUBLE,
        attention_acceleration_contribution DOUBLE,
        smart_money_contribution DOUBLE,
        fundamental_health_contribution DOUBLE,
        valuation_brake_contribution DOUBLE,
        crowding_positioning_contribution DOUBLE,
        opportunity_score_raw DOUBLE,
        opportunity_score DOUBLE,
        coverage_status STRING NOT NULL,
        coverage_reasons_json STRING NOT NULL,
        max_knowledge_date DATE NOT NULL,
        model_version STRING NOT NULL,
        weight_version STRING NOT NULL,
        created_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
_ensure_columns("fact_theme_opportunity_score", {
    "candidate_snapshot_id": "candidate_snapshot_id STRING",
    "candidate_snapshot_ingest_ts": "candidate_snapshot_ingest_ts TIMESTAMP",
})
spark.sql("""
    CREATE TABLE IF NOT EXISTS opportunity_score_snapshot_manifest (
        generation STRING NOT NULL,
        as_of_date DATE NOT NULL,
        model_version STRING NOT NULL,
        weight_version STRING NOT NULL,
        status STRING NOT NULL,
        row_count BIGINT NOT NULL,
        ready_count BIGINT NOT NULL,
        partial_count BIGINT NOT NULL,
        withheld_count BIGINT NOT NULL,
        fingerprint STRING NOT NULL,
        created_at TIMESTAMP NOT NULL,
        completed_at TIMESTAMP
    ) USING DELTA
""")
DeltaTable.forName(spark, "fact_theme_opportunity_score").delete()
DeltaTable.forName(spark, "opportunity_score_snapshot_manifest").delete()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Incremental market metrics from PIT-versioned market facts ---
_MARKET_LOOKBACK_CALENDAR_DAYS = 550
_MAX_MARKET_SNAPSHOT_DATES_PER_RUN = 7

market_revisions = (
    spark.table("fact_market_daily")
    .filter(
        F.col("security_sk").isNotNull()
        & F.col("event_date").isNotNull()
        & F.col("knowledge_date").isNotNull()
        & F.col("price_revision_hash").isNotNull()
    )
    .select(
        "security_sk",
        F.col("event_date").alias("price_event_date"),
        "close",
        "volume",
        F.col("knowledge_date").alias("price_knowledge_date"),
        F.col("ingest_ts").alias("price_ingest_ts"),
        F.col("revision_loaded_at").alias("revision_loaded_at"),
        "price_revision_hash",
    )
)

market_source_updates = (
    market_revisions
    .withColumn(
        "as_of",
        F.greatest(F.col("price_event_date"), F.col("price_knowledge_date")).alias("as_of"),
    )
    .groupBy("as_of")
    .agg(F.max("revision_loaded_at").alias("date_revision_loaded_at"))
)
source_freshness_window = Window.orderBy("as_of").rowsBetween(
    Window.unboundedPreceding, Window.currentRow
)
market_source_freshness = market_source_updates.withColumn(
    "source_revision_loaded_at",
    F.max("date_revision_loaded_at").over(source_freshness_window),
)

feature_freshness = (
    spark.table("security_daily_features")
    .groupBy("as_of")
    .agg(F.min("feature_built_at").alias("feature_built_at"))
)
anchor_manifest_window = Window.partitionBy("as_of_date").orderBy(
    F.col("completed_at").desc(),
    F.col("generation").desc(),
)
latest_anchor_manifests = (
    spark.table("fundamental_anchor_snapshot_manifest")
    .filter(
        (F.col("status") == F.lit("completed"))
        & (F.col("model_version") == F.lit("e20_v2"))
    )
    .withColumn("manifest_row_number", F.row_number().over(anchor_manifest_window))
    .filter(F.col("manifest_row_number") == 1)
    .drop("manifest_row_number")
)
narrative_manifest_window = Window.partitionBy("as_of_date").orderBy(
    F.col("completed_at").desc(),
    F.col("generation").desc(),
)
latest_narrative_manifests = (
    spark.table("narrative_snapshot_manifest")
    .filter(F.col("status") == F.lit("completed"))
    .withColumn("manifest_row_number", F.row_number().over(narrative_manifest_window))
    .filter(F.col("manifest_row_number") == 1)
    .drop("manifest_row_number")
)
narrative_intensity_facts = (
    spark.table("fact_narrative_intensity").alias("i")
    .join(
        latest_narrative_manifests.alias("m"),
        (F.col("i.extraction_generation") == F.col("m.generation"))
        & (F.col("i.date_sk") == F.date_format(F.col("m.as_of_date"), "yyyyMMdd").cast(IntegerType())),
        "inner",
    )
    .filter(
        (F.col("i.model_version") == F.lit("gpt-4o:2024-11-20"))
        & (F.col("i.prompt_version") == F.lit("e21_narrative_v1"))
        & (F.col("i.coverage_status") != F.lit("WITHHELD"))
        & F.col("i.narrative_intensity").isNotNull()
        & (F.col("i.event_date") <= F.col("i.knowledge_date"))
        & (F.col("i.knowledge_date") <= F.col("m.as_of_date"))
    )
    .select(
        F.col("i.security_sk").alias("security_sk"),
        F.col("i.date_sk").alias("date_sk"),
        F.col("m.as_of_date").alias("as_of"),
        F.col("i.narrative_intensity").cast(DoubleType()).alias("narrative_intensity"),
        F.col("i.coverage_status").alias("narrative_coverage_status"),
        F.col("i.coverage_reasons_json").alias("narrative_coverage_reasons_json"),
        F.col("i.knowledge_date").alias("narrative_knowledge_date"),
    )
)
premium_manifest_window = Window.partitionBy("as_of_date").orderBy(
    F.col("completed_at").desc(),
    F.col("generation").desc(),
)
latest_premium_manifests = (
    spark.table("narrative_premium_snapshot_manifest")
    .filter(F.col("status") == F.lit("completed"))
    .withColumn("manifest_row_number", F.row_number().over(premium_manifest_window))
    .filter(F.col("manifest_row_number") == 1)
    .drop("manifest_row_number")
)
narrative_premium_facts = (
    spark.table("fact_narrative_premium").alias("p")
    .join(
        latest_premium_manifests.alias("m"),
        (F.col("p.generation") == F.col("m.generation"))
        & (F.col("p.date_sk") == F.date_format(F.col("m.as_of_date"), "yyyyMMdd").cast(IntegerType())),
        "inner",
    )
    .join(
        latest_narrative_manifests.alias("n"),
        (F.col("p.extraction_generation") == F.col("n.generation"))
        & (F.col("p.e21_manifest_fingerprint") == F.col("n.fingerprint"))
        & (F.col("p.date_sk") == F.date_format(F.col("n.as_of_date"), "yyyyMMdd").cast(IntegerType())),
        "inner",
    )
    .join(
        latest_anchor_manifests.alias("a"),
        (F.col("p.e20_generation") == F.col("a.generation"))
        & (F.col("p.e20_manifest_fingerprint") == F.col("a.fingerprint"))
        & (F.col("p.date_sk") == F.date_format(F.col("a.as_of_date"), "yyyyMMdd").cast(IntegerType())),
        "inner",
    )
    .filter(
        (F.col("p.model_version") == F.lit("e22_v4"))
        & (F.col("p.event_date") <= F.col("p.knowledge_date"))
        & (F.col("p.knowledge_date") <= F.col("m.as_of_date"))
    )
    .select(
        F.col("p.security_sk").alias("security_sk"),
        F.col("p.date_sk").alias("date_sk"),
        F.col("m.as_of_date").alias("as_of"),
        F.col("p.narrative_premium").cast(DoubleType()).alias("narrative_premium"),
        F.col("p.coverage_status").alias("narrative_premium_coverage_status"),
        F.col("p.coverage_reasons_json").alias("narrative_premium_coverage_reasons_json"),
        F.col("p.decision_id").alias("narrative_decision_id"),
        F.col("p.anchor_support_z").cast(DoubleType()).alias("anchor_support_z"),
        F.col("p.divergence_state").alias("divergence_state"),
        F.col("p.is_converging").alias("narrative_is_converging"),
        F.col("p.knowledge_date").alias("narrative_premium_knowledge_date"),
    )
)
all_stale_snapshot_dates = (
    market_source_freshness
    .join(feature_freshness, "as_of", "left")
    .filter(
        F.col("feature_built_at").isNull()
        | (F.col("source_revision_loaded_at") > F.col("feature_built_at"))
    )
    .select("as_of")
    .orderBy("as_of")
)
anchor_feature_differences = (
    spark.table("fact_fundamental_anchor").alias("a")
    .filter(F.col("a.model_version") == "e20_v2")
    .join(
        spark.table("security_daily_features").alias("f"),
        (F.col("a.security_sk") == F.col("f.security_sk"))
        & (F.col("a.date_sk") == F.col("f.date_sk")),
        "left",
    )
    .filter(~F.col("a.fundamental_anchor_z").eqNullSafe(F.col("f.fundamental_anchor_z")))
    .select(F.to_date(F.col("a.date_sk").cast(StringType()), "yyyyMMdd").alias("as_of"))
    .distinct()
)
deleted_anchor_feature_dates = (
    spark.table("security_daily_features").alias("f")
    .filter(F.col("f.fundamental_anchor_z").isNotNull())
    .join(
        spark.table("fact_fundamental_anchor").alias("a").filter(
            (F.col("a.model_version") == "e20_v2")
            & F.col("a.fundamental_anchor_z").isNotNull()
        ),
        (F.col("a.security_sk") == F.col("f.security_sk"))
        & (F.col("a.date_sk") == F.col("f.date_sk")),
        "left_anti",
    )
    .select(F.col("f.as_of").alias("as_of"))
    .distinct()
)
narrative_feature_differences = (
    narrative_intensity_facts.alias("n")
    .join(
        spark.table("security_daily_features").alias("f"),
        (F.col("n.security_sk") == F.col("f.security_sk"))
        & (F.col("n.date_sk") == F.col("f.date_sk")),
        "left",
    )
    .filter(
        ~F.col("n.narrative_intensity").eqNullSafe(F.col("f.narrative_intensity"))
        | ~F.col("n.narrative_coverage_status").eqNullSafe(F.col("f.narrative_coverage_status"))
        | ~F.col("n.narrative_coverage_reasons_json").eqNullSafe(F.col("f.narrative_coverage_reasons_json"))
    )
    .select(F.col("n.as_of").alias("as_of"))
    .distinct()
)
deleted_narrative_feature_dates = (
    spark.table("security_daily_features").alias("f")
    .filter(F.col("f.narrative_intensity").isNotNull())
    .join(
        narrative_intensity_facts.alias("n"),
        (F.col("n.security_sk") == F.col("f.security_sk"))
        & (F.col("n.date_sk") == F.col("f.date_sk")),
        "left_anti",
    )
    .select(F.col("f.as_of").alias("as_of"))
    .distinct()
)
premium_feature_differences = (
    narrative_premium_facts.alias("p")
    .join(
        spark.table("security_daily_features").alias("f"),
        (F.col("p.security_sk") == F.col("f.security_sk"))
        & (F.col("p.date_sk") == F.col("f.date_sk")),
        "left",
    )
    .filter(
        ~F.col("p.narrative_premium").eqNullSafe(F.col("f.narrative_premium"))
        | ~F.col("p.narrative_premium_coverage_status").eqNullSafe(F.col("f.narrative_premium_coverage_status"))
        | ~F.col("p.narrative_premium_coverage_reasons_json").eqNullSafe(F.col("f.narrative_premium_coverage_reasons_json"))
        | ~F.col("p.narrative_decision_id").eqNullSafe(F.col("f.narrative_decision_id"))
        | ~F.col("p.anchor_support_z").eqNullSafe(F.col("f.anchor_support_z"))
        | ~F.col("p.divergence_state").eqNullSafe(F.col("f.divergence_state"))
        | ~F.col("p.narrative_is_converging").eqNullSafe(F.col("f.narrative_is_converging"))
    )
    .select(F.col("p.as_of").alias("as_of"))
    .distinct()
)
deleted_premium_feature_dates = (
    spark.table("security_daily_features").alias("f")
    .filter(F.col("f.narrative_premium_coverage_status").isNotNull())
    .join(
        narrative_premium_facts.alias("p"),
        (F.col("p.security_sk") == F.col("f.security_sk"))
        & (F.col("p.date_sk") == F.col("f.date_sk")),
        "left_anti",
    )
    .select(F.col("f.as_of").alias("as_of"))
    .distinct()
)
theme_source_freshness = (
    spark.table("security_daily_features").select("as_of").distinct().alias("d")
    .join(
        spark.table("fact_theme_membership").alias("m"),
        (F.col("m.event_date") <= F.col("d.as_of"))
        & (F.col("m.knowledge_date") <= F.col("d.as_of"))
        & (F.col("m.is_ground_truth") == F.lit(True)),
        "inner",
    )
    .groupBy(F.col("d.as_of").alias("as_of"))
    .agg(F.max(F.col("m.snapshot_ingest_ts")).alias("theme_source_updated_at"))
)
classification_source_freshness = (
    spark.table("security_daily_features").select("as_of").distinct().alias("d")
    .join(
        spark.table("security_theme_classification").alias("c"),
        (F.col("c.effective_from") <= F.col("d.as_of"))
        & (F.col("c.effective_to").isNull() | (F.col("d.as_of") < F.col("c.effective_to"))),
        "inner",
    )
    .groupBy(F.col("d.as_of").alias("as_of"))
    .agg(F.max(F.col("c.updated_at")).alias("classification_source_updated_at"))
)
score_manifest_freshness = (
    spark.table("opportunity_score_snapshot_manifest")
    .filter(
        (F.col("status") == F.lit("completed"))
        & (F.col("model_version") == F.lit(OPPORTUNITY_MODEL_VERSION))
        & (F.col("weight_version") == F.lit(OPPORTUNITY_WEIGHT_VERSION))
    )
    .groupBy(F.col("as_of_date").alias("as_of"))
    .agg(F.max("completed_at").alias("score_completed_at"))
)
active_weight_updated_at = (
    spark.table("metric_weights")
    .filter(
        (F.col("metric_group") == F.lit("opportunity_score"))
        & (F.col("version") == F.lit(OPPORTUNITY_WEIGHT_VERSION))
        & (F.col("is_active") == F.lit(True))
    )
    .agg(F.max("updated_at").alias("updated_at"))
    .first()
    .updated_at
)
score_stale_dates = (
    theme_source_freshness
    .join(classification_source_freshness, "as_of", "left")
    .join(score_manifest_freshness, "as_of", "left")
    .filter(
        F.col("score_completed_at").isNull()
        | (F.col("theme_source_updated_at") > F.col("score_completed_at"))
        | (F.col("classification_source_updated_at") > F.col("score_completed_at"))
        | (
            F.lit(active_weight_updated_at).isNotNull()
            & (F.lit(active_weight_updated_at) > F.col("score_completed_at"))
        )
    )
    .select("as_of")
)
all_stale_snapshot_dates = (
    all_stale_snapshot_dates
    .unionByName(anchor_feature_differences)
    .unionByName(deleted_anchor_feature_dates)
    .unionByName(narrative_feature_differences)
    .unionByName(deleted_narrative_feature_dates)
    .unionByName(premium_feature_differences)
    .unionByName(deleted_premium_feature_dates)
    .unionByName(score_stale_dates)
    .distinct()
    .orderBy("as_of")
)
if parsed_priority_as_of_date is None:
    processing_dates = all_stale_snapshot_dates.limit(_MAX_MARKET_SNAPSHOT_DATES_PER_RUN)
else:
    priority_processing_dates = all_stale_snapshot_dates.filter(
        F.col("as_of") == F.lit(parsed_priority_as_of_date)
    ).limit(1)
    priority_is_stale = not priority_processing_dates.isEmpty()
    remaining_limit = _MAX_MARKET_SNAPSHOT_DATES_PER_RUN - int(priority_is_stale)
    other_processing_dates = all_stale_snapshot_dates.filter(
        F.col("as_of") != F.lit(parsed_priority_as_of_date)
    ).limit(remaining_limit)
    processing_dates = priority_processing_dates.unionByName(other_processing_dates)
if processing_dates.isEmpty():
    print("No stale market snapshot dates; feature merge is a no-op")

processing_date_values = [row.as_of.isoformat() for row in processing_dates.collect()]
print(f"Market snapshot dates this run: {processing_date_values}")
deferred_stale_dates = all_stale_snapshot_dates.join(processing_dates, "as_of", "left_anti")
if not deferred_stale_dates.isEmpty():
    (
        DeltaTable.forName(spark, "fact_theme_opportunity_score")
        .alias("t")
        .merge(deferred_stale_dates.alias("s"), "t.as_of = s.as_of")
        .whenMatchedDelete()
        .execute()
    )
    (
        DeltaTable.forName(spark, "opportunity_score_snapshot_manifest")
        .alias("t")
        .merge(deferred_stale_dates.alias("s"), "t.as_of_date = s.as_of")
        .whenMatchedDelete()
        .execute()
    )

feature_calendar_dates = (
    spark.table("security_daily_features")
    .select(F.col("as_of").alias("cal_date"))
    .unionByName(processing_dates.select(F.col("as_of").alias("cal_date")))
    .filter(F.col("cal_date").isNotNull())
    .distinct()
)
observed_market_dates = (
    market_revisions
    .select(F.col("price_event_date").alias("cal_date"))
    .filter(F.col("cal_date").isNotNull())
    .distinct()
    .withColumn("observed_trading_day", F.lit(True))
)
existing_date_flags = (
    spark.table("dim_date")
    .select(
        "cal_date",
        F.col("is_trading_day").alias("existing_trading_day"),
    )
)
feature_date_df = (
    feature_calendar_dates
    .join(existing_date_flags, "cal_date", "left")
    .join(observed_market_dates, "cal_date", "left")
    .withColumn("date_sk", F.date_format("cal_date", "yyyyMMdd").cast(IntegerType()))
    .withColumn("year", F.year("cal_date"))
    .withColumn("quarter", F.quarter("cal_date"))
    .withColumn("month", F.month("cal_date"))
    .withColumn("day", F.dayofmonth("cal_date"))
    .withColumn(
        "is_trading_day",
        F.coalesce(F.col("existing_trading_day"), F.lit(False))
        | F.coalesce(F.col("observed_trading_day"), F.lit(False)),
    )
    .withColumn(
        "fiscal_quarter",
        F.concat(F.year("cal_date").cast("string"), F.lit("Q"), F.quarter("cal_date").cast("string")),
    )
    .select("date_sk", "cal_date", "year", "quarter", "month", "day", "is_trading_day", "fiscal_quarter")
)
_merge_all("dim_date", feature_date_df, "t.date_sk = s.date_sk")

market_asof_prices = (
    F.broadcast(processing_dates).alias("dates")
    .join(
        market_revisions.alias("prices"),
        (F.col("prices.price_event_date") <= F.col("dates.as_of"))
        & (
            F.col("prices.price_event_date")
            >= F.date_sub(F.col("dates.as_of"), _MARKET_LOOKBACK_CALENDAR_DAYS)
        )
        & (F.col("prices.price_knowledge_date") <= F.col("dates.as_of")),
        "inner",
    )
    .select(
        F.col("prices.security_sk").alias("security_sk"),
        F.col("dates.as_of").alias("as_of"),
        F.col("prices.price_event_date").alias("price_event_date"),
        F.col("prices.close").alias("close"),
        F.col("prices.volume").alias("volume"),
        F.col("prices.price_knowledge_date").alias("price_knowledge_date"),
        F.col("prices.price_ingest_ts").alias("price_ingest_ts"),
        F.col("prices.revision_loaded_at").alias("revision_loaded_at"),
        F.col("prices.price_revision_hash").alias("price_revision_hash"),
    )
)

market_revision_window = Window.partitionBy(
    "security_sk", "as_of", "price_event_date"
).orderBy(
    F.col("price_knowledge_date").desc(),
    F.col("price_ingest_ts").desc_nulls_last(),
    F.col("price_revision_hash").desc(),
)
market_asof_prices = (
    market_asof_prices
    .withColumn("revision_row_number", F.row_number().over(market_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .drop("revision_row_number")
)

price_window = Window.partitionBy("security_sk", "as_of").orderBy("price_event_date")
market_snapshot_window = Window.partitionBy("security_sk", "as_of")
rows_30 = price_window.rowsBetween(-29, 0)
rows_90 = price_window.rowsBetween(-89, 0)
rows_252 = price_window.rowsBetween(-251, 0)
asof_latest_window = Window.partitionBy("security_sk", "as_of").orderBy(
    F.col("price_event_date").desc()
)

market_base = (
    market_asof_prices
    .withColumn("prev_close", F.lag("close").over(price_window))
    .withColumn(
        "ret_1d",
        F.when(
            F.col("prev_close").isNotNull() & (F.col("prev_close") > 0),
            (F.col("close").cast("double") / F.col("prev_close").cast("double")) - F.lit(1.0),
        ).cast(DecimalType(12, 8)),
    )
    .withColumn(
        "market_knowledge_date",
        F.max("price_knowledge_date").over(market_snapshot_window),
    )
    .withColumn("date_sk", F.date_format("as_of", "yyyyMMdd").cast(IntegerType()))
    .withColumn("close_d", F.col("close").cast(DoubleType()))
    .withColumn("ret_1d_d", F.col("ret_1d").cast(DoubleType()))
    .withColumn("dollar_volume", F.col("close_d") * F.col("volume").cast(DoubleType()))
    .withColumn("close_lag_63", F.lag("close_d", 63).over(price_window))
    .withColumn("close_lag_126", F.lag("close_d", 126).over(price_window))
    .withColumn("close_lag_252", F.lag("close_d", 252).over(price_window))
    .withColumn("momentum_3m", F.when(F.col("close_lag_63") > 0, F.col("close_d") / F.col("close_lag_63") - F.lit(1.0)))
    .withColumn("momentum_6m", F.when(F.col("close_lag_126") > 0, F.col("close_d") / F.col("close_lag_126") - F.lit(1.0)))
    .withColumn("momentum_12m", F.when(F.col("close_lag_252") > 0, F.col("close_d") / F.col("close_lag_252") - F.lit(1.0)))
    .withColumn("realized_vol_30d", F.stddev_samp("ret_1d_d").over(rows_30) * F.lit(sqrt(252.0)))
    .withColumn("realized_vol_90d", F.stddev_samp("ret_1d_d").over(rows_90) * F.lit(sqrt(252.0)))
    .withColumn("realized_vol_252d", F.stddev_samp("ret_1d_d").over(rows_252) * F.lit(sqrt(252.0)))
    .withColumn("downside_sq", F.pow(F.least(F.coalesce(F.col("ret_1d_d"), F.lit(0.0)) - F.lit(_RF_ANNUAL_DEFAULT / 252.0), F.lit(0.0)), 2))
    .withColumn("downside_deviation_252d", F.sqrt(F.avg("downside_sq").over(rows_252)) * F.lit(sqrt(252.0)))
    .withColumn("running_max_252d", F.max("close_d").over(rows_252))
    .withColumn("drawdown", F.when(F.col("running_max_252d") > 0, F.col("close_d") / F.col("running_max_252d") - F.lit(1.0)))
    .withColumn("max_drawdown_252d", F.min("drawdown").over(rows_252))
    .withColumn("avg_dollar_volume_30d", F.avg("dollar_volume").over(rows_30))
    .withColumn("illiquidity", F.when(F.col("avg_dollar_volume_30d") > 0, F.lit(1.0) / F.col("avg_dollar_volume_30d")))
    .withColumn("log_return", F.when(F.col("ret_1d_d") > -1, F.log1p("ret_1d_d")))
    .withColumn("ret_count_252d", F.count("log_return").over(rows_252))
    .withColumn("sum_log_return_252d", F.sum("log_return").over(rows_252))
    .withColumn(
        "ann_return_252d",
        F.when(F.col("ret_count_252d") > 1, F.exp(F.col("sum_log_return_252d") * F.lit(252.0) / F.col("ret_count_252d")) - F.lit(1.0)),
    )
    .withColumn("sharpe_252d", F.when(F.col("realized_vol_252d") > 0, (F.col("ann_return_252d") - F.lit(_RF_ANNUAL_DEFAULT)) / F.col("realized_vol_252d")))
    .withColumn("sortino_252d", F.when(F.col("downside_deviation_252d") > 0, (F.col("ann_return_252d") - F.lit(_RF_ANNUAL_DEFAULT)) / F.col("downside_deviation_252d")))
    .withColumn("calmar_252d", F.when(F.col("max_drawdown_252d") < 0, F.col("ann_return_252d") / F.abs(F.col("max_drawdown_252d"))))
)

sector_lookup = (
    spark.table("dim_security")
    .filter(F.col("is_current") == True)
    .select("security_sk", "ticker", "company_name", "gics_sector", "country")
)
market_with_security = market_base.join(sector_lookup, on="security_sk", how="left")
market_latest = (
    market_with_security
    .withColumn("asof_row_number", F.row_number().over(asof_latest_window))
    .filter(F.col("asof_row_number") == 1)
    .drop("asof_row_number")
)
sector_medians = (
    market_latest
    .withColumn("sector_bucket", F.coalesce(F.col("gics_sector"), F.lit("UNKNOWN")))
    .groupBy("date_sk", "sector_bucket")
    .agg(F.expr("percentile_approx(momentum_3m, 0.5)").alias("sector_median_momentum_3m"))
)
market_metrics = (
    market_latest
    .withColumn("sector_bucket", F.coalesce(F.col("gics_sector"), F.lit("UNKNOWN")))
    .join(sector_medians, ["date_sk", "sector_bucket"], "left")
    .withColumn("rel_strength_sector", F.col("momentum_3m") - F.col("sector_median_momentum_3m"))
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Smart-money metrics from PIT-clean Form 4 facts ---
asof_df = market_metrics.select("security_sk", "date_sk", "as_of")
insider = (
    spark.table("fact_insider_txn")
    .filter(
        F.col("security_sk").isNotNull()
        & F.col("event_date").isNotNull()
        & F.col("knowledge_date").isNotNull()
    )
    .select("security_sk", "entity_sk", "is_buy", "value_usd", "event_date", "knowledge_date")
)

insider_join = asof_df.alias("d").join(
    insider.alias("i"),
    (F.col("d.security_sk") == F.col("i.security_sk"))
    & (F.col("i.event_date") <= F.col("d.as_of"))
    & (F.col("i.knowledge_date") <= F.col("d.as_of"))
    & (F.col("i.event_date") >= F.date_sub(F.col("d.as_of"), 89)),
    "left",
)
insider_90d = (
    insider_join
    .groupBy(F.col("d.security_sk").alias("security_sk"), F.col("d.date_sk").alias("date_sk"))
    .agg(
        F.sum(F.when(F.col("i.is_buy") == True, F.coalesce(F.col("i.value_usd").cast(DoubleType()), F.lit(0.0))).otherwise(F.lit(0.0))).alias("buy_value_90d"),
        F.sum(F.when(F.col("i.is_buy") == False, F.coalesce(F.col("i.value_usd").cast(DoubleType()), F.lit(0.0))).otherwise(F.lit(0.0))).alias("sell_value_90d"),
        F.max(F.col("i.knowledge_date")).alias("insider_knowledge_date_90d"),
    )
    .withColumn(
        "insider_net_buy_ratio_90d",
        F.when(
            (F.col("buy_value_90d") + F.col("sell_value_90d")) > 0,
            (F.col("buy_value_90d") - F.col("sell_value_90d")) / (F.col("buy_value_90d") + F.col("sell_value_90d")),
        ),
    )
)

insider_join_30d = asof_df.alias("d").join(
    insider.alias("i"),
    (F.col("d.security_sk") == F.col("i.security_sk"))
    & (F.col("i.event_date") <= F.col("d.as_of"))
    & (F.col("i.knowledge_date") <= F.col("d.as_of"))
    & (F.col("i.event_date") >= F.date_sub(F.col("d.as_of"), 29)),
    "left",
)
insider_30d = (
    insider_join_30d
    .groupBy(F.col("d.security_sk").alias("security_sk"), F.col("d.date_sk").alias("date_sk"))
    .agg(
        F.countDistinct(F.when((F.col("i.is_buy") == True) & F.col("i.entity_sk").isNotNull(), F.col("i.entity_sk"))).cast(IntegerType()).alias("insider_cluster_buy_30d"),
        F.max(F.col("i.knowledge_date")).alias("insider_knowledge_date_30d"),
    )
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Assemble raw feature rows ---
def _empty_metric_df(columns: list[tuple[str, str]]):
    schema = StructType([StructField(name, type_obj, True) for name, type_obj in columns])
    return spark.createDataFrame([], schema)


fact_fundamentals = spark.table("fact_fundamentals") if spark.catalog.tableExists("fact_fundamentals") else _empty_metric_df([
    ("security_sk", LongType()), ("event_date", DateType()), ("knowledge_date", DateType()),
    ("fundamentals_revision_hash", StringType()), ("silver_loaded_at", TimestampType()),
    ("pe_ratio", DoubleType()), ("peg_ratio", DoubleType()), ("ps_ratio", DoubleType()),
    ("ev_ebitda", DoubleType()), ("profit_margin", DoubleType()), ("rev_growth_yoy", DoubleType()),
    ("fcf_yield", DoubleType()), ("net_debt_to_ebitda", DoubleType()),
])
fundamental_metric_names = [
    "pe_ratio", "peg_ratio", "ps_ratio", "ev_ebitda", "profit_margin",
    "rev_growth_yoy", "fcf_yield", "net_debt_to_ebitda",
]


def _latest_non_null_fundamental_metric(metric_name: str):
    metric_window = Window.partitionBy(F.col("d.security_sk"), F.col("d.date_sk")).orderBy(
        F.col("f.knowledge_date").desc(),
        F.col("f.event_date").desc(),
        F.col("f.silver_loaded_at").desc_nulls_last(),
        F.col("f.fundamentals_revision_hash").desc(),
    )
    return (
        asof_df.alias("d")
        .join(
            fact_fundamentals.alias("f"),
            (F.col("d.security_sk") == F.col("f.security_sk"))
            & (F.col("f.event_date") <= F.col("d.as_of"))
            & (F.col("f.knowledge_date") <= F.col("d.as_of"))
            & F.col(f"f.{metric_name}").isNotNull(),
            "left",
        )
        .withColumn("metric_row_number", F.row_number().over(metric_window))
        .filter(F.col("metric_row_number") == 1)
        .select(
            F.col("d.security_sk").alias("security_sk"),
            F.col("d.date_sk").alias("date_sk"),
            F.col(f"f.{metric_name}").cast(DoubleType()).alias(metric_name),
            F.col("f.knowledge_date").alias(f"{metric_name}_knowledge_date"),
        )
    )


fundamentals_latest = asof_df.select("security_sk", "date_sk")
for fundamental_metric_name in fundamental_metric_names:
    fundamentals_latest = fundamentals_latest.join(
        _latest_non_null_fundamental_metric(fundamental_metric_name),
        ["security_sk", "date_sk"],
        "left",
    )
fundamentals_latest = fundamentals_latest.withColumn(
    "fundamental_knowledge_date",
    F.greatest(*[
        F.col(f"{metric_name}_knowledge_date") for metric_name in fundamental_metric_names
    ]),
).drop(*[
    f"{metric_name}_knowledge_date" for metric_name in fundamental_metric_names
])

fundamental_anchor = (
    spark.table("fact_fundamental_anchor").alias("f")
    .join(
        latest_anchor_manifests.alias("m"),
        (F.col("f.date_sk") == F.date_format(F.col("m.as_of_date"), "yyyyMMdd").cast(IntegerType()))
        & (F.col("f.model_version") == F.col("m.model_version")),
        "inner",
    )
    .filter(
        (F.col("f.event_date") <= F.col("m.as_of_date"))
        & (F.col("f.knowledge_date") <= F.col("m.as_of_date"))
    )
    .select(
        F.col("f.security_sk").alias("security_sk"),
        F.col("f.date_sk").alias("date_sk"),
        F.col("f.fundamental_anchor_z").alias("fundamental_anchor_z"),
        F.col("f.anchor_method").alias("fundamental_anchor_method"),
        F.col("f.imputed_flags").alias("fundamental_anchor_imputed_flags"),
        F.col("f.knowledge_date").alias("anchor_knowledge_date"),
    )
    .dropDuplicates(["security_sk", "date_sk"])
)

fact_news_sentiment = spark.table("fact_news_sentiment") if spark.catalog.tableExists("fact_news_sentiment") else _empty_metric_df([
    ("news_sk", LongType()), ("security_sk", LongType()),
    ("event_date", DateType()), ("knowledge_date", DateType()),
    ("news_revision_hash", StringType()), ("silver_loaded_at", TimestampType()),
    ("sentiment", DoubleType()), ("relevance", DoubleType()),
])
eligible_news_sentiment_revisions = (
    asof_df.alias("d")
    .join(
        fact_news_sentiment.alias("n"),
        (F.col("d.security_sk") == F.col("n.security_sk"))
        & (F.col("n.event_date") <= F.col("d.as_of"))
        & (F.col("n.knowledge_date") <= F.col("d.as_of")),
        "left",
    )
    .select(
        F.col("d.security_sk").alias("security_sk"),
        F.col("d.date_sk").alias("date_sk"),
        F.col("d.as_of").alias("as_of"),
        F.col("n.news_sk").alias("news_sk"),
        F.col("n.sentiment").alias("sentiment"),
        F.col("n.relevance").alias("relevance"),
        F.col("n.event_date").alias("event_date"),
        F.col("n.knowledge_date").alias("knowledge_date"),
        F.col("n.news_revision_hash").alias("news_revision_hash"),
        F.col("n.silver_loaded_at").alias("silver_loaded_at"),
    )
)
news_sentiment_revision_window = Window.partitionBy(
    "security_sk", "date_sk", "news_sk",
).orderBy(
    F.col("knowledge_date").desc_nulls_last(),
    F.col("silver_loaded_at").desc_nulls_last(),
    F.col("news_revision_hash").desc_nulls_last(),
)
latest_news_sentiment_revisions = (
    eligible_news_sentiment_revisions
    .withColumn("revision_row_number", F.row_number().over(news_sentiment_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .drop("revision_row_number")
)
news_sentiment_30d = (
    latest_news_sentiment_revisions
    .filter(F.col("event_date").isNull() | (F.col("event_date") >= F.date_sub(F.col("as_of"), 13)))
    .groupBy("security_sk", "date_sk")
    .agg(
        F.sum(F.col("sentiment").cast(DoubleType()) * F.coalesce(F.col("relevance").cast(DoubleType()), F.lit(1.0))).alias("sentiment_weighted_sum"),
        F.sum(F.when(
            F.col("sentiment").isNotNull(),
            F.coalesce(F.col("relevance").cast(DoubleType()), F.lit(1.0)),
        )).alias("sentiment_weight_sum"),
        F.max(F.col("knowledge_date")).alias("news_sentiment_knowledge_date"),
    )
    .withColumn("news_sentiment_ewma_14d", F.when(F.col("sentiment_weight_sum") > 0, F.col("sentiment_weighted_sum") / F.col("sentiment_weight_sum")))
    .select("security_sk", "date_sk", "news_sentiment_ewma_14d", "news_sentiment_knowledge_date")
)

fact_company_news = spark.table("fact_company_news") if spark.catalog.tableExists("fact_company_news") else _empty_metric_df([
    ("news_sk", LongType()), ("security_sk", LongType()),
    ("event_date", DateType()), ("knowledge_date", DateType()),
    ("news_revision_hash", StringType()), ("silver_loaded_at", TimestampType()),
])
eligible_company_news_revisions = (
    asof_df.alias("d")
    .join(
        fact_company_news.alias("n"),
        (F.col("d.security_sk") == F.col("n.security_sk"))
        & (F.col("n.event_date") <= F.col("d.as_of"))
        & (F.col("n.knowledge_date") <= F.col("d.as_of")),
        "left",
    )
    .select(
        F.col("d.security_sk").alias("security_sk"),
        F.col("d.date_sk").alias("date_sk"),
        F.col("d.as_of").alias("as_of"),
        F.col("n.news_sk").alias("news_sk"),
        F.col("n.event_date").alias("event_date"),
        F.col("n.knowledge_date").alias("knowledge_date"),
        F.col("n.news_revision_hash").alias("news_revision_hash"),
        F.col("n.silver_loaded_at").alias("silver_loaded_at"),
    )
)
company_news_revision_window = Window.partitionBy(
    "security_sk", "date_sk", "news_sk",
).orderBy(
    F.col("knowledge_date").desc_nulls_last(),
    F.col("silver_loaded_at").desc_nulls_last(),
    F.col("news_revision_hash").desc_nulls_last(),
)
latest_company_news_revisions = (
    eligible_company_news_revisions
    .withColumn("revision_row_number", F.row_number().over(company_news_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .drop("revision_row_number")
)
news_counts = (
    latest_company_news_revisions
    .filter(F.col("event_date").isNull() | (F.col("event_date") >= F.date_sub(F.col("as_of"), 59)))
    .groupBy("security_sk", "date_sk")
    .agg(
        F.sum(F.when(F.col("event_date") >= F.date_sub(F.col("as_of"), 29), F.lit(1)).otherwise(F.lit(0))).alias("news_count_30d"),
        F.sum(F.when((F.col("event_date") < F.date_sub(F.col("as_of"), 29)) & F.col("event_date").isNotNull(), F.lit(1)).otherwise(F.lit(0))).alias("news_count_prev_30d"),
        F.max(F.col("knowledge_date")).alias("news_count_knowledge_date"),
    )
    .withColumn(
        "news_volume_z_30d",
        F.when(F.col("news_count_prev_30d") > 0, (F.col("news_count_30d") - F.col("news_count_prev_30d")) / F.sqrt(F.col("news_count_prev_30d")))
        .when(F.col("news_count_30d") > 0, F.col("news_count_30d").cast(DoubleType())),
    )
    .select(
        "security_sk", "date_sk",
        F.col("news_count_30d").cast(DoubleType()).alias("news_count_30d"),
        "news_volume_z_30d", "news_count_knowledge_date",
    )
)

fact_contract_award = spark.table("fact_contract_award") if spark.catalog.tableExists("fact_contract_award") else _empty_metric_df([
    ("security_sk", LongType()), ("transaction_id", StringType()),
    ("award_id", StringType()), ("contract_revision_hash", StringType()),
    ("event_date", DateType()), ("knowledge_date", DateType()), ("amount_usd", DoubleType()),
])
eligible_contract_revisions = (
    asof_df.select("date_sk", "as_of").distinct().alias("d")
    .join(
        fact_contract_award.alias("c"),
        (F.col("c.event_date") <= F.col("d.as_of"))
        & (F.col("c.knowledge_date") <= F.col("d.as_of")),
        "inner",
    )
    .select(
        F.col("c.security_sk").alias("security_sk"),
        F.col("d.date_sk").alias("date_sk"),
        F.col("d.as_of").alias("as_of"),
        F.col("c.transaction_id").alias("transaction_id"),
        F.col("c.award_id").alias("award_id"),
        F.col("c.contract_revision_hash").alias("contract_revision_hash"),
        F.col("c.amount_usd").alias("amount_usd"),
        F.col("c.event_date").alias("event_date"),
        F.col("c.knowledge_date").alias("knowledge_date"),
    )
)
contract_revision_window = Window.partitionBy(
    "date_sk", "transaction_id",
).orderBy(
    F.col("knowledge_date").desc(),
    F.col("event_date").desc(),
    F.col("contract_revision_hash").desc(),
)
latest_contract_revisions = (
    eligible_contract_revisions
    .withColumn("revision_row_number", F.row_number().over(contract_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .drop("revision_row_number")
)
contracts_90d = (
    latest_contract_revisions
    .filter(F.col("event_date") >= F.date_sub(F.col("as_of"), 89))
    .groupBy("security_sk", "date_sk")
    .agg(
        F.sum(F.col("amount_usd").cast(DoubleType())).alias("contract_award_usd_trailing_90d"),
        F.max(F.col("knowledge_date")).alias("contract_knowledge_date"),
    )
)

fact_institutional_holding = spark.table("fact_institutional_holding") if spark.catalog.tableExists("fact_institutional_holding") else _empty_metric_df([
    ("security_sk", LongType()), ("entity_sk", LongType()), ("event_date", DateType()), ("knowledge_date", DateType()),
    ("source_sk", IntegerType()),
    ("accession_no", StringType()), ("holding_revision_hash", StringType()),
    ("silver_natural_key", StringType()), ("silver_loaded_at", TimestampType()),
    ("shares", DoubleType()), ("value_usd", DoubleType()), ("shares_delta_qoq", DoubleType()),
])
eligible_holding_revisions = (
    asof_df.alias("d")
    .join(
        fact_institutional_holding.alias("h"),
        (F.col("d.security_sk") == F.col("h.security_sk"))
        & F.col("h.entity_sk").isNotNull()
        & (F.col("h.event_date") <= F.col("d.as_of"))
        & (F.col("h.knowledge_date") <= F.col("d.as_of")),
        "left",
    )
    .select(
        F.col("d.security_sk").alias("security_sk"),
        F.col("d.date_sk").alias("date_sk"),
        F.col("d.as_of").alias("as_of"),
        F.col("h.source_sk").alias("source_sk"),
        F.col("h.entity_sk").alias("entity_sk"),
        F.col("h.accession_no").alias("accession_no"),
        F.col("h.holding_revision_hash").alias("holding_revision_hash"),
        F.coalesce(
            F.col("h.silver_natural_key"),
            F.concat_ws(
                "|", F.col("h.security_sk"), F.col("h.entity_sk"),
                F.col("h.event_date"),
            ),
        ).alias("holding_natural_key"),
        F.col("h.shares").alias("shares"),
        F.col("h.value_usd").alias("value_usd"),
        F.col("h.shares_delta_qoq").alias("shares_delta_qoq"),
        F.col("h.event_date").alias("event_date"),
        F.col("h.knowledge_date").alias("knowledge_date"),
        F.col("h.silver_loaded_at").alias("silver_loaded_at"),
    )
)
holding_revision_window = Window.partitionBy(
    "security_sk", "date_sk", "source_sk", "entity_sk", "event_date",
).orderBy(
    F.col("knowledge_date").desc(),
    F.col("silver_loaded_at").desc_nulls_last(),
    F.col("holding_revision_hash").desc(),
)
latest_holding_revisions = (
    eligible_holding_revisions
    .withColumn("revision_row_number", F.row_number().over(holding_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .drop("revision_row_number")
)
inst_delta_window = Window.partitionBy(
    "security_sk", "date_sk", "source_sk", "entity_sk",
).orderBy("event_date")
inst_with_delta = (
    latest_holding_revisions
    .withColumn("prev_shares", F.lag(F.col("shares").cast(DoubleType())).over(inst_delta_window))
    .withColumn("prev_value_usd", F.lag(F.col("value_usd").cast(DoubleType())).over(inst_delta_window))
    .withColumn("shares_delta_calc", F.col("shares").cast(DoubleType()) - F.col("prev_shares"))
    .withColumn("value_delta_calc", F.col("value_usd").cast(DoubleType()) - F.col("prev_value_usd"))
)
institutional_metrics = (
    inst_with_delta
    .filter(F.col("event_date").isNull() | (F.col("event_date") >= F.date_sub(F.col("as_of"), 120)))
    .groupBy("security_sk", "date_sk")
    .agg(
        F.sum(F.coalesce(F.col("value_delta_calc"), F.col("shares_delta_qoq").cast(DoubleType()))).alias("inst_net_flow_qoq"),
        F.countDistinct(F.when(F.col("prev_shares").isNull() & (F.col("shares").cast(DoubleType()) > 0), F.col("entity_sk"))).cast(IntegerType()).alias("inst_new_initiations"),
        F.countDistinct(F.when(F.col("shares").cast(DoubleType()) > 0, F.col("entity_sk"))).cast(IntegerType()).alias("institutional_holder_count_120d"),
        F.max(F.col("knowledge_date")).alias("institutional_knowledge_date"),
    )
)

fact_ownership_event = spark.table("fact_ownership_event") if spark.catalog.tableExists("fact_ownership_event") else _empty_metric_df([
    ("security_sk", LongType()), ("entity_sk", LongType()), ("accession_no", StringType()),
    ("ownership_revision_hash", StringType()), ("event_date", DateType()),
    ("knowledge_date", DateType()), ("is_activist", BooleanType()),
])
eligible_ownership_revisions = (
    asof_df.alias("d")
    .join(
        fact_ownership_event.alias("o"),
        (F.col("d.security_sk") == F.col("o.security_sk"))
        & (F.col("o.event_date") <= F.col("d.as_of"))
        & (F.col("o.knowledge_date") <= F.col("d.as_of")),
        "left",
    )
    .select(
        F.col("d.security_sk").alias("security_sk"),
        F.col("d.date_sk").alias("date_sk"),
        F.col("d.as_of").alias("as_of"),
        F.col("o.entity_sk").alias("entity_sk"),
        F.col("o.accession_no").alias("accession_no"),
        F.col("o.ownership_revision_hash").alias("ownership_revision_hash"),
        F.col("o.is_activist").alias("is_activist"),
        F.col("o.event_date").alias("event_date"),
        F.col("o.knowledge_date").alias("knowledge_date"),
    )
)
ownership_revision_window = Window.partitionBy(
    "security_sk", "date_sk", "accession_no", "entity_sk",
).orderBy(
    F.col("knowledge_date").desc(),
    F.col("event_date").desc(),
    F.col("ownership_revision_hash").desc(),
)
latest_ownership_revisions = (
    eligible_ownership_revisions
    .withColumn("revision_row_number", F.row_number().over(ownership_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .drop("revision_row_number")
)
ownership_metrics = (
    latest_ownership_revisions
    .filter(F.col("event_date").isNull() | (F.col("event_date") >= F.date_sub(F.col("as_of"), 365)))
    .groupBy("security_sk", "date_sk")
    .agg(
        F.max(F.coalesce(F.col("is_activist"), F.lit(False)).cast("int")).cast(BooleanType()).alias("activist_13d_flag"),
        F.max(F.col("knowledge_date")).alias("ownership_knowledge_date"),
    )
)

raw_features = (
    market_metrics
    .join(insider_90d, ["security_sk", "date_sk"], "left")
    .join(insider_30d, ["security_sk", "date_sk"], "left")
    .join(fundamentals_latest, ["security_sk", "date_sk"], "left")
    .join(fundamental_anchor, ["security_sk", "date_sk"], "left")
    .join(
        narrative_intensity_facts.drop("as_of"),
        ["security_sk", "date_sk"],
        "left",
    )
    .join(
        narrative_premium_facts.drop("as_of"),
        ["security_sk", "date_sk"],
        "left",
    )
    .join(news_sentiment_30d, ["security_sk", "date_sk"], "left")
    .join(news_counts, ["security_sk", "date_sk"], "left")
    .join(contracts_90d, ["security_sk", "date_sk"], "left")
    .join(institutional_metrics, ["security_sk", "date_sk"], "left")
    .join(ownership_metrics, ["security_sk", "date_sk"], "left")
    .withColumn("beta_252d", F.lit(None).cast(DoubleType()))
    .withColumn("info_ratio_252d", F.lit(None).cast(DoubleType()))
    .withColumn("insider_net_buy_ratio_90d", F.coalesce(F.col("insider_net_buy_ratio_90d"), F.lit(0.0)).cast(DoubleType()))
    .withColumn("insider_cluster_buy_30d", F.coalesce(F.col("insider_cluster_buy_30d"), F.lit(0)).cast(IntegerType()))
    .withColumn("inst_net_flow_qoq", F.coalesce(F.col("inst_net_flow_qoq"), F.lit(0.0)).cast(DoubleType()))
    .withColumn("inst_new_initiations", F.coalesce(F.col("inst_new_initiations"), F.lit(0)).cast(IntegerType()))
    .withColumn("institutional_holder_count_120d", F.coalesce(F.col("institutional_holder_count_120d"), F.lit(0)).cast(IntegerType()))
    .withColumn("activist_13d_flag", F.coalesce(F.col("activist_13d_flag"), F.lit(False)).cast(BooleanType()))
    .withColumn("news_count_30d", F.coalesce(F.col("news_count_30d"), F.lit(0.0)).cast(DoubleType()))
    .withColumn("news_volume_z_30d", F.coalesce(F.col("news_volume_z_30d"), F.lit(0.0)).cast(DoubleType()))
    .withColumn("contract_award_usd_trailing_90d", F.coalesce(F.col("contract_award_usd_trailing_90d"), F.lit(0.0)).cast(DoubleType()))
    .withColumn(
        "max_knowledge_date",
        F.greatest(
            F.col("market_knowledge_date"), F.col("insider_knowledge_date_90d"), F.col("insider_knowledge_date_30d"),
            F.col("fundamental_knowledge_date"), F.col("news_sentiment_knowledge_date"), F.col("news_count_knowledge_date"),
            F.col("contract_knowledge_date"), F.col("institutional_knowledge_date"),
            F.col("ownership_knowledge_date"), F.col("anchor_knowledge_date"),
            F.col("narrative_knowledge_date"), F.col("narrative_premium_knowledge_date"),
        ),
    )
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Deterministic composite metric recipe: winsorize -> z -> sign-align -> blend -> 0-100 ---
metric_names = [
    "momentum_3m",
    "momentum_6m",
    "momentum_12m",
    "realized_vol_30d",
    "insider_net_buy_ratio_90d",
    "insider_cluster_buy_30d",
]

bounds_exprs = []
for metric_name in metric_names:
    bounds_exprs.append(F.expr(f"percentile_approx({metric_name}, 0.01)").alias(f"{metric_name}_p01"))
    bounds_exprs.append(F.expr(f"percentile_approx({metric_name}, 0.99)").alias(f"{metric_name}_p99"))

bounds_df = raw_features.groupBy("date_sk").agg(*bounds_exprs)
scored = raw_features.join(bounds_df, "date_sk", "left")

for metric_name in metric_names:
    scored = scored.withColumn(
        f"{metric_name}_winsor",
        F.when(F.col(metric_name).isNull(), F.lit(None).cast(DoubleType()))
        .when(F.col(f"{metric_name}_p01").isNotNull() & (F.col(metric_name) < F.col(f"{metric_name}_p01")), F.col(f"{metric_name}_p01"))
        .when(F.col(f"{metric_name}_p99").isNotNull() & (F.col(metric_name) > F.col(f"{metric_name}_p99")), F.col(f"{metric_name}_p99"))
        .otherwise(F.col(metric_name).cast(DoubleType())),
    )

stats_exprs = []
for metric_name in metric_names:
    stats_exprs.append(F.avg(f"{metric_name}_winsor").alias(f"{metric_name}_mean"))
    stats_exprs.append(F.stddev_samp(f"{metric_name}_winsor").alias(f"{metric_name}_stddev"))

stats_df = scored.groupBy("date_sk").agg(*stats_exprs)
scored = scored.join(stats_df, "date_sk", "left")

for metric_name in metric_names:
    scored = scored.withColumn(
        f"{metric_name}_z",
        F.when(F.col(f"{metric_name}_winsor").isNull(), F.lit(0.0))
        .when(F.col(f"{metric_name}_stddev") > 0, (F.col(f"{metric_name}_winsor") - F.col(f"{metric_name}_mean")) / F.col(f"{metric_name}_stddev"))
        .otherwise(F.lit(0.0)),
    )

active_weights = {
    row.metric_name: {"weight": float(row.weight), "direction": int(row.direction)}
    for row in spark.table("metric_weights")
    .filter((F.col("metric_group") == "composite_growth_score") & (F.col("is_active") == True) & (F.col("version") == "e6a_v1"))
    .select("metric_name", "weight", "direction")
    .collect()
}

score_expr = F.lit(0.0)
for metric_name in metric_names:
    config = active_weights[metric_name]
    score_expr = score_expr + F.lit(config["weight"] * config["direction"]) * F.col(f"{metric_name}_z")

rank_window = Window.partitionBy("date_sk").orderBy(F.col("composite_growth_score_raw"))
count_window = Window.partitionBy("date_sk")
features_df = (
    scored
    .withColumn("composite_growth_score_raw", score_expr)
    .withColumn("date_security_count", F.count("security_sk").over(count_window))
    .withColumn(
        "composite_growth_score",
        F.when(F.col("date_security_count") <= 1, F.lit(50.0))
        .otherwise(F.round(F.percent_rank().over(rank_window) * F.lit(100.0), 4)),
    )
    .withColumn("opportunity_score", F.lit(None).cast(DoubleType()))
    .withColumn("score_status", F.lit("THEME_CONTEXT_REQUIRED"))
    .withColumn("feature_built_at", F.current_timestamp())
    .withColumn(
        "stale_sources_json",
        F.to_json(F.struct(
            F.col("momentum_3m").isNull().alias("momentum_3m"),
            F.col("momentum_6m").isNull().alias("momentum_6m"),
            F.col("momentum_12m").isNull().alias("momentum_12m"),
            F.col("beta_252d").isNull().alias("benchmark"),
            F.col("inst_net_flow_qoq").isNull().alias("institutional"),
            F.col("news_sentiment_ewma_14d").isNull().alias("news"),
            F.col("contract_award_usd_trailing_90d").isNull().alias("contracts"),
            F.col("pe_ratio").isNull().alias("fundamentals"),
            F.col("fundamental_anchor_z").isNull().alias("fundamental_anchor"),
            F.col("narrative_intensity").isNull().alias("narrative"),
            F.col("narrative_premium").isNull().alias("narrative_premium"),
            F.col("fundamental_anchor_z").isNull().alias("valuation_brake"),
            F.lit(True).alias("theme_context_required"),
        )),
    )
    .select(
        "security_sk", "date_sk", "ticker", "company_name", "gics_sector", "country", "as_of",
        "close", "ret_1d", "momentum_3m", "momentum_6m", "momentum_12m", "rel_strength_sector",
        "realized_vol_30d", "realized_vol_90d", "realized_vol_252d", "downside_deviation_252d",
        "max_drawdown_252d", "beta_252d", "illiquidity", "ann_return_252d", "sharpe_252d",
        "sortino_252d", "calmar_252d", "info_ratio_252d", "insider_net_buy_ratio_90d",
        "insider_cluster_buy_30d", "inst_net_flow_qoq", "inst_new_initiations", "activist_13d_flag",
        "news_sentiment_ewma_14d", "news_count_30d", "news_volume_z_30d", "contract_award_usd_trailing_90d",
        "pe_ratio", "peg_ratio", "ps_ratio", "ev_ebitda", "profit_margin", "rev_growth_yoy",
        "fcf_yield", "net_debt_to_ebitda",
        "fundamental_anchor_z", "fundamental_anchor_method", "fundamental_anchor_imputed_flags",
        "institutional_holder_count_120d", "narrative_intensity", "narrative_coverage_status",
        "narrative_coverage_reasons_json", "narrative_premium",
        "narrative_premium_coverage_status", "narrative_premium_coverage_reasons_json",
        "narrative_decision_id", "anchor_support_z", "divergence_state",
        "narrative_is_converging",
        "composite_growth_score", "opportunity_score", "score_status", "max_knowledge_date", "stale_sources_json",
        "feature_built_at",
    )
    .dropDuplicates(["security_sk", "date_sk"])
)

_merge_all("security_daily_features", features_df, "t.security_sk = s.security_sk AND t.date_sk = s.date_sk")

feature_target = DeltaTable.forName(spark, "security_daily_features")
if not deferred_stale_dates.isEmpty():
    (
        feature_target
        .alias("t")
        .merge(deferred_stale_dates.alias("s"), "t.as_of = s.as_of")
        .whenMatchedDelete()
        .execute()
    )
    print("Removed deferred stale feature dates from serving until a later bounded rerun")

legacy_feature_rows = feature_target.toDF().filter(F.col("feature_built_at").isNull()).count()
if legacy_feature_rows:
    feature_target.delete("feature_built_at IS NULL")
    print(f"Removed {legacy_feature_rows} unversioned legacy feature rows")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- E14/E6b per-theme Opportunity Score over latest PIT-valid TRS candidates ---
theme_dates = processing_dates.withColumn(
    "date_sk", F.date_format("as_of", "yyyyMMdd").cast(IntegerType())
)
eligible_theme_memberships = (
    theme_dates.alias("d")
    .join(
        spark.table("fact_theme_membership").alias("m"),
        (F.col("m.event_date") <= F.col("d.as_of"))
        & (F.col("m.knowledge_date") <= F.col("d.as_of"))
        & (F.col("m.is_ground_truth") == F.lit(True)),
        "inner",
    )
    .join(
        spark.table("dim_theme").filter(F.col("is_active") == F.lit(True)).alias("t"),
        F.col("m.theme_id") == F.col("t.theme_id"),
        "inner",
    )
    .select(
        F.col("d.date_sk").alias("date_sk"),
        F.col("d.as_of").alias("as_of"),
        F.col("m.theme_id").alias("theme_id"),
        F.col("m.etf_symbol").alias("etf_symbol"),
        F.col("m.security_sk").alias("security_sk"),
        F.col("m.weight").cast(DoubleType()).alias("membership_weight"),
        F.col("m.snapshot_batch_id").alias("snapshot_batch_id"),
        F.col("m.snapshot_ingest_ts").alias("snapshot_ingest_ts"),
        F.col("m.event_date").alias("membership_event_date"),
        F.col("m.knowledge_date").alias("membership_knowledge_date"),
    )
)
theme_snapshot_window = Window.partitionBy(
    "date_sk", "theme_id"
).orderBy(
    F.col("snapshot_ingest_ts").desc(),
    F.col("snapshot_batch_id").desc(),
)
latest_theme_snapshot_keys = (
    eligible_theme_memberships
    .select(
        "date_sk", "theme_id",
        "snapshot_batch_id", "snapshot_ingest_ts",
    )
    .distinct()
    .withColumn("snapshot_row_number", F.row_number().over(theme_snapshot_window))
    .filter(F.col("snapshot_row_number") == 1)
    .drop("snapshot_row_number")
)
latest_theme_memberships = (
    eligible_theme_memberships.alias("m")
    .join(
        latest_theme_snapshot_keys.alias("s"),
        (F.col("m.date_sk") == F.col("s.date_sk"))
        & (F.col("m.theme_id") == F.col("s.theme_id"))
        & (F.col("m.snapshot_batch_id") == F.col("s.snapshot_batch_id"))
        & (F.col("m.snapshot_ingest_ts") == F.col("s.snapshot_ingest_ts")),
        "inner",
    )
    .groupBy(
        F.col("m.date_sk").alias("date_sk"),
        F.col("m.as_of").alias("as_of"),
        F.col("m.theme_id").alias("theme_id"),
        F.col("m.security_sk").alias("security_sk"),
    )
    .agg(
        F.max(F.col("m.membership_weight")).alias("membership_weight"),
        F.max(F.col("m.membership_knowledge_date")).alias("membership_knowledge_date"),
        F.max(F.col("m.snapshot_batch_id")).alias("snapshot_batch_id"),
        F.max(F.col("m.snapshot_ingest_ts")).alias("snapshot_ingest_ts"),
    )
    .withColumn("classification_source", F.lit("TRS"))
)
classification_window = Window.partitionBy("date_sk", "security_sk").orderBy(
    F.when(F.col("provenance") == F.lit("manual"), F.lit(0)).otherwise(F.lit(1)),
    F.col("confidence").desc(),
    F.col("updated_at").desc(),
    F.col("classification_id"),
)
resolved_theme_classifications = (
    theme_dates.alias("d")
    .join(
        spark.table("security_theme_classification").alias("c"),
        (F.col("c.effective_from") <= F.col("d.as_of"))
        & (F.col("c.effective_to").isNull() | (F.col("d.as_of") < F.col("c.effective_to")))
        & F.col("c.provenance").isin("manual", "llm"),
        "inner",
    )
    .select(
        F.col("d.date_sk").alias("date_sk"),
        F.col("d.as_of").alias("as_of"),
        F.col("c.security_sk").alias("security_sk"),
        F.col("c.theme_id").alias("theme_id"),
        F.col("c.confidence").alias("confidence"),
        F.col("c.provenance").alias("provenance"),
        F.col("c.classification_id").alias("classification_id"),
        F.col("c.classification_version").alias("classification_version"),
        F.col("c.effective_from").alias("effective_from"),
        F.col("c.updated_at").alias("updated_at"),
    )
    .withColumn("classification_row_number", F.row_number().over(classification_window))
    .filter(F.col("classification_row_number") == 1)
    .drop("classification_row_number")
)
classified_theme_memberships = (
    latest_theme_memberships.alias("m")
    .join(
        resolved_theme_classifications.select("date_sk", "security_sk").alias("c"),
        ["date_sk", "security_sk"],
        "left_anti",
    )
    .unionByName(
        resolved_theme_classifications.select(
            "date_sk", "as_of", "theme_id", "security_sk",
            F.lit(None).cast(DoubleType()).alias("membership_weight"),
            F.col("effective_from").alias("membership_knowledge_date"),
            F.col("classification_id").alias("snapshot_batch_id"),
            F.col("updated_at").alias("snapshot_ingest_ts"),
            F.upper("provenance").alias("classification_source"),
        )
    )
)
opportunity_candidates = (
    classified_theme_memberships.alias("m")
    .join(
        spark.table("security_daily_features").alias("f"),
        (F.col("m.security_sk") == F.col("f.security_sk"))
        & (F.col("m.date_sk") == F.col("f.date_sk")),
        "left",
    )
    .select(
        F.col("m.theme_id").alias("theme_id"),
        F.col("m.security_sk").alias("security_sk"),
        F.col("m.date_sk").alias("date_sk"),
        F.col("m.as_of").alias("as_of"),
        F.col("m.classification_source").alias("candidate_source"),
        F.col("m.snapshot_batch_id").alias("candidate_snapshot_id"),
        F.col("m.snapshot_ingest_ts").alias("candidate_snapshot_ingest_ts"),
        F.col("m.membership_weight").alias("membership_weight"),
        F.col("f.news_volume_z_30d").alias("news_volume_z_30d"),
        F.col("f.insider_net_buy_ratio_90d").alias("insider_net_buy_ratio_90d"),
        F.col("f.insider_cluster_buy_30d").alias("insider_cluster_buy_30d"),
        F.col("f.inst_net_flow_qoq").alias("inst_net_flow_qoq"),
        F.col("f.inst_new_initiations").alias("inst_new_initiations"),
        F.col("f.contract_award_usd_trailing_90d").alias("contract_award_usd_trailing_90d"),
        F.col("f.activist_13d_flag").alias("activist_13d_flag"),
        F.col("f.profit_margin").alias("profit_margin"),
        F.col("f.rev_growth_yoy").alias("rev_growth_yoy"),
        F.col("f.fcf_yield").alias("fcf_yield"),
        F.col("f.net_debt_to_ebitda").alias("net_debt_to_ebitda"),
        F.col("f.fundamental_anchor_z").alias("fundamental_anchor_z"),
        F.col("f.news_count_30d").alias("news_count_30d"),
        F.col("f.institutional_holder_count_120d").alias("institutional_holder_count_120d"),
        F.greatest(
            F.col("f.max_knowledge_date"),
            F.col("m.membership_knowledge_date"),
        ).alias("max_knowledge_date"),
    )
    .filter(F.col("max_knowledge_date") <= F.col("as_of"))
)

opportunity_observations: dict[tuple[str, int], list] = {}
for candidate in opportunity_candidates.orderBy("date_sk", "theme_id", "security_sk").collect():
    key = (candidate.theme_id, candidate.date_sk)
    opportunity_observations.setdefault(key, []).append(OpportunityObservation(
        theme_id=candidate.theme_id,
        security_sk=candidate.security_sk,
        date_sk=candidate.date_sk,
        as_of=candidate.as_of,
        candidate_source=candidate.candidate_source,
        candidate_snapshot_id=candidate.candidate_snapshot_id,
        candidate_snapshot_ingest_ts=candidate.candidate_snapshot_ingest_ts,
        membership_weight=candidate.membership_weight,
        news_volume_z_30d=candidate.news_volume_z_30d,
        insider_net_buy_ratio_90d=candidate.insider_net_buy_ratio_90d,
        insider_cluster_buy_30d=candidate.insider_cluster_buy_30d,
        inst_net_flow_qoq=candidate.inst_net_flow_qoq,
        inst_new_initiations=candidate.inst_new_initiations,
        contract_award_usd_trailing_90d=candidate.contract_award_usd_trailing_90d,
        activist_13d_flag=candidate.activist_13d_flag,
        profit_margin=candidate.profit_margin,
        rev_growth_yoy=candidate.rev_growth_yoy,
        fcf_yield=candidate.fcf_yield,
        net_debt_to_ebitda=candidate.net_debt_to_ebitda,
        fundamental_anchor_z=candidate.fundamental_anchor_z,
        news_count_30d=candidate.news_count_30d,
        institutional_holder_count_120d=candidate.institutional_holder_count_120d,
        max_knowledge_date=candidate.max_knowledge_date,
    ))

opportunity_results = []
for cohort_key in sorted(opportunity_observations):
    opportunity_results.extend(score_theme(
        opportunity_observations[cohort_key],
        opportunity_active_weights,
    ))

score_schema = StructType([
    StructField("score_id", StringType(), False),
    StructField("generation", StringType(), False),
    StructField("cohort_snapshot_hash", StringType(), False),
    StructField("theme_id", StringType(), False),
    StructField("security_sk", LongType(), False),
    StructField("date_sk", IntegerType(), False),
    StructField("as_of", DateType(), False),
    StructField("candidate_source", StringType(), False),
    StructField("candidate_snapshot_id", StringType(), False),
    StructField("candidate_snapshot_ingest_ts", TimestampType(), False),
    StructField("candidate_count", IntegerType(), False),
    *[StructField(column_name, DoubleType(), True) for column_name in (
        "thesis_linkage_z", "attention_acceleration_z", "smart_money_z",
        "fundamental_health_z", "valuation_brake_z", "crowding_positioning_z",
        "thesis_linkage_contribution", "attention_acceleration_contribution",
        "smart_money_contribution", "fundamental_health_contribution",
        "valuation_brake_contribution", "crowding_positioning_contribution",
        "opportunity_score_raw", "opportunity_score",
    )],
    StructField("coverage_status", StringType(), False),
    StructField("coverage_reasons_json", StringType(), False),
    StructField("max_knowledge_date", DateType(), False),
    StructField("model_version", StringType(), False),
    StructField("weight_version", StringType(), False),
    StructField("created_at", TimestampType(), False),
])
manifest_schema = StructType([
    StructField("generation", StringType(), False),
    StructField("as_of_date", DateType(), False),
    StructField("model_version", StringType(), False),
    StructField("weight_version", StringType(), False),
    StructField("status", StringType(), False),
    StructField("row_count", LongType(), False),
    StructField("ready_count", LongType(), False),
    StructField("partial_count", LongType(), False),
    StructField("withheld_count", LongType(), False),
    StructField("fingerprint", StringType(), False),
    StructField("created_at", TimestampType(), False),
    StructField("completed_at", TimestampType(), True),
])

results_by_date: dict[date, list] = {}
for result in opportunity_results:
    results_by_date.setdefault(result.as_of, []).append(result)
score_records = []
manifest_records = []
score_built_at = datetime.now(timezone.utc)
for as_of_value in sorted({row.as_of for row in processing_dates.collect()}):
    date_results = sorted(
        results_by_date.get(as_of_value, []),
        key=lambda result: (result.theme_id, result.security_sk),
    )
    score_ids = sorted(result.score_id for result in date_results)
    fingerprint = hashlib.sha256(
        "|".join(score_ids).encode("ascii")
    ).hexdigest()
    generation = f"e6b-{fingerprint[:32]}"
    for result in date_results:
        record = asdict(result)
        coverage_reasons = record.pop("coverage_reasons")
        record.update({
            "generation": generation,
            "coverage_reasons_json": json.dumps(
                list(coverage_reasons), sort_keys=True, separators=(",", ":")
            ),
            "created_at": score_built_at,
        })
        score_records.append(record)
    coverage_counts = {
        status: sum(result.coverage_status == status for result in date_results)
        for status in ("READY", "PARTIAL", "WITHHELD")
    }
    if date_results:
        manifest_records.append({
            "generation": generation,
            "as_of_date": as_of_value,
            "model_version": OPPORTUNITY_MODEL_VERSION,
            "weight_version": OPPORTUNITY_WEIGHT_VERSION,
            "status": "completed",
            "row_count": len(date_results),
            "ready_count": coverage_counts["READY"],
            "partial_count": coverage_counts["PARTIAL"],
            "withheld_count": coverage_counts["WITHHELD"],
            "fingerprint": fingerprint,
            "created_at": score_built_at,
            "completed_at": score_built_at,
        })

score_frame = spark.createDataFrame(score_records, score_schema)
score_target = DeltaTable.forName(spark, "fact_theme_opportunity_score")
score_merge = (
    score_target.alias("t")
    .merge(
        score_frame.alias("s"),
        "t.score_id = s.score_id",
    )
    .whenNotMatchedInsertAll()
)
processed_score_date_sks = [
    row.date_sk for row in theme_dates.select("date_sk").distinct().collect()
]
if processed_score_date_sks:
    processed_score_dates_sql = ",".join(
        str(value) for value in sorted(processed_score_date_sks)
    )
    score_merge = score_merge.whenNotMatchedBySourceDelete(
        f"t.model_version = '{OPPORTUNITY_MODEL_VERSION}' "
        f"AND t.weight_version = '{OPPORTUNITY_WEIGHT_VERSION}' "
        f"AND t.date_sk IN ({processed_score_dates_sql})"
    )
score_merge.execute()

manifest_frame = spark.createDataFrame(manifest_records, manifest_schema)
manifest_merge = (
    DeltaTable.forName(spark, "opportunity_score_snapshot_manifest")
    .alias("t")
    .merge(
        manifest_frame.alias("s"),
        "t.generation = s.generation AND t.as_of_date = s.as_of_date "
        "AND t.model_version = s.model_version AND t.weight_version = s.weight_version",
    )
    .whenNotMatchedInsertAll()
)
if processed_score_date_sks:
    manifest_merge = manifest_merge.whenNotMatchedBySourceDelete(
        f"t.model_version = '{OPPORTUNITY_MODEL_VERSION}' "
        f"AND t.weight_version = '{OPPORTUNITY_WEIGHT_VERSION}' "
        f"AND CAST(DATE_FORMAT(t.as_of_date, 'yyyyMMdd') AS INT) IN ({processed_score_dates_sql})"
    )
manifest_merge.execute()

active_score_manifest_orphans = (
    spark.table("fact_theme_opportunity_score").alias("s")
    .filter(
        (F.col("s.model_version") == F.lit(OPPORTUNITY_MODEL_VERSION))
        & (F.col("s.weight_version") == F.lit(OPPORTUNITY_WEIGHT_VERSION))
    )
    .join(
        spark.table("opportunity_score_snapshot_manifest").alias("m").filter(
            F.col("m.status") == F.lit("completed")
        ),
        (F.col("s.generation") == F.col("m.generation"))
        & (F.col("s.as_of") == F.col("m.as_of_date"))
        & (F.col("s.model_version") == F.col("m.model_version"))
        & (F.col("s.weight_version") == F.col("m.weight_version")),
        "left_anti",
    )
    .count()
)
if active_score_manifest_orphans:
    raise RuntimeError(
        f"E6b active score facts without completed manifests: {active_score_manifest_orphans}"
    )

active_manifest_records = (
    spark.table("opportunity_score_snapshot_manifest")
    .filter(
        (F.col("status") == F.lit("completed"))
        & (F.col("model_version") == F.lit(OPPORTUNITY_MODEL_VERSION))
        & (F.col("weight_version") == F.lit(OPPORTUNITY_WEIGHT_VERSION))
    )
    .orderBy("as_of_date", "generation")
    .collect()
)
for manifest_record in active_manifest_records:
    persisted_ids = [
        row.score_id
        for row in spark.table("fact_theme_opportunity_score")
        .filter(
            (F.col("generation") == F.lit(manifest_record.generation))
            & (F.col("as_of") == F.lit(manifest_record.as_of_date))
            & (F.col("model_version") == F.lit(OPPORTUNITY_MODEL_VERSION))
            & (F.col("weight_version") == F.lit(OPPORTUNITY_WEIGHT_VERSION))
        )
        .select("score_id").orderBy("score_id").collect()
    ]
    persisted_fingerprint = hashlib.sha256(
        "|".join(persisted_ids).encode("ascii")
    ).hexdigest()
    persisted_coverage_counts = {
        row.coverage_status: row["count"]
        for row in spark.table("fact_theme_opportunity_score")
        .filter(
            (F.col("generation") == F.lit(manifest_record.generation))
            & (F.col("as_of") == F.lit(manifest_record.as_of_date))
            & (F.col("model_version") == F.lit(OPPORTUNITY_MODEL_VERSION))
            & (F.col("weight_version") == F.lit(OPPORTUNITY_WEIGHT_VERSION))
        )
        .groupBy("coverage_status").count().collect()
    }
    if (
        len(persisted_ids) != manifest_record.row_count
        or persisted_coverage_counts.get("READY", 0) != manifest_record.ready_count
        or persisted_coverage_counts.get("PARTIAL", 0) != manifest_record.partial_count
        or persisted_coverage_counts.get("WITHHELD", 0) != manifest_record.withheld_count
        or persisted_fingerprint != manifest_record.fingerprint
    ):
        raise RuntimeError("E6b persisted score snapshot does not match its manifest")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Lakehouse serving projections for the API/agent contract ---
# Fabric SQL endpoints do not reliably expose Spark views, so the notebook
# materializes the v_* projections as Delta tables. The Warehouse SQL files
# still define the promoted Warehouse objects as true SQL views.
_replace_delta_projection("v_market_momentum", """
    SELECT security_sk, date_sk, as_of, close, ret_1d, momentum_3m, momentum_6m, momentum_12m, rel_strength_sector, max_knowledge_date
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

_replace_delta_projection("v_market_risk", """
    SELECT security_sk, date_sk, as_of, realized_vol_30d, realized_vol_90d, realized_vol_252d,
           downside_deviation_252d, max_drawdown_252d, beta_252d, illiquidity, max_knowledge_date
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

_replace_delta_projection("v_risk_adjusted", """
    SELECT security_sk, date_sk, as_of, ann_return_252d, sharpe_252d, sortino_252d, calmar_252d, info_ratio_252d, max_knowledge_date
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

_replace_delta_projection("v_smart_money", """
    SELECT security_sk, date_sk, as_of, insider_net_buy_ratio_90d, insider_cluster_buy_30d,
           inst_net_flow_qoq, inst_new_initiations, institutional_holder_count_120d,
           activist_13d_flag, max_knowledge_date
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

_replace_delta_projection("v_opportunity_legs", """
        SELECT s.score_id, s.generation, s.theme_id, s.security_sk, s.date_sk, s.as_of,
                     s.candidate_source, s.candidate_count,
                       s.candidate_snapshot_id, s.candidate_snapshot_ingest_ts,
                     s.thesis_linkage_z, s.attention_acceleration_z, s.smart_money_z,
                     s.fundamental_health_z, s.valuation_brake_z, s.crowding_positioning_z,
                     s.coverage_status, s.coverage_reasons_json, s.cohort_snapshot_hash,
                     s.model_version, s.weight_version, s.max_knowledge_date
        FROM fact_theme_opportunity_score s
        INNER JOIN opportunity_score_snapshot_manifest m
            ON m.generation = s.generation
         AND m.as_of_date = s.as_of
         AND m.model_version = s.model_version
         AND m.weight_version = s.weight_version
         AND m.status = 'completed'
        WHERE s.max_knowledge_date <= s.as_of
            AND s.model_version = 'e6b_v2'
            AND s.weight_version = 'e6b_balanced_v1'
""")

_replace_delta_projection("v_opportunity_score", """
        SELECT s.score_id, s.generation, s.theme_id, s.security_sk,
             d.ticker, d.company_name, s.date_sk, s.as_of,
                     s.candidate_source, s.candidate_count,
                       s.candidate_snapshot_id, s.candidate_snapshot_ingest_ts,
                     s.opportunity_score, s.coverage_status, s.coverage_reasons_json,
                     s.model_version, s.weight_version, s.max_knowledge_date,
                     f.narrative_premium, f.divergence_state, f.narrative_decision_id
        FROM fact_theme_opportunity_score s
        INNER JOIN opportunity_score_snapshot_manifest m
            ON m.generation = s.generation
         AND m.as_of_date = s.as_of
         AND m.model_version = s.model_version
         AND m.weight_version = s.weight_version
         AND m.status = 'completed'
        INNER JOIN dim_security d
            ON d.security_sk = s.security_sk
        LEFT JOIN security_daily_features f
            ON f.security_sk = s.security_sk AND f.date_sk = s.date_sk
        WHERE s.max_knowledge_date <= s.as_of
            AND s.model_version = 'e6b_v2'
            AND s.weight_version = 'e6b_balanced_v1'
""")

attribution_selects = []
for attribution_leg in OPPORTUNITY_LEG_WEIGHTS:
        attribution_selects.append(f"""
                SELECT s.score_id, s.generation, s.theme_id, s.security_sk,
                 d.ticker, d.company_name, s.date_sk, s.as_of,
                             '{attribution_leg}' AS leg_name,
                             s.{attribution_leg}_z AS leg_z,
                             CAST(w.weight AS DOUBLE) AS leg_weight,
                             s.{attribution_leg}_contribution AS leg_contribution,
                             CASE
                                     WHEN s.{attribution_leg}_contribution > 0 THEN 'RAISED'
                                     WHEN s.{attribution_leg}_contribution < 0 THEN 'LOWERED'
                                     ELSE 'NEUTRAL'
                             END AS contribution_direction,
                             s.opportunity_score, s.coverage_status, s.coverage_reasons_json,
                             f.narrative_premium, f.divergence_state, f.narrative_decision_id,
                             s.model_version, s.weight_version, s.max_knowledge_date
                FROM fact_theme_opportunity_score s
                INNER JOIN opportunity_score_snapshot_manifest m
                    ON m.generation = s.generation
                 AND m.as_of_date = s.as_of
                 AND m.model_version = s.model_version
                 AND m.weight_version = s.weight_version
                 AND m.status = 'completed'
                INNER JOIN dim_security d
                    ON d.security_sk = s.security_sk
                LEFT JOIN security_daily_features f
                    ON f.security_sk = s.security_sk AND f.date_sk = s.date_sk
                INNER JOIN metric_weights w
                    ON w.metric_name = '{attribution_leg}'
                 AND w.metric_group = 'opportunity_score'
                 AND w.version = s.weight_version
                 AND w.is_active = true
                WHERE s.max_knowledge_date <= s.as_of
                    AND s.model_version = 'e6b_v2'
                    AND s.weight_version = 'e6b_balanced_v1'
        """)
_replace_delta_projection(
        "v_security_score_attribution",
        " UNION ALL ".join(attribution_selects),
)

_replace_delta_projection("v_security_daily_features", """
    SELECT security_sk, ticker, company_name, gics_sector, country, as_of, date_sk,
           close, ret_1d, momentum_3m, momentum_6m, momentum_12m, rel_strength_sector,
           realized_vol_252d, downside_deviation_252d, max_drawdown_252d, beta_252d, illiquidity,
           ann_return_252d, sharpe_252d, sortino_252d, calmar_252d, info_ratio_252d,
           insider_net_buy_ratio_90d, insider_cluster_buy_30d, inst_net_flow_qoq, inst_new_initiations,
           activist_13d_flag, news_sentiment_ewma_14d, news_count_30d, news_volume_z_30d,
           contract_award_usd_trailing_90d,
           pe_ratio, peg_ratio, ps_ratio, ev_ebitda, profit_margin, rev_growth_yoy, fcf_yield, net_debt_to_ebitda,
           fundamental_anchor_z, fundamental_anchor_method, fundamental_anchor_imputed_flags,
           institutional_holder_count_120d, narrative_intensity, narrative_coverage_status,
           narrative_coverage_reasons_json, narrative_premium,
           narrative_premium_coverage_status, narrative_premium_coverage_reasons_json,
           narrative_decision_id, anchor_support_z, divergence_state, narrative_is_converging,
           composite_growth_score, opportunity_score, score_status, max_knowledge_date, stale_sources_json
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- E6 validation summary ---
row_count = spark.table("security_daily_features").count()
serving_row_count = spark.table("v_security_daily_features").count()
missing_pit = spark.sql("""
    SELECT COUNT(*) AS n
    FROM security_daily_features
    WHERE as_of IS NULL OR max_knowledge_date IS NULL OR max_knowledge_date > as_of
""").collect()[0].n
weight_validation = spark.sql("""
    SELECT ROUND(SUM(weight), 6) AS weight_sum
    FROM metric_weights
    WHERE metric_group = 'composite_growth_score' AND is_active = true AND version = 'e6a_v1'
""").collect()[0].weight_sum
duplicate_features = spark.sql("""
    SELECT COUNT(*) AS n
    FROM (
        SELECT security_sk, date_sk, COUNT(*) AS duplicate_count
        FROM security_daily_features
        GROUP BY security_sk, date_sk
        HAVING COUNT(*) > 1
    ) d
""").collect()[0].n
invalid_scores = spark.sql("""
    SELECT COUNT(*) AS n
    FROM security_daily_features
    WHERE composite_growth_score < 0 OR composite_growth_score > 100
""").collect()[0].n
invalid_global_score_contract = spark.sql("""
    SELECT COUNT(*) AS n
    FROM security_daily_features
    WHERE opportunity_score IS NOT NULL OR score_status <> 'THEME_CONTEXT_REQUIRED'
""").collect()[0].n
theme_score_fact_count = spark.table("v_opportunity_legs").count()
theme_score_count = spark.table("v_opportunity_score").count()
theme_attribution_count = spark.table("v_security_score_attribution").count()
invalid_score_projection_count = int(theme_score_count != theme_score_fact_count)
duplicate_theme_scores = spark.sql("""
    SELECT COUNT(*) AS n
    FROM (
        SELECT theme_id, security_sk, date_sk, model_version, weight_version, COUNT(*) AS duplicate_count
        FROM fact_theme_opportunity_score
        GROUP BY theme_id, security_sk, date_sk, model_version, weight_version
        HAVING COUNT(*) > 1
    ) d
""").collect()[0].n
invalid_theme_score_contract = spark.sql("""
    SELECT COUNT(*) AS n
    FROM fact_theme_opportunity_score
    WHERE model_version <> 'e6b_v2'
       OR weight_version <> 'e6b_balanced_v1'
       OR coverage_status NOT IN ('READY', 'PARTIAL', 'WITHHELD')
       OR coverage_reasons_json IS NULL
       OR LENGTH(score_id) <> 64
       OR LENGTH(cohort_snapshot_hash) <> 64
    OR candidate_snapshot_id IS NULL
    OR candidate_snapshot_ingest_ts IS NULL
       OR max_knowledge_date > as_of
       OR candidate_count < 1
       OR (
           coverage_status IN ('READY', 'PARTIAL')
           AND (
               candidate_count < 8
               OR opportunity_score IS NULL
               OR opportunity_score NOT BETWEEN 0 AND 100
               OR opportunity_score_raw IS NULL
               OR thesis_linkage_z IS NULL
               OR attention_acceleration_z IS NULL
               OR smart_money_z IS NULL
               OR fundamental_health_z IS NULL
               OR valuation_brake_z IS NULL
               OR crowding_positioning_z IS NULL
               OR thesis_linkage_contribution IS NULL
               OR attention_acceleration_contribution IS NULL
               OR smart_money_contribution IS NULL
               OR fundamental_health_contribution IS NULL
               OR valuation_brake_contribution IS NULL
               OR crowding_positioning_contribution IS NULL
           )
       )
       OR (
           coverage_status = 'WITHHELD'
           AND (
               candidate_count >= 8
               OR opportunity_score IS NOT NULL
               OR opportunity_score_raw IS NOT NULL
           )
       )
       OR (
           opportunity_score_raw IS NOT NULL
           AND ABS(
               opportunity_score_raw
               - thesis_linkage_contribution
               - attention_acceleration_contribution
               - smart_money_contribution
               - fundamental_health_contribution
               - valuation_brake_contribution
               - crowding_positioning_contribution
           ) > 1e-10
       )
""").collect()[0].n
opportunity_weight_validation = spark.sql("""
    SELECT ROUND(SUM(weight), 6) AS weight_sum
    FROM metric_weights
    WHERE metric_group = 'opportunity_score'
      AND is_active = true
      AND version = 'e6b_balanced_v1'
""").collect()[0].weight_sum
invalid_attribution_count = int(theme_attribution_count != theme_score_fact_count * 6)
invalid_narrative_premium_contract = spark.sql("""
    SELECT COUNT(*) AS n
    FROM security_daily_features
    WHERE narrative_premium_coverage_status IS NOT NULL
      AND (
          narrative_premium_coverage_status NOT IN ('READY', 'PARTIAL', 'WITHHELD')
          OR narrative_premium_coverage_reasons_json IS NULL
          OR narrative_decision_id IS NULL
          OR LENGTH(narrative_decision_id) <> 64
          OR (
              narrative_premium_coverage_status = 'WITHHELD'
              AND (
                  narrative_premium IS NOT NULL
                  OR anchor_support_z IS NOT NULL
                  OR divergence_state IS NOT NULL
                  OR narrative_is_converging IS NOT NULL
              )
          )
          OR (
              narrative_premium_coverage_status IN ('READY', 'PARTIAL')
              AND (
                  narrative_premium IS NULL
                  OR anchor_support_z IS NULL
                  OR divergence_state IS NULL
              )
          )
      )
""").collect()[0].n
missing_feature_built_at = spark.sql("""
    SELECT COUNT(*) AS n
    FROM security_daily_features
    WHERE feature_built_at IS NULL
""").collect()[0].n
feature_freshness_after = (
    spark.table("security_daily_features")
    .groupBy("as_of")
    .agg(F.min("feature_built_at").alias("feature_built_at"))
)
remaining_stale_snapshot_dates = (
    market_source_freshness
    .join(feature_freshness_after, "as_of", "left")
    .filter(
        F.col("feature_built_at").isNull()
        | (F.col("source_revision_loaded_at") > F.col("feature_built_at"))
    )
    .count()
)

print(
    f"E6 validation: security_daily_features={row_count}, "
    f"v_security_daily_features={serving_row_count}, "
    f"missing_or_future_pit={missing_pit}, duplicate_features={duplicate_features}, "
    f"invalid_scores={invalid_scores}, "
    f"invalid_global_score_contract={invalid_global_score_contract}, "
    f"theme_score_fact_count={theme_score_fact_count}, theme_score_count={theme_score_count}, "
    f"invalid_score_projection_count={invalid_score_projection_count}, "
    f"duplicate_theme_scores={duplicate_theme_scores}, "
    f"invalid_theme_score_contract={invalid_theme_score_contract}, "
    f"invalid_attribution_count={invalid_attribution_count}, "
    f"invalid_narrative_premium_contract={invalid_narrative_premium_contract}, "
    f"missing_feature_built_at={missing_feature_built_at}, "
    f"remaining_stale_snapshot_dates={remaining_stale_snapshot_dates}, "
    f"weight_sum={weight_validation}, opportunity_weight_sum={opportunity_weight_validation}"
)
if (
    row_count == 0
    or serving_row_count != row_count
    or missing_pit
    or duplicate_features
    or invalid_scores
    or invalid_global_score_contract
    or invalid_score_projection_count
    or duplicate_theme_scores
    or invalid_theme_score_contract
    or invalid_attribution_count
    or invalid_narrative_premium_contract
    or missing_feature_built_at
    or weight_validation != Decimal("1.000000")
    or opportunity_weight_validation != Decimal("1.000000")
):
    raise RuntimeError(
        f"E6 validation failed: security_daily_features={row_count}, "
        f"v_security_daily_features={serving_row_count}, "
        f"missing_or_future_pit={missing_pit}, "
        f"duplicate_features={duplicate_features}, invalid_scores={invalid_scores}, "
        f"invalid_global_score_contract={invalid_global_score_contract}, "
        f"theme_score_fact_count={theme_score_fact_count}, theme_score_count={theme_score_count}, "
        f"invalid_score_projection_count={invalid_score_projection_count}, "
        f"duplicate_theme_scores={duplicate_theme_scores}, "
        f"invalid_theme_score_contract={invalid_theme_score_contract}, "
        f"invalid_attribution_count={invalid_attribution_count}, "
        f"invalid_narrative_premium_contract={invalid_narrative_premium_contract}, "
        f"missing_feature_built_at={missing_feature_built_at}, "
        f"remaining_stale_snapshot_dates={remaining_stale_snapshot_dates}, "
        f"weight_sum={weight_validation}, opportunity_weight_sum={opportunity_weight_validation}"
    )

if remaining_stale_snapshot_dates:
    print(
        "E6 incremental refresh pending: "
        f"remaining_stale_snapshot_dates={remaining_stale_snapshot_dates}. "
        "Rerun nb_04_metrics to process the next bounded batch."
    )

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
