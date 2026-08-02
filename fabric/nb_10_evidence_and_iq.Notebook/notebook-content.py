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

# Fabric Notebook: nb_10_evidence_and_iq
# Materializes the E7 PIT evidence projection and bounded Fabric IQ pilot tables.

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

import json
from datetime import date

from delta.tables import DeltaTable
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql.types import IntegerType, StringType


def _require_table(table_name: str) -> None:
    if not spark.catalog.tableExists(table_name):
        raise RuntimeError(f"Required E7 table is missing: {table_name}")


def _document_id(source_type, source_id, revision_hash, chunk_index):
    source_type = F.col(source_type) if isinstance(source_type, str) else source_type
    source_id = F.col(source_id) if isinstance(source_id, str) else source_id
    revision_hash = F.col(revision_hash) if isinstance(revision_hash, str) else revision_hash
    chunk_index = F.col(chunk_index) if isinstance(chunk_index, str) else chunk_index
    natural_key = F.concat_ws(
        "|",
        source_type,
        source_id,
        revision_hash,
        chunk_index.cast(StringType()),
    )
    return F.concat(
        F.lit("d"),
        F.regexp_replace(
            F.translate(F.base64(F.unhex(F.sha2(natural_key, 256))), "+/", "-_"),
            "=+$",
            "",
        ),
    )


def _replace_table(table_name: str, frame) -> None:
    frame.write.format("delta").mode("overwrite").option(
        "overwriteSchema", "true"
    ).saveAsTable(table_name)


for required_table in [
    "dim_security",
    "dim_entity",
    "fact_company_news",
    "fact_material_event",
    "fact_sec_filing_event",
    "fact_theme_membership",
    "fact_institutional_holding",
    "silver_sec_filing",
]:
    _require_table(required_table)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# PARAMETERS CELL ********************

as_of_date = ""

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

as_of_date = str(as_of_date).strip() or date.today().isoformat()
if date.fromisoformat(as_of_date) > date.today():
    raise ValueError("as_of_date cannot be in the future")
as_of = F.to_date(F.lit(as_of_date))

current_securities = (
    spark.table("dim_security")
    .filter(F.col("is_current") & F.col("is_active"))
    .select(
        "security_sk",
        F.upper(F.trim("ticker")).alias("symbol"),
        "company_name",
        "gics_sector",
        "gics_industry",
        "exchange",
        "currency",
    )
)

duplicate_current_securities = (
    current_securities.groupBy("security_sk").count().filter(F.col("count") > 1).count()
)
if duplicate_current_securities:
    raise RuntimeError("Current security projection contains duplicate security_sk values")

news = (
    spark.table("fact_company_news").alias("n")
    .filter(
        (F.col("n.event_date") <= as_of)
        & (F.col("n.knowledge_date") <= as_of)
    )
    .join(current_securities.select("security_sk", "symbol"), "security_sk", "left")
    .withColumn("source_type", F.lit("news"))
    .withColumn("source_id", F.concat(F.lit("news:"), F.col("news_sk")))
    .withColumn("source_name", F.coalesce(F.col("source"), F.lit("Company news")))
    .withColumn("source_url", F.col("url"))
    .withColumn("title", F.coalesce(F.col("title"), F.lit("Company news")))
    .withColumn(
        "content",
        F.trim(F.concat_ws("\n\n", F.col("title"), F.col("summary"))),
    )
    .withColumn("revision_hash", F.col("news_revision_hash"))
    .withColumn("chunk_index", F.lit(0).cast(IntegerType()))
    .withColumn("content_status", F.lit("summary"))
    .select(
        "security_sk", "symbol", "source_type", "source_id", "source_name",
        "source_url", "title", "content", "event_date", "knowledge_date",
        "published_at", "revision_hash", "chunk_index", "content_status",
    )
)

filing_url_window = Window.partitionBy("accession_no", "filing_revision_hash").orderBy(
    F.col("knowledge_date").desc(), F.col("loaded_at").desc()
)
filing_urls = (
    spark.table("silver_sec_filing")
    .filter(F.col("knowledge_date") <= as_of)
    .withColumn("url_row_number", F.row_number().over(filing_url_window))
    .filter(F.col("url_row_number") == 1)
    .select("accession_no", "filing_revision_hash", "filing_url", "content_hash")
)

material_security_scope = (
    spark.table("fact_material_event")
    .filter(
        (F.col("event_date") <= as_of)
        & (F.col("knowledge_date") <= as_of)
        & F.col("security_sk").isNotNull()
    )
    .select("accession_no", "security_sk")
    .distinct()
)

