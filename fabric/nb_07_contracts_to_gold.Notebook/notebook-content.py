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
from pyspark.sql import Window
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


def _merge_contract_silver(source_df) -> None:
    if source_df.isEmpty():
        return
    (
        DeltaTable.forName(spark, "silver_contract_award")
        .alias("t")
        .merge(
            source_df.alias("s"),
            "t.transaction_id = s.transaction_id AND t.contract_revision_hash = s.contract_revision_hash",
        )
        .whenMatchedUpdate(set={
            "matched_terms": "array_sort(array_union(t.matched_terms, s.matched_terms))",
            "matched_symbols": "array_sort(array_union(t.matched_symbols, s.matched_symbols))",
            "batch_ids": "array_sort(array_union(t.batch_ids, s.batch_ids))",
            "ingest_ts": "least(t.ingest_ts, s.ingest_ts)",
            "knowledge_date": "least(t.knowledge_date, s.knowledge_date)",
        })
        .whenNotMatchedInsertAll()
        .execute()
    )


def _ensure_columns(table_name: str, column_specs: dict[str, str]) -> None:
    existing = set(spark.table(table_name).columns)
    for column_name, ddl in column_specs.items():
        if column_name not in existing:
            spark.sql(f"ALTER TABLE {table_name} ADD COLUMNS ({ddl})")


def _revision_hash(*columns):
    return F.sha2(F.to_json(F.struct(*columns)), 256)


