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

# Fabric Notebook: nb_11_narrative_intensity
# Validates the E21 extraction cache and materializes PIT narrative features.

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from datetime import date, datetime, timezone
from functools import reduce
import hashlib
import json

from delta.tables import DeltaTable
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType,
    DateType,
    DoubleType,
    LongType,
    MapType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

NARRATIVE_PATH = "Files/serving/narrative_features"
PROMPT_VERSION = "e21_narrative_v1"
PROMPT_SHA256 = "70987525ba240b9008ec684c5cab346cfd02b10f8315d7c2f66adff381c930a5"
MODEL_VERSION = "gpt-4o:2024-11-20"
LOOKBACK_DAYS = 90
COMPONENT_WEIGHTS = {
    "sentiment_strength": 0.10,
    "sentiment_velocity_strength": 0.10,
    "theme_concentration": 0.15,
    "forward_promise_ratio": 0.25,
    "hype_density": 0.20,
    "news_attention": 0.15,
    "insider_divergence": 0.05,
}


def _require_table(table_name: str) -> None:
    if not spark.catalog.tableExists(table_name):
        raise RuntimeError(f"Required E21 table is missing: {table_name}")


def _replace_table(table_name: str, frame) -> None:
    for drop_sql in (f"DROP VIEW IF EXISTS {table_name}", f"DROP TABLE IF EXISTS {table_name}"):
        try:
            spark.sql(drop_sql)
        except Exception:
            pass
    frame.write.format("delta").mode("overwrite").option(
        "overwriteSchema", "true"
    ).saveAsTable(table_name)


def _frame_fingerprint(frame, columns: list[str]) -> str:
    row_hashes = frame.select(
        F.sha2(F.to_json(F.struct(*[F.col(column) for column in columns])), 256).alias("row_hash")
    )
    fingerprint = row_hashes.agg(
        F.sha2(F.concat_ws("|", F.sort_array(F.collect_list("row_hash"))), 256).alias("fingerprint")
    ).collect()[0].fingerprint
    return fingerprint or hashlib.sha256(b"").hexdigest()


def _clamp(column):
    value = column.cast(DoubleType())
    return F.when(value.isNull(), F.lit(None).cast(DoubleType())).otherwise(
        F.least(F.lit(1.0), F.greatest(F.lit(0.0), value))
    )


for required_table in ["fact_evidence_chunk", "security_daily_features"]:
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
parsed_as_of_date = date.fromisoformat(as_of_date)
if parsed_as_of_date > date.today():
    raise ValueError("as_of_date cannot be in the future")
as_of = F.to_date(F.lit(as_of_date))
date_sk = int(parsed_as_of_date.strftime("%Y%m%d"))

projection_schema = StructType([
    StructField("id", StringType()),
    StructField("document_id", StringType()),
    StructField("security_sk", LongType()),
    StructField("symbol", StringType()),
    StructField("source_id", StringType()),
    StructField("source_type", StringType()),
    StructField("document_revision_hash", StringType()),
    StructField("sentiment", DoubleType()),
    StructField("relevance", DoubleType()),
    StructField("forward_promise_ratio", DoubleType()),
    StructField("hype_density", DoubleType()),
    StructField("themes", ArrayType(StringType())),
    StructField("evidence_quotes", MapType(StringType(), StringType())),
    StructField("theme_evidence", MapType(StringType(), StringType())),
    StructField("model_version", StringType()),
    StructField("prompt_version", StringType()),
    StructField("prompt_sha256", StringType()),
    StructField("input_generation", StringType()),
    StructField("generation", StringType()),
    StructField("created_at", StringType()),
    StructField("event_date", StringType()),
    StructField("knowledge_date", StringType()),
])

part_paths = sorted(
    item.path
    for item in mssparkutils.fs.ls(NARRATIVE_PATH)
    if not item.isDir and item.name.startswith("part-")
)
if not part_paths:
    raise RuntimeError(f"No part-* JSON files found under {NARRATIVE_PATH}")

cache = (
    spark.read.schema(projection_schema).json(part_paths)
    .withColumn("event_date", F.to_date("event_date"))
    .withColumn("knowledge_date", F.to_date("knowledge_date"))
    .withColumn("extracted_at", F.to_timestamp("created_at"))
    .drop("created_at")
)
if cache.isEmpty():
    raise RuntimeError("E21 narrative cache projection is empty")

generations = [row.generation for row in cache.select("generation").distinct().collect()]
input_generations = [
    row.input_generation for row in cache.select("input_generation").distinct().collect()
]
if len(generations) != 1 or generations[0] is None:
    raise RuntimeError("E21 projection must contain exactly one narrative generation")
if len(input_generations) != 1 or input_generations[0] is None:
    raise RuntimeError("E21 projection must contain exactly one input_generation")
extraction_generation = generations[0]
input_generation = input_generations[0]

