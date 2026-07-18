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

# Fabric Notebook: nb_00_bronze_health
# Read-only bronze health gate. Run before bronze-to-silver/gold notebooks.
# Attaches to: auspex_bronze (default lakehouse)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from datetime import date, timedelta

from pyspark.sql import Window
from pyspark.sql import functions as F

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# PARAMETERS CELL ********************

# --- Parameters: mark this cell as the Fabric parameter cell ---
from_date = ""
to_date = ""
sources_csv = (
    "sec_form4,sec_13f,sec_13dg,sec_8k,sec_s1,prices_eod,"
    "alpha_vantage,etf_holdings,news,contracts"
)
required_sources_csv = sources_csv
expected_schema_version = 1
max_future_minutes = 5

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
sources_csv = str(sources_csv)
required_sources_csv = str(required_sources_csv)
expected_schema_version = int(expected_schema_version)
max_future_minutes = int(max_future_minutes)

start_date = date.fromisoformat(from_date)
end_date = date.fromisoformat(to_date)
if start_date > end_date:
    raise ValueError("from_date must be on or before to_date")
if expected_schema_version <= 0:
    raise ValueError("expected_schema_version must be positive")
if max_future_minutes < 0:
    raise ValueError("max_future_minutes cannot be negative")

source_ids = list(dict.fromkeys(source.strip() for source in sources_csv.split(",") if source.strip()))
required_source_ids = {
    source.strip() for source in required_sources_csv.split(",") if source.strip()
}
if not source_ids:
    raise ValueError("sources must contain at least one source_id")
unknown_required = required_source_ids.difference(source_ids)
if unknown_required:
    raise ValueError(f"required_sources must be included in sources: {sorted(unknown_required)}")

