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

# Fabric Notebook: nb_07_contracts_to_gold
# Reads USASpending E8 bronze records and writes fact_contract_award.
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
from pyspark.sql.types import DecimalType, IntegerType, LongType

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
from_date = str(from_date).strip() or (date.today() - timedelta(days=30)).isoformat()
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


for required in ["dim_security", "dim_source", "fact_contract_award"]:
    _require_table(required)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

paths = _existing_paths(_date_paths("contracts"))
if not paths:
    raise RuntimeError("No contracts bronze files found in window")

raw = spark.read.json(paths).select(
    F.col("batch_id"),
    F.to_timestamp("ingest_ts").alias("ingest_ts"),
    F.upper(F.col("record.symbol")).alias("symbol"),
    F.col("record.search_text").alias("search_text"),
    F.to_json("record.award").alias("award_json"),
)
print(f"Contracts bronze files: {len(paths)}")

security_lookup = (
    spark.table("dim_security")
    .filter(F.col("is_current") == True)
    .select("security_sk", F.upper("ticker").alias("symbol"))
)
source_seed = spark.createDataFrame(
    [(6, "contracts", "contract", "weekly", None, "public_official")],
    "source_sk INT, source_id STRING, source_type STRING, latency_class STRING, reliability_weight DECIMAL(3,2), source_class STRING",
)
_merge_all("dim_source", source_seed, "t.source_sk = s.source_sk")
contracts_source_sk = 6

contract_df = (
    raw
    .select(
        "symbol",
        "search_text",
        F.get_json_object("award_json", "$['Award ID']").alias("award_id"),
        F.get_json_object("award_json", "$['Awarding Agency']").alias("agency"),
        F.get_json_object("award_json", "$['Award Amount']").cast(DecimalType(20, 2)).alias("amount_usd"),
        F.get_json_object("award_json", "$['Description']").alias("description"),
        F.to_date(F.coalesce(
            F.get_json_object("award_json", "$['Start Date']"),
            F.get_json_object("award_json", "$['Action Date']"),
            F.to_date("ingest_ts").cast("string"),
        )).alias("event_date"),
        F.to_date("ingest_ts").alias("knowledge_date"),
    )
    .join(security_lookup, "symbol", "left")
    .withColumn("award_sk", _positive_sk(F.lit("usaspending"), F.col("award_id"), F.col("search_text")))
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("description_hash", F.sha2(F.coalesce(F.col("description"), F.col("award_id"), F.col("search_text")), 256))
    .withColumn("source_sk", F.lit(contracts_source_sk))
    .select("award_sk", "security_sk", "date_sk", "agency", "amount_usd", "description_hash", "source_sk", "event_date", "knowledge_date")
    .filter(F.col("award_sk").isNotNull() & F.col("event_date").isNotNull() & F.col("knowledge_date").isNotNull())
    .dropDuplicates(["award_sk"])
)
if not contract_df.isEmpty():
    _merge_all("fact_contract_award", contract_df, "t.award_sk = s.award_sk")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

missing_pit = spark.table("fact_contract_award").filter(F.col("event_date").isNull() | F.col("knowledge_date").isNull()).count()
print(f"E8 contracts validation: missing_pit={missing_pit}")
if missing_pit:
    raise RuntimeError(f"E8 contracts validation failed: missing_pit={missing_pit}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