duplicate_cache_ids = cache.groupBy("id").count().filter(F.col("count") > 1).count()
duplicate_document_ids = cache.groupBy("document_id").count().filter(F.col("count") > 1).count()
invalid_cache_contract = cache.filter(
    F.col("id").isNull()
    | F.col("document_id").isNull()
    | F.col("source_id").isNull()
    | F.col("document_revision_hash").isNull()
    | F.col("security_sk").isNull()
    | (F.col("source_type") != F.lit("news"))
    | (F.col("prompt_version") != F.lit(PROMPT_VERSION))
    | (F.col("prompt_sha256") != F.lit(PROMPT_SHA256))
    | (F.col("model_version") != F.lit(MODEL_VERSION))
    | F.col("sentiment").isNull()
    | (F.col("sentiment") < -1.0)
    | (F.col("sentiment") > 1.0)
    | F.col("relevance").isNull()
    | (F.col("relevance") < 0.0)
    | (F.col("relevance") > 1.0)
    | F.col("forward_promise_ratio").isNull()
    | (F.col("forward_promise_ratio") < 0.0)
    | (F.col("forward_promise_ratio") > 1.0)
    | F.col("hype_density").isNull()
    | (F.col("hype_density") < 0.0)
    | (F.col("hype_density") > 1.0)
    | F.col("themes").isNull()
    | (F.size("themes") > 5)
    | F.col("evidence_quotes").isNull()
    | (F.size("evidence_quotes") == 0)
    | F.col("theme_evidence").isNull()
    | F.col("event_date").isNull()
    | F.col("knowledge_date").isNull()
    | F.col("extracted_at").isNull()
    | ~(F.col("event_date") <= F.col("knowledge_date"))
    | ~(F.col("knowledge_date") <= F.current_date())
).count()
invalid_theme_contract = cache.selectExpr(
    "*",
    "exists(themes, theme -> theme IS NULL OR length(theme) = 0 "
    "OR theme <> regexp_replace(regexp_replace(lower(trim(theme)), '[^a-z0-9]+', '_'), '^_+|_+$', '')) "
    "AS invalid_theme",
    "size(themes) <> size(array_distinct(themes)) AS duplicate_theme",
    "sort_array(map_keys(theme_evidence)) <> sort_array(themes) AS missing_theme_evidence",
    "sort_array(map_keys(evidence_quotes)) <> "
    "array('forward_promise_ratio', 'hype_density', 'sentiment') AS invalid_evidence_keys",
).filter(
    F.col("invalid_theme")
    | F.col("duplicate_theme")
    | F.col("missing_theme_evidence")
    | F.col("invalid_evidence_keys")
).count()
if duplicate_cache_ids or duplicate_document_ids or invalid_cache_contract or invalid_theme_contract:
    raise RuntimeError(
        "E21 cache contract validation failed: "
        f"duplicate_cache_ids={duplicate_cache_ids}, "
        f"duplicate_document_ids={duplicate_document_ids}, "
        f"invalid_cache_contract={invalid_cache_contract}, "
        f"invalid_theme_contract={invalid_theme_contract}"
    )

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

evidence = spark.table("fact_evidence_chunk").filter(F.col("source_type") == "news")
cache_evidence = cache.alias("c").join(
    evidence.alias("e"),
    (F.col("c.document_id") == F.col("e.id"))
    & (F.col("c.document_revision_hash") == F.col("e.revision_hash")),
    "left",
)
cache_orphan_count = cache_evidence.filter(F.col("e.id").isNull()).count()
cache_identity_mismatch_count = cache_evidence.filter(
    F.col("e.id").isNotNull()
    & (
        ~F.col("c.source_id").eqNullSafe(F.col("e.source_id"))
        | ~F.col("c.security_sk").eqNullSafe(F.col("e.security_sk"))
        | ~F.col("c.event_date").eqNullSafe(F.col("e.event_date"))
        | ~F.col("c.knowledge_date").eqNullSafe(F.col("e.knowledge_date"))
    )
).count()
ungrounded_count = (
    cache_evidence.filter(F.col("e.id").isNotNull())
    .select(
        "c.*",
        F.col("e.content").alias("source_content"),
    )
    .selectExpr(
        "*",
        "exists(map_values(evidence_quotes), quote -> "
        "quote IS NULL OR length(quote) = 0 OR instr(source_content, quote) = 0) "
        "AS invalid_evidence_quote",
        "exists(map_values(theme_evidence), quote -> "
        "quote IS NULL OR length(quote) = 0 OR instr(source_content, quote) = 0) "
        "AS invalid_theme_quote",
    )
    .filter(F.col("invalid_evidence_quote") | F.col("invalid_theme_quote"))
    .count()
)

