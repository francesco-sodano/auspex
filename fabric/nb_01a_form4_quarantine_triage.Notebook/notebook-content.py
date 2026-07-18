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

WITH quarantine AS (

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

loaded_accessions AS (

    SELECT DISTINCT accession_no

    FROM silver_insider_txn

),

loaded_lines AS (

    SELECT DISTINCT accession_no, line_no

    FROM silver_insider_txn

),

terminal_accessions AS (

    SELECT DISTINCT raw_identifier

    FROM quarantine

    WHERE reason IN ('NO_NONDERIVATIVE_TXNS', 'NO_OWNERSHIP_XML', 'INVALID_DATE')

),

matched AS (

    SELECT

        q.*,

        CASE

            WHEN q.line_no IS NOT NULL AND ll.accession_no IS NOT NULL THEN true

            WHEN q.line_no IS NULL AND la.accession_no IS NOT NULL THEN true

            ELSE false

        END AS is_loaded,

        CASE WHEN ta.raw_identifier IS NOT NULL THEN true ELSE false END AS is_terminal

    FROM quarantine q

    LEFT JOIN loaded_accessions la

        ON la.accession_no = q.raw_identifier

    LEFT JOIN loaded_lines ll

        ON ll.accession_no = q.raw_identifier

       AND ll.line_no = q.line_no

    LEFT JOIN terminal_accessions ta

        ON ta.raw_identifier = q.raw_identifier

),

classified AS (

    SELECT

        *,

        CASE

            WHEN is_loaded THEN 'RESOLVED'

            WHEN is_terminal THEN 'ACCEPTED'

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

# Summary by disposition and reason

from pyspark.sql import functions as F



if spark.catalog.isCached("v_sec_form4_quarantine_triage"):

    spark.catalog.uncacheTable("v_sec_form4_quarantine_triage")

spark.catalog.refreshTable("v_sec_form4_quarantine_triage")

triage = spark.table("v_sec_form4_quarantine_triage").cache()

display(

    triage.groupBy("triage_status", "reason")

    .agg(

        F.count("*").alias("rows"),

        F.countDistinct("raw_identifier").alias("accessions"),

    )

    .orderBy("triage_status", F.desc("accessions"), "reason")

)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Actionable rows

actionable = (

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

)

display(actionable.limit(sample_limit))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Gate: duplicates are always invalid; unresolved retryable filings block by threshold.

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

        F.sum(F.when(F.col("triage_status") == "REVIEW", 1).otherwise(0)).alias("review_rows"),

        F.countDistinct(F.when(F.col("triage_status") == "REVIEW", F.col("raw_identifier"))).alias("review_accessions"),

        F.sum(F.when(F.col("triage_status") == "ACCEPTED", 1).otherwise(0)).alias("accepted_rows"),

        F.sum(F.when(F.col("triage_status") == "RESOLVED", 1).otherwise(0)).alias("resolved_rows"),

    )

    .first()

)



print(

    f"Form 4 quarantine: resolved_rows={status_counts.resolved_rows}, "

    f"accepted_rows={status_counts.accepted_rows}, retry_rows={status_counts.retry_rows}, "

    f"retry_accessions={status_counts.retry_accessions}, review_rows={status_counts.review_rows}, "

    f"review_accessions={status_counts.review_accessions}, duplicate_keys={duplicate_natural_keys}"

)

triage.unpersist()



if duplicate_natural_keys:

    raise RuntimeError(f"Quarantine natural_key duplicates found: {duplicate_natural_keys}")

if status_counts.retry_accessions > max_retry_rows:

    raise RuntimeError(

        f"Retryable Form 4 accessions ({status_counts.retry_accessions}) exceed threshold ({max_retry_rows}); "

        "rerun Notebook 01 for their knowledge-date windows before continuing"

    )



print("FORM 4 QUARANTINE GATE PASSED")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
