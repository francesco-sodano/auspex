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

# PARAMETERS CELL ********************

# Parameters: mark this cell as the Fabric parameter cell

max_retry_rows = 0

sample_limit = 100

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Normalize and validate injected parameter values

max_retry_rows = int(max_retry_rows)

sample_limit = int(sample_limit)

if max_retry_rows < 0:

    raise ValueError("max_retry_rows cannot be negative")

if sample_limit <= 0:

    raise ValueError("sample_limit must be positive")


for table_name in ["silver_security_quarantine", "silver_insider_txn"]:

    if not spark.catalog.tableExists(table_name):

        raise RuntimeError(f"Required upstream table is missing: {table_name}")



print(f"Quarantine triage | max retry rows: {max_retry_rows} | sample limit: {sample_limit}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Build a live triage view; the source quarantine table remains unchanged.

spark.sql(r"""

CREATE OR REPLACE VIEW v_sec_form4_quarantine_triage AS

WITH quarantine_base AS (

    SELECT

        q.*,

        CASE
            WHEN q.reason IN ('SECURITY_UNRESOLVED', 'PIT_MISSING', 'INVALID_DATE')
             AND regexp_extract(q.natural_key, ':(\d+)$', 1) <> ''
            THEN CAST(regexp_extract(q.natural_key, ':(\d+)$', 1) AS INT)
            ELSE NULL
        END AS line_no

    FROM silver_security_quarantine q

    WHERE q.source_id = 'sec_form4'

),

quarantine AS (

    SELECT

        *,

        MAX(CASE WHEN reason IN ('ARCHIVE_NOT_FOUND', 'NO_NONDERIVATIVE_TXNS', 'NO_OWNERSHIP_XML', 'INVALID_DATE') THEN 1 ELSE 0 END)

            OVER (PARTITION BY raw_identifier) = 1 AS is_terminal,

        MAX(CASE WHEN reason IN ('ARCHIVE_NOT_FOUND', 'NO_NONDERIVATIVE_TXNS', 'NO_OWNERSHIP_XML', 'INVALID_DATE', 'SECURITY_UNRESOLVED', 'PIT_MISSING') THEN 1 ELSE 0 END)

            OVER (PARTITION BY raw_identifier) = 1 AS has_superseding_outcome

    FROM quarantine_base

),

loaded_accessions AS (

    SELECT DISTINCT accession_no

    FROM silver_insider_txn

),

loaded_lines AS (

    SELECT DISTINCT accession_no, line_no

    FROM silver_insider_txn

),

matched AS (

    SELECT

        q.*,

        CASE

            WHEN q.line_no IS NOT NULL AND ll.accession_no IS NOT NULL THEN true

            WHEN q.line_no IS NULL AND la.accession_no IS NOT NULL THEN true

            ELSE false

        END AS is_loaded,

        q.is_terminal,

        q.has_superseding_outcome

    FROM quarantine q

    LEFT JOIN loaded_accessions la

        ON la.accession_no = q.raw_identifier

    LEFT JOIN loaded_lines ll

        ON ll.accession_no = q.raw_identifier

       AND ll.line_no = q.line_no

),

classified AS (

    SELECT

        *,

        CASE

            WHEN is_loaded THEN 'RESOLVED'

            WHEN is_terminal THEN 'ACCEPTED'

            WHEN reason IN ('XML_FETCH_FAILED', 'FORM4_PROCESSING_FAILED', 'FORM4_WORKER_FAILED')

             AND has_superseding_outcome THEN 'ACCEPTED'

            WHEN reason IN ('XML_FETCH_FAILED', 'FORM4_PROCESSING_FAILED', 'FORM4_WORKER_FAILED') THEN 'RETRY'

            ELSE 'REVIEW'

        END AS triage_status

    FROM matched

)

SELECT

    quarantine_id,

    natural_key,

    source_id,

    raw_identifier,

    reason,

    details,

    line_no,

    event_date,

    knowledge_date,

    batch_id,

    quarantined_at,

    is_loaded,

    is_terminal,

    has_superseding_outcome,

    triage_status,

    CASE triage_status

        WHEN 'RESOLVED' THEN 'Keep for audit; exclude from active quarantine metrics'

        WHEN 'ACCEPTED' THEN 'Keep as an expected terminal exclusion; no retry'

        WHEN 'RETRY' THEN 'Rerun Notebook 01 for the filing window'

        WHEN 'REVIEW' THEN CASE reason

            WHEN 'SECURITY_UNRESOLVED' THEN 'Repair dim_security mapping, then force retry SECURITY_UNRESOLVED'

            WHEN 'PIT_MISSING' THEN 'Inspect filing dates/parser; never promote without event_date and knowledge_date'

            ELSE 'Inspect details and classify before promotion'

        END

    END AS recommended_action

FROM classified

""")

print("Created view: v_sec_form4_quarantine_triage")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Cache the live view once; the final gate is the only unconditional full action.

from pyspark.sql import functions as F



if spark.catalog.isCached("v_sec_form4_quarantine_triage"):

    spark.catalog.uncacheTable("v_sec_form4_quarantine_triage")

spark.catalog.refreshTable("v_sec_form4_quarantine_triage")

triage = spark.table("v_sec_form4_quarantine_triage").cache()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Actionable samples are printed only when the final gate fails.

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Gate: duplicates are always invalid; unresolved retryable filings block by threshold.

import json


duplicate_natural_keys = (

    triage.groupBy("natural_key")

    .count()

    .filter(F.col("count") > 1)

    .count()

)

status_counts = (

    triage.agg(

        F.sum(F.when(F.col("triage_status") == "RETRY", 1).otherwise(0)).alias("retry_rows"),
        F.countDistinct(F.when(F.col("triage_status") == "RETRY", F.col("raw_identifier"))).alias("retry_accessions"),

        F.min(F.when(F.col("triage_status") == "RETRY", F.col("knowledge_date"))).alias("retry_min_knowledge_date"),

        F.max(F.when(F.col("triage_status") == "RETRY", F.col("knowledge_date"))).alias("retry_max_knowledge_date"),

        F.sum(F.when(

            (F.col("triage_status") == "RETRY") & (F.col("reason") == "XML_FETCH_FAILED"),

            1,

        ).otherwise(0)).alias("xml_fetch_failed_rows"),

        F.sum(F.when(

            (F.col("triage_status") == "RETRY") & (F.col("reason") == "FORM4_PROCESSING_FAILED"),

            1,

        ).otherwise(0)).alias("processing_failed_rows"),

        F.sum(F.when(

            (F.col("triage_status") == "RETRY") & (F.col("reason") == "FORM4_WORKER_FAILED"),

            1,

        ).otherwise(0)).alias("worker_failed_rows"),

        F.sum(F.when(

            (F.col("triage_status") == "RETRY")

            & F.coalesce(F.col("details"), F.lit("")).rlike(r"=(403|429|5\d\d)(;|$)|=error:"),

            1,

        ).otherwise(0)).alias("transient_xml_failure_rows"),

        F.sum(F.when(

            (F.col("triage_status") == "RETRY")

            & (F.col("reason") == "XML_FETCH_FAILED")

            & ~F.coalesce(F.col("details"), F.lit("")).rlike(r"=(403|429|5\d\d)(;|$)|=error:"),

            1,

        ).otherwise(0)).alias("archive_not_found_rows"),

        F.sum(F.when(F.col("triage_status") == "REVIEW", 1).otherwise(0)).alias("review_rows"),

        F.countDistinct(F.when(F.col("triage_status") == "REVIEW", F.col("raw_identifier"))).alias("review_accessions"),

        F.sum(F.when(

            (F.col("triage_status") == "REVIEW") & (F.col("reason") == "SECURITY_UNRESOLVED"),

            1,

        ).otherwise(0)).alias("security_unresolved_review_rows"),

        F.countDistinct(F.when(

            (F.col("triage_status") == "REVIEW") & (F.col("reason") == "SECURITY_UNRESOLVED"),

            F.col("raw_identifier"),

        )).alias("security_unresolved_review_accessions"),

        F.sum(F.when(

            (F.col("triage_status") == "REVIEW") & (F.col("reason") == "PIT_MISSING"),

            1,

        ).otherwise(0)).alias("pit_missing_review_rows"),

        F.countDistinct(F.when(

            (F.col("triage_status") == "REVIEW") & (F.col("reason") == "PIT_MISSING"),

            F.col("raw_identifier"),

        )).alias("pit_missing_review_accessions"),

        F.sum(F.when(

            (F.col("triage_status") == "REVIEW")

            & ~F.col("reason").isin("SECURITY_UNRESOLVED", "PIT_MISSING"),

            1,

        ).otherwise(0)).alias("other_review_rows"),

        F.sum(F.when(F.col("triage_status") == "ACCEPTED", 1).otherwise(0)).alias("accepted_rows"),

        F.sum(F.when(F.col("triage_status") == "RESOLVED", 1).otherwise(0)).alias("resolved_rows"),

    )

    .first()

)




gate_summary = {

    "resolved_rows": int(status_counts.resolved_rows or 0),

    "accepted_rows": int(status_counts.accepted_rows or 0),

    "retry_rows": int(status_counts.retry_rows or 0),

    "retry_accessions": int(status_counts.retry_accessions or 0),

    "retry_min_knowledge_date": (

        status_counts.retry_min_knowledge_date.isoformat()

        if status_counts.retry_min_knowledge_date else None

    ),

    "retry_max_knowledge_date": (

        status_counts.retry_max_knowledge_date.isoformat()

        if status_counts.retry_max_knowledge_date else None

    ),

    "xml_fetch_failed_rows": int(status_counts.xml_fetch_failed_rows or 0),

    "processing_failed_rows": int(status_counts.processing_failed_rows or 0),

    "worker_failed_rows": int(status_counts.worker_failed_rows or 0),

    "transient_xml_failure_rows": int(status_counts.transient_xml_failure_rows or 0),

    "archive_not_found_rows": int(status_counts.archive_not_found_rows or 0),

    "review_rows": int(status_counts.review_rows or 0),

    "review_accessions": int(status_counts.review_accessions or 0),

    "security_unresolved_review_rows": int(status_counts.security_unresolved_review_rows or 0),

    "security_unresolved_review_accessions": int(status_counts.security_unresolved_review_accessions or 0),

    "pit_missing_review_rows": int(status_counts.pit_missing_review_rows or 0),

    "pit_missing_review_accessions": int(status_counts.pit_missing_review_accessions or 0),

    "other_review_rows": int(status_counts.other_review_rows or 0),

    "duplicate_keys": int(duplicate_natural_keys),

    "max_retry_rows": max_retry_rows,

}

gate_summary["gate_status"] = (

    "FAILED"

    if gate_summary["duplicate_keys"] or gate_summary["retry_accessions"] > max_retry_rows

    else "PASSED"

)

gate_summary_json = json.dumps(gate_summary, sort_keys=True)

print(

    f"Form 4 quarantine: {gate_summary_json}",

    flush=True,

)

if gate_summary["gate_status"] == "FAILED":

    (

        triage.filter(F.col("triage_status").isin("RETRY", "REVIEW"))

        .select(

            "triage_status",

            "reason",

            "raw_identifier",

            "line_no",

            "knowledge_date",

            "details",

            "recommended_action",

        )

        .orderBy("triage_status", "reason", "raw_identifier", "line_no")

        .show(sample_limit, truncate=False)

    )

triage.unpersist()



if duplicate_natural_keys:

    raise RuntimeError(f"FORM 4 QUARANTINE GATE FAILED: {gate_summary_json}")

if status_counts.retry_accessions > max_retry_rows:

    raise RuntimeError(

        f"FORM 4 QUARANTINE GATE FAILED: {gate_summary_json}; "

        "rerun Notebook 01 for the retryable accessions before continuing"

    )



print("FORM 4 QUARANTINE GATE PASSED", flush=True)

mssparkutils.notebook.exit(gate_summary_json)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