evidence_asof = evidence.filter(
    F.col("security_sk").isNotNull()
    & F.col("event_date").isNotNull()
    & F.col("knowledge_date").isNotNull()
    & (F.col("event_date") <= F.col("knowledge_date"))
    & (F.col("knowledge_date") <= as_of)
    & (F.col("event_date") >= F.date_sub(as_of, LOOKBACK_DAYS - 1))
)
latest_revision_window = Window.partitionBy("source_id").orderBy(
    F.col("knowledge_date").desc(),
    F.col("event_date").desc(),
    F.col("revision_hash").desc(),
    F.col("id").desc(),
)
eligible_latest = (
    evidence_asof.withColumn("revision_row_number", F.row_number().over(latest_revision_window))
    .filter(F.col("revision_row_number") == 1)
    .drop("revision_row_number")
)
source_cache_orphan_count = eligible_latest.alias("e").join(
    cache.alias("c"),
    (F.col("e.id") == F.col("c.document_id"))
    & (F.col("e.revision_hash") == F.col("c.document_revision_hash")),
    "left_anti",
).count()
if (
    cache_orphan_count
    or cache_identity_mismatch_count
    or ungrounded_count
    or source_cache_orphan_count
):
    raise RuntimeError(
        "E21 evidence grounding validation failed: "
        f"cache_orphans={cache_orphan_count}, "
        f"identity_mismatches={cache_identity_mismatch_count}, "
        f"ungrounded={ungrounded_count}, "
        f"source_cache_orphans={source_cache_orphan_count}"
    )

verified_cache = cache_evidence.select(
    "c.*",
    F.col("e.content").alias("source_content"),
)
feature_rows = verified_cache.select(
    F.col("id").alias("cache_key"),
    "document_id",
    "security_sk",
    "symbol",
    "source_id",
    "source_type",
    "document_revision_hash",
    "sentiment",
    "relevance",
    "forward_promise_ratio",
    "hype_density",
    F.to_json("themes").alias("themes_json"),
    F.to_json("evidence_quotes").alias("evidence_quotes_json"),
    F.to_json("theme_evidence").alias("theme_evidence_json"),
    "model_version",
    "prompt_version",
    "prompt_sha256",
    "input_generation",
    F.col("generation").alias("extraction_generation"),
    "extracted_at",
    "event_date",
    "knowledge_date",
)

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_narrative_features (
        cache_key STRING NOT NULL,
        document_id STRING NOT NULL,
        security_sk BIGINT NOT NULL,
        symbol STRING,
        source_id STRING NOT NULL,
        source_type STRING NOT NULL,
        document_revision_hash STRING NOT NULL,
        sentiment DOUBLE NOT NULL,
        relevance DOUBLE NOT NULL,
        forward_promise_ratio DOUBLE NOT NULL,
        hype_density DOUBLE NOT NULL,
        themes_json STRING NOT NULL,
        evidence_quotes_json STRING NOT NULL,
        theme_evidence_json STRING NOT NULL,
        model_version STRING NOT NULL,
        prompt_version STRING NOT NULL,
        prompt_sha256 STRING NOT NULL,
        input_generation STRING NOT NULL,
        extraction_generation STRING NOT NULL,
        extracted_at TIMESTAMP NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL
    ) USING DELTA
