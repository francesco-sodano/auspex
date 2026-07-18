# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "503baa23-d625-4281-83f6-d50b6f34cc5a",
# META       "default_lakehouse_name": "auspex_bronze",
# META       "default_lakehouse_workspace_id": "2036bed1-3b4a-4958-958e-fe9a3b11971c",
# META       "known_lakehouses": [
# META         {
# META           "id": "503baa23-d625-4281-83f6-d50b6f34cc5a"
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
from pyspark.sql import functions as F
from pyspark.sql.types import DateType, DecimalType, LongType, StringType, StructField, StructType, TimestampType

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# PARAMETERS CELL ********************

# --- Parameters: mark this cell as the Fabric parameter cell ---
_today = date.today().isoformat()
from_date = (date.today() - timedelta(days=7)).isoformat()
to_date = _today

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Normalize and validate injected parameter values ---
from_date = str(from_date)
to_date = str(to_date)
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


def _date_paths(from_d: str, to_d: str, source: str = "prices_eod"):
    d = date.fromisoformat(from_d)
    end = date.fromisoformat(to_d)
    paths = []
    while d <= end:
        paths.append(f"Files/bronze/{source}/{d.year}/{d.month:02d}/{d.day:02d}/*.ndjson")
        d += timedelta(days=1)
    return paths


def _existing_paths(paths):
    result = []
    for p in paths:
        dir_path = p.rsplit("/", 1)[0]
        try:
            mssparkutils.fs.ls(dir_path)
            result.append(p)
        except Exception:
            pass
    return result


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

# --- Bronze file paths for the window ---
paths = _existing_paths(_date_paths(from_date, to_date))
print(f"Date folders with data: {len(paths)}")
if not paths:
    raise RuntimeError("No bronze files found in window - check connector ran and lakehouse is attached")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Read bronze ---
bronze_df = (
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
    .dropDuplicates(["symbol", "price_date"])
    .cache()
)
total_bronze = bronze_df.count()
print(f"Bronze rows (symbol+date unique): {total_bronze}")

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
        ingest_ts      TIMESTAMP
    )
    USING DELTA
""")
_ensure_columns("silver_prices", {"security_sk": "security_sk BIGINT"})

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

# --- MERGE into silver_prices on (security_sk, date) ---
target = DeltaTable.forName(spark, "silver_prices")
(
    target
    .alias("t")
    .merge(
        silver_df.alias("s"),
        "t.security_sk = s.security_sk AND t.date = s.date",
    )
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
    .execute()
)
metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
print(f"Merged source_rows={metrics.get('numSourceRows', 'unknown')} into silver_prices")
dq_fails.unpersist()
dq_pass.unpersist()
bronze_df.unpersist()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
