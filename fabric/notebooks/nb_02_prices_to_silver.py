# Fabric Notebook: nb_02_prices_to_silver
# Reads bronze prices_eod NDJSON and writes to silver_prices (Delta MERGE).
# Attaches to: auspex_bronze (default lakehouse)
#
# Pipeline parameters:
#   from_date  YYYY-MM-DD  (default: 7 days ago)
#   to_date    YYYY-MM-DD  (default: today)

# COMMAND ----------
from datetime import date, datetime, timedelta, timezone
from pyspark.sql import functions as F
from pyspark.sql.types import (
    DateType, DecimalType, LongType,
    StringType, StructField, StructType, TimestampType,
)
from delta.tables import DeltaTable

# COMMAND ----------
# --- Parameters ---
def _widget(name, default):
    try:
        return dbutils.widgets.get(name)
    except Exception:
        return default

_today    = date.today().isoformat()
from_date = _widget("from_date", (date.today() - timedelta(days=7)).isoformat())
to_date   = _widget("to_date", _today)
print(f"Window: {from_date} → {to_date}")

# COMMAND ----------
# --- Bronze file paths for the window ---
def _date_paths(from_d: str, to_d: str, source: str = "prices_eod"):
    d   = date.fromisoformat(from_d)
    end = date.fromisoformat(to_d)
    paths = []
    while d <= end:
        paths.append(f"Files/bronze/{source}/{d.year}/{d.month:02d}/{d.day:02d}/*.ndjson")
        d += timedelta(days=1)
    return paths

paths = _date_paths(from_date, to_date)

# COMMAND ----------
# --- Read bronze ---
bronze_df = (
    spark.read.json(paths)
    .select(
        F.col("record.symbol").alias("symbol"),
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
)
total_bronze = bronze_df.count()
print(f"Bronze rows (symbol+date unique): {total_bronze}")

# COMMAND ----------
# --- DQ checks ---
dq_fails = bronze_df.filter(
    (F.col("close") <= 0)
    | F.col("close").isNull()
    | (F.col("volume") < 0)
    | (F.col("price_date") > F.current_date())
)
dq_pass  = bronze_df.filter(
    (F.col("close") > 0)
    & F.col("close").isNotNull()
    & (F.col("volume") >= 0)
    & (F.col("price_date") <= F.current_date())
)

dq_fail_count = dq_fails.count()
print(f"DQ pass: {dq_pass.count()} | DQ fail: {dq_fail_count}")

if dq_fail_count > 0:
    now_ts = datetime.now(timezone.utc)
    import uuid
    q_rows = [
        {
            "quarantine_id":  str(uuid.uuid4()),
            "source_id":      "prices_eod",
            "batch_id":       r.batch_id,
            "raw_record":     str(r.asDict()),
            "dq_rule":        "INVALID_PRICE_OR_DATE",
            "quarantined_at": now_ts,
        }
        for r in dq_fails.limit(1000).collect()
    ]
    from pyspark.sql.types import StructType as ST
    q_schema = StructType([
        StructField("quarantine_id",  StringType()),
        StructField("source_id",      StringType()),
        StructField("batch_id",       StringType()),
        StructField("raw_record",     StringType()),
        StructField("dq_rule",        StringType()),
        StructField("quarantined_at", TimestampType()),
    ])
    (
        spark.createDataFrame(q_rows, q_schema)
        .write.format("delta").mode("append")
        .saveAsTable("silver_dq_quarantine")
    )

# COMMAND ----------
# --- Resolve ticker → CIK via security_master (informational; security_sk assigned in E5 gold load) ---
sm_df = spark.table("security_master").select(
    F.col("ticker"),
    F.col("cik").alias("issuer_cik"),
)
resolved = dq_pass.join(sm_df, dq_pass.symbol == sm_df.ticker, how="left")

# COMMAND ----------
# --- Add PIT columns ---
silver_df = resolved.select(
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
    F.current_date().alias("knowledge_date"),
    F.col("source_id"),
    F.col("batch_id"),
    F.col("ingest_ts"),
)

# COMMAND ----------
# --- Create silver_prices if not exists ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_prices (
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

# COMMAND ----------
# --- MERGE into silver_prices on (symbol, date) ---
(
    DeltaTable.forName(spark, "silver_prices")
    .alias("t")
    .merge(
        silver_df.alias("s"),
        "t.symbol = s.symbol AND t.date = s.date",
    )
    .whenMatchedUpdateAll()   # replay-safe: overwrite with latest ingest
    .whenNotMatchedInsertAll()
    .execute()
)
print(f"Merged {silver_df.count()} rows into silver_prices")
print(f"silver_prices total: {spark.table('silver_prices').count()} rows")