""")

immutable_columns = [
    "document_id", "security_sk", "symbol", "source_id", "source_type",
    "document_revision_hash", "sentiment", "relevance", "forward_promise_ratio",
    "hype_density", "themes_json", "evidence_quotes_json", "theme_evidence_json",
    "model_version", "prompt_version", "prompt_sha256", "extracted_at", "event_date", "knowledge_date",
]
feature_target = DeltaTable.forName(spark, "fact_narrative_features")
immutable_conflicts = (
    feature_target.toDF().alias("t")
    .join(feature_rows.alias("s"), F.col("t.cache_key") == F.col("s.cache_key"), "inner")
    .filter(reduce(
        lambda left, right: left | right,
        [~F.col(f"t.{column}").eqNullSafe(F.col(f"s.{column}")) for column in immutable_columns],
    ))
    .count()
)
if immutable_conflicts:
    raise RuntimeError(f"E21 immutable cache conflicts detected: immutable_conflicts={immutable_conflicts}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

extracted_latest = (
    eligible_latest.alias("e")
    .join(
        cache.alias("c"),
        (F.col("e.id") == F.col("c.document_id"))
        & (F.col("e.revision_hash") == F.col("c.document_revision_hash")),
        "inner",
    )
    .select(
        F.col("e.security_sk").alias("security_sk"),
        F.col("e.id").alias("document_id"),
        F.col("e.source_id").alias("source_id"),
        F.col("e.event_date").alias("event_date"),
        F.col("e.knowledge_date").alias("knowledge_date"),
        F.col("c.sentiment").alias("sentiment"),
        F.col("c.relevance").alias("relevance"),
        F.col("c.forward_promise_ratio").alias("forward_promise_ratio"),
        F.col("c.hype_density").alias("hype_density"),
        F.col("c.themes").alias("themes"),
    )
)

eligible_counts = eligible_latest.groupBy("security_sk").agg(
    F.countDistinct("id").cast(LongType()).alias("eligible_document_count")
)
narrative_aggregate = extracted_latest.groupBy("security_sk").agg(
    F.countDistinct("document_id").cast(LongType()).alias("extracted_document_count"),
    F.sum("relevance").alias("relevance_sum"),
    F.sum(F.col("relevance") * F.col("sentiment")).alias("weighted_sentiment_sum"),
    F.sum(F.col("relevance") * F.col("forward_promise_ratio")).alias("weighted_forward_sum"),
    F.sum(F.col("relevance") * F.col("hype_density")).alias("weighted_hype_sum"),
    F.max("event_date").alias("document_event_date"),
    F.max("knowledge_date").alias("document_knowledge_date"),
    F.sort_array(F.collect_set("document_id")).alias("evidence_document_ids"),
).withColumn(
    "sentiment_level",
    F.when(F.col("relevance_sum") > 0, F.col("weighted_sentiment_sum") / F.col("relevance_sum")),
).withColumn(
    "forward_promise_ratio",
    F.when(F.col("relevance_sum") > 0, F.col("weighted_forward_sum") / F.col("relevance_sum")),
).withColumn(
    "hype_density",
    F.when(F.col("relevance_sum") > 0, F.col("weighted_hype_sum") / F.col("relevance_sum")),
)

theme_rows = (
    extracted_latest.select(
        "security_sk", "document_id", "relevance",
        F.explode(F.array_distinct("themes")).alias("theme_label_raw"),
    )
    .withColumn(
        "theme_label",
        F.regexp_replace(
            F.regexp_replace(F.lower(F.trim("theme_label_raw")), "[^a-z0-9]+", "_"),
            "^_+|_+$",
            "",
        ),
    )
    .filter(F.length("theme_label") > 0)
    .dropDuplicates(["security_sk", "document_id", "theme_label"])
)
theme_weights = theme_rows.groupBy("security_sk", "theme_label").agg(
    F.sum("relevance").alias("theme_relevance")
)
theme_weight_window = Window.partitionBy("security_sk")
theme_concentration = (
    theme_weights.withColumn("theme_relevance_total", F.sum("theme_relevance").over(theme_weight_window))
    .groupBy("security_sk")
    .agg(
        F.sum(
            F.when(
                F.col("theme_relevance_total") > 0,
                F.pow(F.col("theme_relevance") / F.col("theme_relevance_total"), 2),
            )
        ).alias("theme_concentration")
    )
)

sentiment_windows = extracted_latest.groupBy("security_sk").agg(
    F.sum(F.when(
        F.col("event_date").between(F.date_sub(as_of, 13), as_of),
        F.col("relevance") * F.col("sentiment"),
    )).alias("latest_sentiment_sum"),
    F.sum(F.when(
        F.col("event_date").between(F.date_sub(as_of, 13), as_of),
        F.col("relevance"),
    )).alias("latest_relevance_sum"),
    F.sum(F.when(
        F.col("event_date").between(F.date_sub(as_of, 27), F.date_sub(as_of, 14)),
        F.col("relevance") * F.col("sentiment"),
    )).alias("prior_sentiment_sum"),
    F.sum(F.when(
        F.col("event_date").between(F.date_sub(as_of, 27), F.date_sub(as_of, 14)),
        F.col("relevance"),
    )).alias("prior_relevance_sum"),
).withColumn(
    "sentiment_velocity_raw",
    F.when(
        (F.col("latest_relevance_sum") > 0) & (F.col("prior_relevance_sum") > 0),
        (F.col("latest_sentiment_sum") / F.col("latest_relevance_sum"))
        - (F.col("prior_sentiment_sum") / F.col("prior_relevance_sum")),
    ),
)
cross_section_window = Window.partitionBy()
sentiment_velocity = (
    sentiment_windows.withColumn(
        "velocity_mean", F.avg("sentiment_velocity_raw").over(cross_section_window)
    )
    .withColumn(
        "velocity_stddev", F.stddev_pop("sentiment_velocity_raw").over(cross_section_window)
    )
    .withColumn(
        "sentiment_velocity_z",
        F.when(
            F.col("sentiment_velocity_raw").isNotNull() & (F.col("velocity_stddev") > 0),
            (F.col("sentiment_velocity_raw") - F.col("velocity_mean")) / F.col("velocity_stddev"),
        ),
    )
    .select("security_sk", "sentiment_velocity_z")
)

same_date_features = spark.table("security_daily_features").filter(
    (F.col("as_of") == as_of) | (F.col("date_sk") == F.lit(date_sk))
)
invalid_same_date_features = same_date_features.filter(
    F.col("as_of").isNull()
    | (F.col("as_of") != as_of)
    | (F.col("date_sk") != F.lit(date_sk))
    | F.col("max_knowledge_date").isNull()
    | ~(F.col("max_knowledge_date") <= as_of)
).count()
duplicate_same_date_features = same_date_features.groupBy("security_sk").count().filter(
    F.col("count") > 1
).count()
if invalid_same_date_features or duplicate_same_date_features:
    raise RuntimeError(
        "E21 security_daily_features validation failed: "
        f"invalid_same_date={invalid_same_date_features}, "
        f"duplicate_same_date={duplicate_same_date_features}"
    )
metric_inputs = same_date_features.select(
    "security_sk", "news_volume_z_30d", "insider_net_buy_ratio_90d", "max_knowledge_date"
)

scored = (
    eligible_counts
    .join(narrative_aggregate, "security_sk", "left")
    .join(theme_concentration, "security_sk", "left")
    .join(sentiment_velocity, "security_sk", "left")
    .join(metric_inputs, "security_sk", "left")
    .withColumn("extracted_document_count", F.coalesce("extracted_document_count", F.lit(0)).cast(LongType()))
    .withColumn(
        "extraction_coverage",
        F.col("extracted_document_count") / F.col("eligible_document_count"),
    )
    .withColumn("sentiment_strength", _clamp(F.abs("sentiment_level")))
    .withColumn("sentiment_velocity_strength", _clamp(F.abs("sentiment_velocity_z") / F.lit(3.0)))
    .withColumn("theme_concentration", _clamp(F.col("theme_concentration")))
    .withColumn("forward_promise_ratio", _clamp(F.col("forward_promise_ratio")))
    .withColumn("hype_density", _clamp(F.col("hype_density")))
    .withColumn(
        "news_attention",
        F.when(
            F.col("news_volume_z_30d").isNotNull(),
            (F.tanh(F.col("news_volume_z_30d") / F.lit(2.0)) + F.lit(1.0)) / F.lit(2.0),
        ),
    )
    .withColumn(
        "insider_divergence",
        F.when(
            F.col("insider_net_buy_ratio_90d").isNotNull(),
            _clamp(-F.col("insider_net_buy_ratio_90d")),
        ),
    )
    .withColumn("mgmt_reality_gap", F.lit(None).cast(DoubleType()))
    .withColumn("revision_dispersion_z", F.lit(None).cast(DoubleType()))
    .withColumn("options_skew", F.lit(None).cast(DoubleType()))
)

available_weight = sum(
    F.when(F.col(component).isNotNull(), F.lit(weight)).otherwise(F.lit(0.0))
    for component, weight in COMPONENT_WEIGHTS.items()
)
weighted_sum = sum(
    F.coalesce(F.col(component), F.lit(0.0)) * F.lit(weight)
    for component, weight in COMPONENT_WEIGHTS.items()
)
scored = scored.withColumn("available_weight", F.round(available_weight, 6)).withColumn(
    "weighted_sum", weighted_sum
)
withheld = (F.col("extracted_document_count") < 3) | (F.col("available_weight") < 0.5)
reason_columns = [
    F.when(F.col("sentiment_strength").isNull(), F.lit("sentiment_strength:missing")),
    F.when(F.col("sentiment_velocity_strength").isNull(), F.lit("sentiment_velocity_strength:missing")),
    F.when(F.col("theme_concentration").isNull(), F.lit("theme_concentration:missing")),
    F.when(F.col("forward_promise_ratio").isNull(), F.lit("forward_promise_ratio:missing")),
    F.when(F.col("hype_density").isNull(), F.lit("hype_density:missing")),
    F.when(F.col("news_attention").isNull(), F.lit("news_attention:missing")),
    F.when(F.col("insider_divergence").isNull(), F.lit("insider_divergence:missing")),
    F.when(
        F.col("extracted_document_count") < F.col("eligible_document_count"),
        F.lit("document_extraction:incomplete"),
    ),
    F.lit("mgmt_reality_gap:source_unavailable"),
    F.lit("revision_dispersion_z:source_unavailable"),
    F.lit("options_skew:source_unavailable"),
    F.when(F.col("extracted_document_count") < 3, F.lit("minimum_documents:not_met")),
    F.when(F.col("available_weight") < 0.5, F.lit("minimum_weight:not_met")),
]
scored = (
    scored.withColumn("coverage_status", F.when(withheld, F.lit("WITHHELD")).otherwise(F.lit("PARTIAL")))
    .withColumn(
        "narrative_intensity",
        F.when(withheld, F.lit(None).cast(DoubleType())).otherwise(
            F.round(F.lit(100.0) * F.col("weighted_sum") / F.col("available_weight"), 6)
        ),
    )
    .withColumn(
        "coverage_reasons_json",
        F.to_json(F.array_sort(F.array_distinct(F.filter(
            F.array(*reason_columns), lambda reason: reason.isNotNull()
        )))),
    )
    .withColumn("evidence_document_ids_json", F.to_json("evidence_document_ids"))
    .withColumn("date_sk", F.lit(date_sk))
    .withColumn("model_version", F.lit(MODEL_VERSION))
    .withColumn("prompt_version", F.lit(PROMPT_VERSION))
    .withColumn("input_generation", F.lit(input_generation))
    .withColumn("extraction_generation", F.lit(extraction_generation))
    .withColumn("event_date", F.col("document_event_date").cast(DateType()))
    .withColumn(
        "knowledge_date",
        F.greatest(F.col("document_knowledge_date"), F.col("max_knowledge_date")).cast(DateType()),
    )
)

intensity_rows = scored.select(
    "security_sk", "date_sk", "eligible_document_count", "extracted_document_count",
    "extraction_coverage", "sentiment_level", "sentiment_strength",
    "sentiment_velocity_z", "sentiment_velocity_strength", "theme_concentration",
    "forward_promise_ratio", "hype_density", "news_volume_z_30d", "news_attention",
    "insider_net_buy_ratio_90d", "insider_divergence", "mgmt_reality_gap",
    "revision_dispersion_z", "options_skew", "narrative_intensity", "available_weight",
    "coverage_status", "coverage_reasons_json", "evidence_document_ids_json",
    "model_version", "prompt_version", "input_generation", "extraction_generation",
    "event_date", "knowledge_date",
)
if intensity_rows.isEmpty():
    raise RuntimeError("E21 narrative intensity snapshot is empty")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_narrative_intensity (
        security_sk BIGINT NOT NULL,
        date_sk INT NOT NULL,
        eligible_document_count BIGINT NOT NULL,
        extracted_document_count BIGINT NOT NULL,
        extraction_coverage DOUBLE NOT NULL,
        sentiment_level DOUBLE,
        sentiment_strength DOUBLE,
        sentiment_velocity_z DOUBLE,
        sentiment_velocity_strength DOUBLE,
        theme_concentration DOUBLE,
        forward_promise_ratio DOUBLE,
        hype_density DOUBLE,
        news_volume_z_30d DOUBLE,
        news_attention DOUBLE,
        insider_net_buy_ratio_90d DOUBLE,
        insider_divergence DOUBLE,
        mgmt_reality_gap DOUBLE,
        revision_dispersion_z DOUBLE,
        options_skew DOUBLE,
        narrative_intensity DOUBLE,
        available_weight DOUBLE NOT NULL,
        coverage_status STRING NOT NULL,
        coverage_reasons_json STRING NOT NULL,
        evidence_document_ids_json STRING NOT NULL,
        model_version STRING NOT NULL,
        prompt_version STRING NOT NULL,
        input_generation STRING NOT NULL,
        extraction_generation STRING NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL
    ) USING DELTA
""")
spark.sql("""
    CREATE TABLE IF NOT EXISTS narrative_snapshot_manifest (
        generation STRING NOT NULL,
        as_of_date DATE NOT NULL,
        status STRING NOT NULL,
        feature_count BIGINT NOT NULL,
        intensity_count BIGINT NOT NULL,
        fingerprint STRING NOT NULL,
        created_at TIMESTAMP NOT NULL,
        completed_at TIMESTAMP
    ) USING DELTA
""")

