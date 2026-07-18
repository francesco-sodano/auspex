# Fabric Notebook: nb_00_entity_resolution
# Run BEFORE silver transforms each day.
# Attaches to: auspex_bronze (default lakehouse)
#
# 1. Seeds security_master from SEC company_tickers.json (CIK/ticker/name)
# 2. Maintains canonical dim_security with deterministic security_sk and SCD2-ready rows
# 3. Initialises replay-safe quarantine / control tables

# COMMAND ----------
import requests
from datetime import datetime, timezone
from delta.tables import DeltaTable
from pyspark.sql import functions as F
from pyspark.sql.types import StructField, StructType, StringType

# COMMAND ----------
# --- Parameters: mark this cell as the Fabric parameter cell ---
edgar_user_agent = "Auspex/1.0 auspex@auspex.ai"

# COMMAND ----------
# --- Normalize and validate injected parameter values ---
edgar_user_agent = str(edgar_user_agent).strip()
if not edgar_user_agent:
    raise ValueError("edgar_user_agent cannot be empty")

EDGAR_USER_AGENT = edgar_user_agent

# COMMAND ----------
def _ensure_columns(table_name: str, column_specs: dict[str, str]) -> None:
    """Add nullable columns to an existing Delta table when upgrading notebook schemas."""
    existing = set(spark.table(table_name).columns)
    for column_name, ddl in column_specs.items():
        if column_name not in existing:
            spark.sql(f"ALTER TABLE {table_name} ADD COLUMNS ({ddl})")


# COMMAND ----------
# --- Fetch SEC company_tickers.json (CIK/ticker/name) ---
resp = requests.get(
    "https://www.sec.gov/files/company_tickers.json",
    headers={"User-Agent": EDGAR_USER_AGENT},
    timeout=30,
)
resp.raise_for_status()
raw = resp.json()  # {idx: {"cik_str": 1234567, "ticker": "AAPL", "title": "Apple Inc."}}

rows = [
    (str(int(v["cik_str"])), v["ticker"].upper(), v["title"])
    for v in raw.values()
    if v.get("cik_str") and v.get("ticker") and v.get("title")
]
print(f"Fetched {len(rows)} tickers from SEC")

# COMMAND ----------
schema = StructType([
    StructField("cik", StringType(), False),
    StructField("ticker", StringType(), False),
    StructField("company_name", StringType(), False),
])
now_ts = datetime.now(timezone.utc)
source_df = (
    spark.createDataFrame(rows, schema)
    .dropDuplicates(["cik", "ticker"])
    .withColumn("ingested_at", F.lit(now_ts.isoformat()).cast("timestamp"))
    .cache()
)

# COMMAND ----------
# --- Maintain security_master compatibility table ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS security_master (
        cik          STRING    NOT NULL,
        ticker       STRING    NOT NULL,
        company_name STRING    NOT NULL,
        ingested_at  TIMESTAMP
    )
    USING DELTA
""")

(
    DeltaTable.forName(spark, "security_master")
    .alias("t")
    .merge(source_df.alias("s"), "t.cik = s.cik AND t.ticker = s.ticker")
    .whenMatchedUpdate(
        condition="NOT (t.company_name <=> s.company_name)",
        set={
            "company_name": "s.company_name",
            "ingested_at": "s.ingested_at",
        },
    )
    .whenNotMatchedInsertAll()
    .execute()
)
print("security_master merge complete")

# COMMAND ----------
# --- Canonical dim_security (SCD2-ready) ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS dim_security (
        security_sk       BIGINT    NOT NULL,
        cik               STRING,
        ticker            STRING,
        isin              STRING,
        figi              STRING,
        company_name      STRING    NOT NULL,
        gics_sector       STRING,
        gics_industry     STRING,
        country           STRING,
        exchange          STRING,
        currency          STRING,
        mcap_band         STRING,
        is_active         BOOLEAN   NOT NULL,
        valid_from        DATE      NOT NULL,
        valid_to          DATE      NOT NULL,
        is_current        BOOLEAN   NOT NULL,
        resolution_method STRING    NOT NULL,
        source_id         STRING    NOT NULL,
        updated_at        TIMESTAMP
    )
    USING DELTA
""")

_ensure_columns("dim_security", {
    "isin": "isin STRING",
    "figi": "figi STRING",
    "gics_sector": "gics_sector STRING",
    "gics_industry": "gics_industry STRING",
    "country": "country STRING",
    "exchange": "exchange STRING",
    "currency": "currency STRING",
    "mcap_band": "mcap_band STRING",
    "resolution_method": "resolution_method STRING",
    "source_id": "source_id STRING",
    "updated_at": "updated_at TIMESTAMP",
})

