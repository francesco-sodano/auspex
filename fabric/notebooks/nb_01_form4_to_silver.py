# Fabric Notebook: nb_01_form4_to_silver
# Reads bronze sec_form4 NDJSON and writes to silver_insider_txn (Delta MERGE).
# For each new accession_no it fetches the Form 4 XML from SEC EDGAR to get
# actual transaction amounts — the EFTS search hit only carries metadata.
# Attaches to: auspex_bronze (default lakehouse)
#
# Pipeline parameters:
#   from_date  YYYY-MM-DD  (default: 7 days ago)
#   to_date    YYYY-MM-DD  (default: today)
#   edgar_user_agent       (default: Auspex/1.0 ...)

# COMMAND ----------
import time
import uuid
import requests
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pyspark.sql import functions as F
from pyspark.sql.types import (
    BooleanType, DateType, DecimalType, IntegerType,
    LongType, StringType, StructField, StructType, TimestampType,
)
from delta.tables import DeltaTable

# COMMAND ----------
# --- Parameters ---
def _widget(name, default):
    try:
        return dbutils.widgets.get(name)
    except Exception:
        return default

_today = date.today().isoformat()
from_date        = _widget("from_date", (date.today() - timedelta(days=7)).isoformat())
to_date          = _widget("to_date", _today)
EDGAR_USER_AGENT = _widget("edgar_user_agent", "Auspex/1.0 auspex-bot@example.com")

print(f"Window: {from_date} → {to_date}")

# COMMAND ----------
# --- Bronze file paths for the window ---
def _date_paths(from_d: str, to_d: str, source: str = "sec_form4"):
    d = date.fromisoformat(from_d)
    end = date.fromisoformat(to_d)
    paths = []
    while d <= end:
        paths.append(f"Files/bronze/{source}/{d.year}/{d.month:02d}/{d.day:02d}/*.ndjson")
        d += timedelta(days=1)
    return paths

paths = _date_paths(from_date, to_date)

