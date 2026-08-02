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

# Fabric Notebook: nb_02_prices_to_silver
# Reads bronze prices_eod NDJSON and writes entity-resolved silver_prices.
# Attaches to: auspex_bronze (default lakehouse)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from datetime import date, timedelta
from delta.tables import DeltaTable
import json
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql.types import DateType, DecimalType, LongType, StringType, StructField, StructType, TimestampType

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
def _require_table(table_name: str) -> None:
    if not spark.catalog.tableExists(table_name):
        raise RuntimeError(f"Required upstream table is missing: {table_name}")


def _ensure_columns(table_name: str, column_specs: dict[str, str]) -> None:
    existing = set(spark.table(table_name).columns)
    for column_name, ddl in column_specs.items():
        if column_name not in existing:
            spark.sql(f"ALTER TABLE {table_name} ADD COLUMNS ({ddl})")


def _price_revision_hash(date_column: str):
    return F.sha2(
        F.to_json(F.struct(
            F.col("symbol").alias("symbol"),
            F.date_format(F.col(date_column), "yyyy-MM-dd").alias("date"),
            F.col("open").cast(DecimalType(18, 6)).alias("open"),
            F.col("high").cast(DecimalType(18, 6)).alias("high"),
            F.col("low").cast(DecimalType(18, 6)).alias("low"),
            F.col("close").cast(DecimalType(18, 6)).alias("close"),
            F.col("adj_close").cast(DecimalType(18, 6)).alias("adj_close"),
            F.col("volume").cast(LongType()).alias("volume"),
        )),
        256,
    )


def _ensure_not_null_constraints(table_name: str, column_names: list[str]) -> None:
    properties = {
        row.key for row in spark.sql(f"SHOW TBLPROPERTIES {table_name}").collect()
    }
    for column_name in column_names:
        constraint_name = f"{table_name}_{column_name}_not_null"
        property_name = f"delta.constraints.{constraint_name}"
        if property_name not in properties:
            spark.sql(
                f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} "
                f"CHECK ({column_name} IS NOT NULL)"
            )


for required in ["dim_security", "silver_dq_quarantine", "silver_security_quarantine"]:
    _require_table(required)


def _merge_dq_quarantine(q_df) -> None:
    if q_df.isEmpty():
        return
    q_df = q_df.dropDuplicates(["natural_key"])
    target = DeltaTable.forName(spark, "silver_dq_quarantine")
    (
        target
        .alias("t")
        .merge(q_df.alias("s"), "t.natural_key = s.natural_key")
        .whenMatchedUpdate(set={
            "source_id": "s.source_id",
            "batch_id": "s.batch_id",
            "raw_record": "s.raw_record",
            "dq_rule": "s.dq_rule",
            "quarantined_at": "s.quarantined_at",
        })
        .whenNotMatchedInsertAll()
        .execute()
    )
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    print(f"Merged source_rows={metrics.get('numSourceRows', 'unknown')} into silver_dq_quarantine")


def _merge_security_quarantine(q_df) -> None:
    if q_df.isEmpty():
        return
    q_df = q_df.dropDuplicates(["natural_key"])
    target = DeltaTable.forName(spark, "silver_security_quarantine")
    (
        target
        .alias("t")
        .merge(q_df.alias("s"), "t.natural_key = s.natural_key")
        .whenMatchedUpdate(set={
            "source_id": "s.source_id",
            "raw_identifier": "s.raw_identifier",
            "reason": "s.reason",
            "details": "s.details",
            "event_date": "s.event_date",
            "knowledge_date": "s.knowledge_date",
            "batch_id": "s.batch_id",
            "quarantined_at": "s.quarantined_at",
        })
        .whenNotMatchedInsertAll()
        .execute()
    )
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    print(f"Merged source_rows={metrics.get('numSourceRows', 'unknown')} into silver_security_quarantine")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Bronze files for the landing-date window ---
_PATH_PATTERN = r"/bronze/prices_eod/(\d{4})/(\d{2})/(\d{2})/"
bronze_files = (
    spark.read.format("binaryFile")
    .option("recursiveFileLookup", "true")
    .option("pathGlobFilter", "*.ndjson")
    .load("Files/bronze/prices_eod")
    .select("path")
    .withColumn("folder_year", F.regexp_extract("path", _PATH_PATTERN, 1))
    .withColumn("folder_month", F.regexp_extract("path", _PATH_PATTERN, 2))
    .withColumn("folder_day", F.regexp_extract("path", _PATH_PATTERN, 3))
    .withColumn(
        "folder_date",
        F.to_date(F.concat_ws("-", "folder_year", "folder_month", "folder_day")),
    )
    .filter(F.col("folder_date").between(F.lit(from_date), F.lit(to_date)))
)
paths = [row.path for row in bronze_files.select("path").collect()]
print(f"Bronze files in landing-date window: {len(paths)}")
if not paths:
    raise RuntimeError("No bronze files found in window - check connector ran and lakehouse is attached")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Read bronze and retain one first-known row per distinct payload revision ---
