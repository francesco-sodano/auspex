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

# Fabric Notebook: nb_06_sec_filings_to_gold
# Parses SEC filing content into revisioned Silver tables, quarantines unsupported rows,
# and promotes only Silver-backed facts to Gold.
# Attaches to: auspex_bronze (default lakehouse)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

import html
import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from delta.tables import DeltaTable
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType, BooleanType, DecimalType, IntegerType, LongType,
    StringType, StructField, StructType,
)
from pyspark.sql.window import Window

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


def _merge_insert_only(table_name: str, source_df, condition: str) -> None:
    if source_df.isEmpty():
        return
    target = DeltaTable.forName(spark, table_name)
    (
        target
        .alias("t")
        .merge(source_df.alias("s"), condition)
        .whenNotMatchedInsertAll()
        .execute()
    )
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    print(f"Inserted source_rows={metrics.get('numSourceRows', 'unknown')} into {table_name}")


def _merge_canonical_silver(table_name: str, source_df, condition: str) -> None:
    if source_df.isEmpty():
        return
    target = DeltaTable.forName(spark, table_name)
    earlier_observation = (
        "s.ingest_ts < t.ingest_ts OR "
        "(s.ingest_ts = t.ingest_ts AND s.batch_id < t.batch_id)"
    )
    if "source_record_hash" in source_df.columns:
        earlier_observation += (
            " OR (s.ingest_ts = t.ingest_ts AND s.batch_id = t.batch_id "
            "AND s.source_record_hash < t.source_record_hash)"
        )
    (
        target.alias("t")
        .merge(source_df.alias("s"), condition)
        .whenMatchedUpdateAll(condition=earlier_observation)
        .whenNotMatchedInsertAll()
        .execute()
    )
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    print(f"Canonicalized source_rows={metrics.get('numSourceRows', 'unknown')} into {table_name}")


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
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    print(f"Merged source_rows={metrics.get('numSourceRows', 'unknown')} into {table_name}")


def _ensure_columns(table_name: str, column_specs: dict[str, str]) -> None:
    existing = set(spark.table(table_name).columns)
    for column_name, ddl in column_specs.items():
        if column_name not in existing:
            spark.sql(f"ALTER TABLE {table_name} ADD COLUMNS ({ddl})")


def _revision_hash(*columns):
    return F.sha2(F.to_json(F.struct(*columns)), 256)


def _first_revision(source_df, partition_columns: list[str]):
    revision_order = [
        F.col("ingest_ts").asc_nulls_last(),
        F.col("batch_id").asc(),
    ]
    if "source_record_hash" in source_df.columns:
        revision_order.append(F.col("source_record_hash").asc())
    revision_window = Window.partitionBy(*partition_columns).orderBy(*revision_order)
    return (
        source_df
        .withColumn("revision_row_number", F.row_number().over(revision_window))
        .filter(F.col("revision_row_number") == 1)
        .drop("revision_row_number")
    )


def _dq_quarantine_rows(source_df, key_columns: list[str], dq_rule: str):
    natural_key = F.concat_ws(
        ":",
        F.col("source_id"),
        F.lit(dq_rule),
        *[F.coalesce(F.col(column).cast("string"), F.lit("missing")) for column in key_columns],
        F.col("batch_id"),
    )
    return source_df.select(
        F.sha2(natural_key, 256).alias("quarantine_id"),
        natural_key.alias("natural_key"),
        F.col("source_id"),
        F.col("batch_id"),
        F.col("raw_record"),
        F.lit(dq_rule).alias("dq_rule"),
        F.col("ingest_ts").alias("quarantined_at"),
    )


def _security_quarantine_rows(source_df, raw_identifier, reason: str, details: str):
    natural_key = F.concat_ws(
        ":",
        F.col("source_id"),
        F.lit(reason),
        F.coalesce(raw_identifier.cast("string"), F.lit("missing")),
        F.coalesce(F.col("accession_no"), F.lit("missing")),
        F.coalesce(F.col("row_key"), F.lit("missing")),
        F.col("batch_id"),
    )
    return source_df.select(
        F.sha2(natural_key, 256).alias("quarantine_id"),
        natural_key.alias("natural_key"),
        F.col("source_id"),
        raw_identifier.cast("string").alias("raw_identifier"),
        F.lit(reason).alias("reason"),
        F.lit(details).alias("details"),
        F.col("event_date"),
        F.col("knowledge_date"),
        F.col("batch_id"),
        F.col("ingest_ts").alias("quarantined_at"),
    )


for required in [
    "dim_security", "dim_entity", "dim_source", "fact_institutional_holding", "fact_ownership_event",
    "silver_parse_errors", "silver_dq_quarantine", "silver_security_quarantine",
]:
    _require_table(required)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Read raw SEC filing envelopes and locate optional enriched document content ---
content_paths = _existing_paths(
    _date_paths("sec_13f")
    + _date_paths("sec_13dg")
)
metadata_only_paths = _existing_paths(
    _date_paths("sec_8k") + _date_paths("sec_s1")
)
if not content_paths and not metadata_only_paths:
    raise RuntimeError("No E8 SEC bronze files found in window")

def _envelope_schema(include_document_content):
    archive_fields = []
    if include_document_content:
        archive_fields.extend([
            StructField("primary_document", StructType([
                StructField("content", StringType()),
            ])),
            StructField("information_table_xml", StructType([
                StructField("content", StringType()),
            ])),
        ])
    archive_fields.extend([
            StructField("registrant_cik", StringType()),
            StructField("filer_cik", StringType()),
            StructField("subject_issuer", StructType([
                StructField("cik", StringType()),
                StructField("name", StringType()),
                StructField("class_title", StringType()),
                StructField("cusip", StringType()),
            ])),
            StructField("reporting_owners", ArrayType(StructType([
                StructField("cik", StringType()),
                StructField("name", StringType()),
                StructField("percent_owned", StringType()),
            ]))),
            StructField("item_codes", ArrayType(StringType())),
            StructField("missing_document_classes", ArrayType(StringType())),
            StructField("archive_status", StringType()),
    ])
    return StructType([
        StructField("source_id", StringType()),
        StructField("batch_id", StringType()),
        StructField("ingest_ts", StringType()),
        StructField("record", StructType([
            StructField("adsh", StringType()),
            StructField("file_date", StringType()),
            StructField("period_ending", StringType()),
            StructField("display_names", ArrayType(StringType())),
            StructField("ciks", ArrayType(StringType())),
            StructField("tickers", ArrayType(StringType())),
            StructField("form", StringType()),
            StructField("matched_forms", StringType()),
            StructField("filing_url", StringType()),
            StructField("sec_archive", StructType(archive_fields)),
        ])),
    ])


def _read_envelopes(source_paths, include_document_content):
    envelope_df = (
        spark.read.text(source_paths)
        .select(F.col("value").alias("raw_json"))
        .withColumn(
            "envelope",
            F.from_json("raw_json", _envelope_schema(include_document_content)),
        )
    )
    primary_content = (
        F.col("envelope.record.sec_archive.primary_document.content")
        if include_document_content else F.lit(None).cast("string")
    )
    information_content = (
        F.col("envelope.record.sec_archive.information_table_xml.content")
        if include_document_content else F.lit(None).cast("string")
    )
    return envelope_df.select(
        F.col("envelope.source_id").alias("source_id"),
        F.col("envelope.batch_id").alias("batch_id"),
        F.to_timestamp("envelope.ingest_ts").alias("ingest_ts"),
        F.col("envelope.record.adsh").alias("accession_no"),
        F.col("envelope.record.file_date").alias("file_date"),
        F.col("envelope.record.period_ending").alias("period_of_report"),
        F.col("envelope.record.display_names").alias("display_names"),
        F.col("envelope.record.ciks").alias("metadata_ciks"),
        F.col("envelope.record.tickers").alias("metadata_tickers"),
        F.col("envelope.record.form").alias("form"),
        F.col("envelope.record.matched_forms").alias("matched_forms"),
        F.col("envelope.record.filing_url").alias("filing_url"),
        primary_content.alias("primary_document_content"),
        information_content.alias("information_table_content"),
        F.col("envelope.record.sec_archive.registrant_cik").alias("registrant_cik"),
        F.col("envelope.record.sec_archive.filer_cik").alias("filer_cik"),
        F.col("envelope.record.sec_archive.subject_issuer").alias("subject_issuer"),
        F.col("envelope.record.sec_archive.reporting_owners").alias("reporting_owners"),
        F.col("envelope.record.sec_archive.item_codes").alias("item_codes"),
        F.col("envelope.record.sec_archive.missing_document_classes").alias("missing_document_classes"),
        F.col("envelope.record.sec_archive.archive_status").alias("archive_status"),
        F.sha2(F.col("raw_json"), 256).alias("source_record_hash"),
        F.col("raw_json").alias("raw_record"),
    )


envelope_frames = []
if content_paths:
    envelope_frames.append(_read_envelopes(content_paths, True))
if metadata_only_paths:
    envelope_frames.append(_read_envelopes(metadata_only_paths, False))
raw_envelopes = envelope_frames[0]
for envelope_frame in envelope_frames[1:]:
    raw_envelopes = raw_envelopes.unionByName(envelope_frame)

