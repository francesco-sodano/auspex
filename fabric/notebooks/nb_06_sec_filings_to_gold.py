# Fabric Notebook: nb_06_sec_filings_to_gold
# Reads E8 SEC filing bronze records and writes ownership/catalyst placeholder facts.
# Attaches to: auspex_bronze (default lakehouse)

# COMMAND ----------
from datetime import date, timedelta
from delta.tables import DeltaTable
from pyspark.sql import functions as F
from pyspark.sql.types import BooleanType, DecimalType, IntegerType, LongType, StringType

# COMMAND ----------
# --- Parameters: mark this cell as the Fabric parameter cell ---
from_date = ""
to_date = ""

# COMMAND ----------
# --- Normalize and validate injected parameter values ---
_today = date.today().isoformat()
from_date = str(from_date).strip() or (date.today() - timedelta(days=7)).isoformat()
to_date = str(to_date).strip() or _today
if date.fromisoformat(from_date) > date.fromisoformat(to_date):
    raise ValueError("from_date must be on or before to_date")

print(f"Window: {from_date} to {to_date}")

# COMMAND ----------
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


for required in [
    "dim_security", "dim_source", "fact_institutional_holding",
    "fact_ownership_event",
]:
    _require_table(required)

# COMMAND ----------
paths = _existing_paths(_date_paths("sec_13f") + _date_paths("sec_13dg") + _date_paths("sec_8k") + _date_paths("sec_s1"))
if not paths:
    raise RuntimeError("No E8 SEC bronze files found in window")

bronze = (
    spark.read.json(paths)
    .select(
        F.col("source_id"),
        F.col("batch_id"),
        F.col("record.adsh").alias("accession_no"),
        F.col("record.file_date").alias("file_date"),
        F.col("record.period_ending").alias("period_of_report"),
        F.col("record.display_names").cast(StringType()).alias("display_names"),
        F.col("record.form").alias("form"),
        F.col("record.matched_forms").alias("matched_forms"),
    )
    .filter(F.col("accession_no").isNotNull())
    .dropDuplicates(["source_id", "accession_no"])
    .cache()
)
print(f"E8 SEC bronze filings: {bronze.count()}")

security_lookup = (
    spark.table("dim_security")
    .filter(F.col("is_current") == True)
    .select("security_sk", "ticker", "cik")
)

source_seed_schema = "source_sk INT, source_id STRING, source_type STRING, latency_class STRING, reliability_weight DECIMAL(3,2), source_class STRING"
source_seed = spark.createDataFrame([
    (7, "sec_13f", "filing", "quarterly", None, "public_official"),
    (8, "sec_13dg", "filing", "daily", None, "public_official"),
    (9, "sec_8k", "filing", "daily", None, "public_official"),
    (10, "sec_s1", "filing", "daily", None, "public_official"),
], source_seed_schema)
(
    DeltaTable.forName(spark, "dim_source")
    .alias("t")
    .merge(source_seed.alias("s"), "t.source_sk = s.source_sk")
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
    .execute()
)
source_lookup = spark.table("dim_source").select("source_sk", "source_id")
security_by_cik = spark.table("dim_security").filter(F.col("is_current") == True).select("security_sk", "cik")

# COMMAND ----------
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_sec_filing_event (
        filing_event_sk BIGINT NOT NULL, accession_no STRING NOT NULL, filing_type STRING,
        filer_name STRING, source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")

filing_event_df = (
    bronze
    .join(source_lookup, "source_id", "left")
    .withColumn("filing_event_sk", _positive_sk(F.col("source_id"), F.col("accession_no")))
    .withColumn("filing_type", F.coalesce(F.col("form"), F.col("matched_forms")))
    .withColumn("filer_name", F.col("display_names"))
    .withColumn("event_date", F.to_date(F.coalesce(F.col("period_of_report"), F.col("file_date"))))
    .withColumn("knowledge_date", F.to_date("file_date"))
    .select("filing_event_sk", "accession_no", "filing_type", "filer_name", "source_sk", "event_date", "knowledge_date")
)
if not filing_event_df.isEmpty():
    _merge_all("fact_sec_filing_event", filing_event_df, "t.filing_event_sk = s.filing_event_sk")

ownership_df = (
    bronze.filter(F.col("source_id") == "sec_13dg")
    .join(source_lookup, "source_id", "left")
    .withColumn("security_sk", F.lit(None).cast(LongType()))
    .withColumn("entity_sk", _positive_sk(F.lit("13dg_filer"), F.col("display_names")))
    .withColumn("event_date", F.to_date(F.coalesce(F.col("period_of_report"), F.col("file_date"))))
    .withColumn("knowledge_date", F.to_date("file_date"))
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("pct_owned", F.lit(None).cast(DecimalType(9, 6)))
    .withColumn("filing_type", F.coalesce(F.col("form"), F.col("matched_forms")))
    .withColumn("is_activist", F.col("filing_type").contains("13D").cast(BooleanType()))
    .select("security_sk", "entity_sk", "date_sk", "pct_owned", "filing_type", "is_activist", "accession_no", "source_sk", "event_date", "knowledge_date")
)
if not ownership_df.isEmpty():
    _merge_all("fact_ownership_event", ownership_df, "t.accession_no = s.accession_no")

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_material_event (
        event_sk BIGINT NOT NULL, security_sk BIGINT, date_sk INT,
        accession_no STRING NOT NULL, filing_type STRING, description STRING,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")

material_event_df = (
    bronze.filter(F.col("source_id").isin("sec_8k", "sec_s1"))
    .join(source_lookup, "source_id", "left")
    .withColumn("issuer_cik", F.regexp_replace(F.split(F.col("accession_no"), "-").getItem(0), "^0+", ""))
    .join(security_by_cik, F.col("issuer_cik") == F.col("cik"), "left")
    .withColumn("event_sk", _positive_sk(F.col("source_id"), F.col("accession_no")))
    .withColumn("event_date", F.to_date(F.coalesce(F.col("period_of_report"), F.col("file_date"))))
    .withColumn("knowledge_date", F.to_date("file_date"))
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("filing_type", F.coalesce(F.col("form"), F.col("matched_forms")))
    .withColumn("description", F.col("display_names"))
    .select("event_sk", "security_sk", "date_sk", "accession_no", "filing_type", "description", "source_sk", "event_date", "knowledge_date")
)
if not material_event_df.isEmpty():
    _merge_all("fact_material_event", material_event_df, "t.event_sk = s.event_sk")

# COMMAND ----------
missing_pit = spark.sql("""
    SELECT SUM(n) AS n
    FROM (
        SELECT COUNT(*) AS n FROM fact_institutional_holding WHERE event_date IS NULL OR knowledge_date IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_ownership_event WHERE event_date IS NULL OR knowledge_date IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_material_event WHERE event_date IS NULL OR knowledge_date IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_sec_filing_event WHERE event_date IS NULL OR knowledge_date IS NULL
    ) x
""").collect()[0].n
print(
    "E8 SEC validation: "
    f"missing_pit={missing_pit}"
)
if missing_pit:
    raise RuntimeError(f"E8 SEC validation failed: missing_pit={missing_pit}")
bronze.unpersist()
