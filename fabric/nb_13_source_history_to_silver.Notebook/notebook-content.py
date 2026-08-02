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

# Fabric Notebook: nb_13_source_history_to_silver
# Normalizes benchmark, Companyfacts, and N-PORT inputs to current PIT Silver tables.

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from datetime import date, datetime, timedelta, timezone
import hashlib
import json

from delta.tables import DeltaTable
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DateType,
    DecimalType,
    IntegerType,
    LongType,
    MapType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

REQUIRED_SOURCES = ("benchmark_prices", "sec_nport", "sec_companyfacts")
GOVERNED_COMPANYFACTS_CONCEPTS = (
    "Assets",
    "CashAndCashEquivalentsAtCarryingValue",
    "DebtCurrent",
    "Depreciation",
    "DepreciationAndAmortization",
    "DepreciationDepletionAndAmortization",
    "EntityCommonStockSharesOutstanding",
    "GrossProfit",
    "Liabilities",
    "LongTermDebtCurrent",
    "LongTermDebtNoncurrent",
    "AmortizationOfIntangibleAssets",
    "NetCashProvidedByUsedInOperatingActivities",
    "NetIncomeLoss",
    "OperatingIncomeLoss",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "StockholdersEquity",
)
_HASH_SEPARATOR = "\u001f"
_EMPTY_TIMESTAMP = datetime(1970, 1, 1, tzinfo=timezone.utc)
PIT_ORDER_VIOLATION_SQL = "event_date > knowledge_date"


def _require_table(table_name: str) -> None:
    if not spark.catalog.tableExists(table_name):
        raise RuntimeError(f"Required source-normalization table is missing: {table_name}")


def _read_bronze(source_id: str, first_day: date, last_day: date):
    path_pattern = rf"/bronze/{source_id}/(\d{{4}})/(\d{{2}})/(\d{{2}})/"
    bronze_files = (
        spark.read.format("binaryFile")
        .option("recursiveFileLookup", "true")
        .option("pathGlobFilter", "*.ndjson")
        .load(f"Files/bronze/{source_id}")
        .select("path")
        .withColumn("folder_year", F.regexp_extract("path", path_pattern, 1))
        .withColumn("folder_month", F.regexp_extract("path", path_pattern, 2))
        .withColumn("folder_day", F.regexp_extract("path", path_pattern, 3))
        .withColumn(
            "folder_date",
            F.to_date(F.concat_ws("-", "folder_year", "folder_month", "folder_day")),
        )
        .filter(F.col("folder_date").between(F.lit(first_day), F.lit(last_day)))
    )
    paths = [row.path for row in bronze_files.select("path").collect()]
    if not paths:
        raise RuntimeError(f"Required historical Bronze source is absent: {source_id}")
    return (
        spark.read.text(paths)
        .select(F.col("value").alias("raw_json"))
        .withColumn("expected_source_id", F.lit(source_id))
        .withColumn("source_id", F.get_json_object("raw_json", "$.source_id"))
        .withColumn("batch_id", F.get_json_object("raw_json", "$.batch_id"))
        .withColumn("schema_version", F.get_json_object("raw_json", "$.schema_version").cast(IntegerType()))
        .withColumn("watermark_from", F.get_json_object("raw_json", "$.watermark_from"))
        .withColumn("ingest_ts", F.to_timestamp(F.get_json_object("raw_json", "$.ingest_ts")))
        .withColumn("record_json", F.get_json_object("raw_json", "$.record"))
        .withColumn("source_record_hash", F.sha2("raw_json", 256))
        .withColumn(
            "stable_ingest_ts",
            F.coalesce(F.col("ingest_ts"), F.lit(_EMPTY_TIMESTAMP).cast(TimestampType())),
        )
        .cache()
    )


def _canonical_hash(columns: list):
    return F.sha2(
        F.concat_ws(
            _HASH_SEPARATOR,
            *[F.coalesce(column.cast(StringType()), F.lit("<null>")) for column in columns],
        ),
        256,
    )


def _insert_only(table_name: str, source_df, condition: str) -> None:
    if source_df.isEmpty():
        return
    (
        DeltaTable.forName(spark, table_name)
        .alias("t")
        .merge(source_df.alias("s"), condition)
        .whenNotMatchedInsertAll()
        .execute()
    )


def _insert_quarantine(table_name: str, source_df) -> None:
    if source_df.isEmpty():
        return
    _insert_only(table_name, source_df.dropDuplicates(["natural_key"]), "t.natural_key = s.natural_key")


for required_table in [
    "dim_security",
    "fact_market_daily",
    "fact_insider_txn",
    "silver_parse_errors",
    "silver_dq_quarantine",
    "silver_security_quarantine",
]:
    _require_table(required_table)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# PARAMETERS CELL ********************

start_date = ""
end_date = ""

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

end_date = str(end_date).strip() or date.today().isoformat()
start_date = str(start_date).strip() or (
    date.fromisoformat(end_date) - timedelta(days=7)
).isoformat()
parsed_start_date = date.fromisoformat(start_date)
parsed_end_date = date.fromisoformat(end_date)
if parsed_start_date > parsed_end_date:
    raise ValueError("start_date must not follow end_date")
if parsed_end_date > datetime.now(timezone.utc).date():
    raise ValueError("end_date cannot be in the future")

bronze = {
    source_id: _read_bronze(source_id, parsed_start_date, parsed_end_date)
    for source_id in REQUIRED_SOURCES
}
source_envelope_counts = {source_id: frame.count() for source_id, frame in bronze.items()}
missing_required_sources = [
    source_id for source_id, row_count in source_envelope_counts.items() if row_count == 0
]
if missing_required_sources:
    raise RuntimeError(
        "Required historical sources contain no Bronze envelopes: "
        + ", ".join(missing_required_sources)
    )
malformed_envelopes = None
for source_id, frame in bronze.items():
    invalid = frame.filter(
        F.col("source_id").isNull()
        | (F.col("source_id") != F.col("expected_source_id"))
        | F.col("batch_id").isNull()
        | F.col("schema_version").isNull()
        | (F.col("schema_version") != F.lit(1))
        | F.col("ingest_ts").isNull()
        | F.col("record_json").isNull()
    ).withColumn(
        "envelope_error",
        F.when(
            F.col("schema_version").isNotNull()
            & (F.col("schema_version") != F.lit(1)),
            F.lit("UNSUPPORTED_BRONZE_SCHEMA_VERSION"),
        ).otherwise(F.lit("MALFORMED_BRONZE_JSON")),
    )
    natural_key = F.concat_ws(
        ":", F.lit(source_id), F.col("envelope_error"), F.col("source_record_hash")
    )
    rows = invalid.select(
        natural_key.alias("natural_key"),
        F.lit(source_id).alias("source_id"),
        F.coalesce(F.col("batch_id"), F.col("source_record_hash")).alias("batch_id"),
        F.col("raw_json").alias("raw_record"),
        F.col("envelope_error").alias("error_msg"),
        F.col("stable_ingest_ts").alias("occurred_at"),
    )
    malformed_envelopes = rows if malformed_envelopes is None else malformed_envelopes.unionByName(rows)
_insert_quarantine("silver_parse_errors", malformed_envelopes)

valid_bronze = {
    source_id: frame.filter(
        (F.col("source_id") == F.col("expected_source_id"))
        & F.col("batch_id").isNotNull()
        & (F.col("schema_version") == F.lit(1))
        & F.col("ingest_ts").isNotNull()
        & F.col("record_json").isNotNull()
    )
    for source_id, frame in bronze.items()
}

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Benchmark observations: the observation itself is knowable on its market date.
benchmark_schema = StructType([
    StructField("symbol", StringType()),
    StructField("date", StringType()),
    StructField("open", StringType()),
    StructField("high", StringType()),
    StructField("low", StringType()),
    StructField("close", StringType()),
    StructField("adjusted_close", StringType()),
    StructField("volume", StringType()),
    StructField("dividend_amount", StringType()),
    StructField("split_coefficient", StringType()),
])
benchmark = (
    valid_bronze["benchmark_prices"]
    .withColumn("benchmark", F.from_json("record_json", benchmark_schema))
    .select(
        "source_id", "batch_id", "ingest_ts", "source_record_hash", "raw_json",
        F.upper(F.trim(F.col("benchmark.symbol"))).alias("benchmark_symbol"),
        F.col("benchmark.date").alias("observation_date"),
        F.col("benchmark.open").cast(DecimalType(20, 8)).alias("open"),
        F.col("benchmark.high").cast(DecimalType(20, 8)).alias("high"),
        F.col("benchmark.low").cast(DecimalType(20, 8)).alias("low"),
        F.col("benchmark.close").cast(DecimalType(20, 8)).alias("close"),
        F.col("benchmark.adjusted_close").cast(DecimalType(20, 8)).alias("adjusted_close"),
        F.col("benchmark.volume").cast(LongType()).alias("volume"),
        F.col("benchmark.dividend_amount").cast(DecimalType(20, 8)).alias("dividend_amount"),
        F.col("benchmark.split_coefficient").cast(DecimalType(20, 8)).alias("split_coefficient"),
        F.col("benchmark.date").alias("event_date_raw"),
        F.col("benchmark.date").alias("knowledge_date_raw"),
    )
    .withColumn("event_date", F.to_date("event_date_raw"))
    .withColumn("knowledge_date", F.to_date("knowledge_date_raw"))
)
benchmark_failures = benchmark.filter(
    F.col("benchmark_symbol").isNull()
    | F.col("event_date").isNull()
    | F.col("adjusted_close").isNull()
    | (F.col("adjusted_close") <= 0)
    | (F.col("event_date") < F.lit(parsed_start_date))
    | (F.col("event_date") > F.lit(parsed_end_date))
)
benchmark_failure_key = F.concat_ws(
    ":", "source_id", F.lit("INVALID_BENCHMARK_PRICE"), "source_record_hash"
)
_insert_quarantine(
    "silver_dq_quarantine",
    benchmark_failures.select(
        F.sha2(benchmark_failure_key, 256).alias("quarantine_id"),
        benchmark_failure_key.alias("natural_key"), "source_id", "batch_id",
        F.col("raw_json").alias("raw_record"),
        F.lit("INVALID_BENCHMARK_PRICE").alias("dq_rule"),
        F.col("ingest_ts").alias("quarantined_at"),
    ),
)
benchmark_pass = (
    benchmark.join(benchmark_failures.select("source_record_hash"), "source_record_hash", "left_anti")
    .withColumn(
        "canonical_row_hash",
        _canonical_hash([
            F.col("benchmark_symbol"), F.col("event_date"), F.col("open"), F.col("high"),
            F.col("low"), F.col("close"), F.col("adjusted_close"), F.col("volume"),
            F.col("dividend_amount"), F.col("split_coefficient"), F.col("knowledge_date"),
        ]),
    )
    .withColumn(
        "benchmark_revision_id",
        _canonical_hash([
            F.col("source_id"), F.col("batch_id"), F.col("benchmark_symbol"),
            F.col("event_date"), F.col("canonical_row_hash"),
        ]),
    )
    .dropDuplicates(["benchmark_revision_id"])
    .select(
        "benchmark_revision_id", "benchmark_symbol", "open", "high", "low", "close",
        "adjusted_close", "volume", "dividend_amount", "split_coefficient",
        "canonical_row_hash", "event_date", "knowledge_date", "source_id", "batch_id",
        "source_record_hash", "ingest_ts", F.col("ingest_ts").alias("revision_loaded_at"),
    )
)
spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_benchmark_prices (
        benchmark_revision_id STRING NOT NULL,
        benchmark_symbol STRING NOT NULL,
        open DECIMAL(20,8), high DECIMAL(20,8), low DECIMAL(20,8), close DECIMAL(20,8),
        adjusted_close DECIMAL(20,8) NOT NULL, volume BIGINT,
        dividend_amount DECIMAL(20,8), split_coefficient DECIMAL(20,8),
        canonical_row_hash STRING NOT NULL,
        event_date DATE NOT NULL, knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL, batch_id STRING NOT NULL,
        source_record_hash STRING NOT NULL, ingest_ts TIMESTAMP NOT NULL,
        revision_loaded_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
_insert_only(
    "silver_benchmark_prices", benchmark_pass,
    "t.benchmark_revision_id = s.benchmark_revision_id",
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Companyfacts units are exploded only for governed production fundamental concepts.
companyfact_unit_schema = StructType([
    StructField("start", StringType()),
    StructField("end", StringType()),
    StructField("val", StringType()),
    StructField("accn", StringType()),
    StructField("fy", StringType()),
    StructField("fp", StringType()),
    StructField("form", StringType()),
    StructField("filed", StringType()),
    StructField("frame", StringType()),
])
companyfact_concept_schema = StructType([
    StructField("label", StringType()),
    StructField("description", StringType()),
    StructField("units", MapType(StringType(), ArrayType(companyfact_unit_schema))),
])
companyfacts_payload_schema = StructType([
    StructField("cik", StringType()),
    StructField("entityName", StringType()),
    StructField(
        "facts",
        MapType(StringType(), MapType(StringType(), companyfact_concept_schema)),
    ),
])
companyfacts_record_schema = StructType([
    StructField("fetched_at", StringType()),
    StructField("status", StringType()),
    StructField("context", StructType([
        StructField("symbol", StringType()),
        StructField("cik", StringType()),
    ])),
    StructField("payload", companyfacts_payload_schema),
])
companyfacts_envelopes = (
    valid_bronze["sec_companyfacts"]
    .withColumn("company", F.from_json("record_json", companyfacts_record_schema))
    .select(
        "source_id", "batch_id", "ingest_ts", "source_record_hash", "raw_json",
        F.col("company.status").alias("record_status"),
        F.coalesce(F.col("company.payload.cik"), F.col("company.context.cik")).alias("cik"),
        F.col("company.context.symbol").alias("source_symbol"),
        F.col("company.payload.entityName").alias("entity_name"),
        F.col("company.payload.facts").alias("facts"),
    )
)
companyfacts_parse_failures = companyfacts_envelopes.filter(
    (F.col("record_status") != F.lit("ok")) | F.col("cik").isNull() | F.col("facts").isNull()
)
companyfacts_parse_key = F.concat_ws(
    ":", "source_id", F.lit("INVALID_COMPANYFACT_PAYLOAD"), "source_record_hash"
)
_insert_quarantine(
    "silver_parse_errors",
    companyfacts_parse_failures.select(
        companyfacts_parse_key.alias("natural_key"), "source_id", "batch_id",
        F.col("raw_json").alias("raw_record"),
        F.lit("INVALID_COMPANYFACT_PAYLOAD").alias("error_msg"),
        F.col("ingest_ts").alias("occurred_at"),
    ),
)
companyfacts_exploded = (
    companyfacts_envelopes.join(
        companyfacts_parse_failures.select("source_record_hash"), "source_record_hash", "left_anti"
    )
    .select("*", F.explode("facts").alias("taxonomy", "concepts"))
    .select("*", F.explode("concepts").alias("concept", "concept_payload"))
    .filter(F.col("concept").isin(*GOVERNED_COMPANYFACTS_CONCEPTS))
    .select("*", F.explode(F.col("concept_payload.units")).alias("unit", "unit_facts"))
    .select("*", F.explode("unit_facts").alias("fact"))
    .select(
        "source_id", "batch_id", "ingest_ts", "source_record_hash", "raw_json",
        F.regexp_replace(F.trim("cik"), "^0+", "").alias("cik_norm"),
        "source_symbol", "entity_name", "taxonomy", "concept", "unit",
        F.col("fact.accn").alias("accession_no"),
        F.col("fact.form").alias("filing_form"),
        F.col("fact.filed").alias("filed_date"),
        F.col("fact.end").alias("period_end_date"),
        F.col("fact.start").alias("period_start_date"),
        F.col("fact.val").alias("raw_value"),
        F.col("fact.val").cast(DecimalType(38, 10)).alias("fact_value"),
        F.col("fact.fy").cast(IntegerType()).alias("fiscal_year"),
        F.col("fact.fp").alias("fiscal_period"),
        F.col("fact.frame").alias("frame"),
        F.col("fact.end").alias("event_date_raw"),
        F.col("fact.filed").alias("knowledge_date_raw"),
    )
    .withColumn("event_date", F.to_date("event_date_raw"))
    .withColumn("knowledge_date", F.to_date("knowledge_date_raw"))
    .withColumn(
        "companyfact_row_key",
        _canonical_hash([
            F.col("source_record_hash"), F.col("accession_no"), F.col("taxonomy"),
            F.col("concept"), F.col("unit"), F.col("period_start_date"),
            F.col("period_end_date"), F.col("frame"), F.col("raw_value"),
        ]),
    )
)
companyfacts_failures = companyfacts_exploded.filter(
    F.col("cik_norm").isNull()
    | F.col("accession_no").isNull()
    | F.col("event_date").isNull()
    | F.col("knowledge_date").isNull()
    | F.col("fact_value").isNull()
    | F.expr(PIT_ORDER_VIOLATION_SQL)
)
companyfact_failure_key = F.concat_ws(
    ":", "source_id", F.lit("INVALID_COMPANYFACT"),
    F.coalesce("accession_no", "source_record_hash"), F.coalesce("concept", F.lit("missing")),
    F.coalesce("unit", F.lit("missing")), F.coalesce("period_end_date", F.lit("missing")),
)
_insert_quarantine(
    "silver_dq_quarantine",
    companyfacts_failures.select(
        F.sha2(companyfact_failure_key, 256).alias("quarantine_id"),
        companyfact_failure_key.alias("natural_key"), "source_id", "batch_id",
        F.col("raw_json").alias("raw_record"), F.lit("INVALID_COMPANYFACT").alias("dq_rule"),
        F.col("ingest_ts").alias("quarantined_at"),
    ),
)
companyfacts_pass = companyfacts_exploded.join(
    companyfacts_failures.select("companyfact_row_key"),
    ["companyfact_row_key"],
    "left_anti",
).filter(
    (F.col("event_date") >= F.lit(parsed_start_date))
    & (F.col("event_date") <= F.lit(parsed_end_date))
    & (F.col("knowledge_date") <= F.lit(parsed_end_date))
)
security_cik_versions = spark.table("dim_security").select(
    F.col("security_sk").alias("cik_security_sk"),
    F.regexp_replace(F.trim("cik"), "^0+", "").alias("dim_cik_norm"),
    F.col("valid_from").alias("cik_valid_from"),
    F.col("valid_to").alias("cik_valid_to"),
)
companyfact_candidates = (
    companyfacts_pass.alias("f")
    .join(
        security_cik_versions.alias("s"),
        (F.col("f.cik_norm") == F.col("s.dim_cik_norm"))
        & (F.col("f.event_date") >= F.col("s.cik_valid_from"))
        & (F.col("s.cik_valid_to").isNull() | (F.col("f.event_date") < F.col("s.cik_valid_to"))),
        "left",
    )
    .withColumn("companyfact_candidate_key", F.col("f.companyfact_row_key"))
    .withColumn(
        "security_match_count",
        F.count("cik_security_sk").over(Window.partitionBy("companyfact_candidate_key")),
    )
    .withColumn(
        "security_sk",
        F.when(F.col("security_match_count") == 1, F.col("cik_security_sk")),
    )
)
companyfact_unresolved = companyfact_candidates.filter(F.col("security_sk").isNull())
companyfact_security_key = F.concat_ws(
    ":", "source_id", F.lit("UNRESOLVED_COMPANYFACT_SECURITY"),
    "companyfact_candidate_key", "batch_id",
)
_insert_quarantine(
    "silver_security_quarantine",
    companyfact_unresolved.select(
        F.sha2(companyfact_security_key, 256).alias("quarantine_id"),
        companyfact_security_key.alias("natural_key"), "source_id",
        F.col("cik_norm").alias("raw_identifier"),
        F.lit("UNRESOLVED_COMPANYFACT_SECURITY").alias("reason"),
        F.lit("No unique dim_security CIK version matched the Companyfacts period end").alias("details"),
        "event_date", "knowledge_date", "batch_id",
        F.col("ingest_ts").alias("quarantined_at"),
    ),
)
companyfacts_silver = (
    companyfact_candidates.filter(F.col("security_sk").isNotNull())
    .withColumn(
        "canonical_row_hash",
        _canonical_hash([
            F.col("security_sk"), F.col("cik_norm"), F.col("taxonomy"), F.col("concept"),
            F.col("unit"), F.col("fact_value"), F.col("accession_no"), F.col("filing_form"),
            F.col("filed_date"), F.col("period_start_date"), F.col("period_end_date"),
            F.col("fiscal_year"), F.col("fiscal_period"), F.col("frame"),
        ]),
    )
    .withColumn(
        "companyfact_revision_id",
        _canonical_hash([
            F.col("source_id"), F.col("accession_no"),
            F.col("taxonomy"), F.col("concept"), F.col("unit"),
            F.col("period_start_date"), F.col("period_end_date"), F.col("frame"),
            F.col("canonical_row_hash"),
        ]),
    )
    .dropDuplicates(["companyfact_revision_id"])
    .select(
        "companyfact_revision_id", "security_sk", F.col("cik_norm").alias("cik"),
        "source_symbol", "entity_name", "taxonomy", "concept", "unit", "fact_value",
        "raw_value", "accession_no", "filing_form", "filed_date", "period_end_date",
        "period_start_date", "fiscal_year", "fiscal_period", "frame", "canonical_row_hash",
        "event_date", "knowledge_date", "source_id", "batch_id", "source_record_hash",
        "ingest_ts", F.col("ingest_ts").alias("revision_loaded_at"),
    )
)
spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_companyfacts (
        companyfact_revision_id STRING NOT NULL,
        security_sk BIGINT NOT NULL, cik STRING NOT NULL, source_symbol STRING,
        entity_name STRING, taxonomy STRING NOT NULL, concept STRING NOT NULL,
        unit STRING NOT NULL, fact_value DECIMAL(38,10) NOT NULL, raw_value STRING NOT NULL,
        accession_no STRING NOT NULL, filing_form STRING, filed_date STRING NOT NULL,
        period_end_date STRING NOT NULL, period_start_date STRING,
        fiscal_year INT, fiscal_period STRING, frame STRING,
        canonical_row_hash STRING NOT NULL,
        event_date DATE NOT NULL, knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL, batch_id STRING NOT NULL,
        source_record_hash STRING NOT NULL, ingest_ts TIMESTAMP NOT NULL,
        revision_loaded_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
_insert_only(
    "silver_companyfacts", companyfacts_silver,
    "t.companyfact_revision_id = s.companyfact_revision_id",
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# N-PORT submissions retain the SEC report and acceptance dates without backdating.
nport_holding_schema = StructType([
    StructField("name", StringType()),
    StructField("title", StringType()),
    StructField("cusip", StringType()),
    StructField("identifiers", ArrayType(MapType(StringType(), StringType()))),
    StructField("balance", StringType()),
    StructField("units", StringType()),
    StructField("currency", StringType()),
    StructField("value_usd", StringType()),
    StructField("percentage", StringType()),
    StructField("lei", StringType()),
    StructField("payoff_profile", StringType()),
    StructField("asset_category", StringType()),
    StructField("issuer_category", StringType()),
    StructField("investment_country", StringType()),
    StructField("is_restricted_security", StringType()),
    StructField("fair_value_level", StringType()),
])
nport_record_schema = StructType([
    StructField("status", StringType()),
    StructField("symbol", StringType()),
    StructField("cik", StringType()),
    StructField("series_id", StringType()),
    StructField("class_id", StringType()),
    StructField("filed_class_ids", ArrayType(StringType())),
    StructField("form", StringType()),
    StructField("report_date", StringType()),
    StructField("event_date", StringType()),
    StructField("filing_date", StringType()),
    StructField("acceptance_datetime", StringType()),
    StructField("knowledge_date", StringType()),
    StructField("accession_no", StringType()),
    StructField("primary_document", StringType()),
    StructField("primary_document_url", StringType()),
    StructField("submissions_file", StringType()),
    StructField("holdings", ArrayType(nport_holding_schema)),
])
nport = (
    valid_bronze["sec_nport"]
    .withColumn("nport", F.from_json("record_json", nport_record_schema))
    .select(
        "source_id", "batch_id", "ingest_ts", "source_record_hash", "raw_json",
        F.col("nport.status").alias("record_status"),
        F.upper(F.trim(F.col("nport.symbol"))).alias("etf_symbol"),
        F.regexp_replace(F.trim(F.col("nport.cik")), "^0+", "").alias("registrant_cik"),
        F.col("nport.series_id").alias("series_id"),
        F.col("nport.class_id").alias("class_id"),
        F.col("nport.filed_class_ids").alias("filed_class_ids"),
        F.col("nport.form").alias("filing_form"),
        F.col("nport.report_date").alias("report_date"),
        F.col("nport.filing_date").alias("filing_date"),
        F.col("nport.acceptance_datetime").alias("acceptance_datetime"),
        F.col("nport.accession_no").alias("accession_no"),
        F.col("nport.primary_document").alias("primary_document"),
        F.col("nport.primary_document_url").alias("primary_document_url"),
        F.col("nport.submissions_file").alias("submissions_file"),
        F.col("nport.holdings").alias("holdings"),
        F.col("nport.report_date").alias("event_date_raw"),
        F.col("nport.acceptance_datetime").alias("knowledge_date_raw"),
    )
    .withColumn("event_date", F.to_date("event_date_raw"))
    .withColumn("knowledge_date", F.to_date("knowledge_date_raw"))
)
nport_matched = nport.filter(
    F.lower(F.trim("record_status")) == F.lit("matched")
)
nport_failures = nport_matched.filter(
    F.col("etf_symbol").isNull()
    | F.col("accession_no").isNull()
    | ~F.col("filing_form").isin("NPORT-P", "NPORT-P/A")
    | F.col("event_date").isNull()
    | F.col("knowledge_date").isNull()
    | F.expr(PIT_ORDER_VIOLATION_SQL)
    | (F.col("event_date") < F.lit(parsed_start_date))
    | (F.col("event_date") > F.lit(parsed_end_date))
    | (F.col("knowledge_date") > F.lit(parsed_end_date))
)
nport_failure_key = F.concat_ws(
    ":", "source_id", F.lit("INVALID_NPORT_SUBMISSION"),
    F.coalesce("accession_no", "source_record_hash"),
)
_insert_quarantine(
    "silver_dq_quarantine",
    nport_failures.select(
        F.sha2(nport_failure_key, 256).alias("quarantine_id"),
        nport_failure_key.alias("natural_key"), "source_id", "batch_id",
        F.col("raw_json").alias("raw_record"),
        F.lit("INVALID_NPORT_SUBMISSION").alias("dq_rule"),
        F.col("ingest_ts").alias("quarantined_at"),
    ),
)
nport_pass = nport_matched.join(
    nport_failures.select("source_record_hash"), "source_record_hash", "left_anti"
)
nport_submissions = (
    nport_pass
    .withColumn(
        "canonical_row_hash",
        _canonical_hash([
            F.col("etf_symbol"), F.col("registrant_cik"), F.col("series_id"),
            F.col("class_id"), F.col("filing_form"), F.col("report_date"),
            F.col("filing_date"), F.col("acceptance_datetime"), F.col("accession_no"),
            F.col("primary_document_url"), F.size("holdings"),
        ]),
    )
    .withColumn(
        "nport_submission_revision_id",
        _canonical_hash([
            F.col("source_id"), F.col("batch_id"), F.col("accession_no"),
            F.col("canonical_row_hash"),
        ]),
    )
    .dropDuplicates(["nport_submission_revision_id"])
    .select(
        "nport_submission_revision_id", "etf_symbol", "registrant_cik", "series_id",
        "class_id", "filed_class_ids", "filing_form", "report_date", "filing_date",
        "acceptance_datetime", "accession_no", "primary_document", "primary_document_url",
        "submissions_file", F.size("holdings").alias("holding_count"), "canonical_row_hash",
        "event_date", "knowledge_date", "source_id", "batch_id", "source_record_hash",
        "ingest_ts", F.col("ingest_ts").alias("revision_loaded_at"),
    )
)
spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_nport_submission (
        nport_submission_revision_id STRING NOT NULL,
        etf_symbol STRING NOT NULL, registrant_cik STRING, series_id STRING,
        class_id STRING, filed_class_ids ARRAY<STRING>, filing_form STRING NOT NULL,
        report_date STRING NOT NULL, filing_date STRING, acceptance_datetime STRING NOT NULL,
        accession_no STRING NOT NULL, primary_document STRING,
        primary_document_url STRING, submissions_file STRING, holding_count INT NOT NULL,
        canonical_row_hash STRING NOT NULL,
        event_date DATE NOT NULL, knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL, batch_id STRING NOT NULL,
        source_record_hash STRING NOT NULL, ingest_ts TIMESTAMP NOT NULL,
        revision_loaded_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
_insert_only(
    "silver_nport_submission", nport_submissions,
    "t.nport_submission_revision_id = s.nport_submission_revision_id",
)


def _nport_identifier_value(identifier_type: str):
    direct_key = identifier_type.lower()
    return F.expr(f"""
        element_at(
            filter(
                transform(
                    identifiers,
                    identifier -> coalesce(
                        element_at(identifier, '{direct_key}'),
                        CASE
                            WHEN upper(coalesce(
                                element_at(identifier, 'type'),
                                element_at(identifier, 'identifier_type'),
                                element_at(identifier, 'idType')
                            )) = '{identifier_type}'
                            THEN coalesce(
                                element_at(identifier, 'value'),
                                element_at(identifier, 'identifier_value'),
                                element_at(identifier, 'idValue')
                            )
                        END
                    )
                ),
                identifier_value -> identifier_value IS NOT NULL
                    AND length(trim(identifier_value)) > 0
            ),
            1
        )
    """)


nport_holdings_raw = (
    nport_pass
    .select("*", F.posexplode("holdings").alias("holding_ordinal", "holding"))
    .select(
        "source_id", "batch_id", "ingest_ts", "source_record_hash", "raw_json",
        "etf_symbol", "accession_no", "event_date", "knowledge_date", "holding_ordinal",
        F.trim(F.col("holding.name")).alias("issuer_name"),
        F.trim(F.col("holding.title")).alias("security_title"),
        F.upper(F.regexp_replace(F.trim(F.col("holding.cusip")), "[^A-Za-z0-9]", "")).alias("cusip_norm"),
        F.col("holding.identifiers").alias("identifiers"),
        F.col("holding.balance").cast(DecimalType(38, 10)).alias("balance"),
        F.col("holding.units").alias("units"),
        F.upper(F.trim(F.col("holding.currency"))).alias("currency"),
        F.col("holding.value_usd").cast(DecimalType(38, 10)).alias("value_usd"),
        F.col("holding.percentage").cast(DecimalType(18, 10)).alias("percentage"),
        F.col("holding.lei").alias("lei"),
        F.col("holding.payoff_profile").alias("payoff_profile"),
        F.col("holding.asset_category").alias("asset_category"),
        F.col("holding.issuer_category").alias("issuer_category"),
        F.col("holding.investment_country").alias("investment_country"),
        F.col("holding.is_restricted_security").alias("is_restricted_security"),
        F.col("holding.fair_value_level").alias("fair_value_level"),
    )
    .withColumn(
        "ticker_norm",
        F.upper(F.regexp_replace(F.trim(_nport_identifier_value("TICKER")), "\\s+", "")),
    )
    .withColumn(
        "cusip_norm",
        F.coalesce(
            F.col("cusip_norm"),
            F.upper(F.regexp_replace(
                F.trim(_nport_identifier_value("CUSIP")), "[^A-Za-z0-9]", ""
            )),
        ),
    )
    .withColumn(
        "isin_norm",
        F.upper(F.regexp_replace(F.trim(_nport_identifier_value("ISIN")), "[^A-Za-z0-9]", "")),
    )
)
nport_holding_failures = nport_holdings_raw.filter(
    F.col("issuer_name").isNull()
    | F.col("balance").isNull()
    | (F.col("balance") < 0)
    | F.col("value_usd").isNull()
    | (F.col("value_usd") < 0)
)
nport_holding_failure_key = F.concat_ws(
    ":", "source_id", F.lit("INVALID_NPORT_HOLDING"), "accession_no",
    F.col("holding_ordinal").cast(StringType()), "batch_id",
)
_insert_quarantine(
    "silver_dq_quarantine",
    nport_holding_failures.select(
        F.sha2(nport_holding_failure_key, 256).alias("quarantine_id"),
        nport_holding_failure_key.alias("natural_key"), "source_id", "batch_id",
        F.col("raw_json").alias("raw_record"),
        F.lit("INVALID_NPORT_HOLDING").alias("dq_rule"),
        F.col("ingest_ts").alias("quarantined_at"),
    ),
)
nport_holding_pass = nport_holdings_raw.join(
    nport_holding_failures.select("source_record_hash", "holding_ordinal"),
    ["source_record_hash", "holding_ordinal"], "left_anti",
).withColumn(
    "holding_candidate_key",
    _canonical_hash([
        F.col("source_record_hash"), F.col("accession_no"), F.col("holding_ordinal"),
        F.col("issuer_name"), F.col("security_title"), F.col("ticker_norm"),
        F.col("isin_norm"), F.col("cusip_norm"),
    ]),
)

# Direct identifiers resolve against PIT dim_security versions before bridge or name fallback.
security_direct_versions = (
    spark.table("dim_security")
    .select(
        F.col("security_sk").alias("direct_security_sk"),
        F.col("ticker").alias("dim_ticker"),
        F.col("isin").alias("dim_isin"),
        F.col("valid_from").alias("direct_valid_from"),
        F.col("valid_to").alias("direct_valid_to"),
    )
    .withColumn("dim_ticker_norm", F.upper(F.trim("dim_ticker")))
    .withColumn(
        "dim_isin_norm",
        F.upper(F.regexp_replace(F.trim("dim_isin"), "[^A-Za-z0-9]", "")),
    )
)
isin_candidates = nport_holding_pass.alias("h").join(
    security_direct_versions.alias("s"),
    F.col("h.isin_norm").isNotNull()
    & (F.length(F.col("h.isin_norm")) == 12)
    & (F.col("h.isin_norm") == F.col("s.dim_isin_norm"))
    & (F.col("h.event_date") >= F.col("s.direct_valid_from"))
    & (
        F.col("s.direct_valid_to").isNull()
        | (F.col("h.event_date") < F.col("s.direct_valid_to"))
    ),
    "left",
)
isin_window = Window.partitionBy("holding_candidate_key")
isin_resolved = (
    isin_candidates
    .withColumn("isin_match_count", F.count("direct_security_sk").over(isin_window))
    .withColumn(
        "isin_resolved_security_sk",
        F.when(F.col("isin_match_count") == 1, F.col("direct_security_sk")),
    )
    .dropDuplicates(["holding_candidate_key"])
)
ticker_candidates = isin_resolved.alias("h").join(
    security_direct_versions.select(
        F.col("direct_security_sk").alias("ticker_security_sk"),
        "dim_ticker_norm",
        F.col("direct_valid_from").alias("ticker_valid_from"),
        F.col("direct_valid_to").alias("ticker_valid_to"),
    ).alias("s"),
    (F.col("h.isin_match_count") == 0)
    & F.col("h.ticker_norm").isNotNull()
    & (F.length(F.col("h.ticker_norm")) > 0)
    & (F.col("h.ticker_norm") == F.col("s.dim_ticker_norm"))
    & (F.col("h.event_date") >= F.col("s.ticker_valid_from"))
    & (
        F.col("s.ticker_valid_to").isNull()
        | (F.col("h.event_date") < F.col("s.ticker_valid_to"))
    ),
    "left",
)
ticker_window = Window.partitionBy("holding_candidate_key")
direct_resolved = (
    ticker_candidates
    .withColumn("ticker_match_count", F.count("ticker_security_sk").over(ticker_window))
    .withColumn(
        "ticker_resolved_security_sk",
        F.when(F.col("ticker_match_count") == 1, F.col("ticker_security_sk")),
    )
    .withColumn(
        "direct_match_count",
        F.when(F.col("isin_match_count") > 0, F.col("isin_match_count"))
        .otherwise(F.col("ticker_match_count")),
    )
    .withColumn(
        "direct_resolved_security_sk",
        F.coalesce(F.col("isin_resolved_security_sk"), F.col("ticker_resolved_security_sk")),
    )
    .withColumn(
        "direct_resolution_method",
        F.when(F.col("isin_resolved_security_sk").isNotNull(), F.lit("exact_isin_pit"))
        .when(F.col("ticker_resolved_security_sk").isNotNull(), F.lit("exact_ticker_pit")),
    )
    .dropDuplicates(["holding_candidate_key"])
)

# CUSIP resolves only through the maintained identifier bridge when direct identifiers do not match.
if spark.catalog.tableExists("dim_security_identifier"):
    identifier_columns = set(spark.table("dim_security_identifier").columns)
    required_identifier_columns = {
        "security_sk", "identifier_type", "identifier_value", "valid_from", "valid_to",
    }
    missing_identifier_columns = required_identifier_columns - identifier_columns
    if missing_identifier_columns:
        raise RuntimeError(
            "dim_security_identifier is present but missing maintained bridge columns: "
            + ", ".join(sorted(missing_identifier_columns))
        )
    identifier_bridge = (
        spark.table("dim_security_identifier")
        .filter(F.upper(F.trim("identifier_type")) == F.lit("CUSIP"))
        .select(
            F.col("security_sk").alias("cusip_security_sk"),
            F.upper(F.regexp_replace(F.trim("identifier_value"), "[^A-Za-z0-9]", "")).alias("bridge_cusip_norm"),
            F.col("valid_from").alias("bridge_valid_from"),
            F.col("valid_to").alias("bridge_valid_to"),
        )
    )
    cusip_candidates = direct_resolved.alias("h").join(
        identifier_bridge.alias("i"),
        (F.col("h.direct_match_count") == 0)
        & (F.col("h.cusip_norm") == F.col("i.bridge_cusip_norm"))
        & (F.length(F.col("h.cusip_norm")) == 9)
        & (F.col("h.event_date") >= F.col("i.bridge_valid_from"))
        & (F.col("i.bridge_valid_to").isNull() | (F.col("h.event_date") < F.col("i.bridge_valid_to"))),
        "left",
    )
else:
    cusip_candidates = direct_resolved.withColumn(
        "cusip_security_sk", F.lit(None).cast(LongType())
    )

cusip_window = Window.partitionBy("holding_candidate_key")
cusip_resolved = (
    cusip_candidates
    .withColumn("cusip_match_count", F.count("cusip_security_sk").over(cusip_window))
    .withColumn(
        "cusip_resolved_security_sk",
        F.when(F.col("cusip_match_count") == 1, F.col("cusip_security_sk")),
    )
    .dropDuplicates(["holding_candidate_key"])
)
security_name_versions = (
    spark.table("dim_security")
    .withColumn(
        "dim_issuer_name_norm",
        F.upper(F.regexp_replace(F.trim("company_name"), "[^A-Za-z0-9]", "")),
    )
    .select(
        F.col("security_sk").alias("name_security_sk"), "dim_issuer_name_norm",
        F.col("valid_from").alias("name_valid_from"),
        F.col("valid_to").alias("name_valid_to"),
    )
)
name_candidates = (
    cusip_resolved
    .withColumn(
        "issuer_name_norm",
        F.upper(F.regexp_replace(F.trim("issuer_name"), "[^A-Za-z0-9]", "")),
    )
    .join(
        security_name_versions,
        (F.col("direct_match_count") == 0)
        & (F.col("cusip_match_count") == 0)
        & (F.length(F.col("issuer_name_norm")) > 0)
        & (F.col("issuer_name_norm") == F.col("dim_issuer_name_norm"))
        & (F.col("event_date") >= F.col("name_valid_from"))
        & (F.col("name_valid_to").isNull() | (F.col("event_date") < F.col("name_valid_to"))),
        "left",
    )
    .withColumn(
        "security_match_count",
        F.count("name_security_sk").over(Window.partitionBy("holding_candidate_key")),
    )
    .withColumn(
        "security_sk",
        F.coalesce(
            F.col("direct_resolved_security_sk"),
            F.col("cusip_resolved_security_sk"),
            F.when(F.col("security_match_count") == 1, F.col("name_security_sk")),
        ),
    )
    .withColumn(
        "resolution_method",
        F.when(F.col("direct_resolved_security_sk").isNotNull(), F.col("direct_resolution_method"))
        .when(F.col("cusip_resolved_security_sk").isNotNull(), F.lit("exact_cusip_bridge_pit"))
        .when(F.col("security_sk").isNotNull(), F.lit("exact_unique_issuer_name_pit")),
    )
    .dropDuplicates(["holding_candidate_key"])
)
unresolved_holdings = (
    name_candidates.filter(F.col("security_sk").isNull())
    .withColumn(
        "resolution_failure_reason",
        F.when(
            (F.col("direct_match_count") > 1)
            | (F.col("cusip_match_count") > 1)
            | (F.col("security_match_count") > 1),
            F.lit("AMBIGUOUS_NPORT_SECURITY"),
        ).otherwise(F.lit("UNRESOLVED_NPORT_SECURITY")),
    )
)
unresolved_key = F.concat_ws(
    ":", "source_id", F.col("resolution_failure_reason"),
    "holding_candidate_key", "batch_id",
)
_insert_quarantine(
    "silver_security_quarantine",
    unresolved_holdings.select(
        F.sha2(unresolved_key, 256).alias("quarantine_id"),
        unresolved_key.alias("natural_key"), "source_id",
        F.coalesce("isin_norm", "ticker_norm", "cusip_norm", "issuer_name").alias("raw_identifier"),
        F.col("resolution_failure_reason").alias("reason"),
        F.when(
            F.col("resolution_failure_reason") == F.lit("AMBIGUOUS_NPORT_SECURITY"),
            F.lit("Multiple PIT identifier or normalized issuer-name matches"),
        ).otherwise(F.lit("No PIT identifier or normalized issuer-name match")).alias("details"),
        "event_date", "knowledge_date", "batch_id",
        F.col("ingest_ts").alias("quarantined_at"),
    ),
)
resolved_holdings = (
    name_candidates.filter(F.col("security_sk").isNotNull())
    .withColumn(
        "canonical_row_hash",
        _canonical_hash([
            F.col("etf_symbol"), F.col("accession_no"), F.col("holding_ordinal"),
            F.col("security_sk"), F.col("issuer_name"), F.col("security_title"),
            F.col("ticker_norm"), F.col("isin_norm"), F.col("cusip_norm"),
            F.col("balance"), F.col("units"), F.col("currency"),
            F.col("value_usd"), F.col("percentage"), F.col("resolution_method"),
            F.col("event_date"), F.col("knowledge_date"),
        ]),
    )
    .withColumn(
        "nport_holding_revision_id",
        _canonical_hash([
            F.col("source_id"), F.col("batch_id"), F.col("accession_no"),
            F.col("holding_ordinal"), F.col("canonical_row_hash"),
        ]),
    )
    .dropDuplicates(["nport_holding_revision_id"])
    .select(
        "nport_holding_revision_id", "etf_symbol", "accession_no", "holding_ordinal",
        "security_sk", "issuer_name", "security_title", F.col("cusip_norm").alias("cusip"),
        "identifiers", "balance", "units", "currency", "value_usd", "percentage", "lei",
        "payoff_profile", "asset_category", "issuer_category", "investment_country",
        "is_restricted_security", "fair_value_level", "resolution_method",
        "canonical_row_hash", "event_date", "knowledge_date", "source_id", "batch_id",
        "source_record_hash", "ingest_ts", F.col("ingest_ts").alias("revision_loaded_at"),
    )
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_nport_holding (
        nport_holding_revision_id STRING NOT NULL,
        etf_symbol STRING NOT NULL, accession_no STRING NOT NULL, holding_ordinal INT NOT NULL,
        security_sk BIGINT NOT NULL, issuer_name STRING NOT NULL, security_title STRING,
        cusip STRING, identifiers ARRAY<MAP<STRING,STRING>>, balance DECIMAL(38,10) NOT NULL,
        units STRING, currency STRING, value_usd DECIMAL(38,10) NOT NULL,
        percentage DECIMAL(18,10), lei STRING, payoff_profile STRING,
        asset_category STRING, issuer_category STRING, investment_country STRING,
        is_restricted_security STRING, fair_value_level STRING,
        resolution_method STRING NOT NULL, canonical_row_hash STRING NOT NULL,
        event_date DATE NOT NULL, knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL, batch_id STRING NOT NULL,
        source_record_hash STRING NOT NULL, ingest_ts TIMESTAMP NOT NULL,
        revision_loaded_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
_insert_only(
    "silver_nport_holding", resolved_holdings,
    "t.nport_holding_revision_id = s.nport_holding_revision_id",
)

cusip_bridge_candidates = (
    spark.table("silver_nport_holding")
    .filter(F.length(F.col("cusip")) == 9)
    .select("cusip", "security_sk")
    .distinct()
)
unique_cusips = (
    cusip_bridge_candidates.groupBy("cusip")
    .agg(
        F.countDistinct("security_sk").alias("security_count"),
        F.first("security_sk").alias("security_sk"),
    )
    .filter(F.col("security_count") == 1)
    .drop("security_count")
)
cusip_bridge_df = (
    unique_cusips.join(
        spark.table("dim_security").select("security_sk", "valid_from", "valid_to"),
        "security_sk",
        "inner",
    )
    .select(
        "security_sk",
        F.lit("CUSIP").alias("identifier_type"),
        F.col("cusip").alias("identifier_value"),
        "valid_from", "valid_to",
        F.lit("sec_nport").alias("source_id"),
        F.current_timestamp().alias("updated_at"),
    )
)
(
    cusip_bridge_df.write.format("delta").mode("overwrite")
    .option("overwriteSchema", "true").saveAsTable("dim_security_identifier")
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