print(
    f"Bronze health window: {from_date} to {to_date} | "
    f"sources: {len(source_ids)} | required: {len(required_source_ids)}"
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Discover bronze files without loading their content ---
_PATH_PATTERN = r"/bronze/([^/]+)/(\d{4})/(\d{2})/(\d{2})/"


def _with_path_fields(df, path_column: str):
    path = F.col(path_column)
    return (
        df
        .withColumn("folder_source", F.regexp_extract(path, _PATH_PATTERN, 1))
        .withColumn("folder_year", F.regexp_extract(path, _PATH_PATTERN, 2))
        .withColumn("folder_month", F.regexp_extract(path, _PATH_PATTERN, 3))
        .withColumn("folder_day", F.regexp_extract(path, _PATH_PATTERN, 4))
        .withColumn(
            "folder_date",
            F.to_date(F.concat_ws("-", "folder_year", "folder_month", "folder_day")),
        )
    )


all_files = (
    spark.read.format("binaryFile")
    .option("recursiveFileLookup", "true")
    .option("pathGlobFilter", "*.ndjson")
    .load("Files/bronze")
    .select("path", "length", "modificationTime")
)

window_files = (
    _with_path_fields(all_files, "path")
    .filter(F.col("folder_source").isin(source_ids))
    .filter(F.col("folder_date").between(F.lit(from_date), F.lit(to_date)))
    .cache()
)

selected_paths = [row.path for row in window_files.select("path").collect()]
print(f"Bronze NDJSON files in window: {len(selected_paths)}")
if not selected_paths:
    raise RuntimeError("BRONZE HEALTH FAILED: no NDJSON files found for the selected sources/window")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Parse envelope fields from every NDJSON line ---
raw_lines = (
    spark.read.text(selected_paths)
    .select(
        F.col("value").alias("raw_json"),
        F.input_file_name().alias("_input_file"),
    )
)
raw = _with_path_fields(raw_lines, "_input_file")

record_json = F.get_json_object("raw_json", "$.record")
article_json = F.get_json_object("raw_json", "$.record.article")
award_json = F.get_json_object("raw_json", "$.record.award")

raw = (
    raw
    .withColumn("root_json", F.get_json_object("raw_json", "$"))
    .withColumn("envelope_source", F.get_json_object("raw_json", "$.source_id"))
    .withColumn("schema_version", F.get_json_object("raw_json", "$.schema_version").cast("int"))
    .withColumn("batch_id", F.get_json_object("raw_json", "$.batch_id"))
    .withColumn("ingest_ts_raw", F.get_json_object("raw_json", "$.ingest_ts"))
    .withColumn("ingest_ts", F.to_timestamp("ingest_ts_raw"))
    .withColumn("record_json", record_json)
    .withColumn(
        "file_batch_id",
        F.regexp_extract(F.col("_input_file"), r"/([^/]+)\.ndjson$", 1),
    )
)

sec_key = F.get_json_object("raw_json", "$.record.adsh")
price_symbol = F.upper(F.get_json_object("raw_json", "$.record.symbol"))
price_date = F.get_json_object("raw_json", "$.record.date")
av_function = F.get_json_object("raw_json", "$.record.function")
av_context = F.coalesce(
    F.get_json_object("raw_json", "$.record.context.symbol"),
    F.get_json_object("raw_json", "$.record.context.maturity"),
    F.get_json_object("raw_json", "$.record.context.ccy_pair"),
)
av_fetched_at = F.get_json_object("raw_json", "$.record.fetched_at")
news_symbol = F.upper(F.get_json_object("raw_json", "$.record.symbol"))
news_article_id = F.coalesce(
    F.get_json_object(article_json, "$.id"),
    F.get_json_object(article_json, "$.url"),
)
contract_search = F.get_json_object("raw_json", "$.record.search_text")
contract_award_id = F.get_json_object(award_json, "$['Award ID']")

raw = raw.withColumn(
    "natural_key",
    F.when(
        F.col("folder_source").isin("sec_form4", "sec_13f", "sec_13dg", "sec_8k", "sec_s1"),
        sec_key,
    )
    .when(
        F.col("folder_source").isin("prices_eod", "prices_yf"),
        F.concat_ws("|", price_symbol, price_date),
    )
    .when(
        F.col("folder_source").isin("alpha_vantage", "etf_holdings"),
        F.concat_ws("|", av_function, av_context, av_fetched_at),
    )
    .when(
        F.col("folder_source") == "news",
        F.concat_ws("|", news_symbol, news_article_id),
    )
    .when(
        F.col("folder_source") == "contracts",
        F.concat_ws("|", contract_search, contract_award_id),
    ),
)

missing_natural_key = (
    F.when(
        F.col("folder_source").isin("sec_form4", "sec_13f", "sec_13dg", "sec_8k", "sec_s1"),
        sec_key.isNull(),
    )
    .when(
        F.col("folder_source").isin("prices_eod", "prices_yf"),
        price_symbol.isNull() | price_date.isNull(),
    )
    .when(
        F.col("folder_source").isin("alpha_vantage", "etf_holdings"),
        av_function.isNull() | av_context.isNull() | av_fetched_at.isNull(),
    )
    .when(
        F.col("folder_source") == "news",
        news_symbol.isNull() | news_article_id.isNull(),
    )
    .when(
        F.col("folder_source") == "contracts",
        contract_search.isNull() | contract_award_id.isNull(),
    )
    .otherwise(F.lit(True))
)

raw = (
    raw
    .withColumn("invalid_json", F.col("root_json").isNull())
    .withColumn(
        "missing_envelope_field",
        F.col("envelope_source").isNull()
        | F.col("schema_version").isNull()
        | F.col("batch_id").isNull()
        | F.col("ingest_ts_raw").isNull()
        | F.col("record_json").isNull(),
    )
    .withColumn("source_mismatch", F.col("envelope_source") != F.col("folder_source"))
    .withColumn("schema_mismatch", F.col("schema_version") != F.lit(expected_schema_version))
    .withColumn("invalid_ingest_ts", F.col("ingest_ts").isNull())
    .withColumn(
        "future_ingest_ts",
        F.col("ingest_ts") > F.expr(f"current_timestamp() + INTERVAL {max_future_minutes} MINUTES"),
    )
    .withColumn("batch_file_mismatch", F.col("batch_id") != F.col("file_batch_id"))
    .withColumn("missing_natural_key", missing_natural_key)
    .cache()
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- File, row, duplicate, and coverage metrics ---
file_metrics = (
    window_files
    .groupBy(F.col("folder_source").alias("source_id"))
    .agg(
        F.countDistinct("path").alias("files"),
        F.sum("length").alias("bytes"),
        F.sum(F.when(F.col("length") == 0, 1).otherwise(0)).alias("zero_byte_files"),
        F.countDistinct("folder_date").alias("partition_days"),
        F.min("folder_date").alias("first_partition"),
        F.max("folder_date").alias("last_partition"),
    )
)

row_metrics = (
    raw
    .groupBy(F.col("folder_source").alias("source_id"))
    .agg(
        F.count("*").alias("rows"),
        F.countDistinct("batch_id").alias("batches"),
        F.sum(F.col("invalid_json").cast("long")).alias("invalid_json"),
        F.sum(F.col("missing_envelope_field").cast("long")).alias("missing_envelope_fields"),
        F.sum(F.col("source_mismatch").cast("long")).alias("source_mismatches"),
        F.sum(F.col("schema_mismatch").cast("long")).alias("schema_mismatches"),
        F.sum(F.col("invalid_ingest_ts").cast("long")).alias("invalid_ingest_ts"),
        F.sum(F.col("future_ingest_ts").cast("long")).alias("future_ingest_ts"),
        F.sum(F.col("batch_file_mismatch").cast("long")).alias("batch_file_mismatches"),
        F.sum(F.col("missing_natural_key").cast("long")).alias("missing_natural_keys"),
    )
)

duplicate_key_groups = (
    raw
    .filter(
        F.col("batch_id").isNotNull()
        & F.col("natural_key").isNotNull()
        & ~F.col("missing_natural_key")
    )
    .groupBy("folder_source", "batch_id", "natural_key")
    .agg(
        F.count("*").alias("row_count"),
        F.countDistinct(F.sha2("record_json", 256)).alias("payload_variants"),
    )
    .filter(F.col("row_count") > 1)
    .cache()
)

duplicate_metrics = (
    duplicate_key_groups
    .groupBy(F.col("folder_source").alias("source_id"))
    .agg(
        F.sum(F.col("row_count") - 1).alias("duplicate_rows"),
        F.sum(
            F.when(F.col("payload_variants") == 1, F.col("row_count") - 1).otherwise(0)
        ).alias("exact_duplicate_rows"),
        F.sum(
            F.when(F.col("payload_variants") > 1, 1).otherwise(0)
        ).alias("conflicting_duplicate_keys"),
        F.sum(
            F.when(F.col("payload_variants") > 1, F.col("row_count") - 1).otherwise(0)
        ).alias("conflicting_duplicate_rows"),
    )
)

multi_batch_file_metrics = (
    raw
    .groupBy("folder_source", "_input_file")
    .agg(F.countDistinct("batch_id").alias("batch_count"))
    .filter(F.col("batch_count") != 1)
    .groupBy(F.col("folder_source").alias("source_id"))
    .agg(F.count("*").alias("files_with_invalid_batch_count"))
)

multi_file_batch_metrics = (
    raw
    .filter(F.col("batch_id").isNotNull())
    .groupBy("folder_source", "batch_id")
    .agg(F.countDistinct("_input_file").alias("file_count"))
    .filter(F.col("file_count") != 1)
    .groupBy(F.col("folder_source").alias("source_id"))
    .agg(F.count("*").alias("batches_in_multiple_files"))
)

partition_dates = window_files.select("folder_source", "folder_date").distinct()
partition_window = Window.partitionBy("folder_source").orderBy("folder_date")
gap_metrics = (
    partition_dates
    .withColumn("previous_date", F.lag("folder_date").over(partition_window))
    .withColumn(
        "gap_days",
        F.greatest(F.datediff("folder_date", "previous_date") - 1, F.lit(0)),
    )
    .groupBy(F.col("folder_source").alias("source_id"))
    .agg(F.coalesce(F.max("gap_days"), F.lit(0)).alias("largest_partition_gap_days"))
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Build the source-level gate result ---
source_rows = [(source, source in required_source_ids) for source in source_ids]
source_frame = spark.createDataFrame(source_rows, "source_id STRING, required BOOLEAN")

numeric_columns = [
    "files",
    "bytes",
    "zero_byte_files",
    "partition_days",
    "rows",
    "batches",
    "invalid_json",
    "missing_envelope_fields",
    "source_mismatches",
    "schema_mismatches",
    "invalid_ingest_ts",
    "future_ingest_ts",
    "batch_file_mismatches",
    "missing_natural_keys",
    "duplicate_rows",
    "exact_duplicate_rows",
    "conflicting_duplicate_keys",
    "conflicting_duplicate_rows",
    "files_with_invalid_batch_count",
    "batches_in_multiple_files",
    "largest_partition_gap_days",
]

health = (
    source_frame
    .join(file_metrics, "source_id", "left")
    .join(row_metrics, "source_id", "left")
    .join(duplicate_metrics, "source_id", "left")
    .join(multi_batch_file_metrics, "source_id", "left")
    .join(multi_file_batch_metrics, "source_id", "left")
    .join(gap_metrics, "source_id", "left")
    .fillna(0, subset=numeric_columns)
    .withColumn(
        "missing_calendar_days",
        F.lit((end_date - start_date).days + 1) - F.col("partition_days"),
    )
    .withColumn(
        "leading_gap_days",
        F.when(
            F.col("first_partition").isNotNull(),
            F.datediff(F.col("first_partition"), F.lit(from_date)),
        ),
    )
    .withColumn(
        "trailing_gap_days",
        F.when(
            F.col("last_partition").isNotNull(),
            F.datediff(F.lit(to_date), F.col("last_partition")),
        ),
    )
)

structural_error_total = sum(
    (F.col(column_name) for column_name in [
        "zero_byte_files",
        "invalid_json",
        "missing_envelope_fields",
        "source_mismatches",
        "schema_mismatches",
        "invalid_ingest_ts",
        "future_ingest_ts",
        "batch_file_mismatches",
        "missing_natural_keys",
        "conflicting_duplicate_keys",
        "files_with_invalid_batch_count",
        "batches_in_multiple_files",
    ]),
    F.lit(0),
)

health = (
    health
    .withColumn("structural_errors", structural_error_total)
    .withColumn(
        "gate_status",
        F.when(F.col("required") & ((F.col("files") == 0) | (F.col("rows") == 0)), F.lit("FAIL"))
        .when(F.col("structural_errors") > 0, F.lit("FAIL"))
        .when((F.col("files") == 0) | (F.col("rows") == 0), F.lit("SKIP"))
        .otherwise(F.lit("PASS")),
    )
    .withColumn(
        "coverage_note",
        F.when(F.col("trailing_gap_days") > 0, F.lit("CHECK_TRAILING_GAP"))
        .when(F.col("leading_gap_days") > 0, F.lit("CHECK_LEADING_GAP"))
        .when(F.col("missing_calendar_days") > 0, F.lit("CHECK_RUN_LOG"))
        .otherwise(F.lit("CONTIGUOUS")),
    )
    .select(
        "source_id",
        "required",
        "gate_status",
        "files",
        "rows",
        "batches",
        "bytes",
        "first_partition",
        "last_partition",
        "partition_days",
        "missing_calendar_days",
        "leading_gap_days",
        "trailing_gap_days",
        "largest_partition_gap_days",
        "coverage_note",
        "structural_errors",
        "zero_byte_files",
        "invalid_json",
        "missing_envelope_fields",
        "source_mismatches",
        "schema_mismatches",
        "invalid_ingest_ts",
        "future_ingest_ts",
        "batch_file_mismatches",
        "missing_natural_keys",
        "duplicate_rows",
        "exact_duplicate_rows",
        "conflicting_duplicate_keys",
        "conflicting_duplicate_rows",
        "files_with_invalid_batch_count",
        "batches_in_multiple_files",
    )
    .orderBy("source_id")
    .cache()
)

display(health)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Show defect samples and enforce the gate ---
issue_codes = F.concat_ws(
    ",",
    F.when(F.col("invalid_json"), F.lit("INVALID_JSON")),
    F.when(F.col("missing_envelope_field"), F.lit("MISSING_ENVELOPE_FIELD")),
    F.when(F.col("source_mismatch"), F.lit("SOURCE_MISMATCH")),
    F.when(F.col("schema_mismatch"), F.lit("SCHEMA_MISMATCH")),
    F.when(F.col("invalid_ingest_ts"), F.lit("INVALID_INGEST_TS")),
    F.when(F.col("future_ingest_ts"), F.lit("FUTURE_INGEST_TS")),
    F.when(F.col("batch_file_mismatch"), F.lit("BATCH_FILE_MISMATCH")),
    F.when(F.col("missing_natural_key"), F.lit("MISSING_NATURAL_KEY")),
)

issue_samples = (
    raw
    .withColumn("issue_codes", issue_codes)
    .filter(F.length("issue_codes") > 0)
    .select(
        "folder_source",
        "folder_date",
        "_input_file",
        "issue_codes",
        F.substring("raw_json", 1, 500).alias("raw_json_sample"),
    )
    .limit(100)
)
if not issue_samples.isEmpty():
    display(issue_samples)

conflicting_duplicate_samples = (
    duplicate_key_groups
    .filter(F.col("payload_variants") > 1)
    .select(
        F.col("folder_source").alias("source_id"),
        "batch_id",
        "natural_key",
        "row_count",
        "payload_variants",
    )
    .orderBy(F.desc("payload_variants"), F.desc("row_count"))
    .limit(100)
)
if not conflicting_duplicate_samples.isEmpty():
    display(conflicting_duplicate_samples)

failed_sources = [
    row.source_id
    for row in health.filter(F.col("gate_status") == "FAIL").select("source_id").collect()
]
health.unpersist()
duplicate_key_groups.unpersist()
raw.unpersist()
window_files.unpersist()

if failed_sources:
    raise RuntimeError(
        "BRONZE HEALTH FAILED for sources: "
        + ", ".join(failed_sources)
        + ". Inspect the health table and defect samples before running downstream notebooks."
    )

print("BRONZE STRUCTURAL HEALTH PASSED")
print(
    "Coverage notes are informational because successful empty connector runs do not create "
    "bronze files. Reconcile CHECK_RUN_LOG/CHECK_*_GAP against Cosmos runs/backfill manifests."
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
