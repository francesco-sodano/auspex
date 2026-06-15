# Fabric Notebook: nb_00_entity_resolution
# Run BEFORE silver transforms each day.
# Attaches to: auspex_bronze (default lakehouse)
#
# 1. Seeds security_master Delta table from SEC company_tickers.json (CIK ↔ ticker ↔ name)
# 2. Initialises quarantine / control tables if they don't exist

# COMMAND ----------
import requests
from datetime import datetime, timezone
from pyspark.sql import functions as F
from pyspark.sql.types import StructType, StructField, StringType, TimestampType
from delta.tables import DeltaTable

# COMMAND ----------
# Widget parameters (pipeline can override)
try:
    EDGAR_USER_AGENT = dbutils.widgets.get("edgar_user_agent")
except Exception:
    EDGAR_USER_AGENT = "Auspex/1.0 auspex-bot@example.com"

# COMMAND ----------
# --- Fetch SEC company_tickers.json (CIK ↔ ticker ↔ name) ---
resp = requests.get(
    "https://www.sec.gov/files/company_tickers.json",
    headers={"User-Agent": EDGAR_USER_AGENT},
    timeout=30,
)
resp.raise_for_status()
raw = resp.json()  # {idx: {"cik_str": "0001234567", "ticker": "AAPL", "title": "Apple Inc."}}

rows = [
    (str(int(v["cik_str"])), v["ticker"].upper(), v["title"])
    for v in raw.values()
]
print(f"Fetched {len(rows)} tickers from SEC")

# COMMAND ----------
schema = StructType([
    StructField("cik", StringType(), False),
    StructField("ticker", StringType(), False),
    StructField("company_name", StringType(), False),
])
now_ts = datetime.now(timezone.utc)
df = (
    spark.createDataFrame(rows, schema)
    .withColumn("ingested_at", F.lit(now_ts.isoformat()).cast("timestamp"))
)

# COMMAND ----------
# --- Create table if not exists ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS security_master (
        cik          STRING    NOT NULL,
        ticker       STRING    NOT NULL,
        company_name STRING    NOT NULL,
        ingested_at  TIMESTAMP
    )
    USING DELTA
""")

# MERGE: upsert on (cik, ticker) — update name if it changed
(
    DeltaTable.forName(spark, "security_master")
    .alias("t")
    .merge(df.alias("s"), "t.cik = s.cik AND t.ticker = s.ticker")
    .whenMatchedUpdate(set={
        "company_name": "s.company_name",
        "ingested_at":  "s.ingested_at",
    })
    .whenNotMatchedInsertAll()
    .execute()
)
print(f"security_master: {spark.table('security_master').count()} rows")

# COMMAND ----------
# --- Initialise quarantine / control tables ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_security_quarantine (
        quarantine_id  STRING    NOT NULL,
        source_id      STRING    NOT NULL,
        raw_identifier STRING,
        reason         STRING    NOT NULL,
        quarantined_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_dq_quarantine (
        quarantine_id  STRING    NOT NULL,
        source_id      STRING    NOT NULL,
        batch_id       STRING,
        raw_record     STRING,
        dq_rule        STRING    NOT NULL,
        quarantined_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_parse_errors (
        source_id   STRING    NOT NULL,
        batch_id    STRING,
        raw_record  STRING,
        error_msg   STRING    NOT NULL,
        occurred_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
print("Control tables ready.")