feature_count = feature_rows.count()
intensity_count = intensity_rows.count()
feature_fingerprint = _frame_fingerprint(feature_rows, feature_rows.columns)
intensity_fingerprint = _frame_fingerprint(intensity_rows, intensity_rows.columns)
snapshot_fingerprint = hashlib.sha256(
    f"{feature_fingerprint}|{intensity_fingerprint}".encode("utf-8")
).hexdigest()
run_started_at = datetime.now(timezone.utc)
manifest_schema = StructType([
    StructField("generation", StringType(), False),
    StructField("as_of_date", DateType(), False),
    StructField("status", StringType(), False),
    StructField("feature_count", LongType(), False),
    StructField("intensity_count", LongType(), False),
    StructField("fingerprint", StringType(), False),
    StructField("created_at", TimestampType(), False),
    StructField("completed_at", TimestampType()),
])


def _manifest_frame(status: str, completed_at):
    return spark.createDataFrame([(
        extraction_generation,
        parsed_as_of_date,
        status,
        feature_count,
        intensity_count,
        snapshot_fingerprint,
        run_started_at,
        completed_at,
    )], manifest_schema)


manifest_target = DeltaTable.forName(spark, "narrative_snapshot_manifest")
(
    manifest_target.alias("t")
    .merge(
        _manifest_frame("running", None).alias("s"),
        "t.generation = s.generation AND t.as_of_date = s.as_of_date",
    )
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
    .execute()
)

