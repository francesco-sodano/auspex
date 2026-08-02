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
# META     },
# META     "warehouse": {
# META       "known_warehouses": []
# META     }
# META   }
# META }

# CELL ********************


# Fabric Notebook: nb_01_form4_to_silver
# Reads bronze sec_form4 NDJSON and writes entity-resolved silver_insider_txn.
# Attaches to: auspex_bronze (default lakehouse)
#
# For each new accession_no it fetches the full Form 4 XML from SEC EDGAR because
# the EFTS search hit only carries filing metadata, not transaction rows.

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************


import re
import time
import threading
import uuid
import requests
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from delta.tables import DeltaTable
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType, BooleanType, DateType, DecimalType, IntegerType, LongType,
    StringType, StructField, StructType, TimestampType,
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# PARAMETERS CELL ********************


# --- Parameters: mark this cell as the Fabric parameter cell ---
from_date = ""
to_date = ""
edgar_user_agent = "{{EDGAR_USER_AGENT}}"
edgar_requests_per_minute = 450
max_workers = 5
write_batch_size = 500
retry_quarantine_reasons = ""

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
edgar_user_agent = str(edgar_user_agent)
edgar_requests_per_minute = int(edgar_requests_per_minute)
max_workers = int(max_workers)
write_batch_size = int(write_batch_size)
retry_quarantine_reasons = str(retry_quarantine_reasons)

if date.fromisoformat(from_date) > date.fromisoformat(to_date):
    raise ValueError("from_date must be on or before to_date")
if not edgar_user_agent.strip():
    raise ValueError("edgar_user_agent cannot be empty")

EDGAR_USER_AGENT = edgar_user_agent
EDGAR_REQUESTS_PER_MINUTE = edgar_requests_per_minute
_MAX_WORKERS = max(1, max_workers)
_WRITE_BATCH_SIZE = max(1, write_batch_size)
_TERMINAL_QUARANTINE_REASONS = {
    "ARCHIVE_NOT_FOUND",
    "NO_NONDERIVATIVE_TXNS",
    "NO_OWNERSHIP_XML",
    "INVALID_DATE",
    "SECURITY_UNRESOLVED",
    "PIT_MISSING",
}
_RETRY_QUARANTINE_REASONS = {
    reason.strip().upper()
    for reason in retry_quarantine_reasons.split(",")
    if reason.strip()
}
_UNKNOWN_RETRY_REASONS = _RETRY_QUARANTINE_REASONS.difference(_TERMINAL_QUARANTINE_REASONS)
if _UNKNOWN_RETRY_REASONS:
    raise ValueError(
        "retry_quarantine_reasons contains non-terminal reasons: "
        + ", ".join(sorted(_UNKNOWN_RETRY_REASONS))
    )
_ACTIVE_TERMINAL_REASONS = _TERMINAL_QUARANTINE_REASONS.difference(_RETRY_QUARANTINE_REASONS)
_MIN_VALID_DATE = date(1900, 1, 1)

if EDGAR_REQUESTS_PER_MINUTE <= 0:
    raise ValueError("edgar_requests_per_minute must be positive")
if max_workers <= 0:
    raise ValueError("max_workers must be positive")
if write_batch_size <= 0:
    raise ValueError("write_batch_size must be positive")