filings = (
    spark.table("fact_sec_filing_event").alias("f")
    .filter(
        (F.col("f.event_date") <= as_of)
        & (F.col("f.knowledge_date") <= as_of)
    )
    .join(filing_urls.alias("u"), ["accession_no", "filing_revision_hash"], "left")
    .join(material_security_scope, "accession_no", "left")
    .join(current_securities.select("security_sk", "symbol"), "security_sk", "left")
    .withColumn("source_type", F.lit("sec_filing"))
    .withColumn(
        "source_id",
        F.concat_ws(
            ":",
            F.lit("filing"),
            F.col("accession_no"),
            F.coalesce(F.col("security_sk").cast(StringType()), F.lit("unscoped")),
        ),
    )
    .withColumn("source_name", F.lit("SEC EDGAR"))
    .withColumn("source_url", F.col("filing_url"))
    .withColumn(
        "title",
        F.concat_ws(" ", F.col("filing_type"), F.coalesce(F.col("filer_name"), F.lit("SEC filing"))),
    )
    .withColumn(
        "content",
        F.concat_ws(
            ". ",
            F.concat(F.lit("SEC form "), F.col("filing_type")),
            F.concat(F.lit("Filer: "), F.coalesce(F.col("filer_name"), F.lit("unknown"))),
            F.concat(F.lit("Accession: "), F.col("accession_no")),
            F.when(F.col("content_hash").isNotNull(), F.lit("Archived document content was validated upstream")),
        ),
    )
    .withColumn("published_at", F.lit(None).cast("timestamp"))
    .withColumn("revision_hash", F.col("filing_revision_hash"))
    .withColumn("chunk_index", F.lit(0).cast(IntegerType()))
    .withColumn("content_status", F.lit("metadata_only"))
    .select(
        "security_sk", "symbol", "source_type", "source_id", "source_name",
        "source_url", "title", "content", "event_date", "knowledge_date",
        "published_at", "revision_hash", "chunk_index", "content_status",
    )
)

material_url_window = Window.partitionBy("accession_no").orderBy(
    F.col("knowledge_date").desc(), F.col("loaded_at").desc()
)
material_urls = (
    spark.table("silver_sec_filing")
    .filter(F.col("knowledge_date") <= as_of)
    .withColumn("url_row_number", F.row_number().over(material_url_window))
    .filter(F.col("url_row_number") == 1)
    .select("accession_no", "filing_url")
)
material_events = (
    spark.table("fact_material_event").alias("m")
    .filter(
        (F.col("m.event_date") <= as_of)
        & (F.col("m.knowledge_date") <= as_of)
        & F.col("m.description").isNotNull()
    )
    .join(material_urls, "accession_no", "left")
    .join(current_securities.select("security_sk", "symbol"), "security_sk", "left")
    .withColumn("source_type", F.lit("material_event"))
    .withColumn("source_id", F.concat(F.lit("material:"), F.col("event_sk")))
    .withColumn("source_name", F.lit("SEC EDGAR"))
    .withColumn("source_url", F.col("filing_url"))
    .withColumn(
        "title",
        F.concat_ws(" ", F.col("filing_type"), F.lit("material event")),
    )
    .withColumn("content", F.trim(F.col("description")))
    .withColumn("published_at", F.lit(None).cast("timestamp"))
    .withColumn("revision_hash", F.col("material_event_revision_hash"))
    .withColumn("chunk_index", F.lit(0).cast(IntegerType()))
    .withColumn("content_status", F.lit("extracted_event"))
    .select(
        "security_sk", "symbol", "source_type", "source_id", "source_name",
        "source_url", "title", "content", "event_date", "knowledge_date",
        "published_at", "revision_hash", "chunk_index", "content_status",
    )
)

evidence = (
    news.unionByName(filings).unionByName(material_events)
    .filter(F.length(F.trim("content")) > 0)
    .withColumn(
        "id",
        _document_id("source_type", "source_id", "revision_hash", "chunk_index"),
    )
    .select(
        "id", "security_sk", "symbol", "source_type", "source_id", "source_name",
        "source_url", "title", "content", "event_date", "knowledge_date",
        "published_at", "revision_hash", "chunk_index", "content_status",
    )
)