(
    feature_target.alias("t")
    .merge(feature_rows.alias("s"), "t.cache_key = s.cache_key")
    .whenMatchedUpdate(set={
        "input_generation": "s.input_generation",
        "extraction_generation": "s.extraction_generation",
    })
    .whenNotMatchedInsertAll()
    .execute()
)

intensity_target = DeltaTable.forName(spark, "fact_narrative_intensity")
(
    intensity_target.alias("t")
    .merge(
        intensity_rows.alias("s"),
        "t.security_sk = s.security_sk AND t.date_sk = s.date_sk "
        "AND t.model_version = s.model_version AND t.prompt_version = s.prompt_version",
    )
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
    .whenNotMatchedBySourceDelete(
        f"t.date_sk = {date_sk} AND t.model_version = '{MODEL_VERSION}' "
        f"AND t.prompt_version = '{PROMPT_VERSION}'"
    )
    .execute()
)

if spark.catalog.tableExists("dim_security"):
    current_security = spark.table("dim_security").filter(F.col("is_current") == F.lit(True)).select(
        "security_sk", F.col("ticker"), F.col("company_name")
    )
    duplicate_dimension_rows = current_security.groupBy("security_sk").count().filter(
        F.col("count") > 1
    ).count()
    if duplicate_dimension_rows:
        raise RuntimeError(
            f"E21 current dim_security contains duplicate rows: {duplicate_dimension_rows}"
        )
    narrative_projection = intensity_rows.join(current_security, "security_sk", "left")