raw = (
    raw_envelopes
    .withColumn("filing_type", F.coalesce(F.col("form"), F.col("matched_forms")))
    .withColumn("filer_name", F.concat_ws("; ", F.col("display_names")))
    .withColumn("event_date", F.to_date(F.coalesce(F.col("period_of_report"), F.col("file_date"))))
    .withColumn("knowledge_date", F.to_date("file_date"))
    .withColumn(
        "primary_content_present",
        F.when(
            F.col("source_id").isin("sec_8k", "sec_s1"),
            F.col("archive_status") == "complete",
        ).otherwise(
            F.col("primary_document_content").isNotNull()
            & (F.length(F.trim("primary_document_content")) > 0)
        ),
    )
    .withColumn(
        "information_table_content_present",
        F.col("information_table_content").isNotNull()
        & (F.length(F.trim("information_table_content")) > 0),
    )
    .withColumn(
        "raw_content_present",
        F.col("primary_content_present") | F.col("information_table_content_present"),
    )
    .withColumn("archive_complete", F.col("archive_status") == F.lit("complete"))
    .withColumn("content_present", F.col("raw_content_present") & F.col("archive_complete"))
    .withColumn(
        "content_hash",
        F.when(
            F.col("content_present"),
            F.when(
                F.col("source_id").isin("sec_8k", "sec_s1"), F.col("source_record_hash")
            ).otherwise(
                F.sha2(F.concat_ws("|", "primary_document_content", "information_table_content"), 256)
            ),
        ),
    )
    .dropDuplicates(["source_id", "accession_no", "batch_id", "content_hash"])
    .cache()
)
print(f"E8 SEC bronze filings: {raw.count()}")
processed_batch_ids = raw.select("batch_id").where(F.col("batch_id").isNotNull()).distinct()
processed_13f_batch_ids = (
    raw.filter(F.col("source_id") == F.lit("sec_13f"))
    .select("batch_id")
    .where(F.col("batch_id").isNotNull())
    .distinct()
)
(
    DeltaTable.forName(spark, "silver_security_quarantine")
    .alias("t")
    .merge(
        processed_13f_batch_ids.alias("s"),
        "t.source_id = 'sec_13f' AND t.batch_id = s.batch_id",
    )
    .whenMatchedDelete()
    .execute()
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

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Bronze raw text to E8 Silver ---
ownership_schema = StructType([
    StructField("issuer_cik", StringType()),
    StructField("issuer_ticker", StringType()),
    StructField("issuer_exchange", StringType()),
    StructField("issuer_isin", StringType()),
    StructField("issuer_cusip", StringType()),
    StructField("reporting_owner_cik", StringType()),
    StructField("reporting_owner_name", StringType()),
    StructField("pct_owned", StringType()),
    StructField("is_activist", BooleanType()),
    StructField("event_date", StringType()),
])
holding_schema = StructType([
    StructField("issuer_cik", StringType()),
    StructField("issuer_ticker", StringType()),
    StructField("issuer_exchange", StringType()),
    StructField("issuer_isin", StringType()),
    StructField("cusip", StringType()),
    StructField("issuer_name", StringType()),
    StructField("holder_cik", StringType()),
    StructField("holder_name", StringType()),
    StructField("shares", StringType()),
    StructField("value_usd", StringType()),
    StructField("pct_of_portfolio", StringType()),
    StructField("event_date", StringType()),
])
material_schema = StructType([
    StructField("issuer_cik", StringType()),
    StructField("issuer_ticker", StringType()),
    StructField("issuer_exchange", StringType()),
    StructField("issuer_isin", StringType()),
    StructField("item_code", StringType()),
    StructField("description", StringType()),
    StructField("event_date", StringType()),
])
parsed_content_schema = StructType([
    StructField("issuer_cik", StringType()),
    StructField("issuer_ticker", StringType()),
    StructField("issuer_exchange", StringType()),
    StructField("issuer_isin", StringType()),
    StructField("issuer_cusip", StringType()),
    StructField("manager_cik", StringType()),
    StructField("manager_name", StringType()),
    StructField("ownership_events", ArrayType(ownership_schema)),
    StructField("institutional_holdings", ArrayType(holding_schema)),
    StructField("material_events", ArrayType(material_schema)),
    StructField("parse_error", StringType()),
])


def _mapping_value(mapping, aliases):
    if not isinstance(mapping, dict):
        return None
    normalized = {str(key).lower().replace("-", "_"): value for key, value in mapping.items()}
    for alias in aliases:
        value = normalized.get(alias.lower().replace("-", "_"))
        if value not in (None, ""):
            return value
    return None


def _mapping_rows(payload, aliases):
    if not isinstance(payload, dict):
        return []
    value = _mapping_value(payload, aliases)
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        nested = _mapping_value(value, ["data", "rows", "items"])
        if isinstance(nested, list):
            return [row for row in nested if isinstance(row, dict)]
        return [value]
    data = _mapping_value(payload, ["data", "payload", "filing"])
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return _mapping_rows(data, aliases)
    return []


def _string_value(value):
    if value is None or isinstance(value, (dict, list)):
        return None
    text = html.unescape(str(value)).strip()
    return text or None


def _numeric_value(value):
    text = _string_value(value)
    if text is None:
        return None
    normalized = text.replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        return str(Decimal(normalized))
    except InvalidOperation:
        return None


def _boolean_value(value):
    if isinstance(value, bool):
        return value
    text = _string_value(value)
    if text is None:
        return None
    if text.lower() in {"true", "1", "yes", "y"}:
        return True
    if text.lower() in {"false", "0", "no", "n"}:
        return False
    return None


def _tag_value(text, tag_names):
    for tag_name in tag_names:
        match = re.search(
            rf"<(?:(?:[A-Za-z0-9_]+):)?{tag_name}\b[^>]*>(.*?)</(?:(?:[A-Za-z0-9_]+):)?{tag_name}>",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            return _string_value(re.sub(r"<[^>]+>", " ", match.group(1)))
    return None


def _document_event_date(text):
    tagged_date = _tag_value(text, ["dateOfEvent", "eventDate", "periodOfReport"])
    if tagged_date:
        return tagged_date
    header_match = re.search(
        r"(?im)^\s*(?:CONFORMED PERIOD OF REPORT|PERIOD OF REPORT)\s*:\s*(\d{8}|\d{4}-\d{2}-\d{2})",
        text,
    )
    if not header_match:
        return None
    raw_date = header_match.group(1)
    if len(raw_date) == 8:
        return f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
    return raw_date


def _sec_header_value(text, header_names):
    for header_name in header_names:
        match = re.search(
            rf"(?im)^\s*{header_name}\s*:\s*([^\r\n]+)",
            text,
        )
        if match:
            return _string_value(match.group(1))
    return None


def _structured_identifiers(row, payload):
    return {
        "issuer_cik": _string_value(_mapping_value(row, ["issuer_cik", "cik"]) or _mapping_value(payload, ["issuer_cik", "cik"])),
        "issuer_ticker": _string_value(_mapping_value(row, ["issuer_ticker", "ticker", "symbol"]) or _mapping_value(payload, ["issuer_ticker", "ticker", "symbol"])),
        "issuer_exchange": _string_value(_mapping_value(row, ["issuer_exchange", "exchange"]) or _mapping_value(payload, ["issuer_exchange", "exchange"])),
        "issuer_isin": _string_value(_mapping_value(row, ["issuer_isin", "isin"]) or _mapping_value(payload, ["issuer_isin", "isin"])),
    }


def _row_mapping(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "asDict"):
        return value.asDict(recursive=True)
    return {}


def _parse_sec_content(
    primary_document_content,
    information_table_content,
    filing_type,
    registrant_cik,
    filer_cik,
    filer_name,
    subject_issuer,
    reporting_owners,
    item_codes,
):
    subject = _row_mapping(subject_issuer)
    archive_owners = [_row_mapping(owner) for owner in (reporting_owners or [])]
    normalized_form = str(filing_type or "").upper()
    result = {
        "issuer_cik": _string_value(subject.get("cik")),
        "issuer_ticker": None,
        "issuer_exchange": None,
        "issuer_isin": None,
        "issuer_cusip": _string_value(subject.get("cusip")),
        "manager_cik": _string_value(filer_cik) or _string_value(registrant_cik),
        "manager_name": _string_value(filer_name),
        "ownership_events": [],
        "institutional_holdings": [],
        "material_events": [],
        "parse_error": None,
    }
    primary_text = html.unescape(str(primary_document_content or "")).strip()
    information_text = str(information_table_content or "").strip()
    if "13F" not in normalized_form:
        result["issuer_cik"] = result["issuer_cik"] or _string_value(registrant_cik)
    primary_event_date = _document_event_date(primary_text) if primary_text else None

    payload = None
    if primary_text.startswith("{") or primary_text.startswith("["):
        try:
            payload = json.loads(primary_text)
            if isinstance(payload, list):
                payload = {"data": payload}
        except (TypeError, ValueError) as exc:
            result["parse_error"] = f"Invalid enriched SEC JSON: {exc}"
            return result

    if isinstance(payload, dict):
        identifiers = _structured_identifiers(payload, payload)
        for key, value in identifiers.items():
            if value and not result.get(key):
                result[key] = value
        result["manager_cik"] = result["manager_cik"] or _string_value(
            _mapping_value(payload, ["manager_cik", "filing_manager_cik", "filer_cik"])
        )
        result["manager_name"] = _string_value(
            _mapping_value(payload, ["manager_name", "filing_manager_name", "filer_name"])
        ) or result["manager_name"]

        for row in _mapping_rows(payload, ["material_events", "material_items", "filing_items"]):
            row_identifiers = _structured_identifiers(row, payload)
            result["material_events"].append({
                **row_identifiers,
                "item_code": _string_value(_mapping_value(row, ["item_code", "item", "item_number"])),
                "description": _string_value(_mapping_value(row, ["description", "title", "text", "summary"])),
                "event_date": _string_value(_mapping_value(row, ["event_date", "effective_date"])),
            })
    elif primary_text:
        result["issuer_cik"] = result["issuer_cik"] or _tag_value(
            primary_text, ["issuerCik", "issuerCikNumber"],
        ) or _sec_header_value(primary_text, ["CENTRAL INDEX KEY", "ISSUER CIK"])
        result["issuer_ticker"] = _tag_value(
            primary_text, ["issuerTradingSymbol", "tradingSymbol"],
        ) or _sec_header_value(primary_text, ["TRADING SYMBOL", "ISSUER TRADING SYMBOL"])
        result["issuer_exchange"] = _tag_value(
            primary_text, ["securityExchangeName", "exchange"],
        ) or _sec_header_value(primary_text, ["EXCHANGE", "SECURITY EXCHANGE NAME"])
        result["issuer_isin"] = _tag_value(primary_text, ["isin"])
        result["manager_cik"] = result["manager_cik"] or _tag_value(
            primary_text, ["filingManagerCik", "managerCik"],
        )
        result["manager_name"] = _tag_value(
            primary_text, ["filingManagerName", "managerName"],
        ) or result["manager_name"]
        if result["manager_name"] is None:
            manager_block = re.search(
                r"<(?:(?:[A-Za-z0-9_]+):)?filingManager\b[^>]*>(.*?)</(?:(?:[A-Za-z0-9_]+):)?filingManager>",
                primary_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if manager_block:
                result["manager_name"] = _tag_value(manager_block.group(1), ["name"])

    if "13D" in normalized_form or "13G" in normalized_form:
        if archive_owners:
            for owner in archive_owners:
                result["ownership_events"].append({
                    "issuer_cik": result["issuer_cik"],
                    "issuer_ticker": result["issuer_ticker"],
                    "issuer_exchange": result["issuer_exchange"],
                    "issuer_isin": result["issuer_isin"],
                    "issuer_cusip": result["issuer_cusip"],
                    "reporting_owner_cik": _string_value(owner.get("cik")),
                    "reporting_owner_name": _string_value(owner.get("name")),
                    "pct_owned": _numeric_value(owner.get("percent_owned")),
                    "is_activist": "13D" in normalized_form,
                    "event_date": primary_event_date,
                })
        elif primary_text:
            owner_name = _tag_value(primary_text, ["reportingOwnerName", "rptOwnerName", "reportingPersonName"])
            pct_owned = _numeric_value(_tag_value(primary_text, ["percentOfClass", "percentOwned", "pctOwned"]))
            if owner_name and pct_owned:
                result["ownership_events"].append({
                    "issuer_cik": result["issuer_cik"],
                    "issuer_ticker": result["issuer_ticker"],
                    "issuer_exchange": result["issuer_exchange"],
                    "issuer_isin": result["issuer_isin"],
                    "issuer_cusip": result["issuer_cusip"],
                    "reporting_owner_cik": _tag_value(primary_text, ["rptOwnerCik", "reportingOwnerCik"]),
                    "reporting_owner_name": owner_name,
                    "pct_owned": pct_owned,
                    "is_activist": "13D" in normalized_form,
                    "event_date": primary_event_date,
                })

    if "13F" in normalized_form and information_text:
        information_payload = None
        if information_text.startswith("{") or information_text.startswith("["):
            try:
                information_payload = json.loads(information_text)
                if isinstance(information_payload, list):
                    information_payload = {"data": information_payload}
            except (TypeError, ValueError) as exc:
                result["parse_error"] = f"Invalid 13F information-table JSON: {exc}"
                return result
        if isinstance(information_payload, dict):
            for row in _mapping_rows(information_payload, ["institutional_holdings", "holdings", "information_table", "infotable"]):
                row_identifiers = _structured_identifiers(row, information_payload)
                result["institutional_holdings"].append({
                    **row_identifiers,
                    "cusip": _string_value(_mapping_value(row, ["cusip"])),
                    "issuer_name": _string_value(_mapping_value(row, ["issuer_name", "name_of_issuer", "nameofissuer"])),
                    "holder_cik": result["manager_cik"],
                    "holder_name": _string_value(_mapping_value(row, ["holder_name", "manager_name", "institution_name"])) or result["manager_name"],
                    "shares": _numeric_value(_mapping_value(row, ["shares", "ssh_prnamt", "sshprnamt"])),
                    "value_usd": _numeric_value(_mapping_value(row, ["value_usd", "market_value_usd", "value"])),
                    "pct_of_portfolio": _numeric_value(_mapping_value(row, ["pct_of_portfolio", "portfolio_percent", "weight"])),
                    "event_date": _string_value(_mapping_value(row, ["event_date", "report_date", "period_of_report"])) or primary_event_date,
                })
    if primary_text and (
        normalized_form.startswith("8-K")
        or normalized_form.startswith("S-1")
        or normalized_form.startswith("424B")
    ):
        known_item_codes = {
            normalized_code
            for code in (item_codes or [])
            if (normalized_code := _string_value(code)) is not None
        }
        item_matches = re.findall(
            r"(?im)^\s*ITEM\s+([0-9]{1,2}\.[0-9]{2})\s*[-:\u2013]?\s*([^\r\n<]{3,500})",
            re.sub(r"<[^>]+>", "\n", primary_text),
        )
        matched_descriptions = {}
        for item_code, description in item_matches:
            normalized_item_code = _string_value(item_code)
            if known_item_codes and normalized_item_code not in known_item_codes:
                continue
            matched_descriptions[normalized_item_code] = _string_value(description)
        for normalized_item_code in sorted(known_item_codes | set(matched_descriptions)):
            result["material_events"].append({
                "issuer_cik": result["issuer_cik"],
                "issuer_ticker": result["issuer_ticker"],
                "issuer_exchange": result["issuer_exchange"],
                "issuer_isin": result["issuer_isin"],
                "item_code": normalized_item_code,
                "description": matched_descriptions.get(normalized_item_code) or f"SEC Item {normalized_item_code}",
                "event_date": primary_event_date,
            })
    return result


parse_sec_content = F.udf(_parse_sec_content, parsed_content_schema)
parsed_raw = raw.withColumn(
    "parsed_content",
    parse_sec_content(
        F.when(
            F.col("source_id").isin("sec_8k", "sec_s1"), F.lit(None).cast("string")
        ).otherwise(F.col("primary_document_content")),
        F.when(
            F.col("source_id") == "sec_13f", F.lit(None).cast("string")
        ).otherwise(F.col("information_table_content")),
        F.col("filing_type"),
        F.col("registrant_cik"),
        F.col("filer_cik"),
        F.col("filer_name"),
        F.col("subject_issuer"),
        F.col("reporting_owners"),
        F.col("item_codes"),
    ),
).cache()

filing_valid = F.coalesce(
    F.col("source_id").isin("sec_13f", "sec_13dg", "sec_8k", "sec_s1")
    & F.col("accession_no").isNotNull()
    & F.col("filing_type").isNotNull()
    & F.col("batch_id").isNotNull()
    & F.col("ingest_ts").isNotNull()
    & F.col("event_date").isNotNull()
    & F.col("knowledge_date").isNotNull()
    & (F.col("event_date") <= F.col("knowledge_date"))
    & (F.col("knowledge_date") <= F.current_date()),
    F.lit(False),
)
filing_dq_failures = parsed_raw.filter(~filing_valid)
filing_pass = parsed_raw.filter(filing_valid).withColumn(
    "filing_revision_hash",
    _revision_hash(
        F.col("source_id"), F.col("accession_no"), F.col("filing_type"),
        F.col("filer_name"), F.col("filing_url"), F.col("content_hash"),
        F.col("event_date"), F.col("knowledge_date"),
    ),
)
silver_sec_filing_df = _first_revision(
    filing_pass,
    ["source_id", "accession_no", "filing_revision_hash"],
).select(
    "source_id", "accession_no", "filing_type", "filer_name", "filing_url",
    "content_hash", "filing_revision_hash", "event_date", "knowledge_date",
    "batch_id", "ingest_ts", F.current_timestamp().alias("loaded_at"),
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_sec_filing (
        source_id STRING NOT NULL,
        accession_no STRING NOT NULL,
        filing_type STRING NOT NULL,
        filer_name STRING,
        filing_url STRING,
        content_hash STRING,
        filing_revision_hash STRING NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        batch_id STRING NOT NULL,
        ingest_ts TIMESTAMP NOT NULL,
        loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
_merge_canonical_silver(
    "silver_sec_filing",
    silver_sec_filing_df,
    "t.source_id = s.source_id AND t.accession_no = s.accession_no "
    "AND t.filing_revision_hash = s.filing_revision_hash",
)

security_versions = spark.table("dim_security").select(
    F.col("security_sk").alias("resolved_security_sk"),
    F.regexp_replace(F.trim(F.col("cik")), "^0+", "").alias("dim_cik"),
    F.upper(F.trim(F.col("ticker"))).alias("dim_ticker"),
    F.upper(F.trim(F.col("exchange"))).alias("dim_exchange"),
    F.upper(F.trim(F.col("isin"))).alias("dim_isin"),
    F.col("company_name").alias("dim_company_name"),
    "valid_from", "valid_to",
)

security_identifier_lookup = None
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
    security_identifier_lookup = spark.table("dim_security_identifier").select(
        F.col("security_sk").alias("identifier_security_sk"),
        F.upper(F.trim(F.col("identifier_type"))).alias("identifier_type_norm"),
        F.upper(F.regexp_replace(F.trim(F.col("identifier_value")), r"[^A-Za-z0-9]", "")).alias("identifier_value_norm"),
        F.col("valid_from").alias("identifier_valid_from"),
        F.col("valid_to").alias("identifier_valid_to"),
    )


def _resolve_security(source_df):
    candidates = (
        source_df
        .withColumn("issuer_cik_norm", F.regexp_replace(F.trim(F.col("issuer_cik")), "^0+", ""))
        .withColumn("issuer_ticker_norm", F.upper(F.trim(F.col("issuer_ticker"))))
        .withColumn("issuer_exchange_norm", F.upper(F.trim(F.col("issuer_exchange"))))
        .withColumn("issuer_isin_norm", F.upper(F.trim(F.col("issuer_isin"))))
    )
    cik_match = (
        F.col("issuer_cik_norm").isNotNull()
        & (F.length(F.col("issuer_cik_norm")) > 0)
        & (F.col("issuer_cik_norm") == F.col("dim_cik"))
    )
    ticker_match = (
        (F.col("issuer_cik_norm").isNull() | (F.length(F.col("issuer_cik_norm")) == 0))
        & F.col("issuer_ticker_norm").isNotNull()
        & F.col("issuer_exchange_norm").isNotNull()
        & (F.col("issuer_ticker_norm") == F.col("dim_ticker"))
        & (F.col("issuer_exchange_norm") == F.col("dim_exchange"))
    )
    isin_match = (
        (F.col("issuer_cik_norm").isNull() | (F.length(F.col("issuer_cik_norm")) == 0))
        & (F.col("issuer_ticker_norm").isNull() | (F.length(F.col("issuer_ticker_norm")) == 0))
        & F.col("issuer_isin_norm").isNotNull()
        & (F.col("issuer_isin_norm") == F.col("dim_isin"))
    )
    resolution_window = Window.partitionBy("row_key")
    return (
        candidates.join(
            security_versions,
            (candidates.event_date >= security_versions.valid_from)
            & (
                security_versions.valid_to.isNull()
                | (candidates.event_date < security_versions.valid_to)
            )
            & (cik_match | ticker_match | isin_match),
            "left",
        )
        .withColumn(
            "security_match_count",
            F.count(F.col("resolved_security_sk")).over(resolution_window),
        )
        .withColumn(
            "security_sk",
            F.when(F.col("security_match_count") == 1, F.col("resolved_security_sk")),
        )
        .withColumn(
            "resolution_method",
            F.when(F.col("security_sk").isNull(), F.lit(None))
            .when(F.col("issuer_cik_norm").isNotNull() & (F.length("issuer_cik_norm") > 0), F.lit("exact_cik_pit"))
            .when(F.col("issuer_ticker_norm").isNotNull() & (F.length("issuer_ticker_norm") > 0), F.lit("exact_ticker_exchange_pit"))
            .otherwise(F.lit("exact_isin_pit")),
        )
        .dropDuplicates(["row_key"])
        .drop(
            "resolved_security_sk", "dim_cik", "dim_ticker", "dim_exchange", "dim_isin",
            "dim_company_name", "valid_from", "valid_to", "issuer_cik_norm", "issuer_ticker_norm",
            "issuer_exchange_norm", "issuer_isin_norm",
        )
    )


def _resolve_cusip(source_df):
    if "cusip" not in source_df.columns or security_identifier_lookup is None:
        return source_df

    direct_or_without_cusip = source_df.filter(
        F.col("security_sk").isNotNull()
        | F.col("cusip").isNull()
        | (F.length(F.trim(F.col("cusip"))) == 0)
    )
    cusip_candidates = (
        source_df
        .filter(
            F.col("security_sk").isNull()
            & F.col("cusip").isNotNull()
            & (F.length(F.trim(F.col("cusip"))) > 0)
        )
        .drop("security_match_count", "resolution_method")
        .withColumn(
            "cusip_norm",
            F.upper(F.regexp_replace(F.trim(F.col("cusip")), r"[^A-Za-z0-9]", "")),
        )
    )
    bridge_window = Window.partitionBy("row_key")
    bridge_resolved = (
        cusip_candidates.join(
            security_identifier_lookup,
            (F.col("identifier_type_norm") == F.lit("CUSIP"))
            & (F.col("cusip_norm") == F.col("identifier_value_norm"))
            & (F.col("event_date") >= F.col("identifier_valid_from"))
            & (
                F.col("identifier_valid_to").isNull()
                | (F.col("event_date") < F.col("identifier_valid_to"))
            ),
            "left",
        )
        .withColumn(
            "security_match_count",
            F.count(F.col("identifier_security_sk")).over(bridge_window),
        )
        .withColumn(
            "security_sk",
            F.when(
                F.col("security_match_count") == 1,
                F.col("identifier_security_sk"),
            ),
        )
        .withColumn(
            "resolution_method",
            F.when(F.col("security_sk").isNotNull(), F.lit("exact_cusip_identifier_pit")),
        )
        .dropDuplicates(["row_key"])
        .drop(
            "identifier_security_sk", "identifier_type_norm", "identifier_value_norm",
            "identifier_valid_from", "identifier_valid_to", "cusip_norm",
        )
    )
    return direct_or_without_cusip.unionByName(bridge_resolved)


def _normalized_security_name(column):
    normalized = F.upper(column)
    normalized = F.regexp_replace(
        normalized,
        r"\b(CLASS|COM|COMMON|STOCK|SHARES?|INCORPORATED|INC|CORPORATION|CORP|LIMITED|LTD|PLC|SA|NV|AG|HOLDINGS?|GROUP|THE)\b",
        " ",
    )
    return F.regexp_replace(normalized, r"[^A-Z0-9]", "")


def _resolve_unique_issuer_name(source_df):
    if "issuer_name" not in source_df.columns:
        return source_df
    already_resolved = source_df.filter(F.col("security_sk").isNotNull())
    name_candidates = (
        source_df.filter(
            F.col("security_sk").isNull()
            & F.col("issuer_name").isNotNull()
            & (F.length(F.trim(F.col("issuer_name"))) > 0)
        )
        .drop("security_match_count", "resolution_method")
        .withColumn("issuer_name_norm", _normalized_security_name(F.col("issuer_name")))
    )
    security_name_lookup = security_versions.withColumn(
        "dim_company_name_norm", _normalized_security_name(F.col("dim_company_name")),
    )
    name_window = Window.partitionBy("row_key")
    name_resolved = (
        name_candidates.join(
            security_name_lookup,
            (F.col("issuer_name_norm") == F.col("dim_company_name_norm"))
            & (F.length(F.col("issuer_name_norm")) > 0)
            & (F.col("event_date") >= F.col("valid_from"))
            & (F.col("valid_to").isNull() | (F.col("event_date") < F.col("valid_to"))),
            "left",
        )
        .withColumn(
            "security_match_count",
            F.count(F.col("resolved_security_sk")).over(name_window),
        )
        .withColumn(
            "security_sk",
            F.when(F.col("security_match_count") == 1, F.col("resolved_security_sk")),
        )
        .withColumn(
            "resolution_method",
            F.when(
                F.col("security_sk").isNotNull(),
                F.lit("exact_normalized_issuer_name_pit"),
            ),
        )
        .dropDuplicates(["row_key"])
        .drop(
            "resolved_security_sk", "dim_cik", "dim_ticker", "dim_exchange",
            "dim_isin", "dim_company_name", "valid_from", "valid_to",
            "issuer_name_norm", "dim_company_name_norm",
        )
    )
    no_name = source_df.filter(
        F.col("security_sk").isNull()
        & (F.col("issuer_name").isNull() | (F.length(F.trim(F.col("issuer_name"))) == 0))
    )
    return already_resolved.unionByName(name_resolved).unionByName(no_name)


def _with_canonical_entity_identity(source_df, cik_column: str, name_column: str):
    return (
        source_df
        .withColumn(
            "entity_cik_normalized",
            F.regexp_replace(F.trim(F.col(cik_column)), "^0+", ""),
        )
        .withColumn(
            "entity_name_normalized",
            F.trim(F.regexp_replace(F.lower(F.col(name_column)), r"[^\p{L}\p{N}]+", " ")),
        )
        .withColumn(
            "entity_natural_id",
            F.when(
                F.col("entity_cik_normalized").isNotNull()
                & (F.length(F.col("entity_cik_normalized")) > 0),
                F.concat(F.lit("sec_cik:"), F.col("entity_cik_normalized")),
            ).when(
                F.col("entity_name_normalized").isNotNull()
                & (F.length(F.col("entity_name_normalized")) > 0),
                F.concat(F.lit("sec_name:"), F.sha2(F.col("entity_name_normalized"), 256)),
            ),
        )
    )


def _upsert_canonical_entities(source_df, entity_type: str, role: str):
    entity_seed = (
        source_df
        .filter(F.col("entity_natural_id").isNotNull())
        .groupBy("entity_natural_id")
        .agg(
            F.min("entity_name").alias("name"),
            F.min("entity_cik_normalized").alias("cik"),
        )
        .withColumn("entity_sk", _positive_sk(F.col("entity_natural_id")))
        .withColumn("entity_type", F.lit(entity_type))
        .withColumn("role", F.lit(role))
        .select("entity_sk", "entity_natural_id", "entity_type", "name", "role", "cik")
    )
    if not entity_seed.isEmpty():
        (
            DeltaTable.forName(spark, "dim_entity")
            .alias("t")
            .merge(entity_seed.alias("s"), "t.entity_natural_id = s.entity_natural_id")
            .whenMatchedUpdate(set={
                "name": "coalesce(t.name, s.name)",
                "cik": "coalesce(t.cik, s.cik)",
            })
            .whenNotMatchedInsertAll()
            .execute()
        )
    entity_lookup = spark.table("dim_entity").select("entity_natural_id", "entity_sk")
    entity_conflicts = (
        entity_lookup
        .groupBy("entity_natural_id")
        .agg(F.countDistinct("entity_sk").alias("entity_sk_count"))
        .filter(F.col("entity_sk_count") != 1)
        .count()
    )
    if entity_conflicts:
        raise RuntimeError(
            f"Canonical SEC entity resolution failed: conflicting_entity_natural_ids={entity_conflicts}"
        )
    entity_orphans = (
        source_df.select("entity_natural_id").distinct()
        .join(entity_lookup, "entity_natural_id", "left_anti")
        .count()
    )
    if entity_orphans:
        raise RuntimeError(
            f"Canonical SEC entity resolution failed: unresolved_entity_natural_ids={entity_orphans}"
        )
    return entity_lookup


ownership_rows = (
    filing_pass.filter(F.col("source_id") == "sec_13dg")
    .withColumn("ownership", F.explode_outer("parsed_content.ownership_events"))
    .select(
        "source_id", "accession_no", "filing_type", "batch_id", "ingest_ts", "raw_record",
        F.coalesce(F.col("ownership.issuer_cik"), F.col("parsed_content.issuer_cik")).alias("issuer_cik"),
        F.coalesce(F.col("ownership.issuer_ticker"), F.col("parsed_content.issuer_ticker")).alias("issuer_ticker"),
        F.coalesce(F.col("ownership.issuer_exchange"), F.col("parsed_content.issuer_exchange")).alias("issuer_exchange"),
        F.coalesce(F.col("ownership.issuer_isin"), F.col("parsed_content.issuer_isin")).alias("issuer_isin"),
        F.coalesce(F.col("ownership.issuer_cusip"), F.col("parsed_content.issuer_cusip")).alias("cusip"),
        F.col("ownership").isNotNull().alias("detail_present"),
        F.col("ownership.reporting_owner_cik").alias("reporting_owner_cik"),
        F.col("ownership.reporting_owner_name").alias("reporting_owner_name"),
        F.col("ownership.pct_owned").cast(DecimalType(9, 6)).alias("pct_owned"),
        F.col("ownership.is_activist").alias("is_activist"),
        F.coalesce(F.to_date("ownership.event_date"), F.col("event_date")).alias("event_date"),
        "knowledge_date",
    )
    .withColumn(
        "row_key",
        F.sha2(F.concat_ws(
            "|", "source_id", "accession_no", "reporting_owner_cik", "reporting_owner_name",
            F.col("pct_owned").cast("string"), F.col("event_date").cast("string"),
        ), 256),
    )
)
ownership_rows = (
    _with_canonical_entity_identity(
        ownership_rows, "reporting_owner_cik", "reporting_owner_name",
    )
    .withColumn("entity_name", F.col("reporting_owner_name"))
)
ownership_valid = F.coalesce(
    F.col("entity_natural_id").isNotNull()
    & (F.length(F.trim(F.col("reporting_owner_name"))) >= 3)
    & F.col("reporting_owner_name").rlike("[A-Za-z].*[A-Za-z]")
    & ~F.upper(F.trim(F.col("reporting_owner_name"))).isin("I.R.S.", "IRS")
    & F.col("pct_owned").isNotNull()
    & (F.col("pct_owned") >= 0)
    & (F.col("pct_owned") <= 100)
    & F.col("event_date").isNotNull()
    & (F.col("event_date") <= F.col("knowledge_date")),
    F.lit(False),
)
ownership_dq_failures = ownership_rows.filter(F.col("detail_present") & ~ownership_valid)
ownership_candidates = ownership_rows.filter(ownership_valid)
ownership_resolved = _resolve_cusip(_resolve_security(ownership_candidates))
ownership_unresolved = ownership_resolved.filter(
    F.col("security_sk").isNull() | (F.col("security_match_count") != 1)
)
ownership_entity_lookup = _upsert_canonical_entities(
    ownership_candidates, "reporting_owner", "13d_g_reporting_owner",
)
ownership_pass = (
    ownership_resolved.filter(F.col("security_sk").isNotNull() & (F.col("security_match_count") == 1))
    .join(ownership_entity_lookup, "entity_natural_id", "inner")
    .withColumn(
        "ownership_revision_hash",
        _revision_hash(
            F.col("security_sk"), F.col("entity_sk"), F.col("pct_owned"),
            F.col("filing_type"), F.col("is_activist"), F.col("event_date"),
            F.col("knowledge_date"),
        ),
    )
)
silver_ownership_df = _first_revision(
    ownership_pass,
    ["accession_no", "security_sk", "entity_sk", "ownership_revision_hash"],
).select(
    "security_sk", "entity_sk", "reporting_owner_name", "pct_owned", "filing_type",
    "is_activist", "accession_no", "ownership_revision_hash", "event_date",
    "knowledge_date", "source_id", "batch_id", "ingest_ts",
    F.current_timestamp().alias("loaded_at"),
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_ownership_event (
        security_sk BIGINT NOT NULL,
        entity_sk BIGINT NOT NULL,
        reporting_owner_name STRING,
        pct_owned DECIMAL(9,6) NOT NULL,
        filing_type STRING NOT NULL,
        is_activist BOOLEAN NOT NULL,
        accession_no STRING NOT NULL,
        ownership_revision_hash STRING NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL,
        batch_id STRING NOT NULL,
        ingest_ts TIMESTAMP NOT NULL,
        loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
_merge_canonical_silver(
    "silver_ownership_event",
    silver_ownership_df,
    "t.accession_no = s.accession_no AND t.security_sk = s.security_sk "
    "AND t.entity_sk = s.entity_sk AND t.ownership_revision_hash = s.ownership_revision_hash",
)

def _xpath_values(path):
    return F.expr(f"xpath(information_table_content, '{path}')")


native_13f_holdings = F.arrays_zip(
    _xpath_values('//*[local-name()="infoTable"]/*[local-name()="issuerCik"]/text()').alias("issuer_cik"),
    _xpath_values('//*[local-name()="infoTable"]/*[local-name()="issuerTradingSymbol"]/text()').alias("issuer_ticker"),
    _xpath_values('//*[local-name()="infoTable"]/*[local-name()="exchange"]/text()').alias("issuer_exchange"),
    _xpath_values('//*[local-name()="infoTable"]/*[local-name()="isin"]/text()').alias("issuer_isin"),
    _xpath_values('//*[local-name()="infoTable"]/*[local-name()="cusip"]/text()').alias("cusip"),
    _xpath_values('//*[local-name()="infoTable"]/*[local-name()="nameOfIssuer"]/text()').alias("issuer_name"),
    _xpath_values('//*[local-name()="infoTable"]//*[local-name()="sshPrnamt"]/text()').alias("shares"),
    _xpath_values('//*[local-name()="infoTable"]/*[local-name()="value"]/text()').alias("value_thousands"),
)

holding_rows = (
    filing_pass.filter(F.col("source_id") == "sec_13f")
    .withColumn("holding", F.explode_outer(native_13f_holdings))
    .select(
        "source_id", "accession_no", "batch_id", "ingest_ts", "source_record_hash", "raw_record",
        F.coalesce(F.col("holding.issuer_cik"), F.col("parsed_content.issuer_cik")).alias("issuer_cik"),
        F.coalesce(F.col("holding.issuer_ticker"), F.col("parsed_content.issuer_ticker")).alias("issuer_ticker"),
        F.coalesce(F.col("holding.issuer_exchange"), F.col("parsed_content.issuer_exchange")).alias("issuer_exchange"),
        F.coalesce(F.col("holding.issuer_isin"), F.col("parsed_content.issuer_isin")).alias("issuer_isin"),
        F.col("holding").isNotNull().alias("detail_present"),
        F.col("holding.cusip").alias("cusip"),
        F.col("holding.issuer_name").alias("issuer_name"),
        F.col("parsed_content.manager_cik").alias("holder_cik"),
        F.col("parsed_content.manager_name").alias("holder_name"),
        F.col("holding.shares").cast(DecimalType(20, 4)).alias("shares"),
        (F.col("holding.value_thousands").cast(DecimalType(20, 2)) * F.lit(1000)).alias("value_usd"),
        F.lit(None).cast(DecimalType(9, 6)).alias("pct_of_portfolio"),
        F.col("event_date"),
        "knowledge_date",
    )
    .withColumn(
        "row_key",
        F.sha2(F.concat_ws(
            "|", "source_id", "accession_no", "holder_cik", "holder_name", "issuer_cik", "issuer_ticker",
            "issuer_isin", "cusip", F.col("shares").cast("string"), F.col("value_usd").cast("string"),
        ), 256),
    )
)
holding_rows = (
    _with_canonical_entity_identity(holding_rows, "holder_cik", "holder_name")
    .withColumn("entity_name", F.col("holder_name"))
)
holding_valid = F.coalesce(
    F.col("entity_natural_id").isNotNull()
    & (F.col("shares").isNotNull() | F.col("value_usd").isNotNull())
    & (F.col("shares").isNull() | (F.col("shares") >= 0))
    & (F.col("value_usd").isNull() | (F.col("value_usd") >= 0))
    & (F.col("pct_of_portfolio").isNull() | ((F.col("pct_of_portfolio") >= 0) & (F.col("pct_of_portfolio") <= 100)))
    & F.col("event_date").isNotNull()
    & (F.col("event_date") <= F.col("knowledge_date")),
    F.lit(False),
)
holding_dq_failures = holding_rows.filter(F.col("detail_present") & ~holding_valid)
holding_candidates = holding_rows.filter(holding_valid)
holding_resolved = _resolve_unique_issuer_name(
    _resolve_cusip(_resolve_security(holding_candidates))
)
holding_unresolved = holding_resolved.filter(
    F.col("security_sk").isNull() | (F.col("security_match_count") != 1)
)
holding_ambiguous = holding_unresolved.filter(
    F.col("security_match_count") > 1
)
holding_out_of_scope = holding_unresolved.join(
    holding_ambiguous.select("row_key"), "row_key", "left_anti",
)
holding_entity_lookup = _upsert_canonical_entities(
    holding_candidates, "institution", "13f_filing_manager",
)
holding_pass = (
    holding_resolved.filter(F.col("security_sk").isNotNull() & (F.col("security_match_count") == 1))
    .join(holding_entity_lookup, "entity_natural_id", "inner")
    .withColumn(
        "natural_key",
        F.sha2(F.concat_ws(
            "|", "source_id", "accession_no", F.col("security_sk").cast("string"),
            "entity_natural_id",
        ), 256),
    )
    .withColumn("shares_delta_qoq", F.lit(None).cast(DecimalType(20, 4)))
    .withColumn(
        "holding_revision_hash",
        _revision_hash(
            F.col("security_sk"), F.col("entity_sk"), F.col("shares"), F.col("value_usd"),
            F.col("pct_of_portfolio"), F.col("event_date"), F.col("knowledge_date"),
        ),
    )
)
silver_13f_holding_df = _first_revision(
    holding_pass,
    ["natural_key", "holding_revision_hash"],
).select(
    "natural_key", "security_sk", "entity_sk", "holder_name", "cusip", "shares", "value_usd",
    "shares_delta_qoq", "pct_of_portfolio", "accession_no", "holding_revision_hash",
    "event_date", "knowledge_date", "source_id", "batch_id", "ingest_ts",
    "source_record_hash", F.current_timestamp().alias("loaded_at"),
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_13f_holding (
        natural_key STRING NOT NULL,
        security_sk BIGINT NOT NULL,
        entity_sk BIGINT NOT NULL,
        holder_name STRING,
        cusip STRING,
        shares DECIMAL(20,4),
        value_usd DECIMAL(20,2),
        shares_delta_qoq DECIMAL(20,4),
        pct_of_portfolio DECIMAL(9,6),
        accession_no STRING NOT NULL,
        holding_revision_hash STRING NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL,
        batch_id STRING NOT NULL,
        ingest_ts TIMESTAMP NOT NULL,
        source_record_hash STRING NOT NULL,
        loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
_ensure_columns("silver_13f_holding", {
    "natural_key": "natural_key STRING",
    "cusip": "cusip STRING",
    "source_record_hash": "source_record_hash STRING",
})
_merge_canonical_silver(
    "silver_13f_holding",
    silver_13f_holding_df,
    "t.natural_key = s.natural_key AND t.holding_revision_hash = s.holding_revision_hash",
)

parsed_material_rows = (
    filing_pass.filter(F.col("source_id") == "sec_8k")
    .withColumn("item_code", F.explode_outer("item_codes"))
    .select(
        "source_id", "accession_no", "filing_type", "batch_id", "ingest_ts", "raw_record",
        F.col("parsed_content.issuer_cik").alias("issuer_cik"),
        F.col("parsed_content.issuer_ticker").alias("issuer_ticker"),
        F.col("parsed_content.issuer_exchange").alias("issuer_exchange"),
        F.col("parsed_content.issuer_isin").alias("issuer_isin"),
        F.col("item_code").isNotNull().alias("detail_present"),
        "item_code",
        F.concat(F.lit("SEC Item "), F.col("item_code")).alias("description"),
        "event_date", "knowledge_date",
    )
)
s1_material_rows = filing_pass.filter(F.col("source_id") == "sec_s1").select(
    "source_id", "accession_no", "filing_type", "batch_id", "ingest_ts", "raw_record",
    F.col("parsed_content.issuer_cik").alias("issuer_cik"),
    F.col("parsed_content.issuer_ticker").alias("issuer_ticker"),
    F.col("parsed_content.issuer_exchange").alias("issuer_exchange"),
    F.col("parsed_content.issuer_isin").alias("issuer_isin"),
    F.lit(True).alias("detail_present"),
    F.col("filing_type").alias("item_code"),
    F.when(
        F.upper(F.col("filing_type")).endswith("/A"),
        F.lit("SEC registration statement amendment"),
    ).otherwise(F.lit("SEC registration statement")).alias("description"),
    "event_date", "knowledge_date",
)
material_rows = (
    parsed_material_rows.unionByName(s1_material_rows)
    .withColumn(
        "row_key",
        F.sha2(F.concat_ws(
            "|", "source_id", "accession_no", "item_code", "description",
            F.col("event_date").cast("string"),
        ), 256),
    )
)
material_valid = F.coalesce(
    F.col("item_code").isNotNull()
    & F.col("description").isNotNull()
    & F.col("event_date").isNotNull()
    & (F.col("event_date") <= F.col("knowledge_date")),
    F.lit(False),
)
material_dq_failures = material_rows.filter(F.col("detail_present") & ~material_valid)
material_candidates = material_rows.filter(material_valid)
material_resolved = _resolve_security(material_candidates)
material_unresolved = material_resolved.filter(
    F.col("security_sk").isNull() | (F.col("security_match_count") != 1)
)
material_pass = (
    material_resolved.filter(F.col("security_sk").isNotNull() & (F.col("security_match_count") == 1))
    .withColumn(
        "material_event_revision_hash",
        _revision_hash(
            F.col("security_sk"), F.col("item_code"), F.col("description"),
            F.col("filing_type"), F.col("event_date"), F.col("knowledge_date"),
        ),
    )
    .withColumn(
        "event_sk",
        _positive_sk(F.col("source_id"), F.col("accession_no"), F.col("material_event_revision_hash")),
    )
)
silver_material_df = _first_revision(
    material_pass,
    ["accession_no", "security_sk", "material_event_revision_hash"],
).select(
    "event_sk", "security_sk", "item_code", "description", "accession_no", "filing_type",
    "material_event_revision_hash", "event_date", "knowledge_date", "source_id",
    "batch_id", "ingest_ts", F.current_timestamp().alias("loaded_at"),
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_material_event (
        event_sk BIGINT NOT NULL,
        security_sk BIGINT NOT NULL,
        item_code STRING NOT NULL,
        description STRING NOT NULL,
        accession_no STRING NOT NULL,
        filing_type STRING NOT NULL,
        material_event_revision_hash STRING NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        source_id STRING NOT NULL,
        batch_id STRING NOT NULL,
        ingest_ts TIMESTAMP NOT NULL,
        loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")
_merge_canonical_silver(
    "silver_material_event",
    silver_material_df,
    "t.accession_no = s.accession_no AND t.security_sk = s.security_sk "
    "AND t.material_event_revision_hash = s.material_event_revision_hash",
)

parse_errors = parsed_raw.filter(
    F.col("content_present") & F.col("parsed_content.parse_error").isNotNull()
).select(
    F.sha2(F.concat_ws(
        ":", "source_id", "accession_no", "batch_id", "content_hash", F.lit("PARSE_ERROR"),
    ), 256).alias("natural_key"),
    "source_id", "batch_id", "raw_record",
    F.col("parsed_content.parse_error").alias("error_msg"),
    F.col("ingest_ts").alias("occurred_at"),
)
_merge_replay_safe(
    "silver_parse_errors",
    parse_errors,
    ["source_id", "batch_id", "raw_record", "error_msg", "occurred_at"],
)

metadata_only_13f = filing_pass.filter(
    (F.col("source_id") == "sec_13f") & ~F.col("raw_content_present")
)
metadata_only_ownership = filing_pass.filter(
    (F.col("source_id") == "sec_13dg") & ~F.col("raw_content_present")
)
metadata_only_material = filing_pass.filter(
    F.col("source_id").isin("sec_8k", "sec_s1") & ~F.col("raw_content_present")
)
incomplete_archive_evidence = filing_pass.filter(
    F.col("raw_content_present") & ~F.col("archive_complete")
)
unparsed_13f = filing_pass.filter(
    (F.col("source_id") == "sec_13f")
    & F.col("content_present")
    & (F.size(_xpath_values('//*[local-name()="infoTable"]/*[local-name()="cusip"]/text()')) == 0)
    & F.col("parsed_content.parse_error").isNull()
)
unparsed_ownership = filing_pass.filter(
    (F.col("source_id") == "sec_13dg")
    & F.col("content_present")
    & (F.size(F.col("parsed_content.ownership_events")) == 0)
    & F.col("parsed_content.parse_error").isNull()
)
unparsed_material = filing_pass.filter(
    (F.col("source_id") == "sec_8k")
    & F.col("content_present")
    & (F.size(F.col("item_codes")) == 0)
    & F.col("parsed_content.parse_error").isNull()
)

dq_quarantine = (
    _dq_quarantine_rows(filing_dq_failures, ["accession_no", "file_date", "period_of_report"], "INVALID_SEC_FILING_METADATA")
    .unionByName(_dq_quarantine_rows(ownership_dq_failures, ["accession_no", "reporting_owner_name", "pct_owned"], "INVALID_OWNERSHIP_EVENT"))
    .unionByName(_dq_quarantine_rows(holding_dq_failures, ["accession_no", "holder_cik", "holder_name", "shares", "value_usd"], "INVALID_13F_HOLDING"))
    .unionByName(_dq_quarantine_rows(material_dq_failures, ["accession_no", "item_code", "description"], "INVALID_MATERIAL_EVENT"))
    .unionByName(_dq_quarantine_rows(metadata_only_13f, ["accession_no"], "METADATA_ONLY_13F"))
    .unionByName(_dq_quarantine_rows(metadata_only_ownership, ["accession_no"], "METADATA_ONLY_OWNERSHIP"))
    .unionByName(_dq_quarantine_rows(metadata_only_material, ["accession_no"], "METADATA_ONLY_MATERIAL_EVENT"))
    .unionByName(_dq_quarantine_rows(
        incomplete_archive_evidence,
        ["accession_no", "archive_status", "missing_document_classes"],
        "INCOMPLETE_ARCHIVE_EVIDENCE",
    ))
    .unionByName(_dq_quarantine_rows(unparsed_13f, ["accession_no", "content_hash"], "NO_PARSABLE_13F_HOLDINGS"))
    .unionByName(_dq_quarantine_rows(unparsed_ownership, ["accession_no", "content_hash"], "NO_PARSABLE_OWNERSHIP_EVENT"))
    .unionByName(_dq_quarantine_rows(unparsed_material, ["accession_no", "content_hash"], "NO_PARSABLE_MATERIAL_EVENT"))
)
_merge_replay_safe(
    "silver_dq_quarantine",
    dq_quarantine,
    ["source_id", "batch_id", "raw_record", "dq_rule", "quarantined_at"],
)

security_quarantine = (
    _security_quarantine_rows(
        ownership_unresolved,
        F.coalesce(F.col("issuer_cik"), F.col("issuer_ticker"), F.col("issuer_isin"), F.col("cusip")),
        "SECURITY_UNRESOLVED",
        "No unique dim_security version matched the ownership issuer on event_date; CUSIP is resolved only through dim_security_identifier",
    )
    .unionByName(_security_quarantine_rows(
        holding_out_of_scope,
        F.coalesce(F.col("issuer_cik"), F.col("issuer_ticker"), F.col("issuer_isin"), F.col("cusip")),
        "OUT_OF_SCOPE_13F_HOLDING",
        "No unique current US-listed theme-candidate identifier matched; excluded from MVP candidate coverage",
    ))
    .unionByName(_security_quarantine_rows(
        holding_ambiguous,
        F.coalesce(F.col("issuer_cik"), F.col("issuer_ticker"), F.col("issuer_isin"), F.col("cusip")),
        "AMBIGUOUS_13F_CANDIDATE_IDENTIFIER",
        "Multiple current candidate identifiers matched; excluded until the candidate bridge is unique",
    ))
    .unionByName(_security_quarantine_rows(
        material_unresolved,
        F.coalesce(F.col("issuer_cik"), F.col("issuer_ticker"), F.col("issuer_isin")),
        "SECURITY_UNRESOLVED",
        "No unique dim_security version matched the material-event issuer on event_date",
    ))
)
_merge_replay_safe(
    "silver_security_quarantine",
    security_quarantine,
    [
        "source_id", "raw_identifier", "reason", "details", "event_date",
        "knowledge_date", "batch_id", "quarantined_at",
    ],
)

print(
    "E8 SEC Silver merge complete: silver_sec_filing, silver_ownership_event, "
    "silver_13f_holding, silver_material_event, and replay-safe quarantine tables"
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Gold promotion from E8 Silver: SEC filings, holdings, ownership, and material events ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_sec_filing_event (
        filing_event_sk BIGINT NOT NULL,
        accession_no STRING NOT NULL,
        filing_type STRING,
        filer_name STRING,
        filing_revision_hash STRING,
        source_sk INT,
        event_date DATE,
        knowledge_date DATE
    )
    USING DELTA
""")
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_material_event (
        event_sk BIGINT NOT NULL,
        security_sk BIGINT,
        date_sk INT,
        accession_no STRING NOT NULL,
        filing_type STRING,
        description STRING,
        material_event_revision_hash STRING,
        source_sk INT,
        event_date DATE,
        knowledge_date DATE
    )
    USING DELTA
""")
_ensure_columns("fact_sec_filing_event", {
    "filing_revision_hash": "filing_revision_hash STRING",
})
_ensure_columns("fact_institutional_holding", {
    "holding_revision_hash": "holding_revision_hash STRING",
    "silver_source_table": "silver_source_table STRING",
    "silver_natural_key": "silver_natural_key STRING",
    "silver_batch_id": "silver_batch_id STRING",
    "silver_ingest_ts": "silver_ingest_ts TIMESTAMP",
    "silver_source_record_hash": "silver_source_record_hash STRING",
    "silver_loaded_at": "silver_loaded_at TIMESTAMP",
})
_ensure_columns("fact_ownership_event", {
    "ownership_revision_hash": "ownership_revision_hash STRING",
})
_ensure_columns("fact_material_event", {
    "material_event_revision_hash": "material_event_revision_hash STRING",
})

filing_event_df = (
    spark.table("silver_sec_filing")
    .join(processed_batch_ids, "batch_id", "inner")
    .join(source_lookup, "source_id", "left")
    .withColumn(
        "filing_event_sk",
        _positive_sk(F.col("source_id"), F.col("accession_no"), F.col("filing_revision_hash")),
    )
    .select(
        "filing_event_sk", "accession_no", "filing_type", "filer_name",
        "filing_revision_hash", "source_sk", "event_date", "knowledge_date",
    )
)
_merge_insert_only(
    "fact_sec_filing_event",
    filing_event_df,
    "t.accession_no = s.accession_no AND t.source_sk = s.source_sk "
    "AND t.filing_revision_hash = s.filing_revision_hash",
)

sec_13f_holding_df = (
    spark.table("silver_13f_holding")
    .join(processed_batch_ids, "batch_id", "inner")
    .join(source_lookup, "source_id", "left")
    .withColumn("date_sk", _date_sk("event_date"))
    .select(
        "security_sk", "entity_sk", "date_sk", "shares", "value_usd", "shares_delta_qoq",
        "pct_of_portfolio", "accession_no", "holding_revision_hash",
        F.lit("silver_13f_holding").alias("silver_source_table"),
        F.col("natural_key").alias("silver_natural_key"),
        F.col("batch_id").alias("silver_batch_id"),
        F.col("ingest_ts").alias("silver_ingest_ts"),
        F.col("source_record_hash").alias("silver_source_record_hash"),
        F.col("loaded_at").alias("silver_loaded_at"),
        "source_sk",
        "event_date", "knowledge_date",
    )
)
_merge_insert_only(
    "fact_institutional_holding",
    sec_13f_holding_df,
    "t.accession_no = s.accession_no AND t.security_sk = s.security_sk "
    "AND t.entity_sk = s.entity_sk AND t.source_sk = s.source_sk "
    "AND t.holding_revision_hash = s.holding_revision_hash",
)

ownership_df = (
    spark.table("silver_ownership_event")
    .join(processed_batch_ids, "batch_id", "inner")
    .join(source_lookup, "source_id", "left")
    .withColumn("date_sk", _date_sk("event_date"))
    .select(
        "security_sk", "entity_sk", "date_sk", "pct_owned", "filing_type", "is_activist",
        "accession_no", "ownership_revision_hash", "source_sk", "event_date", "knowledge_date",
    )
)
_merge_insert_only(
    "fact_ownership_event",
    ownership_df,
    "t.accession_no = s.accession_no AND t.security_sk = s.security_sk "
    "AND t.entity_sk = s.entity_sk AND t.ownership_revision_hash = s.ownership_revision_hash",
)

material_event_df = (
    spark.table("silver_material_event")
    .join(processed_batch_ids, "batch_id", "inner")
    .join(source_lookup, "source_id", "left")
    .withColumn("date_sk", _date_sk("event_date"))
    .select(
        "event_sk", "security_sk", "date_sk", "accession_no", "filing_type", "description",
        "material_event_revision_hash", "source_sk", "event_date", "knowledge_date",
    )
)
_merge_insert_only(
    "fact_material_event",
    material_event_df,
    "t.accession_no = s.accession_no AND t.security_sk = s.security_sk "
    "AND t.material_event_revision_hash = s.material_event_revision_hash",
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

missing_pit = spark.sql("""
    SELECT SUM(n) AS n
    FROM (
        SELECT COUNT(*) AS n FROM fact_institutional_holding
        WHERE source_sk = 7 AND (event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date)
        UNION ALL SELECT COUNT(*) AS n FROM fact_ownership_event
        WHERE source_sk = 8 AND (event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date)
        UNION ALL SELECT COUNT(*) AS n FROM fact_material_event
        WHERE source_sk IN (9, 10) AND (event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date)
        UNION ALL SELECT COUNT(*) AS n FROM fact_sec_filing_event
        WHERE source_sk IN (7, 8, 9, 10) AND (event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date)
    ) x
""").collect()[0].n

gold_missing_revision_hash = spark.sql("""
    SELECT SUM(n) AS n
    FROM (
        SELECT COUNT(*) AS n FROM fact_institutional_holding
        WHERE source_sk = 7 AND holding_revision_hash IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_ownership_event
        WHERE source_sk = 8 AND ownership_revision_hash IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_material_event
        WHERE source_sk IN (9, 10) AND material_event_revision_hash IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_sec_filing_event
        WHERE source_sk IN (7, 8, 9, 10) AND filing_revision_hash IS NULL
    ) x
""").collect()[0].n

gold_13f_missing_lineage = spark.sql("""
    SELECT COUNT(*) AS n
    FROM fact_institutional_holding
    WHERE source_sk = 7
      AND (
          holding_revision_hash IS NULL
          OR silver_source_table IS NULL
          OR silver_source_table <> 'silver_13f_holding'
          OR silver_natural_key IS NULL
          OR silver_batch_id IS NULL
          OR silver_ingest_ts IS NULL
          OR silver_source_record_hash IS NULL
          OR silver_loaded_at IS NULL
      )
""").collect()[0].n

silver_invalid = spark.sql("""
    SELECT SUM(n) AS n
    FROM (
        SELECT COUNT(*) AS n FROM silver_sec_filing
        WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
           OR filing_revision_hash IS NULL
          UNION ALL SELECT COUNT(*) AS n FROM silver_13f_holding
          WHERE natural_key IS NULL OR security_sk IS NULL OR entity_sk IS NULL
              OR event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
              OR holding_revision_hash IS NULL OR batch_id IS NULL OR ingest_ts IS NULL
              OR source_record_hash IS NULL OR loaded_at IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM silver_ownership_event
        WHERE security_sk IS NULL OR event_date IS NULL OR knowledge_date IS NULL
           OR event_date > knowledge_date OR ownership_revision_hash IS NULL
              OR reporting_owner_name IS NULL OR LENGTH(TRIM(reporting_owner_name)) < 3
              OR UPPER(TRIM(reporting_owner_name)) IN ('I.R.S.', 'IRS')
           OR pct_owned IS NULL OR pct_owned < 0 OR pct_owned > 100
        UNION ALL SELECT COUNT(*) AS n FROM silver_material_event
        WHERE security_sk IS NULL OR event_date IS NULL OR knowledge_date IS NULL
           OR event_date > knowledge_date OR material_event_revision_hash IS NULL
    ) x
""").collect()[0].n

silver_13f_duplicate_revisions = spark.sql("""
    SELECT COUNT(*) AS n
    FROM (
        SELECT natural_key, holding_revision_hash
        FROM silver_13f_holding
        GROUP BY natural_key, holding_revision_hash
        HAVING COUNT(*) > 1
    ) x
""").collect()[0].n

gold_filing_without_silver = (
    spark.table("fact_sec_filing_event").alias("g")
    .join(
        spark.table("silver_sec_filing").alias("s").join(source_lookup.alias("l"), "source_id"),
        (F.col("g.accession_no") == F.col("s.accession_no"))
        & (F.col("g.filing_revision_hash") == F.col("s.filing_revision_hash"))
        & (F.col("g.filing_event_sk") == _positive_sk(F.col("s.source_id"), F.col("s.accession_no"), F.col("s.filing_revision_hash")))
        & F.col("g.filing_type").eqNullSafe(F.col("s.filing_type"))
        & F.col("g.filer_name").eqNullSafe(F.col("s.filer_name"))
        & (F.col("g.source_sk") == F.col("l.source_sk"))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date")),
        "left_anti",
    )
    .filter(F.col("g.source_sk").isin(7, 8, 9, 10))
    .count()
)
gold_holding_without_silver = (
    spark.table("fact_institutional_holding").filter(F.col("source_sk") == 7).alias("g")
    .join(
        spark.table("silver_13f_holding").alias("s").join(source_lookup.alias("l"), "source_id"),
        (F.col("g.accession_no") == F.col("s.accession_no"))
        & (F.col("g.security_sk") == F.col("s.security_sk"))
        & (F.col("g.entity_sk") == F.col("s.entity_sk"))
        & (F.col("g.holding_revision_hash") == F.col("s.holding_revision_hash"))
        & (F.col("g.date_sk") == F.date_format(F.col("s.event_date"), "yyyyMMdd").cast("int"))
        & F.col("g.shares").eqNullSafe(F.col("s.shares"))
        & F.col("g.value_usd").eqNullSafe(F.col("s.value_usd"))
        & F.col("g.shares_delta_qoq").eqNullSafe(F.col("s.shares_delta_qoq"))
        & F.col("g.pct_of_portfolio").eqNullSafe(F.col("s.pct_of_portfolio"))
        & (F.col("g.silver_source_table") == F.lit("silver_13f_holding"))
        & (F.col("g.silver_natural_key") == F.col("s.natural_key"))
        & (F.col("g.silver_batch_id") == F.col("s.batch_id"))
        & (F.col("g.silver_ingest_ts") == F.col("s.ingest_ts"))
        & (F.col("g.silver_source_record_hash") == F.col("s.source_record_hash"))
        & (F.col("g.silver_loaded_at") == F.col("s.loaded_at"))
        & (F.col("g.source_sk") == F.col("l.source_sk"))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date")),
        "left_anti",
    )
    .count()
)
gold_ownership_without_silver = (
    spark.table("fact_ownership_event").alias("g")
    .join(
        spark.table("silver_ownership_event").alias("s").join(source_lookup.alias("l"), "source_id"),
        (F.col("g.accession_no") == F.col("s.accession_no"))
        & (F.col("g.security_sk") == F.col("s.security_sk"))
        & (F.col("g.entity_sk") == F.col("s.entity_sk"))
        & (F.col("g.ownership_revision_hash") == F.col("s.ownership_revision_hash"))
        & (F.col("g.date_sk") == F.date_format(F.col("s.event_date"), "yyyyMMdd").cast("int"))
        & F.col("g.pct_owned").eqNullSafe(F.col("s.pct_owned"))
        & F.col("g.filing_type").eqNullSafe(F.col("s.filing_type"))
        & F.col("g.is_activist").eqNullSafe(F.col("s.is_activist"))
        & (F.col("g.source_sk") == F.col("l.source_sk"))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date")),
        "left_anti",
    )
    .filter(F.col("g.source_sk") == 8)
    .count()
)
gold_material_without_silver = (
    spark.table("fact_material_event").alias("g")
    .join(
        spark.table("silver_material_event").alias("s").join(source_lookup.alias("l"), "source_id"),
        (F.col("g.event_sk") == F.col("s.event_sk"))
        & (F.col("g.accession_no") == F.col("s.accession_no"))
        & (F.col("g.security_sk") == F.col("s.security_sk"))
        & (F.col("g.material_event_revision_hash") == F.col("s.material_event_revision_hash"))
        & (F.col("g.date_sk") == F.date_format(F.col("s.event_date"), "yyyyMMdd").cast("int"))
        & F.col("g.filing_type").eqNullSafe(F.col("s.filing_type"))
        & F.col("g.description").eqNullSafe(F.col("s.description"))
        & (F.col("g.source_sk") == F.col("l.source_sk"))
        & (F.col("g.event_date") == F.col("s.event_date"))
        & (F.col("g.knowledge_date") == F.col("s.knowledge_date")),
        "left_anti",
    )
    .filter(F.col("g.source_sk").isin(9, 10))
    .count()
)
silver_filing_without_gold = (
    spark.table("silver_sec_filing").alias("s")
    .join(source_lookup.alias("l"), "source_id")
    .join(
        spark.table("fact_sec_filing_event").alias("g"),
        (F.col("g.accession_no") == F.col("s.accession_no"))
        & (F.col("g.filing_revision_hash") == F.col("s.filing_revision_hash"))
        & (F.col("g.source_sk") == F.col("l.source_sk")),
        "left_anti",
    )
    .count()
)
silver_holding_without_gold = (
    spark.table("silver_13f_holding").alias("s")
    .join(source_lookup.alias("l"), "source_id")
    .join(
        spark.table("fact_institutional_holding").alias("g"),
        (F.col("g.silver_natural_key") == F.col("s.natural_key"))
        & (F.col("g.holding_revision_hash") == F.col("s.holding_revision_hash"))
        & (F.col("g.source_sk") == F.col("l.source_sk")),
        "left_anti",
    )
    .count()
)
silver_ownership_without_gold = (
    spark.table("silver_ownership_event").alias("s")
    .join(source_lookup.alias("l"), "source_id")
    .join(
        spark.table("fact_ownership_event").alias("g"),
        (F.col("g.accession_no") == F.col("s.accession_no"))
        & (F.col("g.ownership_revision_hash") == F.col("s.ownership_revision_hash"))
        & (F.col("g.source_sk") == F.col("l.source_sk")),
        "left_anti",
    )
    .count()
)
silver_material_without_gold = (
    spark.table("silver_material_event").alias("s")
    .join(source_lookup.alias("l"), "source_id")
    .join(
        spark.table("fact_material_event").alias("g"),
        (F.col("g.event_sk") == F.col("s.event_sk"))
        & (F.col("g.material_event_revision_hash") == F.col("s.material_event_revision_hash"))
        & (F.col("g.source_sk") == F.col("l.source_sk")),
        "left_anti",
    )
    .count()
)
gold_without_silver = (
    gold_filing_without_silver
    + gold_holding_without_silver
    + gold_ownership_without_silver
    + gold_material_without_silver
)
silver_without_gold = (
    silver_filing_without_gold
    + silver_holding_without_gold
    + silver_ownership_without_gold
    + silver_material_without_gold
)

print(
    "E8 SEC validation: "
    f"missing_pit={missing_pit}, gold_missing_revision_hash={gold_missing_revision_hash}, "
    f"gold_13f_missing_lineage={gold_13f_missing_lineage}, "
    f"silver_invalid={silver_invalid}, silver_13f_duplicate_revisions={silver_13f_duplicate_revisions}, "
    f"gold_without_silver={gold_without_silver}, silver_without_gold={silver_without_gold}"
)
if any([
    missing_pit, gold_missing_revision_hash, gold_13f_missing_lineage,
    silver_invalid, silver_13f_duplicate_revisions, gold_without_silver, silver_without_gold,
]):
    raise RuntimeError(
        "E8 SEC validation failed: "
        f"missing_pit={missing_pit}, gold_missing_revision_hash={gold_missing_revision_hash}, "
        f"gold_13f_missing_lineage={gold_13f_missing_lineage}, "
        f"silver_invalid={silver_invalid}, silver_13f_duplicate_revisions={silver_13f_duplicate_revisions}, "
        f"gold_without_silver={gold_without_silver}, silver_without_gold={silver_without_gold}"
    )
parsed_raw.unpersist()
raw.unpersist()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