def _iso_date(column_name: str):
    raw_date = F.trim(F.col(column_name))
    return F.when(
        raw_date.rlike("^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
        F.to_date(raw_date, "yyyy-MM-dd"),
    )


def _parse_error_rows(source_df, error_msg: str):
    natural_key = F.concat_ws(
        ":",
        F.coalesce(F.col("source_id"), F.lit("contracts")),
        F.lit("PARSE_ERROR"),
        F.coalesce(F.col("batch_id"), F.lit("missing")),
        F.sha2(F.coalesce(F.col("raw_record"), F.lit("")), 256),
    )
    return source_df.select(
        F.sha2(natural_key, 256).alias("natural_key"),
        F.coalesce(F.col("source_id"), F.lit("contracts")).alias("source_id"),
        F.col("batch_id"),
        F.col("raw_record"),
        F.lit(error_msg).alias("error_msg"),
        F.coalesce(F.col("ingest_ts"), F.current_timestamp()).alias("occurred_at"),
    )


def _dq_quarantine_rows(source_df, dq_rule: str):
    natural_key = F.concat_ws(
        ":",
        F.col("source_id"),
        F.lit(dq_rule),
        F.coalesce(F.col("transaction_internal_id"), F.lit("missing")),
        F.coalesce(F.col("award_id"), F.lit("missing")),
        F.coalesce(F.col("batch_id"), F.lit("missing")),
        F.sha2(F.coalesce(F.col("raw_record"), F.lit("")), 256),
    )
    return source_df.select(
        F.sha2(natural_key, 256).alias("quarantine_id"),
        natural_key.alias("natural_key"),
        F.col("source_id"),
        F.col("batch_id"),
        F.col("raw_record"),
        F.lit(dq_rule).alias("dq_rule"),
        F.coalesce(F.col("ingest_ts"), F.current_timestamp()).alias("quarantined_at"),
    )


for required in [
    "dim_security", "dim_entity", "dim_source", "fact_contract_award",
    "silver_parse_errors", "silver_dq_quarantine", "silver_security_quarantine",
]:
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

raw = (
    spark.read.text(paths)
    .select(F.col("value").alias("raw_json"))
    .select(
        F.get_json_object("raw_json", "$.source_id").alias("source_id"),
        F.get_json_object("raw_json", "$.batch_id").alias("batch_id"),
        F.to_timestamp(F.get_json_object("raw_json", "$.ingest_ts")).alias("ingest_ts"),
        F.upper(F.get_json_object("raw_json", "$.record.symbol")).alias("symbol"),
        F.get_json_object("raw_json", "$.record.search_text").alias("search_text"),
        F.get_json_object("raw_json", "$.record.award").alias("award_json"),
        F.get_json_object("raw_json", "$.record.search_award").alias("search_award_json"),
        F.get_json_object("raw_json", "$.record.search_transaction").alias("search_transaction_json"),
        F.get_json_object("raw_json", "$.record.award_detail").alias("award_detail_json"),
        F.get_json_object("raw_json", "$.record.transaction_id").alias("enriched_transaction_id"),
        F.get_json_object("raw_json", "$.record.transaction_internal_id").alias("enriched_transaction_internal_id"),
        F.get_json_object("raw_json", "$.record.generated_award_id").alias("enriched_generated_award_id"),
        F.get_json_object("raw_json", "$.record.modification_number").alias("enriched_modification_number"),
        F.get_json_object("raw_json", "$.record.transaction_amount").alias("enriched_transaction_amount"),
        F.get_json_object("raw_json", "$.record.transaction_description").alias("enriched_transaction_description"),
        F.get_json_object("raw_json", "$.record.award_id").alias("enriched_award_id"),
        F.get_json_object("raw_json", "$.record.action_date").alias("enriched_action_date"),
        F.get_json_object("raw_json", "$.record.legal_recipient_name").alias("enriched_recipient_name"),
        F.get_json_object("raw_json", "$.record.recipient_uei").alias("enriched_recipient_uei"),
        F.get_json_object("raw_json", "$.record.recipient_duns").alias("enriched_recipient_duns"),
        F.get_json_object("raw_json", "$.record.recipient_id").alias("enriched_recipient_id"),
        F.get_json_object("raw_json", "$.record.recipient_cik").alias("enriched_recipient_cik"),
        F.col("raw_json").alias("raw_record"),
    )
    .cache()
)
print(f"USASpending contracts bronze files: {len(paths)}, records={raw.count()}")

source_seed = spark.createDataFrame(
    [(6, "contracts", "contract", "weekly", None, "public_official")],
    "source_sk INT, source_id STRING, source_type STRING, latency_class STRING, reliability_weight DECIMAL(3,2), source_class STRING",
)
_merge_all("dim_source", source_seed, "t.source_sk = s.source_sk")
contracts_source_sk = 6

# --- E8 Silver: USASpending contract awards ---
parse_failures = raw.filter(
    F.col("source_id").isNull()
    | F.col("batch_id").isNull()
    | F.col("ingest_ts").isNull()
    | (
        F.col("award_json").isNull()
        & F.col("search_award_json").isNull()
        & F.col("search_transaction_json").isNull()
    )
)
_merge_replay_safe(
    "silver_parse_errors",
    _parse_error_rows(parse_failures, "INVALID_CONTRACT_BRONZE_ENVELOPE_OR_AWARD_JSON"),
    ["source_id", "batch_id", "raw_record", "error_msg", "occurred_at"],
)

parsed_awards = (
    raw.filter(
        F.col("source_id").isNotNull()
        & F.col("batch_id").isNotNull()
        & F.col("ingest_ts").isNotNull()
        & (
            F.col("award_json").isNotNull()
            | F.col("search_award_json").isNotNull()
            | F.col("search_transaction_json").isNotNull()
        )
    )
    .select(
        "source_id", "batch_id", "ingest_ts", "symbol", "search_text", "award_json",
        "search_award_json", "search_transaction_json", "award_detail_json", "raw_record",
        F.trim(F.col("enriched_transaction_id")).alias("provider_transaction_id"),
        F.trim(F.coalesce(
            F.col("enriched_transaction_internal_id"),
            F.get_json_object("search_transaction_json", "$['internal_id']"),
        )).alias("transaction_internal_id"),
        F.trim(F.coalesce(
            F.col("enriched_generated_award_id"),
            F.get_json_object("search_transaction_json", "$['generated_internal_id']"),
            F.get_json_object("search_award_json", "$['generated_internal_id']"),
        )).alias("generated_award_id"),
        F.trim(F.coalesce(
            F.col("enriched_modification_number"),
            F.get_json_object("search_transaction_json", "$['Mod']"),
        )).alias("modification_number"),
        F.trim(F.coalesce(
            F.col("enriched_award_id"),
            F.get_json_object("search_transaction_json", "$['Award ID']"),
            F.get_json_object("search_award_json", "$['Award ID']"),
            F.get_json_object("award_json", "$['Award ID']"),
        )).alias("award_id"),
        F.trim(F.coalesce(
            F.col("enriched_recipient_name"),
            F.get_json_object("search_transaction_json", "$['Recipient Name']"),
            F.get_json_object("search_award_json", "$['Recipient Name']"),
            F.get_json_object("award_json", "$['Recipient Name']"),
        )).alias("recipient_name"),
        F.upper(F.trim(F.coalesce(
            F.col("enriched_recipient_uei"),
            F.get_json_object("search_transaction_json", "$['Recipient UEI']"),
            F.get_json_object("search_award_json", "$['Recipient UEI']"),
            F.get_json_object("award_json", "$['Recipient UEI']"),
            F.get_json_object("award_json", "$['recipient_uei']"),
        ))).alias("recipient_uei"),
        F.regexp_replace(F.trim(F.coalesce(
            F.col("enriched_recipient_duns"),
            F.get_json_object("search_award_json", "$['Recipient DUNS Number']"),
            F.get_json_object("award_json", "$['Recipient DUNS']"),
            F.get_json_object("award_json", "$['Recipient DUNS Number']"),
            F.get_json_object("award_json", "$['recipient_duns']"),
        )), "[^0-9]", "").alias("recipient_duns"),
        F.trim(F.coalesce(
            F.col("enriched_recipient_id"),
            F.get_json_object("search_transaction_json", "$['recipient_id']"),
            F.get_json_object("search_award_json", "$['recipient_id']"),
            F.get_json_object("award_json", "$['Recipient ID']"),
            F.get_json_object("award_json", "$['recipient_id']"),
        )).alias("recipient_id"),
        F.regexp_replace(F.trim(F.coalesce(
            F.col("enriched_recipient_cik"),
            F.get_json_object("award_json", "$['Recipient CIK']"),
            F.get_json_object("award_json", "$['recipient_cik']"),
        )), "^0+", "").alias("recipient_cik"),
        F.trim(F.coalesce(
            F.get_json_object("search_transaction_json", "$['Awarding Agency']"),
            F.get_json_object("search_award_json", "$['Awarding Agency']"),
            F.get_json_object("award_json", "$['Awarding Agency']"),
        )).alias("agency"),
        F.trim(F.coalesce(
            F.get_json_object("search_transaction_json", "$['Awarding Sub Agency']"),
            F.get_json_object("search_award_json", "$['Awarding Sub Agency']"),
            F.get_json_object("award_json", "$['Awarding Sub Agency']"),
        )).alias("sub_agency"),
        F.coalesce(
            F.col("enriched_transaction_amount"),
            F.get_json_object("search_transaction_json", "$['Transaction Amount']"),
            F.get_json_object("award_json", "$['Award Amount']"),
        ).alias("raw_amount_usd"),
        F.trim(F.coalesce(
            F.col("enriched_transaction_description"),
            F.get_json_object("search_transaction_json", "$['Transaction Description']"),
            F.get_json_object("award_json", "$['Description']"),
        )).alias("description"),
        F.trim(F.coalesce(
            F.col("enriched_action_date"),
            F.get_json_object("search_transaction_json", "$['Action Date']"),
            F.get_json_object("search_award_json", "$['Base Obligation Date']"),
            F.get_json_object("award_json", "$['Action Date']"),
        )).alias("raw_action_date"),
        F.trim(F.coalesce(
            F.get_json_object("search_award_json", "$['Start Date']"),
            F.get_json_object("award_json", "$['Start Date']"),
        )).alias("raw_start_date"),
        F.trim(F.coalesce(
            F.get_json_object("search_award_json", "$['End Date']"),
            F.get_json_object("award_json", "$['End Date']"),
        )).alias("raw_end_date"),
        F.to_date("ingest_ts").alias("knowledge_date"),
    )
    .withColumn("recipient_uei", F.when(F.length("recipient_uei") > 0, F.col("recipient_uei")))
    .withColumn("recipient_duns", F.when(F.length("recipient_duns") > 0, F.col("recipient_duns")))
    .withColumn("recipient_id", F.when(F.length("recipient_id") > 0, F.col("recipient_id")))
    .withColumn("recipient_cik", F.when(F.length("recipient_cik") > 0, F.col("recipient_cik")))
    .withColumn("raw_action_date", F.when(F.length("raw_action_date") > 0, F.col("raw_action_date")))
    .withColumn("raw_start_date", F.when(F.length("raw_start_date") > 0, F.col("raw_start_date")))
    .withColumn("raw_end_date", F.when(F.length("raw_end_date") > 0, F.col("raw_end_date")))
    .withColumn("transaction_id", F.sha2(F.col("transaction_internal_id"), 256))
    .withColumn("action_date", _iso_date("raw_action_date"))
    .withColumn("start_date", _iso_date("raw_start_date"))
    .withColumn("end_date", _iso_date("raw_end_date"))
    .withColumn("event_date", F.coalesce(F.col("action_date"), F.col("start_date")))
    .withColumn(
        "event_date_source",
        F.when(F.col("action_date").isNotNull(), F.lit("ACTION_DATE"))
        .when(F.col("start_date").isNotNull(), F.lit("START_DATE")),
    )
    .withColumn("amount_usd", F.col("raw_amount_usd").cast(DecimalType(20, 2)))
)

award_id_failures = parsed_awards.filter(
    F.col("transaction_id").isNull()
    | (F.length(F.col("transaction_id")) == 0)
    | F.col("generated_award_id").isNull()
    | F.col("transaction_internal_id").isNull()
    | F.col("award_id").isNull()
    | (F.length(F.col("award_id")) == 0)
)
event_date_failures = parsed_awards.filter(
    F.col("event_date").isNull()
    | (F.col("raw_action_date").isNotNull() & F.col("action_date").isNull())
    | (F.col("raw_start_date").isNotNull() & F.col("start_date").isNull())
    | (F.col("raw_end_date").isNotNull() & F.col("end_date").isNull())
)
amount_failures = parsed_awards.filter(F.col("amount_usd").isNull())
pit_failures = parsed_awards.filter(
    F.col("knowledge_date").isNull()
    | (F.col("knowledge_date") > F.current_date())
    | (F.col("event_date") > F.col("knowledge_date"))
)

for failures, dq_rule in [
    (award_id_failures, "INVALID_CONTRACT_AWARD_ID"),
    (event_date_failures, "INVALID_CONTRACT_EVENT_DATE"),
    (amount_failures, "INVALID_CONTRACT_AMOUNT"),
    (pit_failures, "INVALID_CONTRACT_KNOWLEDGE_DATE"),
]:
    _merge_replay_safe(
        "silver_dq_quarantine",
        _dq_quarantine_rows(failures, dq_rule),
        ["quarantine_id", "source_id", "batch_id", "raw_record", "dq_rule", "quarantined_at"],
    )

valid_awards = parsed_awards.filter(
    F.col("transaction_id").isNotNull()
    & F.col("generated_award_id").isNotNull()
    & F.col("transaction_internal_id").isNotNull()
    & F.col("award_id").isNotNull()
    & (F.length(F.col("award_id")) > 0)
    & F.col("event_date").isNotNull()
    & ~(F.col("raw_action_date").isNotNull() & F.col("action_date").isNull())
    & ~(F.col("raw_start_date").isNotNull() & F.col("start_date").isNull())
    & ~(F.col("raw_end_date").isNotNull() & F.col("end_date").isNull())
    & F.col("amount_usd").isNotNull()
    & F.col("knowledge_date").isNotNull()
    & (F.col("knowledge_date") <= F.current_date())
    & (F.col("event_date") <= F.col("knowledge_date"))
    & (F.col("source_id") == "contracts")
)

revisioned_awards = valid_awards.withColumn(
    "contract_revision_hash",
    _revision_hash(
        F.col("transaction_id"), F.col("generated_award_id"),
        F.col("transaction_internal_id"), F.col("modification_number"),
        F.col("award_id"), F.col("recipient_name"), F.col("recipient_uei"),
        F.col("recipient_duns"), F.col("recipient_id"), F.col("recipient_cik"),
        F.col("agency"), F.col("sub_agency"), F.col("amount_usd"),
        F.col("description"), F.col("action_date"), F.col("start_date"),
        F.col("end_date"), F.col("event_date_source"),
    ),
)

# Transaction ID is the source natural key. Search terms/symbols are evidence only and
# are excluded from attribution and contract_revision_hash.
silver_contract_df = (
    revisioned_awards
    .groupBy(
        "transaction_id", "generated_award_id", "transaction_internal_id",
        "modification_number", "award_id", "contract_revision_hash",
        "recipient_name", "recipient_uei",
        "recipient_duns", "recipient_id", "recipient_cik", "agency", "sub_agency",
        "amount_usd", "description", "action_date", "start_date", "end_date",
        "event_date", "event_date_source", "source_id",
    )
    .agg(
        F.array_sort(F.collect_set("search_text")).alias("matched_terms"),
        F.array_sort(F.collect_set("symbol")).alias("matched_symbols"),
        F.array_sort(F.collect_set("batch_id")).alias("batch_ids"),
        F.min("ingest_ts").alias("ingest_ts"),
        F.min("knowledge_date").alias("knowledge_date"),
        F.min(F.coalesce("search_transaction_json", "search_award_json", "award_json")).alias("raw_award_json"),
    )
    .withColumn("loaded_at", F.current_timestamp())
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_contract_award (
        transaction_id STRING NOT NULL,
        generated_award_id STRING NOT NULL,
        transaction_internal_id STRING NOT NULL,
        modification_number STRING,
        award_id STRING NOT NULL,
        contract_revision_hash STRING NOT NULL,
        recipient_name STRING,
        recipient_uei STRING,
        recipient_duns STRING,
        recipient_id STRING,
        recipient_cik STRING,
        agency STRING,
        sub_agency STRING,
        amount_usd DECIMAL(20,2) NOT NULL,
        description STRING,
        action_date DATE,
        start_date DATE,
        end_date DATE,
        event_date DATE NOT NULL,
        event_date_source STRING NOT NULL,
        knowledge_date DATE NOT NULL,
        matched_terms ARRAY<STRING> NOT NULL,
        matched_symbols ARRAY<STRING> NOT NULL,
        source_id STRING NOT NULL,
        batch_ids ARRAY<STRING> NOT NULL,
        ingest_ts TIMESTAMP NOT NULL,
        raw_award_json STRING NOT NULL,
        loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
legacy_contract_ids = (
    spark.table("silver_contract_award")
    .filter(F.col("transaction_id") != F.sha2(F.col("transaction_internal_id"), 256))
    .select("transaction_id")
    .distinct()
    .cache()
)
legacy_contract_id_count = legacy_contract_ids.count()
if legacy_contract_id_count:
    if spark.catalog.tableExists("fact_contract_award"):
        (
            DeltaTable.forName(spark, "fact_contract_award").alias("t")
            .merge(legacy_contract_ids.alias("s"), "t.transaction_id = s.transaction_id")
            .whenMatchedDelete()
            .execute()
        )
    (
        DeltaTable.forName(spark, "silver_contract_award").alias("t")
        .merge(legacy_contract_ids.alias("s"), "t.transaction_id = s.transaction_id")
        .whenMatchedDelete()
        .execute()
    )
    print(f"Removed {legacy_contract_id_count} legacy contract transaction identities")
legacy_contract_ids.unpersist()
_merge_contract_silver(silver_contract_df)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Contract recipient entity and security resolution ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_entity_quarantine (
        natural_key STRING NOT NULL,
        source_id STRING NOT NULL,
        transaction_id STRING,
        award_id STRING,
        contract_revision_hash STRING,
        recipient_identifier STRING,
        recipient_name STRING,
        reason STRING NOT NULL,
        details STRING,
        event_date DATE,
        knowledge_date DATE,
        batch_id STRING,
        quarantined_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")

silver_contracts = spark.table("silver_contract_award")
entity_lookup = (
    spark.table("dim_entity")
    .select(
        F.col("entity_sk").alias("resolved_entity_sk"),
        F.lower(F.trim("entity_natural_id")).alias("entity_natural_id_norm"),
        F.lower(F.regexp_replace(F.trim("name"), "[^A-Za-z0-9]", "")).alias("entity_name_norm"),
        F.regexp_replace(F.trim("cik"), "^0+", "").alias("entity_cik"),
    )
)

contract_resolution_source = (
    silver_contracts
    .withColumn(
        "recipient_identifier",
        F.coalesce("recipient_uei", "recipient_duns", "recipient_id", "recipient_cik"),
    )
    .withColumn(
        "recipient_identifier_type",
        F.when(F.col("recipient_uei").isNotNull(), F.lit("uei"))
        .when(F.col("recipient_duns").isNotNull(), F.lit("duns"))
        .when(F.col("recipient_id").isNotNull(), F.lit("usaspending_recipient"))
        .when(F.col("recipient_cik").isNotNull(), F.lit("cik")),
    )
    .withColumn(
        "recipient_name_norm",
        F.lower(F.regexp_replace(F.trim("recipient_name"), "[^A-Za-z0-9]", "")),
    )
)

entity_uei_match = F.col("c.recipient_uei").isNotNull() & (
    (F.col("e.entity_natural_id_norm") == F.lower(F.col("c.recipient_uei")))
    | (
        F.col("e.entity_natural_id_norm")
        == F.concat(F.lit("uei:"), F.lower(F.col("c.recipient_uei")))
    )
)
entity_duns_match = F.col("c.recipient_duns").isNotNull() & (
    (F.col("e.entity_natural_id_norm") == F.lower(F.col("c.recipient_duns")))
    | (
        F.col("e.entity_natural_id_norm")
        == F.concat(F.lit("duns:"), F.lower(F.col("c.recipient_duns")))
    )
)
entity_recipient_id_match = F.col("c.recipient_id").isNotNull() & (
    (F.col("e.entity_natural_id_norm") == F.lower(F.col("c.recipient_id")))
    | (
        F.col("e.entity_natural_id_norm") == F.concat(
            F.lit("usaspending_recipient:"), F.lower(F.col("c.recipient_id")),
        )
    )
)
entity_cik_match = F.col("c.recipient_cik").isNotNull() & (
    (F.col("e.entity_cik") == F.col("c.recipient_cik"))
    | (F.col("e.entity_natural_id_norm") == F.lower(F.col("c.recipient_cik")))
    | (
        F.col("e.entity_natural_id_norm")
        == F.concat(F.lit("cik:"), F.lower(F.col("c.recipient_cik")))
    )
)
has_official_recipient_identifier = (
    F.col("c.recipient_uei").isNotNull()
    | F.col("c.recipient_duns").isNotNull()
    | F.col("c.recipient_id").isNotNull()
    | F.col("c.recipient_cik").isNotNull()
)
entity_name_match = (
    ~has_official_recipient_identifier
    & (F.length(F.col("c.recipient_name_norm")) > 0)
    & (F.col("c.recipient_name_norm") == F.col("e.entity_name_norm"))
)
entity_match = (
    entity_uei_match
    | entity_duns_match
    | entity_recipient_id_match
    | entity_cik_match
    | entity_name_match
)

entity_resolution_window = Window.partitionBy("transaction_id", "contract_revision_hash")
entity_resolved = (
    contract_resolution_source.alias("c")
    .join(entity_lookup.alias("e"), entity_match, "left")
    .withColumn(
        "entity_match_count",
        F.count(F.col("resolved_entity_sk")).over(entity_resolution_window),
    )
    .withColumn(
        "entity_sk",
        F.when(F.col("entity_match_count") == 1, F.col("resolved_entity_sk")),
    )
    .withColumn(
        "entity_match_method",
        F.when((F.col("entity_match_count") == 1) & entity_uei_match, F.lit("OFFICIAL_UEI"))
        .when((F.col("entity_match_count") == 1) & entity_duns_match, F.lit("OFFICIAL_DUNS"))
        .when((F.col("entity_match_count") == 1) & entity_recipient_id_match, F.lit("OFFICIAL_RECIPIENT_ID"))
        .when((F.col("entity_match_count") == 1) & entity_cik_match, F.lit("OFFICIAL_CIK"))
        .when((F.col("entity_match_count") == 1) & entity_name_match, F.lit("UNIQUE_NORMALIZED_NAME")),
    )
    .dropDuplicates(["transaction_id", "contract_revision_hash"])
    .drop("resolved_entity_sk", "entity_natural_id_norm", "entity_name_norm")
)

entity_unresolved = entity_resolved.filter(F.col("entity_sk").isNull())
DeltaTable.forName(spark, "silver_entity_quarantine").delete("source_id = 'contracts'")
if not entity_unresolved.isEmpty():
    entity_quarantine = entity_unresolved.select(
        F.sha2(F.concat_ws(
            ":", F.lit("contracts"), F.lit("ENTITY_UNRESOLVED"),
            F.col("transaction_id"), F.col("contract_revision_hash"),
        ), 256).alias("natural_key"),
        F.col("source_id"),
        F.col("transaction_id"),
        F.col("award_id"),
        F.col("contract_revision_hash"),
        F.col("recipient_identifier"),
        F.col("recipient_name"),
        F.lit("ENTITY_UNRESOLVED_OR_AMBIGUOUS").alias("reason"),
        F.concat(
            F.lit("Official recipient identifier/name did not resolve uniquely; match_count="),
            F.col("entity_match_count").cast("string"),
        ).alias("details"),
        F.col("event_date"),
        F.col("knowledge_date"),
        F.element_at("batch_ids", 1).alias("batch_id"),
        F.current_timestamp().alias("quarantined_at"),
    )
    _merge_replay_safe(
        "silver_entity_quarantine",
        entity_quarantine,
        [
            "source_id", "transaction_id", "award_id", "contract_revision_hash", "recipient_identifier",
            "recipient_name", "reason", "details", "event_date", "knowledge_date",
            "batch_id", "quarantined_at",
        ],
    )

# Security attribution is allowed only through the resolved entity's CIK. Bronze
# search symbols and search text are not resolution inputs.
security_lookup = (
    spark.table("dim_security")
    .filter(F.col("cik").isNotNull())
    .select(
        F.col("security_sk").alias("resolved_security_sk"),
        F.regexp_replace(F.trim("cik"), "^0+", "").alias("security_cik"),
        "valid_from", "valid_to",
    )
)
security_resolution_window = Window.partitionBy("transaction_id", "contract_revision_hash")
security_resolved = (
    entity_resolved.filter(F.col("entity_sk").isNotNull()).alias("c")
    .join(
        security_lookup.alias("s"),
        (F.col("c.entity_cik").isNotNull())
        & (F.col("c.entity_cik") == F.col("s.security_cik"))
        & (F.col("c.event_date") >= F.col("s.valid_from"))
        & (F.col("s.valid_to").isNull() | (F.col("c.event_date") < F.col("s.valid_to"))),
        "left",
    )
    .withColumn(
        "security_match_count",
        F.count(F.col("resolved_security_sk")).over(security_resolution_window),
    )
    .withColumn(
        "security_sk",
        F.when(F.col("security_match_count") == 1, F.col("resolved_security_sk")),
    )
    .dropDuplicates(["transaction_id", "contract_revision_hash"])
    .drop("resolved_security_sk", "security_cik", "valid_from", "valid_to")
)

security_unresolved = security_resolved.filter(F.col("security_sk").isNull())
DeltaTable.forName(spark, "silver_security_quarantine").delete("source_id = 'contracts'")
if not security_unresolved.isEmpty():
    security_quarantine_key = F.concat_ws(
        ":", F.lit("contracts"), F.lit("SECURITY_UNRESOLVED"),
        F.col("transaction_id"), F.col("contract_revision_hash"),
    )
    contract_security_quarantine = security_unresolved.select(
        F.sha2(security_quarantine_key, 256).alias("quarantine_id"),
        security_quarantine_key.alias("natural_key"),
        F.col("source_id"),
        F.coalesce(F.col("entity_cik"), F.col("recipient_identifier"), F.col("recipient_name")).alias("raw_identifier"),
        F.lit("SECURITY_UNRESOLVED").alias("reason"),
        F.concat(
            F.lit("Resolved entity lacks one unique PIT CIK-to-security match; entity_sk="),
            F.col("entity_sk").cast("string"),
            F.lit(", match_count="), F.col("security_match_count").cast("string"),
        ).alias("details"),
        F.col("event_date"),
        F.col("knowledge_date"),
        F.element_at("batch_ids", 1).alias("batch_id"),
        F.current_timestamp().alias("quarantined_at"),
    )
    _merge_replay_safe(
        "silver_security_quarantine",
        contract_security_quarantine,
        [
            "source_id", "raw_identifier", "reason", "details", "event_date",
            "knowledge_date", "batch_id", "quarantined_at",
        ],
    )

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Gold promotion from E8 Silver: contract awards ---
_ensure_columns("fact_contract_award", {
    "transaction_id": "transaction_id STRING",
    "award_id": "award_id STRING",
    "contract_revision_hash": "contract_revision_hash STRING",
    "entity_sk": "entity_sk BIGINT",
})

gold_contract_df = (
    security_resolved.filter(F.col("security_sk").isNotNull())
    .withColumn(
        "award_sk",
        _positive_sk(F.lit("USASpending"), F.col("transaction_id"), F.col("contract_revision_hash")),
    )
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn(
        "description_hash",
        F.sha2(F.coalesce(F.col("description"), F.col("award_id")), 256),
    )
    .withColumn("source_sk", F.lit(contracts_source_sk))
    .select(
        "award_sk", "transaction_id", "award_id", "contract_revision_hash", "security_sk", "entity_sk",
        "date_sk", "agency", "amount_usd", "description_hash", "source_sk",
        "event_date", "knowledge_date",
    )
    .dropDuplicates(["transaction_id", "contract_revision_hash"])
)

gold_target = DeltaTable.forName(spark, "fact_contract_award")
(
    gold_target.alias("t")
    .merge(
        gold_contract_df.alias("s"),
        "t.transaction_id = s.transaction_id AND t.contract_revision_hash = s.contract_revision_hash",
    )
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
    .whenNotMatchedBySourceDelete(condition="t.source_sk = 6")
    .execute()
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

missing_pit = (
    spark.table("fact_contract_award")
    .filter(
        (F.col("source_sk") == contracts_source_sk)
        & (
            F.col("event_date").isNull()
            | F.col("knowledge_date").isNull()
            | (F.col("event_date") > F.col("knowledge_date"))
        )
    )
    .count()
)
silver_duplicate_revisions = spark.sql("""
    SELECT COUNT(*) AS n
    FROM (
        SELECT transaction_id, contract_revision_hash
        FROM silver_contract_award
        GROUP BY transaction_id, contract_revision_hash
        HAVING COUNT(*) > 1
    ) duplicates
""").collect()[0].n
gold_without_silver = (
    spark.table("fact_contract_award").alias("g")
    .filter(F.col("g.source_sk") == contracts_source_sk)
    .join(
        security_resolved.filter(F.col("security_sk").isNotNull()).alias("s"),
        (F.col("g.transaction_id") == F.col("s.transaction_id"))
        & (F.col("g.contract_revision_hash") == F.col("s.contract_revision_hash"))
        & (F.col("g.security_sk") == F.col("s.security_sk"))
        & (F.col("g.entity_sk") == F.col("s.entity_sk"))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date"))
        & F.col("g.amount_usd").eqNullSafe(F.col("s.amount_usd"))
        & F.col("g.agency").eqNullSafe(F.col("s.agency")),
        "left_anti",
    )
    .count()
)
unresolved_in_gold = (
    spark.table("fact_contract_award")
    .filter(
        (F.col("source_sk") == contracts_source_sk)
        & (F.col("security_sk").isNull() | F.col("entity_sk").isNull())
    )
    .count()
)
resolved_silver_without_gold = (
    security_resolved.filter(F.col("security_sk").isNotNull()).alias("s")
    .join(
        spark.table("fact_contract_award").filter(F.col("source_sk") == contracts_source_sk).alias("g"),
        (F.col("g.transaction_id") == F.col("s.transaction_id"))
        & (F.col("g.contract_revision_hash") == F.col("s.contract_revision_hash")),
        "left_anti",
    )
    .count()
)
entity_unresolved_without_quarantine = (
    entity_unresolved.alias("s")
    .join(
        spark.table("silver_entity_quarantine").alias("q"),
        (F.col("q.transaction_id") == F.col("s.transaction_id"))
        & (F.col("q.contract_revision_hash") == F.col("s.contract_revision_hash")),
        "left_anti",
    )
    .count()
)
security_unresolved_without_quarantine = (
    security_unresolved.alias("s")
    .join(
        spark.table("silver_security_quarantine").alias("q"),
        (F.col("q.natural_key") == F.sha2(F.concat_ws(
            ":", F.lit("contracts"), F.lit("SECURITY_UNRESOLVED"),
            F.col("s.transaction_id"), F.col("s.contract_revision_hash"),
        ), 256)),
        "left_anti",
    )
    .count()
)

print(
    "E8 contracts validation: "
    f"missing_pit={missing_pit}, "
    f"silver_duplicate_revisions={silver_duplicate_revisions}, "
    f"gold_without_silver={gold_without_silver}, "
    f"unresolved_in_gold={unresolved_in_gold}, "
    f"resolved_silver_without_gold={resolved_silver_without_gold}, "
    f"entity_unresolved_without_quarantine={entity_unresolved_without_quarantine}, "
    f"security_unresolved_without_quarantine={security_unresolved_without_quarantine}"
)
if any([
    missing_pit, silver_duplicate_revisions, gold_without_silver, unresolved_in_gold,
    resolved_silver_without_gold, entity_unresolved_without_quarantine,
    security_unresolved_without_quarantine,
]):
    raise RuntimeError(
        "E8 contracts validation failed: "
        f"missing_pit={missing_pit}, "
        f"silver_duplicate_revisions={silver_duplicate_revisions}, "
        f"gold_without_silver={gold_without_silver}, "
        f"unresolved_in_gold={unresolved_in_gold}, "
        f"resolved_silver_without_gold={resolved_silver_without_gold}, "
        f"entity_unresolved_without_quarantine={entity_unresolved_without_quarantine}, "
        f"security_unresolved_without_quarantine={security_unresolved_without_quarantine}"
    )
raw.unpersist()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