else:
    narrative_projection = (
        intensity_rows
        .withColumn("ticker", F.lit(None).cast(StringType()))
        .withColumn("company_name", F.lit(None).cast(StringType()))
    )
_replace_table("v_narrative_intensity", narrative_projection.select(
    "security_sk", "ticker", "company_name", "date_sk",
    "eligible_document_count", "extracted_document_count", "extraction_coverage",
    "sentiment_level", "sentiment_velocity_z", "theme_concentration",
    "forward_promise_ratio", "hype_density", "news_volume_z_30d",
    "insider_net_buy_ratio_90d", "narrative_intensity", "available_weight",
    "coverage_status", "coverage_reasons_json", "evidence_document_ids_json",
    "model_version", "prompt_version", "input_generation", "extraction_generation",
    "event_date", "knowledge_date",
))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

target_features = spark.table("fact_narrative_features").filter(
    F.col("extraction_generation") == F.lit(extraction_generation)
)
target_intensity = spark.table("fact_narrative_intensity").filter(
    (F.col("date_sk") == F.lit(date_sk))
    & (F.col("model_version") == F.lit(MODEL_VERSION))
    & (F.col("prompt_version") == F.lit(PROMPT_VERSION))
)
duplicate_feature_grains = target_features.groupBy("cache_key").count().filter(
    F.col("count") > 1
).count()
duplicate_intensity_grains = target_intensity.groupBy(
    "security_sk", "date_sk", "model_version", "prompt_version"
).count().filter(F.col("count") > 1).count()
feature_pit_violations = target_features.filter(
    F.col("event_date").isNull()
    | F.col("knowledge_date").isNull()
    | (F.col("event_date") > F.col("knowledge_date"))
    | (F.col("knowledge_date") > F.current_date())
).count()
intensity_pit_violations = target_intensity.filter(
    F.col("event_date").isNull()
    | F.col("knowledge_date").isNull()
    | (F.col("event_date") > F.col("knowledge_date"))
    | (F.col("knowledge_date") > as_of)
).count()
feature_range_violations = target_features.filter(
    (F.col("sentiment") < -1.0)
    | (F.col("sentiment") > 1.0)
    | (F.col("relevance") < 0.0)
    | (F.col("relevance") > 1.0)
    | (F.col("forward_promise_ratio") < 0.0)
    | (F.col("forward_promise_ratio") > 1.0)
    | (F.col("hype_density") < 0.0)
    | (F.col("hype_density") > 1.0)
).count()
intensity_range_violations = target_intensity.filter(
    (F.col("eligible_document_count") < 0)
    | (F.col("extracted_document_count") < 0)
    | (F.col("extracted_document_count") > F.col("eligible_document_count"))
    | (F.col("extraction_coverage") < 0.0)
    | (F.col("extraction_coverage") > 1.0)
    | (F.col("available_weight") < 0.0)
    | (F.col("available_weight") > 1.0)
    | (F.col("sentiment_level").isNotNull() & ~F.col("sentiment_level").between(-1.0, 1.0))
    | (F.col("sentiment_strength").isNotNull() & ~F.col("sentiment_strength").between(0.0, 1.0))
    | (F.col("sentiment_velocity_strength").isNotNull() & ~F.col("sentiment_velocity_strength").between(0.0, 1.0))
    | (F.col("theme_concentration").isNotNull() & ~F.col("theme_concentration").between(0.0, 1.0))
    | (F.col("forward_promise_ratio").isNotNull() & ~F.col("forward_promise_ratio").between(0.0, 1.0))
    | (F.col("hype_density").isNotNull() & ~F.col("hype_density").between(0.0, 1.0))
    | (F.col("news_attention").isNotNull() & ~F.col("news_attention").between(0.0, 1.0))
    | (F.col("insider_divergence").isNotNull() & ~F.col("insider_divergence").between(0.0, 1.0))
    | (F.col("narrative_intensity").isNotNull() & ~F.col("narrative_intensity").between(0.0, 100.0))
).count()
coverage_violations = target_intensity.filter(
    ~F.col("coverage_status").isin("WITHHELD", "PARTIAL")
    | ((F.col("coverage_status") == "WITHHELD") & F.col("narrative_intensity").isNotNull())
    | ((F.col("coverage_status") == "PARTIAL") & F.col("narrative_intensity").isNull())
    | F.col("mgmt_reality_gap").isNotNull()
    | F.col("revision_dispersion_z").isNotNull()
    | F.col("options_skew").isNotNull()
    | (F.col("coverage_status") == "READY")
    | ~F.col("coverage_reasons_json").contains("mgmt_reality_gap:source_unavailable")
    | ~F.col("coverage_reasons_json").contains("revision_dispersion_z:source_unavailable")
    | ~F.col("coverage_reasons_json").contains("options_skew:source_unavailable")
).count()
version_violations = target_features.filter(
    (F.col("model_version") != F.lit(MODEL_VERSION))
    | (F.col("prompt_version") != F.lit(PROMPT_VERSION))
    | (F.col("prompt_sha256") != F.lit(PROMPT_SHA256))
    | (F.col("input_generation") != F.lit(input_generation))
    | (F.col("extraction_generation") != F.lit(extraction_generation))
).count() + target_intensity.filter(
    (F.col("model_version") != F.lit(MODEL_VERSION))
    | (F.col("prompt_version") != F.lit(PROMPT_VERSION))
    | (F.col("input_generation") != F.lit(input_generation))
    | (F.col("extraction_generation") != F.lit(extraction_generation))
).count()