print(
    f"Window: {from_date} to {to_date} | "
    f"EDGAR cap: {EDGAR_REQUESTS_PER_MINUTE} req/min aggregate | "
    f"workers: {_MAX_WORKERS} | "
    f"write batch: {_WRITE_BATCH_SIZE} rows | "
    f"terminal quarantine skip: {sorted(_ACTIVE_TERMINAL_REASONS)} | "
    f"forced retry: {sorted(_RETRY_QUARANTINE_REASONS)}"
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************


# --- Helpers ---
def _ensure_columns(table_name: str, column_specs: dict[str, str]) -> None:
    existing = set(spark.table(table_name).columns)
    for column_name, ddl in column_specs.items():
        if column_name not in existing:
            spark.sql(f"ALTER TABLE {table_name} ADD COLUMNS ({ddl})")


def _date_paths(from_d: str, to_d: str, source: str = "sec_form4"):
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


def _to_date(s):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _coerce_date(value):
    if isinstance(value, date):
        return value
    return _to_date(value)


def _safe_delta_date(value):
    parsed = _coerce_date(value)
    if parsed is None or parsed < _MIN_VALID_DATE:
        return None
    return parsed


def _merge_security_quarantine(rows: list[dict]) -> None:
    if not rows:
        return

    q_schema = StructType([
        StructField("quarantine_id", StringType()),
        StructField("natural_key", StringType()),
        StructField("source_id", StringType()),
        StructField("raw_identifier", StringType()),
        StructField("reason", StringType()),
        StructField("details", StringType()),
        StructField("event_date", DateType()),
        StructField("knowledge_date", DateType()),
        StructField("batch_id", StringType()),
        StructField("quarantined_at", TimestampType()),
    ])
    q_df = spark.createDataFrame(rows, q_schema).dropDuplicates(["natural_key"])
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
    print(
        "Merged quarantine source_rows="
        f"{metrics.get('numSourceRows', 'unknown')} into silver_security_quarantine"
    )

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

raw_lines = spark.read.text(paths).select(F.col("value").alias("raw_json"))
bronze_df = (
    raw_lines
    .select(
        F.get_json_object("raw_json", "$.record.adsh").alias("accession_no"),
        F.get_json_object("raw_json", "$.record.file_date").alias("file_date"),
        F.get_json_object("raw_json", "$.record.period_ending").alias("period_of_report"),
        F.get_json_object("raw_json", "$.record.display_names").alias("issuer_name_raw"),
        F.from_json(
            F.get_json_object("raw_json", "$.record.ciks"),
            ArrayType(StringType()),
        ).alias("cik_candidates"),
        F.get_json_object("raw_json", "$.record.filing_url").alias("filing_url"),
        F.get_json_object("raw_json", "$.batch_id").alias("batch_id"),
        F.get_json_object("raw_json", "$.source_id").alias("source_id"),
        F.to_timestamp(F.get_json_object("raw_json", "$.ingest_ts")).alias("ingest_ts"),
    )
    .filter(F.col("accession_no").isNotNull())
    .dropDuplicates(["accession_no"])
    .cache()
)
total_bronze = bronze_df.count()
print(f"Bronze rows (unique accession_no): {total_bronze}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************


# --- Create/upgrade silver_insider_txn ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_insider_txn (
        accession_no   STRING        NOT NULL,
        line_no        INT           NOT NULL,
        security_sk    BIGINT        NOT NULL,
        issuer_cik     STRING,
        issuer_ticker  STRING,
        issuer_name    STRING,
        reporter_cik   STRING,
        reporter_name  STRING,
        is_director    BOOLEAN,
        is_officer     BOOLEAN,
        is_ten_pct     BOOLEAN,
        officer_title  STRING,
        txn_code       STRING,
        is_buy         BOOLEAN,
        shares         DECIMAL(20,4),
        price          DECIMAL(18,6),
        value_usd      DECIMAL(20,2),
        shares_after   DECIMAL(20,4),
        event_date     DATE,
        knowledge_date DATE,
        source_id      STRING,
        batch_id       STRING,
        ingest_ts      TIMESTAMP
    )
    USING DELTA
""")
_ensure_columns("silver_insider_txn", {"security_sk": "security_sk BIGINT"})

legacy_bad_df = spark.table("silver_insider_txn").filter(
    F.col("security_sk").isNull()
    | F.col("event_date").isNull()
    | F.col("knowledge_date").isNull()
    | (F.col("event_date") > F.col("knowledge_date"))
)
legacy_bad = legacy_bad_df.count()
if legacy_bad:
    legacy_invalid_dates = legacy_bad_df.filter(
        F.col("event_date").isNotNull()
        & F.col("knowledge_date").isNotNull()
        & (F.col("event_date") > F.col("knowledge_date"))
    )
    if not legacy_invalid_dates.isEmpty():
        legacy_quarantine = legacy_invalid_dates.select(
            F.sha2(F.concat_ws("|", F.lit("sec_form4"), "accession_no", F.col("line_no"), F.lit("INVALID_DATE")), 256).alias("quarantine_id"),
            F.concat_ws(":", F.lit("sec_form4"), F.lit("INVALID_DATE"), "accession_no", F.col("line_no")).alias("natural_key"),
            F.lit("sec_form4").alias("source_id"),
            F.col("accession_no").alias("raw_identifier"),
            F.lit("INVALID_DATE").alias("reason"),
            F.concat(
                F.lit("event_date="), F.col("event_date").cast("string"),
                F.lit("; knowledge_date="), F.col("knowledge_date").cast("string"),
            ).alias("details"),
            "event_date", "knowledge_date", "batch_id",
            F.current_timestamp().alias("quarantined_at"),
        )
        quarantine_target = DeltaTable.forName(spark, "silver_security_quarantine")
        (
            quarantine_target.alias("t")
            .merge(legacy_quarantine.alias("s"), "t.natural_key = s.natural_key")
            .whenMatchedUpdateAll()
            .whenNotMatchedInsertAll()
            .execute()
        )
        print(f"Quarantined {legacy_invalid_dates.count()} legacy future-event Form 4 rows")
    DeltaTable.forName(spark, "silver_insider_txn").delete(
        "security_sk IS NULL OR event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date"
    )
    print(f"Removed {legacy_bad} legacy silver_insider_txn rows with invalid security/PIT fields")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************


# --- Skip only already-resolved filings; reprocess legacy rows with missing security_sk ---
resolved_accessions = spark.sql("""
    SELECT accession_no
    FROM silver_insider_txn
    GROUP BY accession_no
    HAVING SUM(CASE WHEN security_sk IS NULL OR event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date THEN 1 ELSE 0 END) = 0
""")
terminal_quarantine_df = spark.table("silver_security_quarantine").filter(
    F.col("source_id") == "sec_form4"
)
if _ACTIVE_TERMINAL_REASONS:
    terminal_quarantine_df = terminal_quarantine_df.filter(
        F.col("reason").isin(*sorted(_ACTIVE_TERMINAL_REASONS))
    )
else:
    terminal_quarantine_df = terminal_quarantine_df.limit(0)

terminal_accessions = (
    terminal_quarantine_df
    .filter(F.col("raw_identifier").isNotNull())
    .select(F.col("raw_identifier").alias("accession_no"))
    .distinct()
)
if _RETRY_QUARANTINE_REASONS:
    loaded_accessions = spark.table("silver_insider_txn").select("accession_no").distinct()
    loaded_lines = spark.table("silver_insider_txn").select("accession_no", "line_no").distinct()
    selected_retry_rows = (
        spark.table("silver_security_quarantine")
        .filter(
            (F.col("source_id") == "sec_form4")
            & F.col("reason").isin(*sorted(_RETRY_QUARANTINE_REASONS))
            & F.col("raw_identifier").isNotNull()
        )
        .select(
            F.col("raw_identifier").alias("accession_no"),
            F.when(
                F.col("reason").isin("SECURITY_UNRESOLVED", "PIT_MISSING", "INVALID_DATE")
                & (F.regexp_extract("natural_key", r":(\d+)$", 1) != ""),
                F.regexp_extract("natural_key", r":(\d+)$", 1).cast("int"),
            ).alias("line_no"),
        )
        .distinct()
    )
    selected_retry_accessions = (
        selected_retry_rows.alias("q")
        .join(
            loaded_lines.alias("ll"),
            (F.col("q.accession_no") == F.col("ll.accession_no"))
            & F.col("q.line_no").eqNullSafe(F.col("ll.line_no")),
            "left",
        )
        .join(
            loaded_accessions.alias("la"),
            F.col("q.accession_no") == F.col("la.accession_no"),
            "left",
        )
        .filter(
            (F.col("q.line_no").isNotNull() & F.col("ll.accession_no").isNull())
            | (F.col("q.line_no").isNull() & F.col("la.accession_no").isNull())
        )
        .select(F.col("q.accession_no").alias("accession_no"))
        .distinct()
    )
else:
    selected_retry_accessions = spark.createDataFrame([], "accession_no STRING")
completed_accessions = (
    resolved_accessions.unionByName(terminal_accessions).distinct()
    .join(selected_retry_accessions, "accession_no", "left_anti")
)
new_df = bronze_df.join(completed_accessions, "accession_no", "left_anti")
to_process = new_df.collect()
print(
    f"Already resolved or terminal-quarantined: {total_bronze - len(to_process)}, "
    f"new/retryable/legacy-unresolved to fetch: {len(to_process)}"
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************


# --- Load dim_security maps for exact CIK/ticker resolution ---
dim_all = (
    spark.table("dim_security")
    .select("security_sk", "cik", "ticker", "source_id")
)

dim_rows = dim_all.orderBy("cik", "ticker", "security_sk").collect()
_exact_security_candidates = {}
_form4_historical_pairs = set()
for r in dim_rows:
    if r.cik and r.ticker:
        _exact_security_candidates.setdefault((r.cik, r.ticker), set()).add(r.security_sk)
        if r.source_id == "sec_form4":
            _form4_historical_pairs.add((r.cik, r.ticker))

ambiguous_exact_pairs = {
    pair: security_sks
    for pair, security_sks in _exact_security_candidates.items()
    if len(security_sks) != 1
}
if ambiguous_exact_pairs:
    raise RuntimeError(
        f"dim_security has ambiguous exact CIK/ticker mappings: {list(ambiguous_exact_pairs)[:20]}"
    )

_security_by_cik_ticker = {
    pair: next(iter(security_sks))
    for pair, security_sks in _exact_security_candidates.items()
}
print(
    f"Security maps: exact_pairs={len(_security_by_cik_ticker)} | "
    f"form4_historical_pairs={len(_form4_historical_pairs)}"
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************


# --- EDGAR Form 4 XML helpers ---
_req_lock = threading.Lock()
_req_slot = [0.0]
_REQ_GAP = 60.0 / EDGAR_REQUESTS_PER_MINUTE


def _edgar_get(url: str, timeout: int = 15) -> requests.Response:
    with _req_lock:
        now = time.monotonic()
        if _req_slot[0] <= now:
            _req_slot[0] = now + _REQ_GAP
            sleep_for = 0.0
        else:
            sleep_for = _req_slot[0] - now
            _req_slot[0] += _REQ_GAP
    if sleep_for:
        time.sleep(sleep_for)
    return requests.get(url, headers={"User-Agent": EDGAR_USER_AGENT}, timeout=timeout)


def _archive_cik_candidates(meta: dict) -> list[str]:
    candidates = []

    filing_url = meta.get("filing_url") or ""
    filing_url_match = re.search(r"/Archives/edgar/data/(\d+)/", filing_url, re.IGNORECASE)
    if filing_url_match:
        candidates.append(filing_url_match.group(1))

    for raw_cik in meta.get("cik_candidates") or []:
        digits = re.sub(r"\D", "", str(raw_cik))
        if digits:
            candidates.append(digits)

    candidates.append(meta["accession_no"].split("-")[0])
    return list(dict.fromkeys(str(int(cik)) for cik in candidates if cik))


def _fetch_form4_xml(meta: dict) -> tuple[str | None, str | None, str]:
    accno = meta["accession_no"]
    accno_nodash = accno.replace("-", "")
    attempts = []
    archive_found = False
    transient_failure = False

    for cik in _archive_cik_candidates(meta):
        base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accno_nodash}"
        try:
            index_response = _edgar_get(f"{base}/index.json", timeout=20)
            attempts.append(f"cik={cik}:index={index_response.status_code}")
            if index_response.status_code != 200:
                if index_response.status_code in (403, 429) or index_response.status_code >= 500:
                    transient_failure = True
                continue

            archive_found = True
            items = index_response.json().get("directory", {}).get("item", [])
            xml_names = list(dict.fromkeys(
                item.get("name")
                for item in items
                if item.get("name") and item["name"].lower().endswith(".xml")
            ))
            for xml_name in xml_names:
                try:
                    xml_response = _edgar_get(f"{base}/{xml_name}", timeout=20)
                    attempts.append(f"cik={cik}:{xml_name}={xml_response.status_code}")
                    if xml_response.status_code in (403, 429) or xml_response.status_code >= 500:
                        transient_failure = True
                    if xml_response.status_code == 200 and re.search(
                        r"<ownershipDocument(?:\s|>)",
                        xml_response.text,
                        re.IGNORECASE,
                    ):
                        return xml_response.text, None, "; ".join(attempts)
                except Exception as exc:
                    transient_failure = True
                    attempts.append(f"cik={cik}:{xml_name}=error:{type(exc).__name__}")
        except Exception as exc:
            transient_failure = True
            attempts.append(f"cik={cik}:index=error:{type(exc).__name__}")

    if transient_failure:
        return None, "XML_FETCH_FAILED", "; ".join(attempts)
    if archive_found:
        return None, "NO_OWNERSHIP_XML", "; ".join(attempts)
    return None, "ARCHIVE_NOT_FOUND", "; ".join(attempts)


def _txt(el, path: str, default=None) -> str | None:
    node = el.find(path) if el is not None else None
    return node.text.strip() if node is not None and node.text else default


def _resolve_security(issuer_cik: str | None, issuer_ticker: str | None) -> tuple[int | None, str | None]:
    ticker = issuer_ticker.upper() if issuer_ticker else None
    if not issuer_cik or not ticker:
        return None, ticker
    return _security_by_cik_ticker.get((issuer_cik, ticker)), ticker


def _select_form4_event_date(*candidates) -> date | None:
    for candidate in candidates:
        parsed = _to_date(candidate)
        if parsed is not None:
            return parsed
    return None


def _parse_form4_xml(xml_text: str, meta: dict) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RuntimeError(f"XML_PARSE_FAILED: {exc}") from exc

    issuer_cik = (_txt(root, "issuer/issuerCik") or "").lstrip("0") or None
    issuer_name = _txt(root, "issuer/issuerName") or meta.get("issuer_name_raw")
    issuer_ticker_raw = _txt(root, "issuer/issuerTradingSymbol")
    security_sk, issuer_ticker = _resolve_security(issuer_cik, issuer_ticker_raw)

    reporter_cik = (_txt(root, "reportingOwner/reportingOwnerId/rptOwnerCik") or "").lstrip("0") or None
    reporter_name = _txt(root, "reportingOwner/reportingOwnerId/rptOwnerName")
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    is_dir = _txt(rel, "isDirector", "0") == "1"
    is_off = _txt(rel, "isOfficer", "0") == "1"
    is_10pct = _txt(rel, "isTenPercentOwner", "0") == "1"
    off_title = _txt(rel, "officerTitle")
    xml_period_of_report = _txt(root, "periodOfReport")

    rows = []
    for line_no, txn in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction")):
        try:
            txn_date = _select_form4_event_date(
                _txt(txn, "transactionDate/value"),
                xml_period_of_report,
                meta.get("period_of_report"),
            )
            code = _txt(txn, "transactionCodes/transactionCode") or ""
            adc = _txt(txn, "transactionAmounts/transactionAcquiredDisposedCode/value", "D")
            shares = float(_txt(txn, "transactionAmounts/transactionShares/value") or 0)
            price = float(_txt(txn, "transactionAmounts/transactionPricePerShare/value") or 0)
            post_sh = float(_txt(txn, "postTransactionAmounts/sharesOwnedFollowingTransaction/value") or 0)

            rows.append({
                "accession_no": meta["accession_no"],
                "line_no": line_no,
                "security_sk": security_sk,
                "issuer_cik": issuer_cik,
                "issuer_ticker": issuer_ticker,
                "issuer_name": issuer_name,
                "reporter_cik": reporter_cik,
                "reporter_name": reporter_name,
                "is_director": is_dir,
                "is_officer": is_off,
                "is_ten_pct": is_10pct,
                "officer_title": off_title,
                "txn_code": code,
                "is_buy": adc == "A",
                "shares": Decimal(str(shares)),
                "price": Decimal(str(price)),
                "value_usd": Decimal(str(round(shares * price, 2))),
                "shares_after": Decimal(str(post_sh)),
                "event_date": txn_date,
                "knowledge_date": _to_date(meta.get("file_date")),
                "source_id": meta.get("source_id", "sec_form4"),
                "batch_id": meta.get("batch_id"),
                "ingest_ts": meta.get("ingest_ts"),
            })
        except Exception as exc:
            raise RuntimeError(f"TXN_PARSE_FAILED line {line_no}: {exc}") from exc
    return rows

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************


# --- Fetch + parse each new filing (parallel, rate-limited) ---
def _quarantine_row(meta: dict, reason: str, details: str | None = None, line_no: int | None = None) -> dict:
    suffix = f":{line_no}" if line_no is not None else ""
    return {
        "quarantine_id": str(uuid.uuid4()),
        "natural_key": f"sec_form4:{reason}:{meta['accession_no']}{suffix}",
        "source_id": "sec_form4",
        "raw_identifier": meta["accession_no"],
        "reason": reason,
        "details": details,
        "event_date": _safe_delta_date(meta.get("event_date") or meta.get("period_of_report")),
        "knowledge_date": _safe_delta_date(meta.get("knowledge_date") or meta.get("file_date")),
        "batch_id": meta.get("batch_id"),
        "quarantined_at": datetime.now(timezone.utc),
    }


txn_schema = StructType([
    StructField("accession_no", StringType()),
    StructField("line_no", IntegerType()),
    StructField("security_sk", LongType()),
    StructField("issuer_cik", StringType()),
    StructField("issuer_ticker", StringType()),
    StructField("issuer_name", StringType()),
    StructField("reporter_cik", StringType()),
    StructField("reporter_name", StringType()),
    StructField("is_director", BooleanType()),
    StructField("is_officer", BooleanType()),
    StructField("is_ten_pct", BooleanType()),
    StructField("officer_title", StringType()),
    StructField("txn_code", StringType()),
    StructField("is_buy", BooleanType()),
    StructField("shares", DecimalType(20, 4)),
    StructField("price", DecimalType(18, 6)),
    StructField("value_usd", DecimalType(20, 2)),
    StructField("shares_after", DecimalType(20, 4)),
    StructField("event_date", DateType()),
    StructField("knowledge_date", DateType()),
    StructField("source_id", StringType()),
    StructField("batch_id", StringType()),
    StructField("ingest_ts", TimestampType()),
])


def _is_valid_delta_date(value) -> bool:
    parsed = _coerce_date(value)
    return parsed is not None and parsed >= _MIN_VALID_DATE


historical_security_schema = StructType([
    StructField("cik", StringType(), False),
    StructField("ticker", StringType(), False),
    StructField("company_name", StringType(), False),
    StructField("first_seen", DateType(), False),
    StructField("last_seen", DateType(), False),
])


def _seed_historical_securities(rows: list[dict]) -> int:
    candidates = {}
    for row in rows:
        issuer_cik = row.get("issuer_cik")
        issuer_ticker = (row.get("issuer_ticker") or "").upper() or None
        if not issuer_cik or not issuer_ticker:
            continue

        pair = (issuer_cik, issuer_ticker)
        existing_sk = _security_by_cik_ticker.get((issuer_cik, issuer_ticker))
        if existing_sk is not None:
            row["security_sk"] = existing_sk
            row["issuer_ticker"] = issuer_ticker
        if existing_sk is not None and pair not in _form4_historical_pairs:
            continue

        event_date = _coerce_date(row.get("event_date"))
        knowledge_date = _coerce_date(row.get("knowledge_date"))
        if not _is_valid_delta_date(event_date) or not _is_valid_delta_date(knowledge_date):
            continue

        candidate = candidates.setdefault(pair, {
            "company_names": set(),
            "first_seen": event_date,
            "last_seen": event_date,
        })
        candidate["first_seen"] = min(candidate["first_seen"], event_date)
        candidate["last_seen"] = max(candidate["last_seen"], event_date)
        company_name = (row.get("issuer_name") or "").strip()
        if company_name:
            candidate["company_names"].add(company_name)

    if not candidates:
        return 0

    seed_rows = [
        (
            cik,
            ticker,
            min(values["company_names"]) if values["company_names"] else ticker,
            values["first_seen"],
            values["last_seen"],
        )
        for (cik, ticker), values in sorted(candidates.items())
    ]
    historical_seed = (
        spark.createDataFrame(seed_rows, historical_security_schema)
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
        .withColumn("is_active", F.lit(False))
        .withColumn("valid_from", F.col("first_seen"))
        .withColumn("valid_to", F.col("last_seen"))
        .withColumn("is_current", F.lit(False))
        .withColumn("resolution_method", F.lit("SEC_FORM4_XML"))
        .withColumn("source_id", F.lit("sec_form4"))
        .withColumn("updated_at", F.current_timestamp())
        .select(
            "security_sk", "cik", "ticker", "isin", "figi", "company_name",
            "gics_sector", "gics_industry", "country", "exchange", "currency",
            "mcap_band", "is_active", "valid_from", "valid_to", "is_current",
            "resolution_method", "source_id", "updated_at",
        )
        .cache()
    )

    source_collisions = (
        historical_seed
        .groupBy("security_sk")
        .agg(F.countDistinct(F.concat_ws("|", "cik", "ticker")).alias("pair_count"))
        .filter(F.col("pair_count") != 1)
    )
    if not source_collisions.isEmpty():
        sample = [row.asDict() for row in source_collisions.limit(20).collect()]
        historical_seed.unpersist()
        raise RuntimeError(f"Historical source security_sk collision: {sample}")

    target_pair_duplicates = (
        spark.table("dim_security")
        .filter(F.col("source_id") == "sec_form4")
        .groupBy("cik", "ticker")
        .agg(F.count("*").alias("row_count"))
        .filter(F.col("row_count") != 1)
    )
    if not target_pair_duplicates.isEmpty():
        sample = [row.asDict() for row in target_pair_duplicates.limit(20).collect()]
        historical_seed.unpersist()
        raise RuntimeError(f"Duplicate historical dim_security targets: {sample}")

    collisions = (
        historical_seed.alias("s")
        .join(
            spark.table("dim_security").select("security_sk", "cik", "ticker").alias("t"),
            F.col("s.security_sk") == F.col("t.security_sk"),
            "inner",
        )
        .filter(
            (~F.col("s.cik").eqNullSafe(F.col("t.cik")))
            | (~F.col("s.ticker").eqNullSafe(F.col("t.ticker")))
        )
        .select("s.security_sk", "s.cik", "s.ticker", "t.cik", "t.ticker")
    )
    if not collisions.isEmpty():
        sample = [row.asDict() for row in collisions.limit(20).collect()]
        historical_seed.unpersist()
        raise RuntimeError(f"Historical security_sk collision: {sample}")

    target = DeltaTable.forName(spark, "dim_security")
    (
        target.alias("t")
        .merge(
            historical_seed.alias("s"),
            "t.cik = s.cik AND t.ticker = s.ticker AND t.source_id = 'sec_form4'",
        )
        .whenMatchedUpdate(
            condition="""
                s.company_name < t.company_name
                OR s.valid_from < t.valid_from
                OR s.valid_to > t.valid_to
            """,
            set={
                "company_name": "least(t.company_name, s.company_name)",
                "valid_from": "least(t.valid_from, s.valid_from)",
                "valid_to": "greatest(t.valid_to, s.valid_to)",
                "updated_at": "s.updated_at",
            },
        )
        .whenNotMatchedInsertAll()
        .execute()
    )
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    inserted = int(metrics.get("numTargetRowsInserted", 0))
    updated = int(metrics.get("numTargetRowsUpdated", 0))
    mappings = historical_seed.select("cik", "ticker", "security_sk").collect()
    historical_seed.unpersist()

    for mapping in mappings:
        _security_by_cik_ticker[(mapping.cik, mapping.ticker)] = mapping.security_sk
        _form4_historical_pairs.add((mapping.cik, mapping.ticker))
    for row in rows:
        issuer_cik = row.get("issuer_cik")
        issuer_ticker = (row.get("issuer_ticker") or "").upper() or None
        if issuer_cik and issuer_ticker:
            security_sk = _security_by_cik_ticker.get((issuer_cik, issuer_ticker))
            if security_sk is not None:
                row["security_sk"] = security_sk
                row["issuer_ticker"] = issuer_ticker

    print(
        f"Historical SEC Form 4 securities: candidates={len(mappings)} | "
        f"inserted={inserted} | updated={updated}"
    )
    return inserted


def _append_silver_insider_txns(rows: list[dict]) -> int:
    if not rows:
        return 0

    target_columns = spark.table("silver_insider_txn").columns
    txn_df = (
        spark.createDataFrame(rows, txn_schema)
        .dropDuplicates(["accession_no", "line_no"])
        .select(*target_columns)
    )
    target = DeltaTable.forName(spark, "silver_insider_txn")
    (
        target.alias("t")
        .merge(
            txn_df.alias("s"),
            "t.accession_no = s.accession_no AND t.line_no = s.line_no",
        )
        .whenNotMatchedInsertAll()
        .execute()
    )
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    return int(metrics.get("numTargetRowsInserted", 0))


def _flush_batch(batch_name: str, txn_rows: list[dict], quarantine_rows: list[dict]) -> dict:
    historical_securities_seeded = _seed_historical_securities(txn_rows)
    resolved = [row for row in txn_rows if row.get("security_sk") is not None]
    unresolved = [row for row in txn_rows if row.get("security_sk") is None]
    batch_quarantine = list(quarantine_rows)

    for row in unresolved:
        batch_quarantine.append(_quarantine_row(
            row,
            "SECURITY_UNRESOLVED",
            details=f"issuer_cik={row.get('issuer_cik')}; issuer_ticker={row.get('issuer_ticker')}",
            line_no=row.get("line_no"),
        ))

    pit_missing = [row for row in resolved if row.get("event_date") is None or row.get("knowledge_date") is None]
    for row in pit_missing:
        batch_quarantine.append(_quarantine_row(
            row,
            "PIT_MISSING",
            details=f"event_date={row.get('event_date')}; knowledge_date={row.get('knowledge_date')}",
            line_no=row.get("line_no"),
        ))

    pit_ready = [row for row in resolved if row.get("event_date") is not None and row.get("knowledge_date") is not None]
    invalid_date = [
        row for row in pit_ready
        if not _is_valid_delta_date(row.get("event_date"))
        or not _is_valid_delta_date(row.get("knowledge_date"))
        or row.get("event_date") > row.get("knowledge_date")
    ]
    for row in invalid_date:
        batch_quarantine.append(_quarantine_row(
            row,
            "INVALID_DATE",
            details=(
                f"line_no={row.get('line_no')}; issuer_ticker={row.get('issuer_ticker')}; "
                f"event_date={row.get('event_date')}; knowledge_date={row.get('knowledge_date')}"
            ),
            line_no=row.get("line_no"),
        ))

    valid_rows = [
        row for row in pit_ready
        if _is_valid_delta_date(row.get("event_date"))
        and _is_valid_delta_date(row.get("knowledge_date"))
        and row.get("event_date") <= row.get("knowledge_date")
    ]

    _merge_security_quarantine(batch_quarantine)
    appended = _append_silver_insider_txns(valid_rows)
    print(
        f"{batch_name}: txns={len(txn_rows)} | resolved={len(valid_rows)} | "
        f"unresolved={len(unresolved)} | pit_missing={len(pit_missing)} | "
        f"invalid_date={len(invalid_date)} | quarantine={len(batch_quarantine)} | "
        f"historical_securities_seeded={historical_securities_seeded} | appended={appended}"
    )
    return {
        "txns": len(txn_rows),
        "resolved": len(valid_rows),
        "unresolved": len(unresolved),
        "pit_missing": len(pit_missing),
        "invalid_date": len(invalid_date),
        "quarantine": len(batch_quarantine),
        "historical_securities_seeded": historical_securities_seeded,
        "appended": appended,
    }


def _process_row(row):
    meta = row.asDict()
    try:
        xml_text, fetch_reason, fetch_details = _fetch_form4_xml(meta)
        if xml_text is None:
            return "quarantine", [_quarantine_row(meta, fetch_reason, details=fetch_details)]
        txns = _parse_form4_xml(xml_text, meta)
        if not txns:
            return "quarantine", [_quarantine_row(meta, "NO_NONDERIVATIVE_TXNS")]
        return "txns", txns
    except Exception as exc:
        return "quarantine", [_quarantine_row(meta, "FORM4_PROCESSING_FAILED", details=str(exc))]


batch_txns = []
batch_quarantine = []
batch_number = 0
totals = {
    "txns": 0,
    "resolved": 0,
    "unresolved": 0,
    "pit_missing": 0,
    "invalid_date": 0,
    "quarantine": 0,
    "historical_securities_seeded": 0,
    "appended": 0,
}

with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
    futures = {ex.submit(_process_row, row): row.accession_no for row in to_process}
    done = 0
    for f in as_completed(futures):
        try:
            kind, payload = f.result()
        except Exception as exc:
            accno = futures[f]
            kind, payload = "quarantine", [{
                "quarantine_id": str(uuid.uuid4()),
                "natural_key": f"sec_form4:FORM4_WORKER_FAILED:{accno}",
                "source_id": "sec_form4",
                "raw_identifier": accno,
                "reason": "FORM4_WORKER_FAILED",
                "details": str(exc),
                "event_date": None,
                "knowledge_date": None,
                "batch_id": None,
                "quarantined_at": datetime.now(timezone.utc),
            }]
        if kind == "quarantine":
            batch_quarantine.extend(payload)
        else:
            batch_txns.extend(payload)
        done += 1
        if len(batch_txns) + len(batch_quarantine) >= _WRITE_BATCH_SIZE:
            batch_number += 1
            summary = _flush_batch(f"Batch {batch_number}", batch_txns, batch_quarantine)
            for key, value in summary.items():
                totals[key] += value
            batch_txns = []
            batch_quarantine = []
        if done % 500 == 0:
            print(f"  {done}/{len(futures)} filings processed")

if batch_txns or batch_quarantine:
    batch_number += 1
    summary = _flush_batch(f"Batch {batch_number}", batch_txns, batch_quarantine)
    for key, value in summary.items():
        totals[key] += value

print(
    f"Transactions parsed: {totals['txns']} | "
    f"resolved: {totals['resolved']} | unresolved: {totals['unresolved']} | "
    f"pit_missing: {totals['pit_missing']} | invalid_date: {totals['invalid_date']} | "
    f"quarantine rows: {totals['quarantine']} | appended: {totals['appended']}"
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************


# --- Update prices symbol universe from resolved silver insider rows ---
import json as _json

_tickers = [
    r.issuer_ticker
    for r in spark.sql(
        """
        SELECT DISTINCT issuer_ticker
        FROM silver_insider_txn
        WHERE issuer_ticker IS NOT NULL AND security_sk IS NOT NULL
        """
    ).collect()
]
_universe = _json.dumps({
    "symbols": sorted(_tickers),
    "updated_at": datetime.now(timezone.utc).isoformat(),
})
mssparkutils.fs.put("Files/config/prices_universe.json", _universe, True)
print(f"Prices universe: {len(_tickers)} tickers -> Files/config/prices_universe.json")

run_summary = {
    "from_date": from_date,
    "to_date": to_date,
    "bronze_accessions": total_bronze,
    "processed_accessions": len(to_process),
    "transactions": totals["txns"],
    "resolved": totals["resolved"],
    "unresolved": totals["unresolved"],
    "pit_missing": totals["pit_missing"],
    "invalid_date": totals["invalid_date"],
    "quarantine_rows": totals["quarantine"],
    "historical_securities_seeded": totals["historical_securities_seeded"],
    "appended": totals["appended"],
    "prices_universe_tickers": len(_tickers),
}
run_summary_json = _json.dumps(run_summary, sort_keys=True)
print(f"FORM 4 SILVER SUMMARY: {run_summary_json}", flush=True)
bronze_df.unpersist()
mssparkutils.notebook.exit(run_summary_json)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
