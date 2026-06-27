# Fabric Notebook: nb_01_form4_to_silver
# Reads bronze sec_form4 NDJSON and writes entity-resolved silver_insider_txn.
# Attaches to: auspex_bronze (default lakehouse)
#
# For each new accession_no it fetches the full Form 4 XML from SEC EDGAR because
# the EFTS search hit only carries filing metadata, not transaction rows.

# COMMAND ----------
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
    BooleanType, DateType, DecimalType, IntegerType, LongType,
    StringType, StructField, StructType, TimestampType,
)

# COMMAND ----------
# --- Parameters ---
def _widget(name, default):
    try:
        return mssparkutils.widgets.get(name)
    except Exception:
        return default


_today = date.today().isoformat()
from_date = _widget("from_date", (date.today() - timedelta(days=7)).isoformat())
to_date = _widget("to_date", _today)
EDGAR_USER_AGENT = _widget("edgar_user_agent", "Auspex/1.0 auspex@auspex.ai")
EDGAR_REQUESTS_PER_MINUTE = int(_widget("edgar_requests_per_minute", "450"))
_MAX_WORKERS = max(1, int(_widget("max_workers", "5")))

if EDGAR_REQUESTS_PER_MINUTE <= 0:
    raise ValueError("edgar_requests_per_minute must be positive")

print(
    f"Window: {from_date} to {to_date} | "
    f"EDGAR cap: {EDGAR_REQUESTS_PER_MINUTE} req/min aggregate | "
    f"workers: {_MAX_WORKERS}"
)

# COMMAND ----------
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
    (
        DeltaTable.forName(spark, "silver_security_quarantine")
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
    print(f"Merged {q_df.count()} rows into silver_security_quarantine")


# COMMAND ----------
# --- Bronze file paths for the window ---
paths = _existing_paths(_date_paths(from_date, to_date))
print(f"Date folders with data: {len(paths)}")
if not paths:
    raise RuntimeError("No bronze files found in window - check connector ran and lakehouse is attached")

bronze_df = (
    spark.read.json(paths)
    .select(
        F.col("record.adsh").alias("accession_no"),
        F.col("record.file_date").alias("file_date"),
        F.col("record.period_ending").alias("period_of_report"),
        F.col("record.display_names").cast(StringType()).alias("issuer_name_raw"),
        F.col("batch_id"),
        F.col("source_id"),
        F.to_timestamp("ingest_ts").alias("ingest_ts"),
    )
    .filter(F.col("accession_no").isNotNull())
    .dropDuplicates(["accession_no"])
)
total_bronze = bronze_df.count()
print(f"Bronze rows (unique accession_no): {total_bronze}")

# COMMAND ----------
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

legacy_bad = (
    spark.table("silver_insider_txn")
    .filter(F.col("security_sk").isNull() | F.col("event_date").isNull() | F.col("knowledge_date").isNull())
    .count()
)
if legacy_bad:
    DeltaTable.forName(spark, "silver_insider_txn").delete(
        "security_sk IS NULL OR event_date IS NULL OR knowledge_date IS NULL"
    )
    print(f"Removed {legacy_bad} legacy silver_insider_txn rows with missing security/PIT fields")

# COMMAND ----------
# --- Skip only already-resolved filings; reprocess legacy rows with missing security_sk ---
done_set = {
    r.accession_no
    for r in (
        spark.sql("""
            SELECT accession_no
            FROM silver_insider_txn
            GROUP BY accession_no
            HAVING SUM(CASE WHEN security_sk IS NULL OR event_date IS NULL OR knowledge_date IS NULL THEN 1 ELSE 0 END) = 0
        """).collect()
    )
}
terminal_quarantine_set = {
    r.raw_identifier
    for r in (
        spark.table("silver_security_quarantine")
        .filter((F.col("source_id") == "sec_form4") & (F.col("reason") == "NO_NONDERIVATIVE_TXNS"))
        .select("raw_identifier")
        .distinct()
        .collect()
    )
    if r.raw_identifier
}
done_set.update(terminal_quarantine_set)
new_df = bronze_df.filter(~F.col("accession_no").isin(done_set))
to_process = new_df.collect()
print(
    f"Already resolved or terminal-quarantined: {len(done_set)}, "
    f"new/retryable/legacy-unresolved to fetch: {len(to_process)}"
)

# COMMAND ----------
# --- Load dim_security maps for exact/current resolution ---
dim_current = (
    spark.table("dim_security")
    .filter(F.col("is_current") == True)
    .select("security_sk", "cik", "ticker")
)

_security_by_cik_ticker = {
    (r.cik, r.ticker): r.security_sk
    for r in dim_current.collect()
    if r.cik and r.ticker
}
_security_by_cik = {}
for r in dim_current.orderBy("ticker").collect():
    if r.cik and r.cik not in _security_by_cik:
        _security_by_cik[r.cik] = {"security_sk": r.security_sk, "ticker": r.ticker}

# COMMAND ----------
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


def _cik_from_accno(accno: str) -> str:
    return str(int(accno.split("-")[0]))


def _fetch_form4_xml(cik: str, accno: str) -> str | None:
    accno_nodash = accno.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accno_nodash}"

    for fname in ("4.xml", "form4.xml"):
        try:
            r = _edgar_get(f"{base}/{fname}")
            if r.status_code == 200 and "<ownershipDocument" in r.text:
                return r.text
        except Exception:
            pass

    try:
        idx = _edgar_get(f"{base}/{accno}-index.htm", timeout=10)
        if idx.status_code == 200:
            seen = set()
            for full_path in re.findall(r'href="(/Archives/edgar/data/[^"]+\.xml)"', idx.text, re.IGNORECASE):
                if full_path in seen:
                    continue
                seen.add(full_path)
                try:
                    r2 = _edgar_get(f"https://www.sec.gov{full_path}")
                    if r2.status_code == 200 and "<ownershipDocument" in r2.text:
                        return r2.text
                except Exception:
                    pass
    except Exception:
        pass

    return None


def _txt(el, path: str, default=None) -> str | None:
    node = el.find(path) if el is not None else None
    return node.text.strip() if node is not None and node.text else default


def _resolve_security(issuer_cik: str | None, issuer_ticker: str | None) -> tuple[int | None, str | None]:
    ticker = issuer_ticker.upper() if issuer_ticker else None
    if issuer_cik and ticker:
        security_sk = _security_by_cik_ticker.get((issuer_cik, ticker))
        if security_sk is not None:
            return security_sk, ticker
    if issuer_cik and issuer_cik in _security_by_cik:
        resolved = _security_by_cik[issuer_cik]
        return resolved["security_sk"], resolved["ticker"]
    return None, ticker


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

    rows = []
    for line_no, txn in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction")):
        try:
            txn_date = _txt(txn, "transactionDate/value") or meta.get("period_of_report")
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
                "event_date": _to_date(txn_date),
                "knowledge_date": _to_date(meta.get("file_date")),
                "source_id": meta.get("source_id", "sec_form4"),
                "batch_id": meta.get("batch_id"),
                "ingest_ts": meta.get("ingest_ts"),
            })
        except Exception as exc:
            raise RuntimeError(f"TXN_PARSE_FAILED line {line_no}: {exc}") from exc
    return rows

# COMMAND ----------
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
        "event_date": _coerce_date(meta.get("event_date") or meta.get("period_of_report")),
        "knowledge_date": _coerce_date(meta.get("knowledge_date") or meta.get("file_date")),
        "batch_id": meta.get("batch_id"),
        "quarantined_at": datetime.now(timezone.utc),
    }


def _process_row(row):
    meta = row.asDict()
    try:
        cik = _cik_from_accno(row.accession_no)
        xml_text = _fetch_form4_xml(cik, row.accession_no)
        if xml_text is None:
            return "quarantine", [_quarantine_row(meta, "XML_FETCH_FAILED")]
        txns = _parse_form4_xml(xml_text, meta)
        if not txns:
            return "quarantine", [_quarantine_row(meta, "NO_NONDERIVATIVE_TXNS")]
        return "txns", txns
    except Exception as exc:
        return "quarantine", [_quarantine_row(meta, "FORM4_PROCESSING_FAILED", details=str(exc))]


all_txns = []
quarantine = []

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
            quarantine.extend(payload)
        else:
            all_txns.extend(payload)
        done += 1
        if done % 500 == 0:
            print(f"  {done}/{len(futures)} filings processed")

resolved_txns = [row for row in all_txns if row.get("security_sk") is not None]
unresolved_txns = [row for row in all_txns if row.get("security_sk") is None]
for row in unresolved_txns:
    quarantine.append(_quarantine_row(
        row,
        "SECURITY_UNRESOLVED",
        details=f"issuer_cik={row.get('issuer_cik')}; issuer_ticker={row.get('issuer_ticker')}",
        line_no=row.get("line_no"),
    ))

pit_missing_txns = [row for row in resolved_txns if row.get("event_date") is None or row.get("knowledge_date") is None]
for row in pit_missing_txns:
    quarantine.append(_quarantine_row(
        row,
        "PIT_MISSING",
        details=f"event_date={row.get('event_date')}; knowledge_date={row.get('knowledge_date')}",
        line_no=row.get("line_no"),
    ))
resolved_txns = [row for row in resolved_txns if row.get("event_date") is not None and row.get("knowledge_date") is not None]

print(
    f"Transactions parsed: {len(all_txns)} | "
    f"resolved: {len(resolved_txns)} | unresolved: {len(unresolved_txns)} | pit_missing: {len(pit_missing_txns)} | "
    f"quarantine rows: {len(quarantine)}"
)

# COMMAND ----------
# --- Write resolved rows to silver_insider_txn via Delta MERGE ---
if resolved_txns:
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
    txn_df = spark.createDataFrame(resolved_txns, txn_schema)

    (
        DeltaTable.forName(spark, "silver_insider_txn")
        .alias("t")
        .merge(txn_df.alias("s"), "t.accession_no = s.accession_no AND t.line_no = s.line_no")
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )
    print(f"Merged {len(resolved_txns)} rows into silver_insider_txn")

# COMMAND ----------
# --- Write replay-safe quarantine rows ---
_merge_security_quarantine(quarantine)

# COMMAND ----------
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