security_seed = (
    source_df.select("cik", "ticker", "company_name")
    .withColumn(
        "security_sk",
        F.pmod(
            F.xxhash64(F.concat_ws("|", F.lit("security"), F.col("cik"), F.col("ticker"))),
            F.lit(9223372036854775807),
        ).cast("long"),
    )
    .withColumn("isin", F.lit(None).cast("string"))
    .withColumn("figi", F.lit(None).cast("string"))
    .withColumn("gics_sector", F.lit(None).cast("string"))
    .withColumn("gics_industry", F.lit(None).cast("string"))
    .withColumn("country", F.lit("US"))
    .withColumn("exchange", F.lit(None).cast("string"))
    .withColumn("currency", F.lit("USD"))
    .withColumn("mcap_band", F.lit(None).cast("string"))
    .withColumn("is_active", F.lit(True))
    .withColumn("valid_from", F.to_date(F.lit("1900-01-01")))
    .withColumn("valid_to", F.to_date(F.lit("9999-12-31")))
    .withColumn("is_current", F.lit(True))
    .withColumn("resolution_method", F.lit("SEC_COMPANY_TICKERS"))
    .withColumn("source_id", F.lit("sec_company_tickers"))
    .withColumn("updated_at", F.current_timestamp())
    .select(
        "security_sk", "cik", "ticker", "isin", "figi", "company_name",
        "gics_sector", "gics_industry", "country", "exchange", "currency",
        "mcap_band", "is_active", "valid_from", "valid_to", "is_current",
        "resolution_method", "source_id", "updated_at",
    )
    .cache()
)

current_keys = (
    spark.table("dim_security")
    .filter((F.col("is_current") == True) & (F.col("source_id") == "sec_company_tickers"))
    .select("cik", "ticker")
)
retired_keys = current_keys.join(security_seed.select("cik", "ticker"), ["cik", "ticker"], "left_anti")
if not retired_keys.isEmpty():
    (
        DeltaTable.forName(spark, "dim_security")
        .alias("t")
        .merge(
            retired_keys.alias("s"),
            "t.cik = s.cik AND t.ticker = s.ticker AND t.source_id = 'sec_company_tickers' AND t.is_current = true",
        )
        .whenMatchedUpdate(set={
            "is_current": "false",
            "is_active": "false",
            "valid_to": "date_sub(current_date(), 1)",
            "updated_at": "current_timestamp()",
        })
        .execute()
    )
    print("dim_security retirement merge complete")

(
    DeltaTable.forName(spark, "dim_security")
    .alias("t")
    .merge(
        security_seed.alias("s"),
        "t.cik = s.cik AND t.ticker = s.ticker AND t.source_id = 'sec_company_tickers' AND t.is_current = true",
    )
    .whenMatchedUpdate(
        condition="""
            NOT (t.company_name <=> s.company_name)
            OR NOT (t.country <=> s.country)
            OR NOT (t.currency <=> s.currency)
            OR NOT (t.resolution_method <=> s.resolution_method)
        """,
        set={
            "company_name": "s.company_name",
            "country": "s.country",
            "currency": "s.currency",
            "resolution_method": "s.resolution_method",
            "source_id": "s.source_id",
            "updated_at": "s.updated_at",
        },
    )
    .whenNotMatchedInsertAll()
    .execute()
)
print("dim_security merge complete")

# COMMAND ----------
# --- Initialise replay-safe quarantine / control tables ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_security_quarantine (
        quarantine_id  STRING    NOT NULL,
        natural_key    STRING    NOT NULL,
        source_id      STRING    NOT NULL,
        raw_identifier STRING,
        reason         STRING    NOT NULL,
        details        STRING,
        event_date     DATE,
        knowledge_date DATE,
        batch_id       STRING,
        quarantined_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")

_ensure_columns("silver_security_quarantine", {
    "natural_key": "natural_key STRING",
    "details": "details STRING",
    "event_date": "event_date DATE",
    "knowledge_date": "knowledge_date DATE",
    "batch_id": "batch_id STRING",
})

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_dq_quarantine (
        quarantine_id  STRING    NOT NULL,
        natural_key    STRING    NOT NULL,
        source_id      STRING    NOT NULL,
        batch_id       STRING,
        raw_record     STRING,
        dq_rule        STRING    NOT NULL,
        quarantined_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")

_ensure_columns("silver_dq_quarantine", {
    "natural_key": "natural_key STRING",
})

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_parse_errors (
        natural_key STRING    NOT NULL,
        source_id   STRING    NOT NULL,
        batch_id    STRING,
        raw_record  STRING,
        error_msg   STRING    NOT NULL,
        occurred_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")

_ensure_columns("silver_parse_errors", {
    "natural_key": "natural_key STRING",
})

# Downstream notebooks write quarantine rows with Delta MERGE on natural_key.
print("Control tables ready: silver_security_quarantine, silver_dq_quarantine, silver_parse_errors")
security_seed.unpersist()
source_df.unpersist()