stored_grounding = (
    target_features
    .withColumn("stored_evidence_quotes", F.from_json("evidence_quotes_json", MapType(StringType(), StringType())))
    .withColumn("stored_theme_evidence", F.from_json("theme_evidence_json", MapType(StringType(), StringType())))
    .alias("f")
    .join(
        evidence.alias("e"),
        (F.col("f.document_id") == F.col("e.id"))
        & (F.col("f.document_revision_hash") == F.col("e.revision_hash")),
        "left",
    )
)
post_write_orphans = stored_grounding.filter(F.col("e.id").isNull()).count()
post_write_identity_mismatches = stored_grounding.filter(
    F.col("e.id").isNotNull()
    & (
        ~F.col("f.source_id").eqNullSafe(F.col("e.source_id"))
        | ~F.col("f.security_sk").eqNullSafe(F.col("e.security_sk"))
        | ~F.col("f.event_date").eqNullSafe(F.col("e.event_date"))
        | ~F.col("f.knowledge_date").eqNullSafe(F.col("e.knowledge_date"))
    )
).count()
post_write_ungrounded = stored_grounding.filter(F.col("e.id").isNotNull()).select(
    "f.*", F.col("e.content").alias("source_content")
).selectExpr(
    "*",
    "exists(map_values(stored_evidence_quotes), quote -> "
    "quote IS NULL OR length(quote) = 0 OR instr(source_content, quote) = 0) "
    "AS invalid_evidence_quote",
    "exists(map_values(stored_theme_evidence), quote -> "
    "quote IS NULL OR length(quote) = 0 OR instr(source_content, quote) = 0) "
    "AS invalid_theme_quote",
).filter(F.col("invalid_evidence_quote") | F.col("invalid_theme_quote")).count()

actual_feature_count = target_features.count()
actual_intensity_count = target_intensity.count()
projection_count = spark.table("v_narrative_intensity").count()
running_manifest_rows = spark.table("narrative_snapshot_manifest").filter(
    (F.col("generation") == F.lit(extraction_generation))
    & (F.col("as_of_date") == as_of)
    & (F.col("status") == F.lit("running"))
).collect()
manifest_count_mismatch = (
    len(running_manifest_rows) != 1
    or running_manifest_rows[0].feature_count != actual_feature_count
    or running_manifest_rows[0].intensity_count != actual_intensity_count
    or running_manifest_rows[0].fingerprint != snapshot_fingerprint
)

validation_counts = {
    "duplicate_feature_grains": duplicate_feature_grains,
    "duplicate_intensity_grains": duplicate_intensity_grains,
    "feature_pit_violations": feature_pit_violations,
    "intensity_pit_violations": intensity_pit_violations,
    "source_cache_orphans": source_cache_orphan_count + post_write_orphans,
    "identity_mismatches": cache_identity_mismatch_count + post_write_identity_mismatches,
    "ungrounded_citations": ungrounded_count + post_write_ungrounded,
    "feature_range_violations": feature_range_violations,
    "intensity_range_violations": intensity_range_violations,
    "coverage_violations": coverage_violations,
    "version_violations": version_violations,
    "manifest_count_mismatch": int(manifest_count_mismatch),
    "projection_count_mismatch": int(projection_count != actual_intensity_count),
}
if any(validation_counts.values()):
    raise RuntimeError(f"E21 post-write validation failed: {json.dumps(validation_counts, sort_keys=True)}")

completed_at = datetime.now(timezone.utc)
(
    manifest_target.alias("t")
    .merge(
        _manifest_frame("completed", completed_at).alias("s"),
        "t.generation = s.generation AND t.as_of_date = s.as_of_date",
    )
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
    .execute()
)

run_summary = {
    "generation": extraction_generation,
    "input_generation": input_generation,
    "as_of_date": as_of_date,
    "status": "completed",
    "feature_count": actual_feature_count,
    "intensity_count": actual_intensity_count,
    "fingerprint": snapshot_fingerprint,
    "coverage_status_counts": {
        row.coverage_status: row["count"]
        for row in target_intensity.groupBy("coverage_status").count().collect()
    },
    "validation": validation_counts,
}
run_summary_json = json.dumps(run_summary, sort_keys=True)
print(run_summary_json)
mssparkutils.notebook.exit(run_summary_json)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }