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

# Fabric Notebook: nb_05_alpha_vantage_to_gold
# Reads Alpha Vantage E8 bronze payloads and writes gold fundamentals, news,
# macro risk-free, FX, institutional holding, and ETF theme-membership facts.
# Attaches to: auspex_bronze (default lakehouse)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from datetime import date, timedelta
from decimal import Decimal
from delta.tables import DeltaTable
from pyspark.sql import functions as F
from pyspark.sql.window import Window
from pyspark.sql.types import (
    ArrayType, DateType, DecimalType, IntegerType, LongType,
    MapType, StringType, StructField, StructType,
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# PARAMETERS CELL ********************

# --- Parameters: mark this cell as the Fabric parameter cell ---
from_date = ""
to_date = ""

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Normalize and validate injected parameter values ---
_today = date.today().isoformat()
from_date = str(from_date).strip() or (date.today() - timedelta(days=7)).isoformat()
to_date = str(to_date).strip() or _today
if date.fromisoformat(from_date) > date.fromisoformat(to_date):
    raise ValueError("from_date must be on or before to_date")

print(f"Window: {from_date} to {to_date}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Helpers ---
_MAX_BIGINT = 9223372036854775807


def _require_table(table_name: str) -> None:
    if not spark.catalog.tableExists(table_name):
        raise RuntimeError(f"Required upstream table is missing: {table_name}")


def _date_paths(source: str):
    d = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    paths = []
    while d <= end:
        paths.append(f"Files/bronze/{source}/{d.year}/{d.month:02d}/{d.day:02d}/*.ndjson")
        d += timedelta(days=1)
    return paths


def _existing_paths(paths):
    result = []
    for p in paths:
        try:
            mssparkutils.fs.ls(p.rsplit("/", 1)[0])
            result.append(p)
        except Exception:
            pass
    return result


def _positive_sk(*cols):
    return F.pmod(F.xxhash64(F.concat_ws("|", *cols)), F.lit(_MAX_BIGINT)).cast(LongType())


def _date_sk(col_name: str):
    return F.date_format(F.col(col_name), "yyyyMMdd").cast(IntegerType())


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


def _merge_insert_only(table_name: str, source_df, condition: str) -> None:
    if source_df.isEmpty():
        return
    target = DeltaTable.forName(spark, table_name)
    (
        target
        .alias("t")
        .merge(source_df.alias("s"), condition)
        .whenNotMatchedInsertAll()
        .execute()
    )
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    print(f"Inserted source_rows={metrics.get('numSourceRows', 'unknown')} into {table_name}")


def _merge_replay_safe(table_name: str, source_df, update_columns: list[str]) -> None:
    if source_df.isEmpty():
        return
    source_df = source_df.dropDuplicates(["natural_key"])
    immutable_audit_columns = {"occurred_at", "quarantined_at"}
    matched_updates = {
        column: f"s.{column}"
        for column in update_columns
        if column not in immutable_audit_columns
    }
    target = DeltaTable.forName(spark, table_name)
    merge = target.alias("t").merge(source_df.alias("s"), "t.natural_key = s.natural_key")
    if matched_updates:
        merge = merge.whenMatchedUpdate(set=matched_updates)
    merge.whenNotMatchedInsertAll().execute()
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    print(f"Merged source_rows={metrics.get('numSourceRows', 'unknown')} into {table_name}")


def _ensure_columns(table_name: str, column_specs: dict[str, str]) -> None:
    existing = set(spark.table(table_name).columns)
    for column_name, ddl in column_specs.items():
        if column_name not in existing:
            spark.sql(f"ALTER TABLE {table_name} ADD COLUMNS ({ddl})")


def _revision_hash(*columns):
    return F.sha2(F.to_json(F.struct(*columns)), 256)


def _resolve_ticker_pit(source_df, row_key_column: str):
    security_history = (
        spark.table("dim_security")
        .filter(F.col("ticker").isNotNull())
        .select(
            F.col("security_sk").alias("resolved_security_sk"),
            F.upper(F.trim(F.col("ticker"))).alias("dim_ticker"),
            "valid_from",
            "valid_to",
        )
    )
    matches = (
        source_df.alias("r")
        .join(
            security_history.alias("d"),
            (F.col("r.symbol") == F.col("d.dim_ticker"))
            & (F.col("d.valid_from") <= F.col("r.event_date"))
            & (F.col("d.valid_to").isNull() | (F.col("r.event_date") < F.col("d.valid_to"))),
            "inner",
        )
        .select(
            F.col(f"r.{row_key_column}").alias(row_key_column),
            F.col("d.resolved_security_sk"),
        )
    )
    resolutions = matches.groupBy(row_key_column).agg(
        F.count(F.lit(1)).alias("security_match_count"),
        F.max("resolved_security_sk").alias("resolved_security_sk"),
    )
    return (
        source_df.join(resolutions, row_key_column, "left")
        .withColumn("security_match_count", F.coalesce(F.col("security_match_count"), F.lit(0)))
        .withColumn(
            "security_sk",
            F.when(F.col("security_match_count") == 1, F.col("resolved_security_sk")),
        )
        .drop("resolved_security_sk")
    )


for required in [
    "dim_security", "dim_date", "dim_source", "dim_entity",
    "silver_parse_errors", "silver_dq_quarantine", "silver_security_quarantine",
]:
    _require_table(required)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Read Alpha Vantage, ETF, and Finnhub news bronze records ---
av_paths = _existing_paths(_date_paths("alpha_vantage") + _date_paths("etf_holdings"))
news_paths = _existing_paths(_date_paths("news"))
classification_paths = _existing_paths(_date_paths("theme_classifier"))
paths = av_paths + news_paths + classification_paths
if not paths:
    raise RuntimeError("No Alpha Vantage/ETF/news bronze files found in window")

raw_lines = spark.read.text(paths).select(F.col("value").alias("raw_json"))
raw = raw_lines.select(
    F.get_json_object("raw_json", "$.source_id").alias("source_id"),
    F.get_json_object("raw_json", "$.batch_id").alias("batch_id"),
    F.to_timestamp(F.get_json_object("raw_json", "$.ingest_ts")).alias("ingest_ts"),
    F.get_json_object("raw_json", "$.record.profile").alias("profile"),
    F.get_json_object("raw_json", "$.record.function").alias("function"),
    F.get_json_object("raw_json", "$.record.context.symbol").alias("symbol"),
    F.get_json_object("raw_json", "$.record.context.maturity").alias("maturity"),
    F.get_json_object("raw_json", "$.record.context.ccy_pair").alias("ccy_pair"),
    F.to_timestamp(F.get_json_object("raw_json", "$.record.fetched_at")).alias("fetched_at"),
    F.get_json_object("raw_json", "$.record.payload").alias("payload_json"),
    F.upper(F.get_json_object("raw_json", "$.record.symbol")).alias("finnhub_symbol"),
    F.get_json_object("raw_json", "$.record.article").alias("article_json"),
    F.col("raw_json").alias("raw_record"),
).cache()
print(f"Alpha Vantage bronze records: {raw.count()}")
processed_batch_ids = raw.select("batch_id").where(F.col("batch_id").isNotNull()).distinct()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Source dimension upsert ---
source_schema = StructType([
    StructField("source_sk", IntegerType(), False),
    StructField("source_id", StringType(), False),
    StructField("source_type", StringType(), True),
    StructField("latency_class", StringType(), True),
    StructField("reliability_weight", DecimalType(3, 2), True),
    StructField("source_class", StringType(), True),
])
source_rows = [
    (3, "alpha_vantage", "fundamental", "daily", None, "provider_api"),
    (4, "news", "news", "daily", None, "provider_api"),
    (5, "etf_holdings", "etf", "weekly", None, "provider_api"),
    (6, "contracts", "contract", "weekly", None, "public_official"),
    (7, "sec_13f", "filing", "quarterly", None, "public_official"),
    (8, "sec_13dg", "filing", "daily", None, "public_official"),
    (9, "sec_8k", "filing", "daily", None, "public_official"),
    (10, "sec_s1", "filing", "daily", None, "public_official"),
]
source_df = spark.createDataFrame(source_rows, source_schema)
_merge_all("dim_source", source_df, "t.source_sk = s.source_sk")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- E8 Silver: macro, FX, and theme membership ---
def _parse_error_rows(source_df, error_msg: str):
    return source_df.select(
        F.concat_ws(
            ":",
            F.col("source_id"),
            F.col("function"),
            F.lit("PARSE_ERROR"),
            F.col("batch_id"),
            F.sha2(F.coalesce(
                F.col("payload_json"), F.col("article_json"), F.col("raw_record"), F.lit(""),
            ), 256),
        ).alias("natural_key"),
        F.col("source_id"),
        F.col("batch_id"),
        F.col("raw_record"),
        F.lit(error_msg).alias("error_msg"),
        F.coalesce(F.col("ingest_ts"), F.col("fetched_at")).alias("occurred_at"),
    )


def _dq_quarantine_rows(source_df, key_columns: list, dq_rule: str):
    natural_key = F.concat_ws(
        ":",
        F.col("source_id"),
        F.lit(dq_rule),
        *[F.coalesce(F.col(column).cast("string"), F.lit("missing")) for column in key_columns],
        F.col("batch_id"),
    )
    return source_df.select(
        F.sha2(natural_key, 256).alias("quarantine_id"),
        natural_key.alias("natural_key"),
        F.col("source_id"),
        F.col("batch_id"),
        F.col("raw_record"),
        F.lit(dq_rule).alias("dq_rule"),
        F.coalesce(F.col("ingest_ts"), F.col("fetched_at")).alias("quarantined_at"),
    )


theme_component_schema = StructType([
    StructField("theme_id", StringType(), False),
    StructField("theme_name", StringType(), False),
    StructField("benchmark_symbol", StringType(), False),
    StructField("etf_symbol", StringType(), False),
    StructField("blend_weight", DecimalType(9, 6), False),
])
theme_component_df = spark.createDataFrame([
    ("ai_compute_semiconductors", "AI Compute & Semiconductors", "SMH", "SMH", Decimal("1.0")),
    ("enterprise_technology", "Enterprise Technology", "XLK", "XLK", Decimal("1.0")),
    ("energy_security_producers", "Energy Security & Producers", "XLE", "XLE", Decimal("1.0")),
    ("healthcare", "Healthcare", "XLV", "XLV", Decimal("1.0")),
    ("data_center_buildout", "Data Center Buildout", "DTCR", "DTCR", Decimal("0.5")),
    ("data_center_buildout", "Data Center Buildout", "DTCR", "PAVE", Decimal("0.25")),
    ("data_center_buildout", "Data Center Buildout", "DTCR", "GRID", Decimal("0.25")),
], theme_component_schema).cache()

spark.sql("""
    CREATE TABLE IF NOT EXISTS dim_theme (
        theme_id STRING NOT NULL, theme_name STRING NOT NULL,
        benchmark_symbol STRING NOT NULL, is_active BOOLEAN NOT NULL,
        catalog_version INT NOT NULL, updated_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
theme_dimension_df = (
    theme_component_df.select("theme_id", "theme_name", "benchmark_symbol").distinct()
    .unionByName(spark.createDataFrame(
        [("quantum_computing", "Quantum Computing", "QTUM")],
        "theme_id STRING, theme_name STRING, benchmark_symbol STRING",
    ))
    .withColumn("is_active", F.lit(True))
    .withColumn("catalog_version", F.lit(1))
    .withColumn("updated_at", F.current_timestamp())
)
(
    theme_dimension_df.write.format("delta").mode("overwrite")
    .option("overwriteSchema", "true").saveAsTable("dim_theme")
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS bridge_theme_etf (
        theme_id STRING NOT NULL, etf_symbol STRING NOT NULL,
        blend_weight DECIMAL(9,6) NOT NULL, is_active BOOLEAN NOT NULL,
        catalog_version INT NOT NULL, updated_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
theme_bridge_df = (
    theme_component_df.select("theme_id", "etf_symbol", "blend_weight")
    .withColumn("is_active", F.lit(True))
    .withColumn("catalog_version", F.lit(1))
    .withColumn("updated_at", F.current_timestamp())
)
(
    theme_bridge_df.write.format("delta").mode("overwrite")
    .option("overwriteSchema", "true").saveAsTable("bridge_theme_etf")
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS security_theme_classification (
        classification_id STRING NOT NULL,
        security_sk BIGINT NOT NULL,
        ticker STRING NOT NULL,
        theme_id STRING NOT NULL,
        provenance STRING NOT NULL,
        confidence DOUBLE NOT NULL,
        rationale STRING NOT NULL,
        effective_from DATE NOT NULL,
        effective_to DATE,
        classification_version STRING NOT NULL,
        updated_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
manual_theme_seed = spark.createDataFrame([
    ("AMD", "data_center_buildout", "Compute accelerators used in data-center infrastructure."),
    ("AVGO", "data_center_buildout", "Networking and custom silicon used in data centers."),
    ("CAMT", "ai_compute_semiconductors", "Semiconductor inspection and metrology equipment."),
    ("COHR", "data_center_buildout", "Optical communications components used in data-center interconnects."),
    ("INTC", "data_center_buildout", "Data-center processors, accelerators, and platform infrastructure."),
    ("MRVL", "data_center_buildout", "Data-center connectivity, switching, and custom silicon."),
    ("NVDA", "ai_compute_semiconductors", "AI accelerators and compute platforms."),
    ("PLTR", "enterprise_technology", "Enterprise data and software platform."),
    ("RGTI", "quantum_computing", "Quantum processors and cloud quantum-computing systems."),
    ("VRT", "data_center_buildout", "Power, cooling, and infrastructure for data centers."),
], "ticker STRING, theme_id STRING, rationale STRING")
manual_classifications = (
    manual_theme_seed.alias("s")
    .join(
        spark.table("dim_security")
        .filter(F.col("is_current") == F.lit(True))
        .select("security_sk", F.upper("ticker").alias("ticker"))
        .alias("d"),
        "ticker",
        "inner",
    )
    .withColumn(
        "classification_id",
        F.sha2(F.concat_ws("|", F.lit("manual_v1"), "ticker", "theme_id"), 256),
    )
    .withColumn("provenance", F.lit("manual"))
    .withColumn("confidence", F.lit(1.0))
    .withColumn("effective_from", F.to_date(F.lit("2026-08-04")))
    .withColumn("effective_to", F.lit(None).cast(DateType()))
    .withColumn("classification_version", F.lit("manual_v1"))
    .withColumn("updated_at", F.to_timestamp(F.lit("2026-08-04T00:00:00Z")))
    .select(
        "classification_id", "security_sk", "ticker", "theme_id", "provenance",
        "confidence", "rationale", "effective_from", "effective_to",
        "classification_version", "updated_at",
    )
)
if manual_classifications.count() != 10:
    raise RuntimeError("Manual portfolio theme seed did not resolve exactly ten securities")
classification_target = DeltaTable.forName(spark, "security_theme_classification")
(
    classification_target.alias("t")
    .merge(manual_classifications.alias("s"), "t.classification_id = s.classification_id")
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
    .execute()
)

llm_classification_source = (
    raw_lines
    .filter(F.get_json_object("raw_json", "$.source_id") == F.lit("theme_classifier"))
    .filter(F.get_json_object("raw_json", "$.record.classification_status") == F.lit("classified"))
    .select(
        F.get_json_object("raw_json", "$.record.classification_id").alias("classification_id"),
        F.get_json_object("raw_json", "$.record.security_sk").cast(LongType()).alias("security_sk"),
        F.upper(F.get_json_object("raw_json", "$.record.ticker")).alias("ticker"),
        F.get_json_object("raw_json", "$.record.theme_id").alias("theme_id"),
        F.get_json_object("raw_json", "$.record.provenance").alias("provenance"),
        F.get_json_object("raw_json", "$.record.confidence").cast("double").alias("confidence"),
        F.get_json_object("raw_json", "$.record.rationale").alias("rationale"),
        F.to_date(F.get_json_object("raw_json", "$.record.effective_from")).alias("effective_from"),
        F.get_json_object("raw_json", "$.record.classification_version").alias("classification_version"),
        F.to_timestamp(F.get_json_object("raw_json", "$.record.classified_at")).alias("updated_at"),
    )
)
invalid_llm_classifications = (
    llm_classification_source.alias("c")
    .join(
        spark.table("dim_theme").filter(F.col("is_active") == F.lit(True)).select("theme_id").alias("t"),
        "theme_id",
        "left",
    )
    .join(
        spark.table("dim_security")
        .filter(F.col("is_current") == F.lit(True))
        .select("security_sk", F.upper("ticker").alias("dim_ticker"))
        .alias("d"),
        "security_sk",
        "left",
    )
    .filter(
        F.col("classification_id").isNull()
        | (F.length("classification_id") != 64)
        | F.col("t.theme_id").isNull()
        | F.col("d.security_sk").isNull()
        | (F.col("ticker") != F.col("dim_ticker"))
        | (F.col("provenance") != F.lit("llm"))
        | F.col("confidence").isNull()
        | (F.col("confidence") < F.lit(0.0))
        | (F.col("confidence") > F.lit(0.85))
        | F.col("rationale").isNull()
        | F.col("effective_from").isNull()
        | F.col("updated_at").isNull()
    )
)
if not invalid_llm_classifications.isEmpty():
    raise RuntimeError("LLM theme classification Bronze validation failed")
if not llm_classification_source.isEmpty():
    llm_classifications = (
        llm_classification_source
        .withColumn("effective_to", F.lit(None).cast(DateType()))
        .select(
            "classification_id", "security_sk", "ticker", "theme_id", "provenance",
            "confidence", "rationale", "effective_from", "effective_to",
            "classification_version", "updated_at",
        )
        .dropDuplicates(["classification_id"])
    )
    (
        classification_target.alias("t")
        .merge(llm_classifications.alias("s"), "t.classification_id = s.classification_id")
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )


macro_schema = StructType([StructField("data", ArrayType(MapType(StringType(), StringType())))])
macro_source = (
    raw.filter(F.col("function") == "TREASURY_YIELD")
    .withColumn("parsed_payload", F.from_json("payload_json", macro_schema))
)
macro_parse_failures = macro_source.filter(
    F.col("parsed_payload").isNull()
    | F.col("parsed_payload.data").isNull()
    | (F.size(F.col("parsed_payload.data")) == 0)
)
macro_points = (
    macro_source.filter(
        F.col("parsed_payload").isNotNull()
        & F.col("parsed_payload.data").isNotNull()
        & (F.size(F.col("parsed_payload.data")) > 0)
    )
    .withColumn("point", F.explode("parsed_payload.data"))
    .select(
        "source_id", "profile", "batch_id", "ingest_ts", "fetched_at", "raw_record",
        F.concat(F.lit("US_TREASURY_"), F.upper(F.coalesce(F.col("maturity"), F.lit("3month")))).alias("indicator_code"),
        F.element_at("point", "date").alias("raw_event_date"),
        F.element_at("point", "value").alias("raw_value"),
    )
    .withColumn("event_date", F.to_date("raw_event_date"))
    .withColumn("knowledge_date", F.to_date("fetched_at"))
    .withColumn("value", F.col("raw_value").cast(DecimalType(20, 6)))
)
macro_valid = (
    F.col("indicator_code").isNotNull()
    & F.col("event_date").isNotNull()
    & F.col("knowledge_date").isNotNull()
    & F.col("value").isNotNull()
    & (F.col("event_date") <= F.col("knowledge_date"))
    & (F.col("knowledge_date") <= F.current_date())
)
macro_dq_failures = macro_points.filter(~macro_valid)
macro_pass = macro_points.filter(macro_valid).withColumn(
    "macro_revision_hash",
    _revision_hash(F.col("indicator_code"), F.col("value"), F.col("knowledge_date")),
)
macro_revision_window = Window.partitionBy(
    "indicator_code", "event_date", "macro_revision_hash"
).orderBy(F.col("ingest_ts").asc_nulls_last(), F.col("batch_id").asc())
silver_macro_df = (
    macro_pass
    .withColumn("revision_row_number", F.row_number().over(macro_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .select(
        "indicator_code", "value", "macro_revision_hash", "event_date", "knowledge_date",
        "source_id", "profile", "batch_id", "ingest_ts",
        F.current_timestamp().alias("loaded_at"),
    )
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_macro_observation (
        indicator_code STRING NOT NULL,
        value DECIMAL(20,6) NOT NULL,
        macro_revision_hash STRING NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL,
        profile STRING,
        batch_id STRING NOT NULL,
        ingest_ts TIMESTAMP NOT NULL,
        loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
_merge_insert_only(
    "silver_macro_observation",
    silver_macro_df,
    "t.indicator_code = s.indicator_code AND t.event_date = s.event_date "
    "AND t.macro_revision_hash = s.macro_revision_hash",
)

fx_schema = StructType([
    StructField("Realtime Currency Exchange Rate", MapType(StringType(), StringType()))
])
fx_source = (
    raw.filter(F.col("function") == "CURRENCY_EXCHANGE_RATE")
    .withColumn("parsed_payload", F.from_json("payload_json", fx_schema))
    .withColumn("exchange_rate", F.col("parsed_payload").getField("Realtime Currency Exchange Rate"))
)
fx_parse_failures = fx_source.filter(
    F.col("parsed_payload").isNull() | F.col("exchange_rate").isNull()
)
fx_rows = (
    fx_source.filter(F.col("parsed_payload").isNotNull() & F.col("exchange_rate").isNotNull())
    .select(
        "source_id", "profile", "batch_id", "ingest_ts", "fetched_at", "raw_record",
        F.upper(F.coalesce(F.col("ccy_pair"), F.lit("USDCHF"))).alias("ccy_pair"),
        F.element_at("exchange_rate", "6. Last Refreshed").alias("raw_event_date"),
        F.element_at("exchange_rate", "5. Exchange Rate").alias("raw_rate"),
    )
    .withColumn("event_date", F.coalesce(F.to_date("raw_event_date"), F.to_date("fetched_at")))
    .withColumn("knowledge_date", F.to_date("fetched_at"))
    .withColumn("rate", F.col("raw_rate").cast(DecimalType(18, 8)))
)
fx_valid = (
    F.col("ccy_pair").rlike("^[A-Z]{6}$")
    & F.col("event_date").isNotNull()
    & F.col("knowledge_date").isNotNull()
    & F.col("rate").isNotNull()
    & (F.col("rate") > 0)
    & (F.col("event_date") <= F.col("knowledge_date"))
    & (F.col("knowledge_date") <= F.current_date())
)
fx_dq_failures = fx_rows.filter(~fx_valid)
fx_pass = fx_rows.filter(fx_valid).withColumn(
    "fx_revision_hash",
    _revision_hash(F.col("ccy_pair"), F.col("rate"), F.col("knowledge_date")),
)
fx_revision_window = Window.partitionBy(
    "ccy_pair", "event_date", "fx_revision_hash"
).orderBy(F.col("ingest_ts").asc_nulls_last(), F.col("batch_id").asc())
silver_fx_df = (
    fx_pass
    .withColumn("revision_row_number", F.row_number().over(fx_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .select(
        "ccy_pair", "rate", "fx_revision_hash", "event_date", "knowledge_date",
        "source_id", "profile", "batch_id", "ingest_ts",
        F.current_timestamp().alias("loaded_at"),
    )
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_fx_rate (
        ccy_pair STRING NOT NULL,
        rate DECIMAL(18,8) NOT NULL,
        fx_revision_hash STRING NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL,
        profile STRING,
        batch_id STRING NOT NULL,
        ingest_ts TIMESTAMP NOT NULL,
        loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
_merge_insert_only(
    "silver_fx_rate",
    silver_fx_df,
    "t.ccy_pair = s.ccy_pair AND t.event_date = s.event_date "
    "AND t.fx_revision_hash = s.fx_revision_hash",
)

theme_schema = StructType([StructField("holdings", ArrayType(MapType(StringType(), StringType())))])
theme_source = (
    raw.filter(F.col("function") == "ETF_PROFILE")
    .withColumn("parsed_payload", F.from_json("payload_json", theme_schema))
)
theme_parse_failures = theme_source.filter(
    F.col("parsed_payload").isNull()
    | F.col("parsed_payload.holdings").isNull()
    | (F.size(F.col("parsed_payload.holdings")) == 0)
)
theme_rows = (
    theme_source.filter(
        F.col("parsed_payload").isNotNull()
        & F.col("parsed_payload.holdings").isNotNull()
        & (F.size(F.col("parsed_payload.holdings")) > 0)
    )
    .withColumn("holding", F.explode("parsed_payload.holdings"))
    .select(
        "source_id", "profile", "batch_id", "ingest_ts", "fetched_at", "raw_record",
        F.upper(F.trim(F.col("symbol"))).alias("etf_symbol"),
        F.upper(F.trim(F.element_at("holding", "symbol"))).alias("constituent_symbol"),
        F.element_at("holding", "description").alias("holding_description"),
        F.element_at("holding", "weight").alias("raw_weight"),
    )
    .join(F.broadcast(theme_component_df), "etf_symbol", "inner")
    .withColumn("event_date", F.to_date("fetched_at"))
    .withColumn("knowledge_date", F.to_date("fetched_at"))
    .withColumn("weight", F.regexp_replace(F.col("raw_weight"), "%", "").cast(DecimalType(9, 6)))
)
non_security_symbols = ["N/A", "NA", "NONE", "CASH", ""]
is_non_security_holding = F.coalesce(
    F.col("constituent_symbol").isin(non_security_symbols),
    F.lit(False),
)
theme_non_security_rows = theme_rows.filter(
    is_non_security_holding
)
theme_security_candidates = theme_rows.filter(
    ~is_non_security_holding
)
theme_valid = (
    F.col("etf_symbol").isNotNull()
    & (F.length(F.col("etf_symbol")) > 0)
    & F.col("constituent_symbol").isNotNull()
    & (F.length(F.col("constituent_symbol")) > 0)
    & F.col("event_date").isNotNull()
    & F.col("knowledge_date").isNotNull()
    & F.col("weight").isNotNull()
    & (F.col("weight") >= 0)
    & (F.col("weight") <= 100)
    & (F.col("event_date") <= F.col("knowledge_date"))
    & (F.col("knowledge_date") <= F.current_date())
)
theme_dq_failures = theme_security_candidates.filter(~theme_valid)
theme_valid_rows = theme_security_candidates.filter(theme_valid).withColumn(
    "theme_member_key",
    F.sha2(F.concat_ws(
            "|", "source_id", "batch_id", "theme_id", "etf_symbol", "constituent_symbol",
        F.col("event_date").cast("string"),
    ), 256),
).withColumn(
    "theme_row_key",
    F.sha2(F.concat_ws(
        "|", "source_id", "batch_id", "theme_id", "etf_symbol", "constituent_symbol",
        F.col("event_date").cast("string"), "raw_weight",
    ), 256),
).dropDuplicates(["theme_row_key"])
theme_conflict_keys = (
    theme_valid_rows
    .groupBy("theme_member_key")
    .agg(F.countDistinct("weight").alias("distinct_weight_count"))
    .filter(F.col("distinct_weight_count") > 1)
    .select("theme_member_key")
)
theme_conflicting_rows = theme_valid_rows.join(
    theme_conflict_keys,
    "theme_member_key",
    "inner",
)
theme_pass = theme_valid_rows.join(
    theme_conflict_keys,
    "theme_member_key",
    "left_anti",
)

theme_security_lookup = (
    spark.table("dim_security")
    .filter(
        F.col("ticker").isNotNull()
        & (F.col("is_active") == F.lit(True))
        & F.upper(F.trim(F.col("exchange"))).isin("NASDAQ", "NYSE", "CBOE")
    )
    .select(
        F.col("security_sk").alias("resolved_security_sk"),
        F.upper("ticker").alias("dim_ticker"),
        "valid_from",
        "valid_to",
    )
)
theme_resolution_window = Window.partitionBy("theme_row_key")
theme_resolved = (
    theme_pass.join(
        theme_security_lookup,
        (theme_pass.constituent_symbol == theme_security_lookup.dim_ticker)
        & (theme_pass.event_date >= theme_security_lookup.valid_from)
        & (
            theme_security_lookup.valid_to.isNull()
            | (theme_pass.event_date < theme_security_lookup.valid_to)
        ),
        "left",
    )
    .withColumn(
        "security_match_count",
        F.count(F.col("resolved_security_sk")).over(theme_resolution_window),
    )
    .withColumn(
        "security_sk",
        F.when(F.col("security_match_count") == 1, F.col("resolved_security_sk")),
    )
    .dropDuplicates(["theme_row_key"])
    .drop("resolved_security_sk", "dim_ticker", "valid_from", "valid_to")
)
theme_out_of_scope = theme_resolved.filter(F.col("security_match_count") == 0)
theme_unresolved = theme_resolved.filter(F.col("security_match_count") > 1)

if not theme_out_of_scope.isEmpty():
    out_of_scope_key = F.concat_ws(
        ":", F.col("source_id"), F.lit("OUT_OF_SCOPE_NON_US_LISTING"),
        F.col("theme_id"), F.col("etf_symbol"), F.col("constituent_symbol"),
        F.col("event_date").cast("string"), F.col("batch_id"),
    )
    _merge_replay_safe(
        "silver_security_quarantine",
        theme_out_of_scope.select(
            F.sha2(out_of_scope_key, 256).alias("quarantine_id"),
            out_of_scope_key.alias("natural_key"), "source_id",
            F.col("constituent_symbol").alias("raw_identifier"),
            F.lit("OUT_OF_SCOPE_NON_US_LISTING").alias("reason"),
            F.lit("No active Nasdaq, NYSE, or CBOE listing matched the ETF constituent").alias("details"),
            "event_date", "knowledge_date", "batch_id",
            F.coalesce(F.col("ingest_ts"), F.col("fetched_at")).alias("quarantined_at"),
        ),
        [
            "source_id", "raw_identifier", "reason", "details", "event_date",
            "knowledge_date", "batch_id", "quarantined_at",
        ],
    )

if not theme_unresolved.isEmpty():
    unresolved_natural_key = F.concat_ws(
        ":",
        F.col("source_id"), F.lit("SECURITY_UNRESOLVED"), F.col("theme_id"),
        F.col("constituent_symbol"), F.col("event_date").cast("string"), F.col("batch_id"),
    )
    theme_security_quarantine = theme_unresolved.select(
        F.sha2(unresolved_natural_key, 256).alias("quarantine_id"),
        unresolved_natural_key.alias("natural_key"),
        F.col("source_id"),
        F.col("constituent_symbol").alias("raw_identifier"),
        F.lit("SECURITY_UNRESOLVED").alias("reason"),
        F.concat(F.lit("No unique PIT ticker match; active snapshot withheld; theme="), F.col("theme_id")).alias("details"),
        F.col("event_date"),
        F.col("knowledge_date"),
        F.col("batch_id"),
        F.coalesce(F.col("ingest_ts"), F.col("fetched_at")).alias("quarantined_at"),
    )
    _merge_replay_safe(
        "silver_security_quarantine",
        theme_security_quarantine,
        [
            "source_id", "raw_identifier", "reason", "details", "event_date",
            "knowledge_date", "batch_id", "quarantined_at",
        ],
    )

observed_theme_components = (
    theme_source.filter(
        F.col("parsed_payload").isNotNull()
        & F.col("parsed_payload.holdings").isNotNull()
        & (F.size(F.col("parsed_payload.holdings")) > 0)
    )
    .select(
        "source_id", "batch_id", "ingest_ts", "fetched_at", "raw_record",
        F.upper(F.trim(F.col("symbol"))).alias("etf_symbol"),
        F.to_date("fetched_at").alias("event_date"),
    )
    .join(F.broadcast(theme_component_df), "etf_symbol", "inner")
)
expected_theme_components = theme_component_df.groupBy("theme_id").agg(
    F.countDistinct("etf_symbol").alias("expected_component_count"),
)
missing_theme_components = (
    observed_theme_components.groupBy(
        "source_id", "batch_id", "theme_id", "event_date",
    )
    .agg(F.countDistinct("etf_symbol").alias("observed_component_count"))
    .join(expected_theme_components, "theme_id", "inner")
    .filter(F.col("observed_component_count") != F.col("expected_component_count"))
    .select("source_id", "batch_id", "theme_id", "event_date")
)
incomplete_theme_snapshots = (
    theme_dq_failures.select("source_id", "batch_id", "theme_id", "event_date")
    .unionByName(theme_unresolved.select("source_id", "batch_id", "theme_id", "event_date"))
    .unionByName(theme_conflicting_rows.select("source_id", "batch_id", "theme_id", "event_date"))
    .unionByName(missing_theme_components)
    .dropDuplicates()
)
theme_snapshot_evidence = (
    observed_theme_components.groupBy("source_id", "batch_id", "theme_id", "event_date")
    .agg(
        F.max("ingest_ts").alias("ingest_ts"),
        F.max("fetched_at").alias("fetched_at"),
        F.first("raw_record", ignorenulls=True).alias("raw_record"),
    )
)
incomplete_theme_snapshot_failures = (
    incomplete_theme_snapshots.alias("s")
    .join(
        theme_snapshot_evidence.alias("e"),
        (F.col("s.source_id") == F.col("e.source_id"))
        & (F.col("s.batch_id") == F.col("e.batch_id"))
        & F.col("s.theme_id").eqNullSafe(F.col("e.theme_id"))
        & F.col("s.event_date").eqNullSafe(F.col("e.event_date")),
        "left",
    )
    .select(
        F.col("s.source_id").alias("source_id"),
        F.col("s.batch_id").alias("batch_id"),
        F.col("s.theme_id").alias("theme_id"),
        F.col("s.event_date").alias("event_date"),
        F.col("e.ingest_ts").alias("ingest_ts"),
        F.col("e.fetched_at").alias("fetched_at"),
        F.col("e.raw_record").alias("raw_record"),
    )
)
theme_resolved_pass = (
    theme_resolved.filter(F.col("security_sk").isNotNull() & (F.col("security_match_count") == 1))
    .join(
        incomplete_theme_snapshots,
        ["source_id", "batch_id", "theme_id", "event_date"],
        "left_anti",
    )
)
component_weight_window = Window.partitionBy(
    "source_id", "batch_id", "theme_id", "etf_symbol", "event_date",
)
silver_theme_component_df = (
    theme_resolved_pass
    .withColumn("component_total_weight", F.sum("weight").over(component_weight_window))
    .withColumn(
        "normalized_etf_weight",
        F.when(
            F.col("component_total_weight") > 0,
            F.col("weight") / F.col("component_total_weight"),
        ),
    )
    .withColumn(
        "weighted_theme_weight",
        F.col("normalized_etf_weight") * F.col("blend_weight") * F.lit(100),
    )
    .withColumn(
        "component_revision_hash",
        _revision_hash(
            F.col("theme_id"), F.col("etf_symbol"), F.col("security_sk"),
            F.col("weight"), F.col("normalized_etf_weight"),
            F.col("weighted_theme_weight"), F.col("knowledge_date"),
        ),
    )
    .select(
        "theme_id", "theme_name", "benchmark_symbol", "etf_symbol", "blend_weight",
        "security_sk", "constituent_symbol", F.col("weight").alias("raw_weight"),
        "normalized_etf_weight", "weighted_theme_weight", "component_revision_hash",
        "event_date", "knowledge_date", "source_id", "profile", "batch_id", "ingest_ts",
        F.current_timestamp().alias("loaded_at"),
    )
)

silver_theme_df = (
    silver_theme_component_df.groupBy(
        "source_id", "batch_id", "theme_id", "theme_name", "benchmark_symbol",
        "security_sk", "constituent_symbol", "event_date",
    )
    .agg(
        F.sum("weighted_theme_weight").cast(DecimalType(9, 6)).alias("weight"),
        F.max("knowledge_date").alias("knowledge_date"),
        F.max("ingest_ts").alias("ingest_ts"),
        F.max("profile").alias("profile"),
        F.concat_ws(",", F.sort_array(F.collect_set("etf_symbol"))).alias("component_etf_symbols"),
        F.to_json(F.array_sort(F.collect_list(F.struct(
            "etf_symbol", "blend_weight", "raw_weight", "normalized_etf_weight",
            "weighted_theme_weight", "component_revision_hash",
        )))).alias("component_lineage_json"),
    )
    .withColumn("etf_symbol", F.col("benchmark_symbol"))
    .withColumn(
        "theme_revision_hash",
        _revision_hash(
            F.col("theme_id"), F.col("security_sk"), F.col("weight"),
            F.col("component_lineage_json"), F.col("knowledge_date"),
        ),
    )
    .withColumn("is_ground_truth", F.lit(True))
    .withColumn("loaded_at", F.current_timestamp())
    .select(
        "theme_id", "etf_symbol", "security_sk", "constituent_symbol", "weight",
        "is_ground_truth", "component_etf_symbols", "component_lineage_json",
        "theme_revision_hash", "event_date", "knowledge_date", "source_id",
        "profile", "batch_id", "ingest_ts", "loaded_at",
    )
)

latest_theme_batch_window = Window.partitionBy("theme_id").orderBy(
    F.col("ingest_ts").desc(), F.col("batch_id").desc(),
)
latest_theme_batches = (
    silver_theme_df.select("theme_id", "batch_id", "ingest_ts").distinct()
    .withColumn("batch_rank", F.row_number().over(latest_theme_batch_window))
    .filter(F.col("batch_rank") == 1)
    .drop("batch_rank")
)
silver_theme_df = silver_theme_df.join(
    latest_theme_batches,
    ["theme_id", "batch_id", "ingest_ts"],
    "inner",
)
silver_theme_component_df = silver_theme_component_df.join(
    latest_theme_batches,
    ["theme_id", "batch_id", "ingest_ts"],
    "inner",
)

(
    silver_theme_component_df.write.format("delta").mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable("silver_theme_component_membership")
)

(
    silver_theme_df.write.format("delta").mode("overwrite")
    .option("overwriteSchema", "true").saveAsTable("silver_theme_membership")
)

parse_errors = (
    _parse_error_rows(macro_parse_failures, "TREASURY_YIELD payload is not a valid data object")
    .unionByName(_parse_error_rows(fx_parse_failures, "CURRENCY_EXCHANGE_RATE payload is not a valid exchange-rate object"))
    .unionByName(_parse_error_rows(theme_parse_failures, "ETF_PROFILE payload is not a valid holdings object"))
)
_merge_replay_safe(
    "silver_parse_errors",
    parse_errors,
    ["source_id", "batch_id", "raw_record", "error_msg", "occurred_at"],
)

dq_quarantine = (
    _dq_quarantine_rows(
        macro_dq_failures,
        ["indicator_code", "raw_event_date", "raw_value"],
        "INVALID_MACRO_PIT_OR_VALUE",
    )
    .unionByName(_dq_quarantine_rows(
        fx_dq_failures,
        ["ccy_pair", "raw_event_date", "raw_rate"],
        "INVALID_FX_PIT_OR_RATE",
    ))
    .unionByName(_dq_quarantine_rows(
        theme_dq_failures,
        ["theme_id", "constituent_symbol", "raw_weight"],
        "INVALID_THEME_HOLDING",
    ))
    .unionByName(_dq_quarantine_rows(
        theme_non_security_rows,
        ["theme_id", "holding_description", "raw_weight"],
        "NON_SECURITY_THEME_HOLDING",
    ))
    .unionByName(_dq_quarantine_rows(
        theme_conflicting_rows,
        ["theme_id", "constituent_symbol", "event_date"],
        "CONFLICTING_THEME_WEIGHT",
    ))
    .unionByName(_dq_quarantine_rows(
        incomplete_theme_snapshot_failures,
        ["theme_id", "event_date"],
        "INCOMPLETE_THEME_SNAPSHOT",
    ))
)
_merge_replay_safe(
    "silver_dq_quarantine",
    dq_quarantine,
    ["source_id", "batch_id", "raw_record", "dq_rule", "quarantined_at"],
)

print(
    "E8 Silver merge complete: silver_macro_observation, silver_fx_rate, "
    "silver_theme_membership, and replay-safe quarantine tables"
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- E8 Silver: fundamentals ---
statement_schema = StructType([
    StructField("quarterlyReports", ArrayType(MapType(StringType(), StringType())))
])

overview_source = (
    raw.filter(F.col("function") == "OVERVIEW")
    .withColumn("parsed_payload", F.from_json("payload_json", MapType(StringType(), StringType())))
)
overview_parse_failures = overview_source.filter(
    F.col("parsed_payload").isNull() | (F.size(F.col("parsed_payload")) == 0)
)
overview_rows = (
    overview_source.filter(F.col("parsed_payload").isNotNull() & (F.size(F.col("parsed_payload")) > 0))
    .select(
        "source_id", "profile", "batch_id", "ingest_ts", "fetched_at", "raw_record",
        F.upper(F.trim(F.col("symbol"))).alias("symbol"),
        F.coalesce(F.col("fetched_at"), F.col("ingest_ts")).alias("knowledge_ts"),
        F.element_at("parsed_payload", "Currency").alias("currency"),
        F.element_at("parsed_payload", "Sector").alias("sector"),
        F.element_at("parsed_payload", "Industry").alias("industry"),
        F.element_at("parsed_payload", "MarketCapitalization").cast(DecimalType(20, 2)).alias("market_cap"),
        F.element_at("parsed_payload", "SharesOutstanding").cast(DecimalType(20, 4)).alias("shares_outstanding"),
        F.element_at("parsed_payload", "EBITDA").cast(DecimalType(20, 2)).alias("ebitda"),
        F.element_at("parsed_payload", "PERatio").cast(DecimalType(18, 6)).alias("pe_ratio"),
        F.element_at("parsed_payload", "PEGRatio").cast(DecimalType(18, 6)).alias("peg_ratio"),
        F.element_at("parsed_payload", "PriceToSalesRatioTTM").cast(DecimalType(18, 6)).alias("ps_ratio"),
        F.element_at("parsed_payload", "EVToEBITDA").cast(DecimalType(18, 6)).alias("ev_ebitda"),
        F.element_at("parsed_payload", "GrossProfitTTM").cast(DecimalType(20, 2)).alias("gross_profit_ttm"),
        F.element_at("parsed_payload", "ProfitMargin").cast(DecimalType(18, 6)).alias("profit_margin"),
        F.element_at("parsed_payload", "QuarterlyRevenueGrowthYOY").cast(DecimalType(18, 6)).alias("rev_growth_yoy"),
    )
    .withColumn("event_date", F.to_date("knowledge_ts"))
    .withColumn("knowledge_date", F.to_date("knowledge_ts"))
    .withColumn("source_record_hash", F.sha2("raw_record", 256))
    .withColumn(
        "fundamentals_row_key",
        F.sha2(F.concat_ws(
            "|", "source_id", "batch_id", "symbol", F.lit("OVERVIEW_SNAPSHOT"),
            F.col("knowledge_ts").cast("string"), "source_record_hash",
        ), 256),
    )
)
overview_valid = (
    F.col("symbol").isNotNull()
    & (F.length(F.col("symbol")) > 0)
    & F.col("event_date").isNotNull()
    & F.col("knowledge_date").isNotNull()
    & (F.col("event_date") <= F.col("knowledge_date"))
    & (F.col("knowledge_date") <= F.current_date())
    & F.coalesce(
        F.col("currency"), F.col("sector"), F.col("industry"),
        F.col("market_cap").cast("string"), F.col("shares_outstanding").cast("string"),
        F.col("ebitda").cast("string"),
        F.col("pe_ratio").cast("string"), F.col("rev_growth_yoy").cast("string"),
    ).isNotNull()
)
overview_dq_failures = overview_rows.filter(~overview_valid)
overview_pass = overview_rows.filter(overview_valid)

balance_source = (
    raw.filter(F.col("function") == "BALANCE_SHEET")
    .withColumn("parsed_payload", F.from_json("payload_json", statement_schema))
)
balance_parse_failures = balance_source.filter(
    F.col("parsed_payload").isNull()
    | F.col("parsed_payload.quarterlyReports").isNull()
    | (F.size(F.col("parsed_payload.quarterlyReports")) == 0)
)
balance_rows = (
    balance_source.filter(
        F.col("parsed_payload").isNotNull()
        & F.col("parsed_payload.quarterlyReports").isNotNull()
        & (F.size(F.col("parsed_payload.quarterlyReports")) > 0)
    )
    .withColumn("statement", F.explode("parsed_payload.quarterlyReports"))
    .select(
        "source_id", "profile", "batch_id", "ingest_ts", "fetched_at", "raw_record",
        F.upper(F.trim(F.col("symbol"))).alias("symbol"),
        F.to_date(F.element_at("statement", "fiscalDateEnding")).alias("event_date"),
        F.coalesce(F.col("fetched_at"), F.col("ingest_ts")).alias("knowledge_ts"),
        F.upper(F.trim(F.element_at("statement", "reportedCurrency"))).alias("balance_currency"),
        F.element_at("statement", "cashAndCashEquivalentsAtCarryingValue").cast(DecimalType(20, 2)).alias("cash_and_equivalents"),
        F.element_at("statement", "shortLongTermDebtTotal").cast(DecimalType(20, 2)).alias("reported_total_debt"),
        F.element_at("statement", "shortTermDebt").cast(DecimalType(20, 2)).alias("short_term_debt"),
        F.element_at("statement", "longTermDebt").cast(DecimalType(20, 2)).alias("long_term_debt"),
    )
    .withColumn("knowledge_date", F.to_date("knowledge_ts"))
    .withColumn(
        "total_debt",
        F.coalesce(
            F.col("reported_total_debt"),
            F.when(
                F.col("short_term_debt").isNotNull() & F.col("long_term_debt").isNotNull(),
                F.col("short_term_debt") + F.col("long_term_debt"),
            ),
        ).cast(DecimalType(20, 2)),
    )
    .withColumn("source_record_hash", F.sha2("raw_record", 256))
)
balance_valid = (
    F.col("symbol").isNotNull()
    & (F.length(F.col("symbol")) > 0)
    & F.col("event_date").isNotNull()
    & F.col("knowledge_date").isNotNull()
    & (F.col("event_date") <= F.col("knowledge_date"))
    & (F.col("knowledge_date") <= F.current_date())
    & (F.col("cash_and_equivalents").isNotNull() | F.col("total_debt").isNotNull())
)
balance_dq_failures = balance_rows.filter(~balance_valid)
balance_pass = balance_rows.filter(balance_valid).dropDuplicates([
    "source_id", "batch_id", "symbol", "event_date", "knowledge_ts", "source_record_hash",
])

cashflow_source = (
    raw.filter(F.col("function") == "CASH_FLOW")
    .withColumn("parsed_payload", F.from_json("payload_json", statement_schema))
)
cashflow_parse_failures = cashflow_source.filter(
    F.col("parsed_payload").isNull()
    | F.col("parsed_payload.quarterlyReports").isNull()
    | (F.size(F.col("parsed_payload.quarterlyReports")) == 0)
)
cashflow_rows = (
    cashflow_source.filter(
        F.col("parsed_payload").isNotNull()
        & F.col("parsed_payload.quarterlyReports").isNotNull()
        & (F.size(F.col("parsed_payload.quarterlyReports")) > 0)
    )
    .withColumn("statement", F.explode("parsed_payload.quarterlyReports"))
    .select(
        "source_id", "profile", "batch_id", "ingest_ts", "fetched_at", "raw_record",
        F.upper(F.trim(F.col("symbol"))).alias("symbol"),
        F.to_date(F.element_at("statement", "fiscalDateEnding")).alias("event_date"),
        F.coalesce(F.col("fetched_at"), F.col("ingest_ts")).alias("knowledge_ts"),
        F.upper(F.trim(F.element_at("statement", "reportedCurrency"))).alias("cashflow_currency"),
        F.element_at("statement", "operatingCashflow").cast(DecimalType(20, 2)).alias("operating_cashflow"),
        F.element_at("statement", "capitalExpenditures").cast(DecimalType(20, 2)).alias("capital_expenditures"),
    )
    .withColumn("knowledge_date", F.to_date("knowledge_ts"))
    .withColumn("source_record_hash", F.sha2("raw_record", 256))
)
cashflow_valid = (
    F.col("symbol").isNotNull()
    & (F.length(F.col("symbol")) > 0)
    & F.col("event_date").isNotNull()
    & F.col("knowledge_date").isNotNull()
    & (F.col("event_date") <= F.col("knowledge_date"))
    & (F.col("knowledge_date") <= F.current_date())
    & (F.col("operating_cashflow").isNotNull() | F.col("capital_expenditures").isNotNull())
)
cashflow_dq_failures = cashflow_rows.filter(~cashflow_valid)
cashflow_pass = cashflow_rows.filter(cashflow_valid).dropDuplicates([
    "source_id", "batch_id", "symbol", "event_date", "knowledge_ts", "source_record_hash",
])

statement_rows = (
    balance_pass.alias("b")
    .join(
        cashflow_pass.alias("c"),
        (F.col("b.source_id") == F.col("c.source_id"))
        & (F.col("b.batch_id") == F.col("c.batch_id"))
        & (F.col("b.symbol") == F.col("c.symbol"))
        & (F.col("b.event_date") == F.col("c.event_date"))
        & (F.col("b.knowledge_ts") == F.col("c.knowledge_ts")),
        "full_outer",
    )
    .select(
        F.coalesce(F.col("b.source_id"), F.col("c.source_id")).alias("source_id"),
        F.coalesce(F.col("b.profile"), F.col("c.profile")).alias("profile"),
        F.coalesce(F.col("b.batch_id"), F.col("c.batch_id")).alias("batch_id"),
        F.coalesce(F.col("b.ingest_ts"), F.col("c.ingest_ts")).alias("ingest_ts"),
        F.coalesce(F.col("b.fetched_at"), F.col("c.fetched_at")).alias("fetched_at"),
        F.coalesce(F.col("b.symbol"), F.col("c.symbol")).alias("symbol"),
        F.coalesce(F.col("b.event_date"), F.col("c.event_date")).alias("event_date"),
        F.coalesce(F.col("b.knowledge_ts"), F.col("c.knowledge_ts")).alias("knowledge_ts"),
        F.coalesce(F.col("b.knowledge_date"), F.col("c.knowledge_date")).alias("knowledge_date"),
        F.col("b.balance_currency").alias("balance_currency"),
        F.col("c.cashflow_currency").alias("cashflow_currency"),
        F.coalesce(F.col("b.balance_currency"), F.col("c.cashflow_currency")).alias("statement_currency"),
        F.col("b.cash_and_equivalents").alias("cash_and_equivalents"),
        F.col("b.total_debt").alias("total_debt"),
        F.col("c.operating_cashflow").alias("operating_cashflow"),
        F.col("c.capital_expenditures").alias("capital_expenditures"),
        F.col("b.source_record_hash").alias("balance_record_hash"),
        F.col("c.source_record_hash").alias("cashflow_record_hash"),
        F.concat_ws("|", F.col("b.raw_record"), F.col("c.raw_record")).alias("raw_record"),
    )
    .withColumn(
        "statement_currency_mismatch",
        F.col("balance_currency").isNull()
        | F.col("cashflow_currency").isNull()
        | (F.col("balance_currency") != F.col("cashflow_currency")),
    )
    .withColumn(
        "statement_row_key",
        F.sha2(F.concat_ws(
            "|", "source_id", "batch_id", "symbol", F.col("event_date").cast("string"),
            F.col("knowledge_ts").cast("string"),
            F.coalesce(F.col("balance_record_hash"), F.lit("")),
            F.coalesce(F.col("cashflow_record_hash"), F.lit("")),
        ), 256),
    )
)
statement_currency_failures = statement_rows.filter(F.col("statement_currency_mismatch"))
statement_rows_pass = statement_rows.filter(~F.col("statement_currency_mismatch"))

statement_overview_candidates = (
    statement_rows_pass.alias("s")
    .join(
        overview_pass.alias("o"),
        (F.col("s.symbol") == F.col("o.symbol"))
        & (F.col("o.knowledge_ts") <= F.col("s.knowledge_ts")),
        "left",
    )
    .select(
        "s.*",
        F.col("o.knowledge_ts").alias("overview_knowledge_ts"),
        F.col("o.ingest_ts").alias("overview_ingest_ts"),
        F.col("o.batch_id").alias("overview_batch_id"),
        F.col("o.source_record_hash").alias("overview_record_hash"),
        F.coalesce(F.col("s.statement_currency"), F.col("o.currency")).alias("currency"),
        F.col("o.currency").alias("overview_currency"), F.col("o.sector").alias("sector"),
        F.col("o.industry").alias("industry"), F.col("o.market_cap").alias("market_cap"),
        F.col("o.shares_outstanding").alias("shares_outstanding"),
        F.col("o.ebitda").alias("ebitda"), F.col("o.pe_ratio").alias("pe_ratio"),
        F.col("o.peg_ratio").alias("peg_ratio"), F.col("o.ps_ratio").alias("ps_ratio"),
        F.col("o.ev_ebitda").alias("ev_ebitda"),
        F.col("o.gross_profit_ttm").alias("gross_profit_ttm"),
        F.col("o.profit_margin").alias("profit_margin"),
        F.col("o.rev_growth_yoy").alias("rev_growth_yoy"),
    )
)
overview_asof_window = Window.partitionBy("statement_row_key").orderBy(
    F.col("overview_knowledge_ts").desc_nulls_last(),
    F.col("overview_ingest_ts").asc_nulls_last(),
    F.col("overview_batch_id").asc_nulls_last(),
    F.col("overview_record_hash").asc_nulls_last(),
)
statement_with_overview_ranked = (
    statement_overview_candidates
    .withColumn("overview_row_number", F.row_number().over(overview_asof_window))
    .filter(F.col("overview_row_number") == 1)
    .withColumn(
        "overview_currency_mismatch",
        F.col("overview_currency").isNull()
        | (F.col("statement_currency") != F.col("overview_currency")),
    )
    .withColumn("fundamentals_kind", F.lit("STATEMENT"))
    .withColumn("fundamentals_row_key", F.col("statement_row_key"))
    .withColumn(
        "source_record_hash",
        F.sha2(F.concat_ws(
            "|", F.coalesce(F.col("balance_record_hash"), F.lit("")),
            F.coalesce(F.col("cashflow_record_hash"), F.lit("")),
            F.coalesce(F.col("overview_record_hash"), F.lit("")),
        ), 256),
    )
)
statement_overview_currency_failures = statement_with_overview_ranked.filter(
    F.col("overview_currency_mismatch")
)
statement_with_overview = statement_with_overview_ranked.filter(
    ~F.col("overview_currency_mismatch")
)

fundamentals_columns = [
    "source_id", "profile", "batch_id", "ingest_ts", "symbol", "event_date", "knowledge_date",
    "fundamentals_kind", "fundamentals_row_key", "source_record_hash",
    "currency", "sector", "industry", "market_cap", "shares_outstanding", "ebitda",
    "pe_ratio", "peg_ratio",
    "ps_ratio", "ev_ebitda", "gross_profit_ttm", "profit_margin", "rev_growth_yoy",
    "cash_and_equivalents", "total_debt", "operating_cashflow", "capital_expenditures",
]
overview_fundamentals = (
    overview_pass
    .withColumn("fundamentals_kind", F.lit("OVERVIEW_SNAPSHOT"))
    .withColumn("cash_and_equivalents", F.lit(None).cast(DecimalType(20, 2)))
    .withColumn("total_debt", F.lit(None).cast(DecimalType(20, 2)))
    .withColumn("operating_cashflow", F.lit(None).cast(DecimalType(20, 2)))
    .withColumn("capital_expenditures", F.lit(None).cast(DecimalType(20, 2)))
    .select(*fundamentals_columns)
)
fundamentals_candidates = overview_fundamentals.unionByName(
    statement_with_overview.select(*fundamentals_columns)
)
fundamentals_resolved = _resolve_ticker_pit(fundamentals_candidates, "fundamentals_row_key")
fundamentals_unresolved = fundamentals_resolved.filter(
    F.col("security_sk").isNull() | (F.col("security_match_count") != 1)
)
if not fundamentals_unresolved.isEmpty():
    fundamentals_security_key = F.concat_ws(
        ":", "source_id", F.lit("SECURITY_UNRESOLVED"), "fundamentals_row_key", "batch_id",
    )
    fundamentals_security_quarantine = fundamentals_unresolved.select(
        F.sha2(fundamentals_security_key, 256).alias("quarantine_id"),
        fundamentals_security_key.alias("natural_key"),
        "source_id", F.col("symbol").alias("raw_identifier"),
        F.lit("SECURITY_UNRESOLVED").alias("reason"),
        F.concat(F.lit("No unique PIT ticker match for fundamentals; kind="), F.col("fundamentals_kind")).alias("details"),
        "event_date", "knowledge_date", "batch_id", F.col("ingest_ts").alias("quarantined_at"),
    )
    _merge_replay_safe(
        "silver_security_quarantine", fundamentals_security_quarantine,
        [
            "source_id", "raw_identifier", "reason", "details", "event_date",
            "knowledge_date", "batch_id", "quarantined_at",
        ],
    )

fundamentals_pass = (
    fundamentals_resolved.filter(F.col("security_sk").isNotNull() & (F.col("security_match_count") == 1))
    .withColumn(
        "natural_key",
        F.sha2(F.concat_ws(
            "|", "source_id", "symbol", "fundamentals_kind", F.col("event_date").cast("string"),
        ), 256),
    )
    .withColumn(
        "fcf_yield",
        F.when(
            F.col("operating_cashflow").isNotNull()
            & F.col("capital_expenditures").isNotNull()
            & (F.col("market_cap") > 0),
            (
                F.col("operating_cashflow") - F.abs(F.col("capital_expenditures"))
            ) / F.col("market_cap"),
        ).cast(DecimalType(18, 6)),
    )
    .withColumn(
        "net_debt_to_ebitda",
        F.when(
            F.col("ebitda") != 0,
            (F.col("total_debt") - F.col("cash_and_equivalents")) / F.col("ebitda"),
        ).cast(DecimalType(18, 6)),
    )
    .withColumn(
        "fundamentals_revision_hash",
        _revision_hash(
            F.col("security_sk"), F.col("fundamentals_kind"), F.col("event_date"),
            F.col("knowledge_date"), F.col("currency"), F.col("sector"), F.col("industry"),
            F.col("market_cap"), F.col("shares_outstanding"), F.col("ebitda"),
            F.col("pe_ratio"), F.col("peg_ratio"),
            F.col("ps_ratio"), F.col("ev_ebitda"), F.col("gross_profit_ttm"),
            F.col("profit_margin"), F.col("rev_growth_yoy"), F.col("cash_and_equivalents"),
            F.col("total_debt"), F.col("operating_cashflow"), F.col("capital_expenditures"),
            F.col("fcf_yield"), F.col("net_debt_to_ebitda"),
        ),
    )
)
fundamentals_revision_window = Window.partitionBy(
    "natural_key", "fundamentals_revision_hash"
).orderBy(
    F.col("ingest_ts").asc_nulls_last(), F.col("batch_id").asc(), F.col("source_record_hash").asc(),
)
silver_fundamentals_df = (
    fundamentals_pass
    .withColumn("revision_row_number", F.row_number().over(fundamentals_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .select(
        "natural_key", "security_sk", "symbol", "fundamentals_kind", "currency", "sector", "industry",
        "market_cap", "shares_outstanding", "ebitda", "pe_ratio", "peg_ratio",
        "ps_ratio", "ev_ebitda",
        "gross_profit_ttm", "profit_margin", "rev_growth_yoy", "cash_and_equivalents",
        "total_debt", "operating_cashflow", "capital_expenditures", "fcf_yield",
        "net_debt_to_ebitda", "fundamentals_revision_hash", "event_date", "knowledge_date",
        "source_id", "profile", "batch_id", "ingest_ts", "source_record_hash",
        F.current_timestamp().alias("loaded_at"),
    )
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_fundamentals (
        natural_key STRING NOT NULL, security_sk BIGINT NOT NULL, symbol STRING NOT NULL,
        fundamentals_kind STRING NOT NULL,
        currency STRING, sector STRING, industry STRING,
        market_cap DECIMAL(20,2), shares_outstanding DECIMAL(20,4),
        ebitda DECIMAL(20,2), pe_ratio DECIMAL(18,6), peg_ratio DECIMAL(18,6),
        ps_ratio DECIMAL(18,6), ev_ebitda DECIMAL(18,6), gross_profit_ttm DECIMAL(20,2),
        profit_margin DECIMAL(18,6), rev_growth_yoy DECIMAL(18,6),
        cash_and_equivalents DECIMAL(20,2), total_debt DECIMAL(20,2),
        operating_cashflow DECIMAL(20,2), capital_expenditures DECIMAL(20,2),
        fcf_yield DECIMAL(18,6), net_debt_to_ebitda DECIMAL(18,6),
        fundamentals_revision_hash STRING NOT NULL,
        event_date DATE NOT NULL, knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL, profile STRING, batch_id STRING NOT NULL,
        ingest_ts TIMESTAMP NOT NULL, source_record_hash STRING NOT NULL, loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
_ensure_columns("silver_fundamentals", {
    "shares_outstanding": "shares_outstanding DECIMAL(20,4)",
})
_merge_insert_only(
    "silver_fundamentals", silver_fundamentals_df,
    "t.natural_key = s.natural_key AND t.fundamentals_revision_hash = s.fundamentals_revision_hash",
)

fundamentals_parse_errors = (
    _parse_error_rows(overview_parse_failures, "OVERVIEW payload is not a valid object")
    .unionByName(_parse_error_rows(balance_parse_failures, "BALANCE_SHEET payload has no quarterly reports"))
    .unionByName(_parse_error_rows(cashflow_parse_failures, "CASH_FLOW payload has no quarterly reports"))
)
_merge_replay_safe(
    "silver_parse_errors", fundamentals_parse_errors,
    ["source_id", "batch_id", "raw_record", "error_msg", "occurred_at"],
)
fundamentals_dq_quarantine = (
    _dq_quarantine_rows(
        overview_dq_failures, ["symbol", "event_date", "knowledge_date"],
        "INVALID_FUNDAMENTALS_OVERVIEW_SNAPSHOT",
    )
    .unionByName(_dq_quarantine_rows(
        balance_dq_failures, ["symbol", "event_date", "knowledge_date"],
        "INVALID_FUNDAMENTALS_BALANCE_STATEMENT",
    ))
    .unionByName(_dq_quarantine_rows(
        cashflow_dq_failures, ["symbol", "event_date", "knowledge_date"],
        "INVALID_FUNDAMENTALS_CASHFLOW_STATEMENT",
    ))
    .unionByName(_dq_quarantine_rows(
        statement_currency_failures,
        ["symbol", "event_date", "knowledge_date", "balance_currency", "cashflow_currency"],
        "FUNDAMENTALS_STATEMENT_CURRENCY_MISMATCH",
    ))
    .unionByName(_dq_quarantine_rows(
        statement_overview_currency_failures,
        ["symbol", "event_date", "knowledge_date", "statement_currency", "overview_currency"],
        "FUNDAMENTALS_OVERVIEW_CURRENCY_MISMATCH",
    ))
)
_merge_replay_safe(
    "silver_dq_quarantine", fundamentals_dq_quarantine,
    ["source_id", "batch_id", "raw_record", "dq_rule", "quarantined_at"],
)
print("E8 Silver merge complete: silver_fundamentals")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- E8 Silver: Alpha Vantage and Finnhub news ---
news_payload_schema = StructType([
    StructField("feed", ArrayType(StructType([
        StructField("title", StringType()),
        StructField("url", StringType()),
        StructField("source", StringType()),
        StructField("summary", StringType()),
        StructField("time_published", StringType()),
        StructField("overall_sentiment_score", StringType()),
        StructField("ticker_sentiment", ArrayType(MapType(StringType(), StringType()))),
    ])))
])

av_news_source = (
    raw.filter(F.col("function") == "NEWS_SENTIMENT")
    .withColumn("parsed_payload", F.from_json("payload_json", news_payload_schema))
)
av_news_parse_failures = av_news_source.filter(
    F.col("parsed_payload").isNull()
    | F.col("parsed_payload.feed").isNull()
    | (F.size(F.col("parsed_payload.feed")) == 0)
)
av_news_rows = (
    av_news_source.filter(
        F.col("parsed_payload").isNotNull()
        & F.col("parsed_payload.feed").isNotNull()
        & (F.size(F.col("parsed_payload.feed")) > 0)
    )
    .withColumn("article", F.explode("parsed_payload.feed"))
    .withColumn("ticker_sentiment", F.explode_outer("article.ticker_sentiment"))
    .filter(
        (F.upper(F.element_at("ticker_sentiment", "ticker")) == F.upper(F.col("symbol")))
        | F.col("ticker_sentiment").isNull()
    )
    .select(
        "source_id", "profile", "batch_id", "ingest_ts", "fetched_at", "raw_record",
        F.upper(F.trim(F.col("symbol"))).alias("symbol"),
        F.col("article.title").alias("title"),
        F.col("article.summary").alias("summary"),
        F.col("article.url").alias("url"),
        F.col("article.source").alias("source"),
        F.coalesce(
            F.to_timestamp("article.time_published", "yyyyMMdd'T'HHmmss"),
            F.to_timestamp("article.time_published", "yyyyMMdd'T'HHmm"),
        ).alias("published_at"),
        F.coalesce(F.col("fetched_at"), F.col("ingest_ts")).alias("knowledge_ts"),
        F.coalesce(F.element_at("ticker_sentiment", "ticker_sentiment_score"), F.col("article.overall_sentiment_score")).cast(DecimalType(5, 4)).alias("sentiment"),
        F.element_at("ticker_sentiment", "relevance_score").cast(DecimalType(5, 4)).alias("relevance"),
    )
    .withColumn("event_date", F.to_date("published_at"))
    .withColumn("knowledge_date", F.to_date("knowledge_ts"))
    .withColumn("source_record_hash", F.sha2("raw_record", 256))
)

finnhub_article_schema = StructType([
    StructField("datetime", LongType()),
    StructField("headline", StringType()),
    StructField("summary", StringType()),
    StructField("url", StringType()),
    StructField("source", StringType()),
])
finnhub_news_source = (
    raw.filter(F.col("source_id") == "news")
    .withColumn("parsed_article", F.from_json("article_json", finnhub_article_schema))
)
finnhub_news_parse_failures = finnhub_news_source.filter(F.col("parsed_article").isNull())
finnhub_news_rows = (
    finnhub_news_source.filter(F.col("parsed_article").isNotNull())
    .select(
        "source_id", "profile", "batch_id", "ingest_ts", "fetched_at", "raw_record",
        F.col("finnhub_symbol").alias("symbol"),
        F.col("parsed_article.headline").alias("title"),
        F.col("parsed_article.summary").alias("summary"),
        F.col("parsed_article.url").alias("url"),
        F.col("parsed_article.source").alias("source"),
        F.to_timestamp(F.from_unixtime(F.col("parsed_article.datetime"))).alias("published_at"),
        F.coalesce(F.col("ingest_ts"), F.col("fetched_at")).alias("knowledge_ts"),
        F.lit(None).cast(DecimalType(5, 4)).alias("sentiment"),
        F.lit(None).cast(DecimalType(5, 4)).alias("relevance"),
    )
    .withColumn("event_date", F.to_date("published_at"))
    .withColumn("knowledge_date", F.to_date("knowledge_ts"))
    .withColumn("source_record_hash", F.sha2("raw_record", 256))
)
news_rows = av_news_rows.unionByName(finnhub_news_rows, allowMissingColumns=True)
news_valid = (
    F.col("symbol").isNotNull()
    & (F.length(F.trim(F.col("symbol"))) > 0)
    & F.col("url").isNotNull()
    & (F.length(F.trim(F.col("url"))) > 0)
    & F.col("published_at").isNotNull()
    & F.col("event_date").isNotNull()
    & F.col("knowledge_date").isNotNull()
    & (F.col("event_date") <= F.col("knowledge_date"))
    & (F.col("knowledge_date") <= F.current_date())
    & (F.col("sentiment").isNull() | F.col("sentiment").between(F.lit(-1), F.lit(1)))
    & (F.col("relevance").isNull() | F.col("relevance").between(F.lit(0), F.lit(1)))
)
news_dq_failures = news_rows.filter(~news_valid)
news_candidates = (
    news_rows.filter(news_valid)
    .withColumn(
        "news_row_key",
        F.sha2(F.concat_ws(
            "|", "source_id", "batch_id", "symbol", "url",
            F.col("published_at").cast("string"), "source_record_hash",
        ), 256),
    )
)
news_resolved = _resolve_ticker_pit(news_candidates, "news_row_key")
news_unresolved = news_resolved.filter(
    F.col("security_sk").isNull() | (F.col("security_match_count") != 1)
)
if not news_unresolved.isEmpty():
    news_security_key = F.concat_ws(
        ":", "source_id", F.lit("SECURITY_UNRESOLVED"), "news_row_key", "batch_id",
    )
    news_security_quarantine = news_unresolved.select(
        F.sha2(news_security_key, 256).alias("quarantine_id"),
        news_security_key.alias("natural_key"),
        "source_id", F.col("symbol").alias("raw_identifier"),
        F.lit("SECURITY_UNRESOLVED").alias("reason"),
        F.concat(F.lit("No unique PIT ticker match for news; url="), F.col("url")).alias("details"),
        "event_date", "knowledge_date", "batch_id", F.col("ingest_ts").alias("quarantined_at"),
    )
    _merge_replay_safe(
        "silver_security_quarantine", news_security_quarantine,
        [
            "source_id", "raw_identifier", "reason", "details", "event_date",
            "knowledge_date", "batch_id", "quarantined_at",
        ],
    )

news_pass = (
    news_resolved.filter(F.col("security_sk").isNotNull() & (F.col("security_match_count") == 1))
    .withColumn(
        "natural_key",
        F.sha2(F.concat_ws(
            "|", "source_id", "symbol", "url", F.col("published_at").cast("string"),
        ), 256),
    )
    .withColumn("news_sk", _positive_sk("source_id", "symbol", "url", F.col("published_at").cast("string")))
    .withColumn("title_hash", F.sha2(F.concat_ws("|", F.col("title"), F.col("url")), 256))
    .withColumn(
        "news_revision_hash",
        _revision_hash(
            F.col("security_sk"), F.col("published_at"), F.col("knowledge_date"),
            F.col("title"), F.col("summary"), F.col("url"), F.col("source"),
            F.col("sentiment"), F.col("relevance"),
        ),
    )
)
news_revision_window = Window.partitionBy("natural_key", "news_revision_hash").orderBy(
    F.col("ingest_ts").asc_nulls_last(), F.col("batch_id").asc(), F.col("source_record_hash").asc(),
)
silver_news_df = (
    news_pass
    .withColumn("revision_row_number", F.row_number().over(news_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .select(
        "natural_key", "news_sk", "security_sk", "symbol", "published_at", "title", "summary",
        "url", "source", "sentiment", "relevance", "title_hash", "news_revision_hash",
        "event_date", "knowledge_date", "source_id", "profile", "batch_id", "ingest_ts",
        "source_record_hash", F.current_timestamp().alias("loaded_at"),
    )
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_news (
        natural_key STRING NOT NULL, news_sk BIGINT NOT NULL, security_sk BIGINT NOT NULL,
        symbol STRING NOT NULL, published_at TIMESTAMP NOT NULL,
        title STRING, summary STRING, url STRING NOT NULL, source STRING,
        sentiment DECIMAL(5,4), relevance DECIMAL(5,4), title_hash STRING NOT NULL,
        news_revision_hash STRING NOT NULL,
        event_date DATE NOT NULL, knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL, profile STRING, batch_id STRING NOT NULL,
        ingest_ts TIMESTAMP NOT NULL, source_record_hash STRING NOT NULL, loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
_merge_insert_only(
    "silver_news", silver_news_df,
    "t.natural_key = s.natural_key AND t.news_revision_hash = s.news_revision_hash",
)

news_parse_errors = (
    _parse_error_rows(av_news_parse_failures, "NEWS_SENTIMENT payload has no valid feed")
    .unionByName(_parse_error_rows(finnhub_news_parse_failures, "Finnhub article is not a valid object"))
)
_merge_replay_safe(
    "silver_parse_errors", news_parse_errors,
    ["source_id", "batch_id", "raw_record", "error_msg", "occurred_at"],
)
news_dq_quarantine = _dq_quarantine_rows(
    news_dq_failures, ["symbol", "url", "event_date", "knowledge_date"],
    "INVALID_NEWS_PIT_OR_CONTENT",
)
_merge_replay_safe(
    "silver_dq_quarantine", news_dq_quarantine,
    ["source_id", "batch_id", "raw_record", "dq_rule", "quarantined_at"],
)
print("E8 Silver merge complete: silver_news")

# --- E8 Silver: Alpha Vantage institutional holdings ---
holdings_schema = StructType([StructField("data", ArrayType(MapType(StringType(), StringType())))])
holdings_source = (
    raw.filter(F.col("function") == "INSTITUTIONAL_HOLDINGS")
    .withColumn("parsed_payload", F.from_json("payload_json", holdings_schema))
)
holdings_parse_failures = holdings_source.filter(
    F.col("parsed_payload").isNull()
    | F.col("parsed_payload.data").isNull()
    | (F.size(F.col("parsed_payload.data")) == 0)
)
holding_rows = (
    holdings_source.filter(
        F.col("parsed_payload").isNotNull()
        & F.col("parsed_payload.data").isNotNull()
        & (F.size(F.col("parsed_payload.data")) > 0)
    )
    .withColumn("holding", F.explode("parsed_payload.data"))
    .select(
        "source_id", "profile", "batch_id", "ingest_ts", "fetched_at", "raw_record",
        F.upper(F.trim(F.col("symbol"))).alias("symbol"),
        F.trim(F.element_at("holding", "holder")).alias("holder"),
        F.to_date(F.element_at("holding", "date_reported")).alias("event_date"),
        F.coalesce(F.col("fetched_at"), F.col("ingest_ts")).alias("knowledge_ts"),
        F.element_at("holding", "shares").cast(DecimalType(20, 4)).alias("shares"),
        F.element_at("holding", "value").cast(DecimalType(20, 2)).alias("value_usd"),
    )
    .withColumn("knowledge_date", F.to_date("knowledge_ts"))
    .withColumn("source_record_hash", F.sha2("raw_record", 256))
    .withColumn(
        "holder_name_normalized",
        F.trim(F.regexp_replace(F.lower(F.col("holder")), r"[^\p{L}\p{N}]+", " ")),
    )
    .withColumn(
        "entity_natural_id",
        F.concat(
            F.lit("institution_name:"),
            F.sha2(F.col("holder_name_normalized"), 256),
        ),
    )
)
holding_valid = F.coalesce(
    F.col("symbol").isNotNull()
    & (F.length(F.col("symbol")) > 0)
    & F.col("holder").isNotNull()
    & (F.length(F.col("holder")) > 0)
    & (F.length(F.col("holder_name_normalized")) > 0)
    & F.col("event_date").isNotNull()
    & F.col("knowledge_date").isNotNull()
    & (F.col("event_date") <= F.col("knowledge_date"))
    & (F.col("knowledge_date") <= F.current_date())
    & (F.col("shares").isNotNull() | F.col("value_usd").isNotNull())
    & (F.col("shares").isNull() | (F.col("shares") >= 0))
    & (F.col("value_usd").isNull() | (F.col("value_usd") >= 0)),
    F.lit(False),
)
holding_dq_failures = holding_rows.filter(~holding_valid)
holding_candidates = (
    holding_rows.filter(holding_valid)
    .withColumn(
        "holding_row_key",
        F.sha2(F.concat_ws(
            "|", "source_id", "batch_id", "symbol", "entity_natural_id",
            F.col("event_date").cast("string"), "source_record_hash",
        ), 256),
    )
)
holding_resolved = _resolve_ticker_pit(holding_candidates, "holding_row_key")
holding_unresolved = holding_resolved.filter(
    F.col("security_sk").isNull() | (F.col("security_match_count") != 1)
)
if not holding_unresolved.isEmpty():
    holding_security_key = F.concat_ws(
        ":", "source_id", F.lit("SECURITY_UNRESOLVED"), "holding_row_key", "batch_id",
    )
    holding_security_quarantine = holding_unresolved.select(
        F.sha2(holding_security_key, 256).alias("quarantine_id"),
        holding_security_key.alias("natural_key"),
        "source_id", F.col("symbol").alias("raw_identifier"),
        F.lit("SECURITY_UNRESOLVED").alias("reason"),
        F.concat(
            F.lit("No unique PIT ticker match for institutional holding; holder="), F.col("holder"),
        ).alias("details"),
        "event_date", "knowledge_date", "batch_id", F.col("ingest_ts").alias("quarantined_at"),
    )
    _merge_replay_safe(
        "silver_security_quarantine", holding_security_quarantine,
        [
            "source_id", "raw_identifier", "reason", "details", "event_date",
            "knowledge_date", "batch_id", "quarantined_at",
        ],
    )

av_holder_entity_df = (
    holding_candidates
    .groupBy("entity_natural_id")
    .agg(F.min("holder").alias("name"))
    .withColumn("entity_sk", _positive_sk(F.col("entity_natural_id")))
    .withColumn("entity_type", F.lit("institution"))
    .withColumn("role", F.lit(None).cast(StringType()))
    .withColumn("cik", F.lit(None).cast(StringType()))
    .select("entity_sk", "entity_natural_id", "entity_type", "name", "role", "cik")
)
_merge_all("dim_entity", av_holder_entity_df, "t.entity_natural_id = s.entity_natural_id")
av_holder_entity_lookup = spark.table("dim_entity").select("entity_natural_id", "entity_sk")
av_holder_entity_conflicts = (
    av_holder_entity_lookup
    .groupBy("entity_natural_id")
    .agg(F.countDistinct("entity_sk").alias("entity_sk_count"))
    .filter(F.col("entity_sk_count") != 1)
    .count()
)
if av_holder_entity_conflicts:
    raise RuntimeError(
        "Canonical AV holder entity resolution failed: "
        f"conflicting_entity_natural_ids={av_holder_entity_conflicts}"
    )
av_holder_entity_orphans = (
    holding_resolved.select("entity_natural_id").distinct()
    .join(av_holder_entity_lookup, "entity_natural_id", "left_anti")
    .count()
)
if av_holder_entity_orphans:
    raise RuntimeError(
        "Canonical AV holder entity resolution failed: "
        f"unresolved_entity_natural_ids={av_holder_entity_orphans}"
    )

holding_pass = (
    holding_resolved.filter(F.col("security_sk").isNotNull() & (F.col("security_match_count") == 1))
    .join(av_holder_entity_lookup, "entity_natural_id", "inner")
    .withColumn(
        "natural_key",
        F.sha2(F.concat_ws(
            "|", "source_id", "symbol", "entity_natural_id",
            F.col("event_date").cast("string"),
        ), 256),
    )
    .withColumn("shares_delta_qoq", F.lit(None).cast(DecimalType(20, 4)))
    .withColumn("pct_of_portfolio", F.lit(None).cast(DecimalType(9, 6)))
    .withColumn(
        "holding_revision_hash",
        _revision_hash(
            F.col("security_sk"), F.col("entity_sk"), F.col("event_date"),
            F.col("knowledge_date"), F.col("shares"), F.col("value_usd"),
            F.col("shares_delta_qoq"), F.col("pct_of_portfolio"),
        ),
    )
    .withColumn(
        "accession_no",
        F.substring(F.sha2(F.concat_ws("|", "natural_key", "holding_revision_hash"), 256), 1, 25),
    )
)
holding_revision_window = Window.partitionBy("natural_key", "holding_revision_hash").orderBy(
    F.col("ingest_ts").asc_nulls_last(), F.col("batch_id").asc(), F.col("source_record_hash").asc(),
)
silver_av_institutional_holding_df = (
    holding_pass
    .withColumn("revision_row_number", F.row_number().over(holding_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .select(
        "natural_key", "security_sk", "symbol", "entity_sk", "entity_natural_id", "holder",
        "shares", "value_usd",
        "shares_delta_qoq", "pct_of_portfolio", "accession_no", "holding_revision_hash",
        "event_date", "knowledge_date", "source_id", "profile", "batch_id", "ingest_ts",
        "source_record_hash", F.current_timestamp().alias("loaded_at"),
    )
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_av_institutional_holding (
        natural_key STRING NOT NULL, security_sk BIGINT NOT NULL, symbol STRING NOT NULL,
        entity_sk BIGINT NOT NULL, entity_natural_id STRING NOT NULL, holder STRING NOT NULL,
        shares DECIMAL(20,4), value_usd DECIMAL(20,2),
        shares_delta_qoq DECIMAL(20,4), pct_of_portfolio DECIMAL(9,6),
        accession_no STRING NOT NULL, holding_revision_hash STRING NOT NULL,
        event_date DATE NOT NULL, knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL, profile STRING, batch_id STRING NOT NULL,
        ingest_ts TIMESTAMP NOT NULL, source_record_hash STRING NOT NULL, loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
_merge_insert_only(
    "silver_av_institutional_holding", silver_av_institutional_holding_df,
    "t.natural_key = s.natural_key AND t.holding_revision_hash = s.holding_revision_hash",
)
_merge_replay_safe(
    "silver_parse_errors",
    _parse_error_rows(holdings_parse_failures, "INSTITUTIONAL_HOLDINGS payload has no valid data array"),
    ["source_id", "batch_id", "raw_record", "error_msg", "occurred_at"],
)
holding_dq_quarantine = _dq_quarantine_rows(
    holding_dq_failures, ["symbol", "holder", "event_date", "knowledge_date"],
    "INVALID_INSTITUTIONAL_HOLDING",
)
_merge_replay_safe(
    "silver_dq_quarantine", holding_dq_quarantine,
    ["source_id", "batch_id", "raw_record", "dq_rule", "quarantined_at"],
)
print("E8 Silver merge complete: silver_av_institutional_holding")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Gold promotion from E8 Silver: fundamentals, news, and holdings ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_fundamentals (
        security_sk BIGINT NOT NULL, date_sk INT NOT NULL,
        fundamentals_kind STRING,
        currency STRING, sector STRING, industry STRING,
        market_cap DECIMAL(20,2), shares_outstanding DECIMAL(20,4),
        ebitda DECIMAL(20,2), pe_ratio DECIMAL(18,6), peg_ratio DECIMAL(18,6),
        ps_ratio DECIMAL(18,6), ev_ebitda DECIMAL(18,6), gross_profit_ttm DECIMAL(20,2),
        profit_margin DECIMAL(18,6), rev_growth_yoy DECIMAL(18,6),
        cash_and_equivalents DECIMAL(20,2), total_debt DECIMAL(20,2),
        operating_cashflow DECIMAL(20,2), capital_expenditures DECIMAL(20,2),
        fcf_yield DECIMAL(18,6), net_debt_to_ebitda DECIMAL(18,6),
        fundamentals_revision_hash STRING,
        silver_source_table STRING, silver_natural_key STRING, silver_batch_id STRING,
        silver_ingest_ts TIMESTAMP, silver_source_record_hash STRING, silver_loaded_at TIMESTAMP,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")
_ensure_columns("fact_fundamentals", {
    "shares_outstanding": "shares_outstanding DECIMAL(20,4)",
    "fundamentals_kind": "fundamentals_kind STRING",
    "fundamentals_revision_hash": "fundamentals_revision_hash STRING",
    "silver_source_table": "silver_source_table STRING",
    "silver_natural_key": "silver_natural_key STRING",
    "silver_batch_id": "silver_batch_id STRING",
    "silver_ingest_ts": "silver_ingest_ts TIMESTAMP",
    "silver_source_record_hash": "silver_source_record_hash STRING",
    "silver_loaded_at": "silver_loaded_at TIMESTAMP",
})
fundamentals_df = (
    spark.table("silver_fundamentals")
    .join(processed_batch_ids, "batch_id", "inner")
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("source_sk", F.lit(3))
    .select(
        "security_sk", "date_sk", "fundamentals_kind", "currency", "sector", "industry",
        "market_cap", "shares_outstanding", "ebitda", "pe_ratio", "peg_ratio",
        "ps_ratio", "ev_ebitda",
        "gross_profit_ttm", "profit_margin", "rev_growth_yoy", "cash_and_equivalents",
        "total_debt", "operating_cashflow", "capital_expenditures", "fcf_yield",
        "net_debt_to_ebitda", "fundamentals_revision_hash",
        F.lit("silver_fundamentals").alias("silver_source_table"),
        F.col("natural_key").alias("silver_natural_key"),
        F.col("batch_id").alias("silver_batch_id"),
        F.col("ingest_ts").alias("silver_ingest_ts"),
        F.col("source_record_hash").alias("silver_source_record_hash"),
        F.col("loaded_at").alias("silver_loaded_at"),
        "source_sk", "event_date", "knowledge_date",
    )
)
_merge_insert_only(
    "fact_fundamentals", fundamentals_df,
    "t.security_sk = s.security_sk AND t.event_date = s.event_date "
    "AND t.fundamentals_revision_hash = s.fundamentals_revision_hash",
)

legacy_fundamentals = spark.table("fact_fundamentals").filter(
    (F.col("source_sk") == 3) & F.col("silver_natural_key").isNull()
)
if not legacy_fundamentals.isEmpty():
    uncovered_legacy_fundamentals = (
        legacy_fundamentals.alias("g")
        .join(spark.table("silver_fundamentals").alias("s"),
            (F.col("g.security_sk") == F.col("s.security_sk"))
            & (F.col("g.event_date") == F.col("s.event_date"))
            & F.col("g.currency").eqNullSafe(F.col("s.currency"))
            & F.col("g.sector").eqNullSafe(F.col("s.sector"))
            & F.col("g.industry").eqNullSafe(F.col("s.industry"))
            & F.col("g.market_cap").eqNullSafe(F.col("s.market_cap"))
            & F.col("g.shares_outstanding").eqNullSafe(F.col("s.shares_outstanding"))
            & F.col("g.ebitda").eqNullSafe(F.col("s.ebitda"))
            & F.col("g.pe_ratio").eqNullSafe(F.col("s.pe_ratio"))
            & F.col("g.peg_ratio").eqNullSafe(F.col("s.peg_ratio"))
            & F.col("g.ps_ratio").eqNullSafe(F.col("s.ps_ratio"))
            & F.col("g.ev_ebitda").eqNullSafe(F.col("s.ev_ebitda"))
            & F.col("g.gross_profit_ttm").eqNullSafe(F.col("s.gross_profit_ttm"))
            & F.col("g.profit_margin").eqNullSafe(F.col("s.profit_margin"))
            & F.col("g.rev_growth_yoy").eqNullSafe(F.col("s.rev_growth_yoy"))
            & F.col("g.cash_and_equivalents").eqNullSafe(F.col("s.cash_and_equivalents"))
            & F.col("g.total_debt").eqNullSafe(F.col("s.total_debt"))
            & F.col("g.operating_cashflow").eqNullSafe(F.col("s.operating_cashflow"))
            & F.col("g.capital_expenditures").eqNullSafe(F.col("s.capital_expenditures"))
            & F.col("g.fcf_yield").eqNullSafe(F.col("s.fcf_yield"))
            & F.col("g.net_debt_to_ebitda").eqNullSafe(F.col("s.net_debt_to_ebitda")),
            "left_anti",
        )
        .count()
    )
    if uncovered_legacy_fundamentals:
        raise RuntimeError(
            "Legacy fact_fundamentals rows lack rebuilt Silver coverage: "
            f"uncovered_security_count={uncovered_legacy_fundamentals}"
        )
    DeltaTable.forName(spark, "fact_fundamentals").delete(
        "source_sk = 3 AND silver_natural_key IS NULL"
    )

spark.sql("""
    CREATE TABLE IF NOT EXISTS v_fundamentals_latest
    USING DELTA AS
    SELECT * FROM fact_fundamentals WHERE 1 = 0
""")
_ensure_columns("v_fundamentals_latest", {
    "shares_outstanding": "shares_outstanding DECIMAL(20,4)",
    "fundamentals_kind": "fundamentals_kind STRING",
    "fundamentals_revision_hash": "fundamentals_revision_hash STRING",
    "silver_source_table": "silver_source_table STRING",
    "silver_natural_key": "silver_natural_key STRING",
    "silver_batch_id": "silver_batch_id STRING",
    "silver_ingest_ts": "silver_ingest_ts TIMESTAMP",
    "silver_source_record_hash": "silver_source_record_hash STRING",
    "silver_loaded_at": "silver_loaded_at TIMESTAMP",
})
DeltaTable.forName(spark, "v_fundamentals_latest").delete("1 = 1")
latest_fundamentals_order = Window.partitionBy("security_sk").orderBy(
    F.col("knowledge_date").desc(),
    F.col("event_date").desc(),
    F.col("silver_ingest_ts").desc_nulls_last(),
    F.col("silver_batch_id").desc_nulls_last(),
    F.col("fundamentals_kind").asc_nulls_last(),
    F.col("fundamentals_revision_hash").desc(),
)
latest_fundamentals_values = latest_fundamentals_order.rowsBetween(
    Window.unboundedPreceding,
    Window.unboundedFollowing,
)
latest_fundamentals_value_columns = [
    "currency", "sector", "industry", "market_cap", "shares_outstanding", "ebitda",
    "pe_ratio", "peg_ratio",
    "ps_ratio", "ev_ebitda", "gross_profit_ttm", "profit_margin", "rev_growth_yoy",
    "cash_and_equivalents", "total_debt", "operating_cashflow", "capital_expenditures",
    "fcf_yield", "net_debt_to_ebitda",
]
latest_fundamentals_candidates = (
    spark.table("fact_fundamentals")
    .filter(
        (F.col("event_date") <= F.lit(to_date).cast("date"))
        & (F.col("knowledge_date") <= F.lit(to_date).cast("date"))
    )
)
for metric_column in latest_fundamentals_value_columns:
    latest_fundamentals_candidates = latest_fundamentals_candidates.withColumn(
        metric_column,
        F.first(F.col(metric_column), ignorenulls=True).over(latest_fundamentals_values),
    )
latest_fundamentals_df = (
    latest_fundamentals_candidates
    .withColumn("latest_row_number", F.row_number().over(latest_fundamentals_order))
    .filter(F.col("latest_row_number") == 1)
    .drop("latest_row_number")
    .withColumn("fundamentals_kind", F.lit("MERGED_LATEST"))
    .withColumn(
        "fundamentals_revision_hash",
        _revision_hash(
            F.col("security_sk"), F.col("fundamentals_kind"), F.lit(to_date),
            *[F.col(column_name) for column_name in latest_fundamentals_value_columns],
        ),
    )
    .withColumn("silver_natural_key", F.lit(None).cast(StringType()))
    .withColumn("silver_batch_id", F.lit(None).cast(StringType()))
    .withColumn("silver_ingest_ts", F.lit(None).cast("timestamp"))
    .withColumn("silver_source_record_hash", F.lit(None).cast(StringType()))
    .withColumn("silver_loaded_at", F.lit(None).cast("timestamp"))
)
if not latest_fundamentals_df.isEmpty():
    latest_fundamentals_df.write.format("delta").mode("append").saveAsTable("v_fundamentals_latest")

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_company_news (
        news_sk BIGINT NOT NULL, security_sk BIGINT NOT NULL, date_sk INT NOT NULL,
        published_at TIMESTAMP, title STRING, summary STRING, url STRING, source STRING,
        news_revision_hash STRING,
        silver_natural_key STRING, silver_batch_id STRING,
        silver_ingest_ts TIMESTAMP, silver_source_record_hash STRING, silver_loaded_at TIMESTAMP,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")
_ensure_columns("fact_company_news", {
    "published_at": "published_at TIMESTAMP",
    "news_revision_hash": "news_revision_hash STRING",
    "silver_natural_key": "silver_natural_key STRING",
    "silver_batch_id": "silver_batch_id STRING",
    "silver_ingest_ts": "silver_ingest_ts TIMESTAMP",
    "silver_source_record_hash": "silver_source_record_hash STRING",
    "silver_loaded_at": "silver_loaded_at TIMESTAMP",
})
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_news_sentiment (
        news_sk BIGINT, security_sk BIGINT, date_sk INT,
        published_at TIMESTAMP, sentiment DECIMAL(5,4), relevance DECIMAL(5,4),
        title_hash STRING NOT NULL, url STRING,
        news_revision_hash STRING,
        silver_natural_key STRING, silver_batch_id STRING,
        silver_ingest_ts TIMESTAMP, silver_source_record_hash STRING, silver_loaded_at TIMESTAMP,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")
_ensure_columns("fact_news_sentiment", {
    "published_at": "published_at TIMESTAMP",
    "news_revision_hash": "news_revision_hash STRING",
    "silver_natural_key": "silver_natural_key STRING",
    "silver_batch_id": "silver_batch_id STRING",
    "silver_ingest_ts": "silver_ingest_ts TIMESTAMP",
    "silver_source_record_hash": "silver_source_record_hash STRING",
    "silver_loaded_at": "silver_loaded_at TIMESTAMP",
})
news_gold_source = (
    spark.table("silver_news")
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("source_sk", F.when(F.col("source_id") == "news", F.lit(4)).otherwise(F.lit(3)))
    .cache()
)
company_news_df = news_gold_source.select(
    "news_sk", "security_sk", "date_sk", "published_at", "title", "summary", "url", "source",
    "news_revision_hash", F.col("natural_key").alias("silver_natural_key"),
    F.col("batch_id").alias("silver_batch_id"), F.col("ingest_ts").alias("silver_ingest_ts"),
    F.col("source_record_hash").alias("silver_source_record_hash"),
    F.col("loaded_at").alias("silver_loaded_at"), "source_sk", "event_date", "knowledge_date",
)
(
    company_news_df.write.format("delta").mode("overwrite")
    .option("overwriteSchema", "true").saveAsTable("fact_company_news")
)
news_sentiment_df = news_gold_source.select(
    "news_sk", "security_sk", "date_sk", "published_at", "sentiment", "relevance", "title_hash", "url",
    "news_revision_hash", F.col("natural_key").alias("silver_natural_key"),
    F.col("batch_id").alias("silver_batch_id"), F.col("ingest_ts").alias("silver_ingest_ts"),
    F.col("source_record_hash").alias("silver_source_record_hash"),
    F.col("loaded_at").alias("silver_loaded_at"), "source_sk", "event_date", "knowledge_date",
)
(
    news_sentiment_df.write.format("delta").mode("overwrite")
    .option("overwriteSchema", "true").saveAsTable("fact_news_sentiment")
)
news_gold_source.unpersist()

# --- Gold promotion from E8 Silver: macro, FX, and themes ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_macro (
        indicator_code STRING NOT NULL,
        date_sk INT NOT NULL,
        value DECIMAL(20,6) NOT NULL,
        macro_revision_hash STRING,
        source_sk INT,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL
    )
    USING DELTA
""")
_ensure_columns("fact_macro", {
    "macro_revision_hash": "macro_revision_hash STRING",
})
macro_df = (
    spark.table("silver_macro_observation")
    .join(processed_batch_ids, "batch_id", "inner")
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("source_sk", F.lit(3))
    .select(
        "indicator_code", "date_sk", "value", "macro_revision_hash",
        "source_sk", "event_date", "knowledge_date",
    )
)
_merge_insert_only(
    "fact_macro",
    macro_df,
    "t.indicator_code = s.indicator_code AND t.event_date = s.event_date "
    "AND t.macro_revision_hash = s.macro_revision_hash",
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_fx_rate (
        ccy_pair STRING NOT NULL,
        date_sk INT NOT NULL,
        rate DECIMAL(18,8) NOT NULL,
        fx_revision_hash STRING,
        source_sk INT,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL
    )
    USING DELTA
""")
_ensure_columns("fact_fx_rate", {
    "fx_revision_hash": "fx_revision_hash STRING",
})
fx_df = (
    spark.table("silver_fx_rate")
    .join(processed_batch_ids, "batch_id", "inner")
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("source_sk", F.lit(3))
    .select(
        "ccy_pair", "date_sk", "rate", "fx_revision_hash",
        "source_sk", "event_date", "knowledge_date",
    )
)
_merge_insert_only(
    "fact_fx_rate",
    fx_df,
    "t.ccy_pair = s.ccy_pair AND t.event_date = s.event_date "
    "AND t.fx_revision_hash = s.fx_revision_hash",
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Institutional holding Gold promotion from E8 Silver ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_institutional_holding (
        security_sk BIGINT, entity_sk BIGINT, date_sk INT,
        shares DECIMAL(20,4), value_usd DECIMAL(20,2),
        shares_delta_qoq DECIMAL(20,4), pct_of_portfolio DECIMAL(9,6),
        accession_no STRING NOT NULL, holding_revision_hash STRING,
        silver_natural_key STRING, silver_batch_id STRING,
        silver_ingest_ts TIMESTAMP, silver_source_record_hash STRING, silver_loaded_at TIMESTAMP,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")
_ensure_columns("fact_institutional_holding", {
    "holding_revision_hash": "holding_revision_hash STRING",
    "silver_source_table": "silver_source_table STRING",
    "silver_natural_key": "silver_natural_key STRING",
    "silver_batch_id": "silver_batch_id STRING",
    "silver_ingest_ts": "silver_ingest_ts TIMESTAMP",
    "silver_source_record_hash": "silver_source_record_hash STRING",
    "silver_loaded_at": "silver_loaded_at TIMESTAMP",
})
av_institutional_holding_df = (
    spark.table("silver_av_institutional_holding")
    .join(processed_batch_ids, "batch_id", "inner")
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("source_sk", F.lit(3))
    .select(
        "security_sk", "entity_sk", "date_sk", "shares", "value_usd", "shares_delta_qoq",
        "pct_of_portfolio", "accession_no", "holding_revision_hash",
        F.lit("silver_av_institutional_holding").alias("silver_source_table"),
        F.col("natural_key").alias("silver_natural_key"),
        F.col("batch_id").alias("silver_batch_id"),
        F.col("ingest_ts").alias("silver_ingest_ts"),
        F.col("source_record_hash").alias("silver_source_record_hash"),
        F.col("loaded_at").alias("silver_loaded_at"),
        "source_sk", "event_date", "knowledge_date",
    )
)
_merge_insert_only(
    "fact_institutional_holding", av_institutional_holding_df,
    "t.security_sk = s.security_sk AND t.entity_sk = s.entity_sk "
    "AND t.event_date = s.event_date AND t.holding_revision_hash = s.holding_revision_hash",
)

theme_df = (
    spark.table("silver_theme_membership")
    .join(processed_batch_ids, "batch_id", "inner")
    .withColumn("source_sk", F.lit(5))
    .select(
        "theme_id", "etf_symbol", "security_sk", "weight", "is_ground_truth",
        "theme_revision_hash", F.col("batch_id").alias("snapshot_batch_id"),
        F.col("ingest_ts").alias("snapshot_ingest_ts"),
        "source_sk", "event_date", "knowledge_date",
    )
)
(
    theme_df.write.format("delta").mode("overwrite")
    .option("overwriteSchema", "true").saveAsTable("fact_theme_membership")
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Extend the conformed calendar for every E8 fact date produced here ---
e8_fact_tables = [
    "fact_fundamentals", "fact_company_news", "fact_news_sentiment", "fact_macro",
    "fact_fx_rate", "fact_institutional_holding", "fact_theme_membership",
]
e8_date_keyed_fact_tables = [
    "fact_fundamentals", "fact_company_news", "fact_news_sentiment", "fact_macro",
    "fact_fx_rate", "fact_institutional_holding",
]
e8_date_candidates = None
for fact_table in e8_fact_tables:
    fact_dates = spark.table(fact_table).select(
        F.col("event_date").alias("cal_date")
    ).unionByName(
        spark.table(fact_table).select(F.col("knowledge_date").alias("cal_date"))
    )
    e8_date_candidates = (
        fact_dates if e8_date_candidates is None
        else e8_date_candidates.unionByName(fact_dates)
    )

e8_trading_dates = (
    spark.table("silver_prices")
    .select(F.col("date").alias("cal_date"))
    .filter(F.col("cal_date").isNotNull())
    .distinct()
    .withColumn("is_trading_day", F.lit(True))
)
e8_date_df = (
    e8_date_candidates.filter(F.col("cal_date").isNotNull()).distinct()
    .join(e8_trading_dates, "cal_date", "left")
    .withColumn("date_sk", _date_sk("cal_date"))
    .withColumn("year", F.year("cal_date"))
    .withColumn("quarter", F.quarter("cal_date"))
    .withColumn("month", F.month("cal_date"))
    .withColumn("day", F.dayofmonth("cal_date"))
    .withColumn("is_trading_day", F.coalesce(F.col("is_trading_day"), F.lit(False)))
    .withColumn(
        "fiscal_quarter",
        F.concat(
            F.year("cal_date").cast("string"), F.lit("Q"),
            F.quarter("cal_date").cast("string"),
        ),
    )
    .select(
        "date_sk", "cal_date", "year", "quarter", "month", "day",
        "is_trading_day", "fiscal_quarter",
    )
)
_merge_all("dim_date", e8_date_df, "t.date_sk = s.date_sk")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Validation summary ---
updated_tables = e8_fact_tables
print(f"E8 Alpha Vantage tables updated: {', '.join(updated_tables)}")

missing_pit = spark.sql("""
    SELECT SUM(n) AS n
    FROM (
        SELECT COUNT(*) AS n FROM fact_fundamentals WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
        UNION ALL SELECT COUNT(*) AS n FROM fact_company_news WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
        UNION ALL SELECT COUNT(*) AS n FROM fact_news_sentiment WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
        UNION ALL SELECT COUNT(*) AS n FROM fact_macro WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
        UNION ALL SELECT COUNT(*) AS n FROM fact_fx_rate WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
        UNION ALL SELECT COUNT(*) AS n FROM fact_institutional_holding
            WHERE source_sk = 3 AND (event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date)
        UNION ALL SELECT COUNT(*) AS n FROM fact_theme_membership WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
    ) x
""").collect()[0].n

silver_invalid = spark.sql("""
    SELECT SUM(n) AS n
    FROM (
          SELECT COUNT(*) AS n FROM silver_fundamentals
          WHERE security_sk IS NULL OR event_date IS NULL OR knowledge_date IS NULL
              OR event_date > knowledge_date OR natural_key IS NULL
              OR fundamentals_revision_hash IS NULL OR source_record_hash IS NULL
          UNION ALL SELECT COUNT(*) AS n FROM silver_news
          WHERE security_sk IS NULL OR event_date IS NULL OR knowledge_date IS NULL
              OR event_date > knowledge_date OR natural_key IS NULL
              OR news_revision_hash IS NULL OR source_record_hash IS NULL OR published_at IS NULL
          UNION ALL SELECT COUNT(*) AS n FROM silver_av_institutional_holding
          WHERE security_sk IS NULL OR entity_sk IS NULL OR entity_natural_id IS NULL
              OR event_date IS NULL OR knowledge_date IS NULL
              OR event_date > knowledge_date OR natural_key IS NULL
              OR holding_revision_hash IS NULL OR source_record_hash IS NULL
          UNION ALL SELECT COUNT(*) AS n FROM silver_macro_observation
        WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
           OR macro_revision_hash IS NULL OR value IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM silver_fx_rate
        WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
           OR fx_revision_hash IS NULL OR rate IS NULL OR rate <= 0
        UNION ALL SELECT COUNT(*) AS n FROM silver_theme_membership
        WHERE security_sk IS NULL OR event_date IS NULL OR knowledge_date IS NULL
           OR event_date > knowledge_date OR theme_revision_hash IS NULL
           OR weight IS NULL OR weight < 0 OR weight > 100
    ) x
""").collect()[0].n

silver_duplicate_revisions = spark.sql("""
    SELECT SUM(n) AS n
    FROM (
        SELECT COUNT(*) AS n FROM (
            SELECT natural_key, fundamentals_revision_hash
            FROM silver_fundamentals
            GROUP BY natural_key, fundamentals_revision_hash
            HAVING COUNT(*) > 1
        ) fundamentals_dupes
        UNION ALL SELECT COUNT(*) AS n FROM (
            SELECT natural_key, news_revision_hash
            FROM silver_news
            GROUP BY natural_key, news_revision_hash
            HAVING COUNT(*) > 1
        ) news_dupes
        UNION ALL SELECT COUNT(*) AS n FROM (
            SELECT natural_key, holding_revision_hash
            FROM silver_av_institutional_holding
            GROUP BY natural_key, holding_revision_hash
            HAVING COUNT(*) > 1
        ) holding_dupes
        UNION ALL SELECT COUNT(*) AS n FROM (
            SELECT indicator_code, event_date, macro_revision_hash
            FROM silver_macro_observation
            GROUP BY indicator_code, event_date, macro_revision_hash
            HAVING COUNT(*) > 1
        ) macro_dupes
        UNION ALL SELECT COUNT(*) AS n FROM (
            SELECT ccy_pair, event_date, fx_revision_hash
            FROM silver_fx_rate
            GROUP BY ccy_pair, event_date, fx_revision_hash
            HAVING COUNT(*) > 1
        ) fx_dupes
        UNION ALL SELECT COUNT(*) AS n FROM (
            SELECT batch_id, theme_id, security_sk, event_date, theme_revision_hash
            FROM silver_theme_membership
            GROUP BY batch_id, theme_id, security_sk, event_date, theme_revision_hash
            HAVING COUNT(*) > 1
        ) theme_dupes
    ) x
""").collect()[0].n

security_orphans = (
    spark.table("silver_fundamentals").select("security_sk")
    .unionByName(spark.table("silver_news").select("security_sk"))
    .unionByName(spark.table("silver_av_institutional_holding").select("security_sk"))
    .unionByName(spark.table("silver_theme_membership").select("security_sk"))
    .distinct()
    .join(spark.table("dim_security").select("security_sk").distinct(), "security_sk", "left_anti")
    .count()
)

av_holding_entity_orphans = (
    spark.table("silver_av_institutional_holding")
    .select("entity_sk", "entity_natural_id")
    .distinct()
    .join(
        spark.table("dim_entity").select("entity_sk", "entity_natural_id").distinct(),
        ["entity_sk", "entity_natural_id"],
        "left_anti",
    )
    .count()
)

gold_missing_revision_hash = spark.sql("""
    SELECT SUM(n) AS n
    FROM (
        SELECT COUNT(*) AS n FROM fact_fundamentals WHERE fundamentals_revision_hash IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_company_news WHERE news_revision_hash IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_news_sentiment WHERE news_revision_hash IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_institutional_holding
            WHERE source_sk = 3 AND holding_revision_hash IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_macro WHERE macro_revision_hash IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_fx_rate WHERE fx_revision_hash IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_theme_membership
        WHERE theme_revision_hash IS NULL OR snapshot_batch_id IS NULL OR snapshot_ingest_ts IS NULL
    ) x
""").collect()[0].n

gold_duplicate_revisions = spark.sql("""
    SELECT SUM(n) AS n
    FROM (
        SELECT COUNT(*) AS n FROM (
            SELECT security_sk, event_date, fundamentals_revision_hash
            FROM fact_fundamentals
            GROUP BY security_sk, event_date, fundamentals_revision_hash
            HAVING COUNT(*) > 1
        ) fundamentals_dupes
        UNION ALL SELECT COUNT(*) AS n FROM (
            SELECT news_sk, security_sk, news_revision_hash
            FROM fact_company_news
            GROUP BY news_sk, security_sk, news_revision_hash
            HAVING COUNT(*) > 1
        ) company_news_dupes
        UNION ALL SELECT COUNT(*) AS n FROM (
            SELECT news_sk, security_sk, news_revision_hash
            FROM fact_news_sentiment
            GROUP BY news_sk, security_sk, news_revision_hash
            HAVING COUNT(*) > 1
        ) news_sentiment_dupes
        UNION ALL SELECT COUNT(*) AS n FROM (
            SELECT security_sk, entity_sk, event_date, holding_revision_hash
            FROM fact_institutional_holding
            WHERE source_sk = 3
            GROUP BY security_sk, entity_sk, event_date, holding_revision_hash
            HAVING COUNT(*) > 1
        ) holding_dupes
        UNION ALL SELECT COUNT(*) AS n FROM (
            SELECT indicator_code, event_date, macro_revision_hash
            FROM fact_macro
            GROUP BY indicator_code, event_date, macro_revision_hash
            HAVING COUNT(*) > 1
        ) macro_dupes
        UNION ALL SELECT COUNT(*) AS n FROM (
            SELECT ccy_pair, event_date, fx_revision_hash
            FROM fact_fx_rate
            GROUP BY ccy_pair, event_date, fx_revision_hash
            HAVING COUNT(*) > 1
        ) fx_dupes
        UNION ALL SELECT COUNT(*) AS n FROM (
            SELECT snapshot_batch_id, theme_id, security_sk, event_date, theme_revision_hash
            FROM fact_theme_membership
            GROUP BY snapshot_batch_id, theme_id, security_sk, event_date, theme_revision_hash
            HAVING COUNT(*) > 1
        ) theme_dupes
    ) x
""").collect()[0].n

gold_fundamentals_without_silver = (
    spark.table("fact_fundamentals").alias("g")
    .join(
        spark.table("silver_fundamentals").alias("s"),
        (F.col("g.silver_natural_key") == F.col("s.natural_key"))
        & (F.col("g.fundamentals_revision_hash") == F.col("s.fundamentals_revision_hash"))
        & (F.col("g.silver_source_table") == F.lit("silver_fundamentals"))
        & (F.col("g.security_sk") == F.col("s.security_sk"))
        & (F.col("g.date_sk") == F.date_format(F.col("s.event_date"), "yyyyMMdd").cast("int"))
        & (F.col("g.fundamentals_kind") == F.col("s.fundamentals_kind"))
        & F.col("g.currency").eqNullSafe(F.col("s.currency"))
        & F.col("g.sector").eqNullSafe(F.col("s.sector"))
        & F.col("g.industry").eqNullSafe(F.col("s.industry"))
        & F.col("g.market_cap").eqNullSafe(F.col("s.market_cap"))
        & F.col("g.shares_outstanding").eqNullSafe(F.col("s.shares_outstanding"))
        & F.col("g.ebitda").eqNullSafe(F.col("s.ebitda"))
        & F.col("g.pe_ratio").eqNullSafe(F.col("s.pe_ratio"))
        & F.col("g.peg_ratio").eqNullSafe(F.col("s.peg_ratio"))
        & F.col("g.ps_ratio").eqNullSafe(F.col("s.ps_ratio"))
        & F.col("g.ev_ebitda").eqNullSafe(F.col("s.ev_ebitda"))
        & F.col("g.gross_profit_ttm").eqNullSafe(F.col("s.gross_profit_ttm"))
        & F.col("g.profit_margin").eqNullSafe(F.col("s.profit_margin"))
        & F.col("g.rev_growth_yoy").eqNullSafe(F.col("s.rev_growth_yoy"))
        & F.col("g.cash_and_equivalents").eqNullSafe(F.col("s.cash_and_equivalents"))
        & F.col("g.total_debt").eqNullSafe(F.col("s.total_debt"))
        & F.col("g.operating_cashflow").eqNullSafe(F.col("s.operating_cashflow"))
        & F.col("g.capital_expenditures").eqNullSafe(F.col("s.capital_expenditures"))
        & F.col("g.fcf_yield").eqNullSafe(F.col("s.fcf_yield"))
        & F.col("g.net_debt_to_ebitda").eqNullSafe(F.col("s.net_debt_to_ebitda"))
        & (F.col("g.source_sk") == F.lit(3))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date"))
        & (F.col("g.silver_batch_id") == F.col("s.batch_id"))
        & (F.col("g.silver_ingest_ts") == F.col("s.ingest_ts"))
        & (F.col("g.silver_source_record_hash") == F.col("s.source_record_hash"))
        & (F.col("g.silver_loaded_at") == F.col("s.loaded_at")),
        "left_anti",
    )
    .count()
)
gold_company_news_without_silver = (
    spark.table("fact_company_news").alias("g")
    .join(
        spark.table("silver_news").alias("s"),
        (F.col("g.silver_natural_key") == F.col("s.natural_key"))
        & (F.col("g.news_revision_hash") == F.col("s.news_revision_hash"))
        & (F.col("g.news_sk") == F.col("s.news_sk"))
        & (F.col("g.security_sk") == F.col("s.security_sk"))
        & (F.col("g.date_sk") == F.date_format(F.col("s.event_date"), "yyyyMMdd").cast("int"))
        & F.col("g.published_at").eqNullSafe(F.col("s.published_at"))
        & F.col("g.title").eqNullSafe(F.col("s.title"))
        & F.col("g.summary").eqNullSafe(F.col("s.summary"))
        & F.col("g.url").eqNullSafe(F.col("s.url"))
        & F.col("g.source").eqNullSafe(F.col("s.source"))
        & (F.col("g.source_sk") == F.when(F.col("s.source_id") == "news", F.lit(4)).otherwise(F.lit(3)))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date"))
        & (F.col("g.silver_batch_id") == F.col("s.batch_id"))
        & (F.col("g.silver_ingest_ts") == F.col("s.ingest_ts"))
        & (F.col("g.silver_source_record_hash") == F.col("s.source_record_hash"))
        & (F.col("g.silver_loaded_at") == F.col("s.loaded_at")),
        "left_anti",
    )
    .count()
)
gold_news_sentiment_without_silver = (
    spark.table("fact_news_sentiment").alias("g")
    .join(
        spark.table("silver_news").alias("s"),
        (F.col("g.silver_natural_key") == F.col("s.natural_key"))
        & (F.col("g.news_revision_hash") == F.col("s.news_revision_hash"))
        & (F.col("g.news_sk") == F.col("s.news_sk"))
        & (F.col("g.security_sk") == F.col("s.security_sk"))
        & (F.col("g.date_sk") == F.date_format(F.col("s.event_date"), "yyyyMMdd").cast("int"))
        & F.col("g.published_at").eqNullSafe(F.col("s.published_at"))
        & F.col("g.sentiment").eqNullSafe(F.col("s.sentiment"))
        & F.col("g.relevance").eqNullSafe(F.col("s.relevance"))
        & F.col("g.title_hash").eqNullSafe(F.col("s.title_hash"))
        & F.col("g.url").eqNullSafe(F.col("s.url"))
        & (F.col("g.source_sk") == F.when(F.col("s.source_id") == "news", F.lit(4)).otherwise(F.lit(3)))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date"))
        & (F.col("g.silver_batch_id") == F.col("s.batch_id"))
        & (F.col("g.silver_ingest_ts") == F.col("s.ingest_ts"))
        & (F.col("g.silver_source_record_hash") == F.col("s.source_record_hash"))
        & (F.col("g.silver_loaded_at") == F.col("s.loaded_at")),
        "left_anti",
    )
    .count()
)
gold_av_holding_without_silver = (
    spark.table("fact_institutional_holding").filter(F.col("source_sk") == 3).alias("g")
    .join(
        spark.table("silver_av_institutional_holding").alias("s"),
        (F.col("g.silver_natural_key") == F.col("s.natural_key"))
        & (F.col("g.holding_revision_hash") == F.col("s.holding_revision_hash"))
        & (F.col("g.silver_source_table") == F.lit("silver_av_institutional_holding"))
        & (F.col("g.security_sk") == F.col("s.security_sk"))
        & (F.col("g.entity_sk") == F.col("s.entity_sk"))
        & (F.col("g.date_sk") == F.date_format(F.col("s.event_date"), "yyyyMMdd").cast("int"))
        & F.col("g.shares").eqNullSafe(F.col("s.shares"))
        & F.col("g.value_usd").eqNullSafe(F.col("s.value_usd"))
        & F.col("g.shares_delta_qoq").eqNullSafe(F.col("s.shares_delta_qoq"))
        & F.col("g.pct_of_portfolio").eqNullSafe(F.col("s.pct_of_portfolio"))
        & (F.col("g.accession_no") == F.col("s.accession_no"))
        & (F.col("g.source_sk") == F.lit(3))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date"))
        & (F.col("g.silver_batch_id") == F.col("s.batch_id"))
        & (F.col("g.silver_ingest_ts") == F.col("s.ingest_ts"))
        & (F.col("g.silver_source_record_hash") == F.col("s.source_record_hash"))
        & (F.col("g.silver_loaded_at") == F.col("s.loaded_at")),
        "left_anti",
    )
    .count()
)

gold_macro_without_silver = (
    spark.table("fact_macro").alias("g")
    .join(
        spark.table("silver_macro_observation").alias("s"),
        (F.col("g.indicator_code") == F.col("s.indicator_code"))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.macro_revision_hash") == F.col("s.macro_revision_hash"))
        & (F.col("g.date_sk") == F.date_format(F.col("s.event_date"), "yyyyMMdd").cast("int"))
        & F.col("g.value").eqNullSafe(F.col("s.value"))
        & (F.col("g.source_sk") == F.lit(3))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date")),
        "left_anti",
    )
    .count()
)
gold_fx_without_silver = (
    spark.table("fact_fx_rate").alias("g")
    .join(
        spark.table("silver_fx_rate").alias("s"),
        (F.col("g.ccy_pair") == F.col("s.ccy_pair"))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.fx_revision_hash") == F.col("s.fx_revision_hash"))
        & (F.col("g.date_sk") == F.date_format(F.col("s.event_date"), "yyyyMMdd").cast("int"))
        & F.col("g.rate").eqNullSafe(F.col("s.rate"))
        & (F.col("g.source_sk") == F.lit(3))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date")),
        "left_anti",
    )
    .count()
)
gold_theme_without_silver = (
    spark.table("fact_theme_membership").alias("g")
    .join(
        spark.table("silver_theme_membership").alias("s"),
        (F.col("g.theme_id") == F.col("s.theme_id"))
        & (F.col("g.snapshot_batch_id") == F.col("s.batch_id"))
        & (F.col("g.security_sk") == F.col("s.security_sk"))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.theme_revision_hash") == F.col("s.theme_revision_hash"))
        & (F.col("g.etf_symbol") == F.col("s.etf_symbol"))
        & F.col("g.weight").eqNullSafe(F.col("s.weight"))
        & F.col("g.is_ground_truth").eqNullSafe(F.col("s.is_ground_truth"))
        & (F.col("g.source_sk") == F.lit(5))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date")),
        "left_anti",
    )
    .count()
)
gold_without_silver = (
    gold_fundamentals_without_silver
    + gold_company_news_without_silver
    + gold_news_sentiment_without_silver
    + gold_av_holding_without_silver
    + gold_macro_without_silver
    + gold_fx_without_silver
    + gold_theme_without_silver
)
silver_fundamentals_without_gold = (
    spark.table("silver_fundamentals").alias("s")
    .join(
        spark.table("fact_fundamentals").alias("g"),
        (F.col("s.natural_key") == F.col("g.silver_natural_key"))
        & (F.col("s.fundamentals_revision_hash") == F.col("g.fundamentals_revision_hash"))
        & (F.col("g.silver_source_table") == F.lit("silver_fundamentals")),
        "left_anti",
    ).count()
)
silver_company_news_without_gold = (
    spark.table("silver_news").alias("s")
    .join(
        spark.table("fact_company_news").alias("g"),
        (F.col("s.natural_key") == F.col("g.silver_natural_key"))
        & (F.col("s.news_revision_hash") == F.col("g.news_revision_hash")),
        "left_anti",
    ).count()
)
silver_news_sentiment_without_gold = (
    spark.table("silver_news").alias("s")
    .join(
        spark.table("fact_news_sentiment").alias("g"),
        (F.col("s.natural_key") == F.col("g.silver_natural_key"))
        & (F.col("s.news_revision_hash") == F.col("g.news_revision_hash")),
        "left_anti",
    ).count()
)
silver_av_holding_without_gold = (
    spark.table("silver_av_institutional_holding").alias("s")
    .join(
        spark.table("fact_institutional_holding").filter(F.col("source_sk") == 3).alias("g"),
        (F.col("s.natural_key") == F.col("g.silver_natural_key"))
        & (F.col("s.holding_revision_hash") == F.col("g.holding_revision_hash"))
        & (F.col("g.silver_source_table") == F.lit("silver_av_institutional_holding")),
        "left_anti",
    ).count()
)
silver_macro_without_gold = (
    spark.table("silver_macro_observation").alias("s")
    .join(
        spark.table("fact_macro").alias("g"),
        (F.col("s.indicator_code") == F.col("g.indicator_code"))
        & (F.col("s.event_date") == F.col("g.event_date"))
        & (F.col("s.macro_revision_hash") == F.col("g.macro_revision_hash")),
        "left_anti",
    ).count()
)
silver_fx_without_gold = (
    spark.table("silver_fx_rate").alias("s")
    .join(
        spark.table("fact_fx_rate").alias("g"),
        (F.col("s.ccy_pair") == F.col("g.ccy_pair"))
        & (F.col("s.event_date") == F.col("g.event_date"))
        & (F.col("s.fx_revision_hash") == F.col("g.fx_revision_hash")),
        "left_anti",
    ).count()
)
silver_theme_without_gold = (
    spark.table("silver_theme_membership").alias("s")
    .join(
        spark.table("fact_theme_membership").alias("g"),
        (F.col("s.theme_id") == F.col("g.theme_id"))
        & (F.col("s.batch_id") == F.col("g.snapshot_batch_id"))
        & (F.col("s.security_sk") == F.col("g.security_sk"))
        & (F.col("s.event_date") == F.col("g.event_date"))
        & (F.col("s.theme_revision_hash") == F.col("g.theme_revision_hash")),
        "left_anti",
    ).count()
)
silver_without_gold = (
    silver_fundamentals_without_gold
    + silver_company_news_without_gold
    + silver_news_sentiment_without_gold
    + silver_av_holding_without_gold
    + silver_macro_without_gold
    + silver_fx_without_gold
    + silver_theme_without_gold
)
date_orphans = 0
for fact_table in e8_date_keyed_fact_tables:
    date_orphans += (
        spark.table(fact_table).alias("f")
        .join(
            spark.table("dim_date").alias("d"),
            F.col("f.date_sk") == F.col("d.date_sk"),
            "left_anti",
        )
        .filter(F.col("f.date_sk").isNotNull())
        .count()
    )

print(
    "E8 Alpha Vantage validation: "
    f"missing_pit={missing_pit}, silver_invalid={silver_invalid}, "
    f"silver_duplicate_revisions={silver_duplicate_revisions}, "
    f"security_orphans={security_orphans}, "
    f"av_holding_entity_orphans={av_holding_entity_orphans}, "
    f"gold_missing_revision_hash={gold_missing_revision_hash}, "
    f"gold_duplicate_revisions={gold_duplicate_revisions}, "
    f"gold_fundamentals_without_silver={gold_fundamentals_without_silver}, "
    f"gold_company_news_without_silver={gold_company_news_without_silver}, "
    f"gold_news_sentiment_without_silver={gold_news_sentiment_without_silver}, "
    f"gold_av_holding_without_silver={gold_av_holding_without_silver}, "
    f"gold_without_silver={gold_without_silver}, "
    f"silver_without_gold={silver_without_gold}, "
    f"date_orphans={date_orphans}"
)
if any([
    missing_pit,
    silver_invalid,
    silver_duplicate_revisions,
    security_orphans,
    av_holding_entity_orphans,
    gold_missing_revision_hash,
    gold_duplicate_revisions,
    gold_without_silver,
    silver_without_gold,
    date_orphans,
]):
    raise RuntimeError(
        "E8 Alpha Vantage validation failed: "
        f"missing_pit={missing_pit}, silver_invalid={silver_invalid}, "
        f"silver_duplicate_revisions={silver_duplicate_revisions}, "
        f"security_orphans={security_orphans}, "
        f"av_holding_entity_orphans={av_holding_entity_orphans}, "
        f"gold_missing_revision_hash={gold_missing_revision_hash}, "
        f"gold_duplicate_revisions={gold_duplicate_revisions}, "
        f"gold_without_silver={gold_without_silver}, "
        f"silver_without_gold={silver_without_gold}, "
        f"date_orphans={date_orphans}"
    )
raw.unpersist()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