bronze_revisions = (
    spark.read.json(paths)
    .select(
        F.upper(F.col("record.symbol")).alias("symbol"),
        F.to_date(F.col("record.date")).alias("price_date"),
        F.col("record.open").cast(DecimalType(18, 6)).alias("open"),
        F.col("record.high").cast(DecimalType(18, 6)).alias("high"),
        F.col("record.low").cast(DecimalType(18, 6)).alias("low"),
        F.col("record.close").cast(DecimalType(18, 6)).alias("close"),
        F.col("record.adj_close").cast(DecimalType(18, 6)).alias("adj_close"),
        F.col("record.volume").cast(LongType()).alias("volume"),
        F.col("batch_id"),
        F.col("source_id"),
        F.to_timestamp("ingest_ts").alias("ingest_ts"),
    )
    .filter(F.col("symbol").isNotNull() & F.col("price_date").isNotNull())
    .withColumn("price_revision_hash", _price_revision_hash("price_date"))
)
earliest_revision_window = Window.partitionBy(
    "symbol", "price_date", "price_revision_hash"
).orderBy(F.col("ingest_ts").asc_nulls_last(), F.col("batch_id").asc())
bronze_df = (
    bronze_revisions
    .withColumn("revision_row_number", F.row_number().over(earliest_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .drop("revision_row_number")
    .cache()
)
total_bronze = bronze_df.count()
print(f"Bronze payload revisions (exact repeats collapsed): {total_bronze}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- DQ checks ---
dq_fails = bronze_df.filter(
    (F.col("close") <= 0)
    | F.col("close").isNull()
    | (F.col("volume") < 0)
    | (F.col("price_date") > F.current_date())
).cache()
dq_pass = bronze_df.filter(
    (F.col("close") > 0)
    & F.col("close").isNotNull()
    & (F.col("volume").isNull() | (F.col("volume") >= 0))
    & (F.col("price_date") <= F.current_date())
).cache()

dq_fail_count = dq_fails.count()
print(f"DQ pass: {dq_pass.count()} | DQ fail: {dq_fail_count}")

if dq_fail_count > 0:
    dq_quarantine_df = dq_fails.select(
        F.sha2(
            F.concat_ws("|", F.lit("prices_eod"), F.lit("INVALID_PRICE_OR_DATE"), F.col("symbol"), F.col("price_date").cast("string"), F.col("batch_id")),
            256,
        ).alias("quarantine_id"),
        F.concat_ws(":", F.lit("prices_eod"), F.lit("INVALID_PRICE_OR_DATE"), F.col("symbol"), F.col("price_date").cast("string"), F.col("batch_id")).alias("natural_key"),
        F.lit("prices_eod").alias("source_id"),
        F.col("batch_id"),
        F.to_json(F.struct(*[F.col(c) for c in dq_fails.columns])).alias("raw_record"),
        F.lit("INVALID_PRICE_OR_DATE").alias("dq_rule"),
        F.current_timestamp().alias("quarantined_at"),
    )
    _merge_dq_quarantine(dq_quarantine_df)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Resolve ticker to current dim_security ---
dim_current = (
    spark.table("dim_security")
    .filter(F.col("is_current") == True)
    .select(
        F.col("security_sk"),
        F.col("ticker").alias("dim_ticker"),
        F.col("cik").alias("issuer_cik"),
    )
)
ambiguous_current_tickers = (
    dim_current
    .filter(F.col("dim_ticker").isNotNull())
    .groupBy("dim_ticker")
    .agg(F.countDistinct("security_sk").alias("security_count"))
    .filter(F.col("security_count") > 1)
)
ambiguous_ticker_count = ambiguous_current_tickers.count()
if ambiguous_ticker_count:
    display(ambiguous_current_tickers.orderBy(F.desc("security_count")).limit(100))
    raise RuntimeError(
        "PRICE SILVER RESOLUTION FAILED: "
        f"ambiguous_current_tickers={ambiguous_ticker_count}"
    )
resolved = dq_pass.join(dim_current, dq_pass.symbol == dim_current.dim_ticker, how="left")

unresolved_prices = resolved.filter(F.col("security_sk").isNull())
resolved_prices = resolved.filter(F.col("security_sk").isNotNull())

if not unresolved_prices.isEmpty():
    security_quarantine_df = unresolved_prices.select(
        F.sha2(
            F.concat_ws("|", F.lit("prices_eod"), F.lit("SECURITY_UNRESOLVED"), F.col("symbol"), F.col("price_date").cast("string"), F.col("batch_id")),
            256,
        ).alias("quarantine_id"),
        F.concat_ws(":", F.lit("prices_eod"), F.lit("SECURITY_UNRESOLVED"), F.col("symbol"), F.col("price_date").cast("string"), F.col("batch_id")).alias("natural_key"),
        F.lit("prices_eod").alias("source_id"),
        F.col("symbol").alias("raw_identifier"),
        F.lit("SECURITY_UNRESOLVED").alias("reason"),
        F.concat(F.lit("symbol="), F.col("symbol")).alias("details"),
        F.col("price_date").alias("event_date"),
        F.to_date("ingest_ts").alias("knowledge_date"),
        F.col("batch_id"),
        F.current_timestamp().alias("quarantined_at"),
    )
    _merge_security_quarantine(security_quarantine_df)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Add PIT columns ---
silver_df = resolved_prices.select(
    F.col("security_sk").cast(LongType()).alias("security_sk"),
    F.col("symbol"),
    F.col("price_date").alias("date"),
    F.col("price_revision_hash"),
    F.col("open"),
    F.col("high"),
    F.col("low"),
    F.col("close"),
    F.col("adj_close"),
    F.col("volume"),
    F.col("issuer_cik"),
    F.col("price_date").alias("event_date"),
    F.to_date("ingest_ts").alias("knowledge_date"),
    F.col("source_id"),
    F.col("batch_id"),
    F.col("ingest_ts"),
    F.current_timestamp().alias("revision_loaded_at"),
)
silver_source_duplicates = (
    silver_df
    .groupBy("security_sk", "date", "price_revision_hash")
    .count()
    .filter(F.col("count") > 1)
    .count()
)
if silver_source_duplicates:
    raise RuntimeError(
        "PRICE SILVER SOURCE VALIDATION FAILED: "
        f"duplicate_revision_keys={silver_source_duplicates}"
    )

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Create/upgrade silver_prices ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_prices (
        security_sk    BIGINT        NOT NULL,
        symbol         STRING        NOT NULL,
        date           DATE          NOT NULL,
        price_revision_hash STRING    NOT NULL,
        open           DECIMAL(18,6),
        high           DECIMAL(18,6),
        low            DECIMAL(18,6),
        close          DECIMAL(18,6) NOT NULL,
        adj_close      DECIMAL(18,6),
        volume         BIGINT,
        issuer_cik     STRING,
        event_date     DATE          NOT NULL,
        knowledge_date DATE          NOT NULL,
        source_id      STRING,
        batch_id       STRING,
        ingest_ts      TIMESTAMP NOT NULL,
        revision_loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
_ensure_columns("silver_prices", {
    "security_sk": "security_sk BIGINT",
    "price_revision_hash": "price_revision_hash STRING",
    "revision_loaded_at": "revision_loaded_at TIMESTAMP",
})

target = DeltaTable.forName(spark, "silver_prices")
target.update(
    condition=F.col("revision_loaded_at").isNull(),
    set={"revision_loaded_at": F.current_timestamp()},
)
legacy_revision_rows = target.toDF().filter(F.col("price_revision_hash").isNull()).count()
if legacy_revision_rows:
    target.update(
        condition=F.col("price_revision_hash").isNull(),
        set={"price_revision_hash": _price_revision_hash("date")},
    )
    print(f"Backfilled price_revision_hash for {legacy_revision_rows} legacy silver_prices rows")

legacy_prices = (
    spark.table("silver_prices")
    .filter(F.col("security_sk").isNull() if "security_sk" in spark.table("silver_prices").columns else F.lit(False))
    .select("symbol", "date")
    .dropDuplicates(["symbol", "date"])
)
legacy_resolved = legacy_prices.join(dim_current, legacy_prices.symbol == dim_current.dim_ticker, how="inner")
if not legacy_resolved.isEmpty():
    target = DeltaTable.forName(spark, "silver_prices")
    (
        target
        .alias("t")
        .merge(
            legacy_resolved.select("security_sk", "issuer_cik", "symbol", "date").alias("s"),
            "t.symbol = s.symbol AND t.date = s.date AND t.security_sk IS NULL",
        )
        .whenMatchedUpdate(set={
            "security_sk": "s.security_sk",
            "issuer_cik": "s.issuer_cik",
        })
        .execute()
    )
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    print(f"Backfilled legacy rows={metrics.get('numTargetRowsUpdated', 'unknown')} in silver_prices")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- MERGE into silver_prices on the PIT-safe payload revision key ---
target = DeltaTable.forName(spark, "silver_prices")
(
    target
    .alias("t")
    .merge(
        silver_df.alias("s"),
        "t.security_sk = s.security_sk AND t.date = s.date "
        "AND t.price_revision_hash = s.price_revision_hash",
    )
    .whenMatchedUpdateAll(condition="t.ingest_ts IS NULL OR s.ingest_ts < t.ingest_ts")
    .whenNotMatchedInsertAll()
    .execute()
)
metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
print(f"Merged source_rows={metrics.get('numSourceRows', 'unknown')} into silver_prices")

silver_target_df = target.toDF().cache()
silver_revision_duplicates = (
    silver_target_df
    .groupBy("security_sk", "date", "price_revision_hash")
    .count()
    .filter(F.col("count") > 1)
    .count()
)
silver_missing_revision_hash = silver_target_df.filter(F.col("price_revision_hash").isNull()).count()
silver_missing_ingest_ts = silver_target_df.filter(F.col("ingest_ts").isNull()).count()
silver_missing_loaded_at = silver_target_df.filter(F.col("revision_loaded_at").isNull()).count()
if (
    silver_revision_duplicates
    or silver_missing_revision_hash
    or silver_missing_ingest_ts
    or silver_missing_loaded_at
):
    raise RuntimeError(
        "PRICE SILVER REVISION VALIDATION FAILED: "
        f"duplicate_revision_keys={silver_revision_duplicates}, "
        f"missing_revision_hash={silver_missing_revision_hash}, "
        f"missing_ingest_ts={silver_missing_ingest_ts}, "
        f"missing_revision_loaded_at={silver_missing_loaded_at}"
    )
print(
    "Price silver revision validation passed: "
    f"duplicate_revision_keys={silver_revision_duplicates}, "
    f"missing_revision_hash={silver_missing_revision_hash}, "
    f"missing_ingest_ts={silver_missing_ingest_ts}, "
    f"missing_revision_loaded_at={silver_missing_loaded_at}"
)
_ensure_not_null_constraints(
    "silver_prices",
    ["price_revision_hash", "ingest_ts", "revision_loaded_at"],
)
silver_counts = silver_target_df.agg(
    F.count("*").alias("silver_rows"),
    F.countDistinct("symbol", "date").alias("natural_keys"),
).first()
revised_key_counts = (
    silver_target_df
    .groupBy("symbol", "date")
    .agg(F.countDistinct("price_revision_hash").alias("revision_count"))
    .filter(F.col("revision_count") > 1)
)
revised_natural_keys = revised_key_counts.count()
revised_key_details = [
    json.loads(row)
    for row in (
        silver_target_df.alias("prices")
        .join(revised_key_counts.select("symbol", "date").alias("revised"), ["symbol", "date"])
        .select(
            "symbol",
            "date",
            "price_revision_hash",
            "open",
            "high",
            "low",
            "close",
            "adj_close",
            "volume",
            "knowledge_date",
            "ingest_ts",
            "batch_id",
        )
        .orderBy("symbol", "date", "ingest_ts", "price_revision_hash")
        .limit(100)
        .toJSON()
        .collect()
    )
]
run_summary_json = json.dumps({
    "from_date": from_date,
    "to_date": to_date,
    "bronze_payload_revisions": total_bronze,
    "silver_rows": silver_counts.silver_rows,
    "natural_keys": silver_counts.natural_keys,
    "revised_natural_keys": revised_natural_keys,
    "revised_key_details": revised_key_details,
    "duplicate_revision_keys": silver_revision_duplicates,
    "missing_revision_hash": silver_missing_revision_hash,
    "missing_ingest_ts": silver_missing_ingest_ts,
    "missing_revision_loaded_at": silver_missing_loaded_at,
}, sort_keys=True)
print(f"PRICE SILVER SUMMARY: {run_summary_json}", flush=True)
silver_target_df.unpersist()
dq_fails.unpersist()
dq_pass.unpersist()
bronze_df.unpersist()
mssparkutils.notebook.exit(run_summary_json)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