# Read bronze — select only the fields we need
bronze_df = (
    spark.read.json(paths)
    .select(
        F.col("record.accession_no").alias("accession_no"),
        F.col("record.file_date").alias("file_date"),
        F.col("record.period_of_report").alias("period_of_report"),
        F.col("record.entity_name").alias("issuer_name_raw"),
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
# --- Create silver_insider_txn if not exists ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_insider_txn (
        accession_no   STRING        NOT NULL,
        line_no        INT           NOT NULL,
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

# COMMAND ----------
# --- Skip already-processed accession_nos (idempotency) ---
done_set = {
    r.accession_no
    for r in spark.table("silver_insider_txn").select("accession_no").distinct().collect()
}
new_df = bronze_df.filter(~F.col("accession_no").isin(done_set))
to_process = new_df.collect()
print(f"Already done: {len(done_set)}, new to fetch: {len(to_process)}")

# COMMAND ----------
# --- Load security_master for ticker lookup ---
# key: cik (str, no leading zeros) → ticker
_sm = {
    r.cik: r.ticker
    for r in spark.table("security_master").select("cik", "ticker").collect()
}

# COMMAND ----------
# --- EDGAR Form 4 XML helpers ---

def _cik_from_accno(accno: str) -> str:
    """Extract filer CIK from accession number (first segment, strip leading zeros)."""
    return str(int(accno.split("-")[0]))


def _fetch_form4_xml(cik: str, accno: str) -> str | None:
    """Fetch Form 4 XML from EDGAR. Returns raw XML string or None."""
    accno_nodash = accno.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accno_nodash}"
    hdrs = {"User-Agent": EDGAR_USER_AGENT}

    # Try common primary-document filenames
    for fname in (f"{accno}.xml", "4.xml", "form4.xml"):
        try:
            r = requests.get(f"{base}/{fname}", headers=hdrs, timeout=15)
            if r.status_code == 200 and "<ownershipDocument" in r.text:
                return r.text
        except Exception:
            pass

    # Fallback: consult the filing index
    try:
        idx_r = requests.get(f"{base}/{accno}-index.json", headers=hdrs, timeout=10)
        if idx_r.status_code == 200:
            for item in idx_r.json().get("directory", {}).get("item", []):
                name = item.get("name", "")
                if name.endswith(".xml") and item.get("type", "") in ("4", "4/A"):
                    r2 = requests.get(f"{base}/{name}", headers=hdrs, timeout=15)
                    if r2.status_code == 200:
                        return r2.text
    except Exception:
        pass

    return None


def _txt(el, path: str, default=None) -> str | None:
    """Safe text extraction from an ElementTree element."""
    node = el.find(path) if el is not None else None
    return node.text.strip() if node is not None and node.text else default


def _parse_form4_xml(xml_text: str, meta: dict, sm: dict) -> list[dict]:
    """Parse Form 4 XML → list of non-derivative transaction rows."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"XML parse error {meta['accession_no']}: {e}")
        return []

    # Issuer
    issuer_cik   = (_txt(root, "issuer/issuerCik") or "").lstrip("0") or None
    issuer_name  = _txt(root, "issuer/issuerName") or meta.get("issuer_name_raw")
    issuer_tick  = sm.get(issuer_cik) if issuer_cik else None

    # Reporter
    reporter_cik  = (_txt(root, "reportingOwner/reportingOwnerId/rptOwnerCik") or "").lstrip("0") or None
    reporter_name = _txt(root, "reportingOwner/reportingOwnerId/rptOwnerName")
    rel           = root.find("reportingOwner/reportingOwnerRelationship")
    is_dir        = _txt(rel, "isDirector",       "0") == "1"
    is_off        = _txt(rel, "isOfficer",        "0") == "1"
    is_10pct      = _txt(rel, "isTenPercentOwner","0") == "1"
    off_title     = _txt(rel, "officerTitle")

    rows = []
    for i, txn in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction")):
        try:
            txn_date = _txt(txn, "transactionDate/value") or meta.get("period_of_report")
            code     = _txt(txn, "transactionCodes/transactionCode") or ""
            adc      = _txt(txn, "transactionAmounts/transactionAcquiredDisposedCode/value", "D")
            shares   = float(_txt(txn, "transactionAmounts/transactionShares/value") or 0)
            price    = float(_txt(txn, "transactionAmounts/transactionPricePerShare/value") or 0)
            post_sh  = float(_txt(txn, "postTransactionAmounts/sharesOwnedFollowingTransaction/value") or 0)

            rows.append({
                "accession_no":   meta["accession_no"],
                "line_no":        i,
                "issuer_cik":     issuer_cik,
                "issuer_ticker":  issuer_tick,
                "issuer_name":    issuer_name,
                "reporter_cik":   reporter_cik,
                "reporter_name":  reporter_name,
                "is_director":    is_dir,
                "is_officer":     is_off,
                "is_ten_pct":     is_10pct,
                "officer_title":  off_title,
                "txn_code":       code,
                "is_buy":         adc == "A",
                "shares":         shares,
                "price":          price,
                "value_usd":      round(shares * price, 2),
                "shares_after":   post_sh,
                "event_date":     txn_date,
                "knowledge_date": meta.get("file_date"),
                "source_id":      meta.get("source_id", "sec_form4"),
                "batch_id":       meta.get("batch_id"),
                "ingest_ts":      meta.get("ingest_ts"),
            })
        except Exception as e:
            print(f"Txn parse error row {i} of {meta['accession_no']}: {e}")
    return rows

# COMMAND ----------
# --- Fetch + parse each new filing ---
all_txns   = []
quarantine = []
now_ts     = datetime.now(timezone.utc)

for row in to_process:
    meta = row.asDict()
    cik  = _cik_from_accno(row.accession_no)

    xml_text = _fetch_form4_xml(cik, row.accession_no)
    if xml_text is None:
        quarantine.append({
            "quarantine_id":  str(uuid.uuid4()),
            "source_id":      "sec_form4",
            "raw_identifier": row.accession_no,
            "reason":         "XML_FETCH_FAILED",
            "quarantined_at": now_ts,
        })
        time.sleep(0.1)
        continue

    txns = _parse_form4_xml(xml_text, meta, _sm)
    if not txns:
        # Derivative-only or amendment with no non-derivative section — not an error
        quarantine.append({
            "quarantine_id":  str(uuid.uuid4()),
            "source_id":      "sec_form4",
            "raw_identifier": row.accession_no,
            "reason":         "NO_NONDERIVATIVE_TXNS",
            "quarantined_at": now_ts,
        })
    else:
        all_txns.extend(txns)

    time.sleep(0.12)  # ~8 req/s — within EDGAR fair-use guideline

print(f"Transactions parsed: {len(all_txns)} | Quarantined filings: {len(quarantine)}")

# COMMAND ----------
# --- Write to silver_insider_txn via Delta MERGE ---
if all_txns:
    txn_schema = StructType([
        StructField("accession_no",   StringType()),
        StructField("line_no",        IntegerType()),
        StructField("issuer_cik",     StringType()),
        StructField("issuer_ticker",  StringType()),
        StructField("issuer_name",    StringType()),
        StructField("reporter_cik",   StringType()),
        StructField("reporter_name",  StringType()),
        StructField("is_director",    BooleanType()),
        StructField("is_officer",     BooleanType()),
        StructField("is_ten_pct",     BooleanType()),
        StructField("officer_title",  StringType()),
        StructField("txn_code",       StringType()),
        StructField("is_buy",         BooleanType()),
        StructField("shares",         DecimalType(20, 4)),
        StructField("price",          DecimalType(18, 6)),
        StructField("value_usd",      DecimalType(20, 2)),
        StructField("shares_after",   DecimalType(20, 4)),
        StructField("event_date",     DateType()),
        StructField("knowledge_date", DateType()),
        StructField("source_id",      StringType()),
        StructField("batch_id",       StringType()),
        StructField("ingest_ts",      TimestampType()),
    ])
    txn_df = spark.createDataFrame(all_txns, txn_schema)

    (
        DeltaTable.forName(spark, "silver_insider_txn")
        .alias("t")
        .merge(
            txn_df.alias("s"),
            "t.accession_no = s.accession_no AND t.line_no = s.line_no",
        )
        .whenNotMatchedInsertAll()
        .execute()
    )
    print(f"Merged {len(all_txns)} rows into silver_insider_txn")

# COMMAND ----------
# --- Write quarantined filings ---
if quarantine:
    from pyspark.sql.types import StructType, StructField, StringType, TimestampType
    q_schema = StructType([
        StructField("quarantine_id",  StringType()),
        StructField("source_id",      StringType()),
        StructField("raw_identifier", StringType()),
        StructField("reason",         StringType()),
        StructField("quarantined_at", TimestampType()),
    ])
    (
        spark.createDataFrame(quarantine, q_schema)
        .write.format("delta").mode("append")
        .saveAsTable("silver_security_quarantine")
    )
    print(f"Quarantined {len(quarantine)} filings")