duplicate_evidence = evidence.groupBy("id").count().filter(F.col("count") > 1).count()
invalid_evidence_pit = evidence.filter(
    F.col("event_date").isNull()
    | F.col("knowledge_date").isNull()
    | (F.col("event_date") > F.col("knowledge_date"))
    | (F.col("knowledge_date") > as_of)
).count()
if duplicate_evidence or invalid_evidence_pit or evidence.isEmpty():
    raise RuntimeError(
        "E7 evidence validation failed: "
        f"duplicates={duplicate_evidence}, invalid_pit={invalid_evidence_pit}, empty={evidence.isEmpty()}"
    )

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_evidence_chunk (
        id STRING NOT NULL,
        security_sk BIGINT,
        symbol STRING,
        source_type STRING NOT NULL,
        source_id STRING NOT NULL,
        source_name STRING,
        source_url STRING,
        title STRING,
        content STRING NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        published_at TIMESTAMP,
        revision_hash STRING NOT NULL,
        chunk_index INT NOT NULL,
        content_status STRING NOT NULL
    ) USING DELTA
""")

evidence_merge = (
    DeltaTable.forName(spark, "fact_evidence_chunk").alias("t")
    .merge(evidence.alias("s"), "t.id = s.id")
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
)
if as_of_date == date.today().isoformat():
    evidence_merge = evidence_merge.whenNotMatchedBySourceDelete()
evidence_merge.execute()

projection = spark.table("fact_evidence_chunk").filter(F.col("knowledge_date") <= as_of)
generation_hash = projection.agg(
    F.sha2(
        F.concat_ws(
            "|",
            F.sort_array(F.collect_list(F.concat_ws("|", "id", "revision_hash"))),
        ),
        256,
    ).alias("generation_hash")
).collect()[0].generation_hash
if not generation_hash:
    raise RuntimeError("E7 projection generation fingerprint is empty")
projection_generation = f"e7-{generation_hash[:32]}"
projection = projection.withColumn("generation", F.lit(projection_generation))

projection_output_path = (
    "Files/serving/evidence"
    if as_of_date == date.today().isoformat()
    else f"Files/audit/evidence_asof/{as_of_date}"
)
projection.coalesce(1).write.mode("overwrite").json(projection_output_path)

source_counts = {
    row.source_type: row["count"]
    for row in projection.groupBy("source_type").count().collect()
}
content_status_counts = {
    row.content_status: row["count"]
    for row in projection.groupBy("content_status").count().collect()
}
manifest = spark.createDataFrame([{
    "generation": projection_generation,
    "as_of_date": date.fromisoformat(as_of_date),
    "document_count": projection.count(),
    "source_counts_json": json.dumps(source_counts, sort_keys=True),
    "content_status_counts_json": json.dumps(content_status_counts, sort_keys=True),
}]).withColumn("created_at", F.current_timestamp())

spark.sql("""
    CREATE TABLE IF NOT EXISTS evidence_projection_manifest (
        generation STRING NOT NULL,
        as_of_date DATE NOT NULL,
        document_count BIGINT NOT NULL,
        source_counts_json STRING NOT NULL,
        content_status_counts_json STRING NOT NULL,
        created_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
(
    DeltaTable.forName(spark, "evidence_projection_manifest").alias("t")
    .merge(manifest.alias("s"), "t.generation = s.generation")
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
    .execute()
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# The IQ pilot is intentionally current-state and shared-data only. It does not
# participate in production retrieval and contains no owner-scoped portfolio rows.
iq_security = current_securities.select(
    "security_sk", "symbol", "company_name", "gics_sector", "gics_industry",
    "exchange", "currency",
)
iq_company = current_securities.select(
    F.col("security_sk").alias("company_sk"),
    "company_name", "gics_sector", "gics_industry",
)
iq_security_issued_by_company = current_securities.select(
    "security_sk", F.col("security_sk").alias("company_sk")
)

theme_window = Window.partitionBy("theme_id", "security_sk").orderBy(
    F.col("event_date").desc(),
    F.col("knowledge_date").desc(),
    F.col("theme_revision_hash").desc(),
)
latest_trs = (
    spark.table("fact_theme_membership")
    .filter(
        (F.col("event_date") <= as_of)
        & (F.col("knowledge_date") <= as_of)
        & (F.col("is_ground_truth") == F.lit(True))
    )
    .withColumn("theme_row_number", F.row_number().over(theme_window))
    .filter(F.col("theme_row_number") == 1)
    .drop("theme_row_number")
)
iq_theme = latest_trs.groupBy("theme_id").agg(
    F.min("etf_symbol").alias("etf_symbol"),
    F.max("event_date").alias("event_date"),
    F.max("knowledge_date").alias("knowledge_date"),
)
iq_security_in_theme = latest_trs.select(
    "security_sk", "theme_id", "weight", "is_ground_truth",
    "event_date", "knowledge_date",
)

evidence_window = Window.partitionBy("source_id").orderBy(
    F.col("knowledge_date").desc(),
    F.col("event_date").desc(),
    F.col("revision_hash").desc(),
)
iq_evidence_document = (
    projection.withColumn("evidence_row_number", F.row_number().over(evidence_window))
    .filter(F.col("evidence_row_number") == 1)
    .select(
        F.col("id").alias("document_id"), "source_type", "source_id", "title",
        "source_url", "content_status", "event_date", "knowledge_date",
    )
)
iq_security_has_evidence = (
    projection.filter(F.col("security_sk").isNotNull())
    .join(iq_evidence_document.select("document_id"), projection.id == F.col("document_id"), "inner")
    .select("security_sk", "document_id")
    .distinct()
)

iq_material_event = (
    spark.table("fact_material_event")
    .filter((F.col("event_date") <= as_of) & (F.col("knowledge_date") <= as_of))
    .select(
        F.col("event_sk").cast(StringType()).alias("event_id"),
        "accession_no", "filing_type", "description", "event_date", "knowledge_date",
    )
)
iq_security_has_material_event = (
    spark.table("fact_material_event")
    .filter(
        (F.col("event_date") <= as_of)
        & (F.col("knowledge_date") <= as_of)
        & F.col("security_sk").isNotNull()
    )
    .select(
        "security_sk", F.col("event_sk").cast(StringType()).alias("event_id")
    )
    .distinct()
)

iq_institution = (
    spark.table("dim_entity")
    .filter(F.col("entity_type") == "institution")
    .select(
        F.col("entity_sk").alias("institution_sk"),
        F.col("name").alias("institution_name"),
        "cik",
    )
)
holding_window = Window.partitionBy("entity_sk", "security_sk").orderBy(
    F.col("event_date").desc(),
    F.col("knowledge_date").desc(),
    F.col("holding_revision_hash").desc(),
)
iq_institution_holds_security = (
    spark.table("fact_institutional_holding")
    .filter((F.col("event_date") <= as_of) & (F.col("knowledge_date") <= as_of))
    .withColumn("holding_row_number", F.row_number().over(holding_window))
    .filter(F.col("holding_row_number") == 1)
    .select(
        F.col("entity_sk").alias("institution_sk"), "security_sk", "shares",
        "value_usd", "event_date", "knowledge_date",
    )
)

for table_name, frame in [
    ("iq_security", iq_security),
    ("iq_company", iq_company),
    ("iq_theme", iq_theme),
    ("iq_evidence_document", iq_evidence_document),
    ("iq_material_event", iq_material_event),
    ("iq_institution", iq_institution),
    ("iq_security_issued_by_company", iq_security_issued_by_company),
    ("iq_security_in_theme", iq_security_in_theme),
    ("iq_security_has_evidence", iq_security_has_evidence),
    ("iq_security_has_material_event", iq_security_has_material_event),
    ("iq_institution_holds_security", iq_institution_holds_security),
]:
    _replace_table(table_name, frame)

trs_source_count = latest_trs.select("security_sk", "theme_id").distinct().count()
trs_projection_count = spark.table("iq_security_in_theme").select(
    "security_sk", "theme_id"
).distinct().count()
trs_orphans = (
    spark.table("iq_security_in_theme")
    .join(spark.table("iq_security").select("security_sk"), "security_sk", "left_anti")
    .count()
)
if trs_source_count != trs_projection_count or trs_orphans:
    raise RuntimeError(
        "Fabric IQ TRS projection validation failed: "
        f"trs_source_count={trs_source_count}, "
        f"trs_projection_count={trs_projection_count}, trs_orphans={trs_orphans}"
    )

run_summary = {
    "generation": projection_generation,
    "projection_output_path": projection_output_path,
    "evidence_documents": projection.count(),
    "source_counts": source_counts,
    "content_status_counts": content_status_counts,
    "trs_source_count": trs_source_count,
    "trs_projection_count": trs_projection_count,
    "trs_orphans": trs_orphans,
}
run_summary_json = json.dumps(run_summary, sort_keys=True)
print(run_summary_json)
mssparkutils.notebook.exit(run_summary_json)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }