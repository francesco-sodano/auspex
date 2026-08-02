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

# Fabric Notebook: nb_12_narrative_premium
# Attributes E20 valuation residuals to E21 narrative intensity without model calls.

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from datetime import date, datetime, timezone
from functools import reduce
import hashlib
import importlib.util
import json
import os
import sys
import tempfile

from delta.tables import DeltaTable
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql.types import (
    BooleanType,
    DateType,
    DoubleType,
    IntegerType,
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

MODEL_VERSION = "e22_v4"
E20_MODEL_VERSION = "e20_v2"
E21_MODEL_VERSION = "gpt-4o:2024-11-20"
E21_PROMPT_VERSION = "e21_narrative_v1"
ENGINE_LAKEHOUSE_PATH = "Files/config/e22/09e9532dd031ecb45e8e3591986164d763a4ebbec3da43246c8ca8040aaa02ea.py"
ENGINE_SHA256 = "09e9532dd031ecb45e8e3591986164d763a4ebbec3da43246c8ca8040aaa02ea"


def _require_table(table_name: str) -> None:
    if not spark.catalog.tableExists(table_name):
        raise RuntimeError(f"Required E22 table is missing: {table_name}")


def _ensure_columns(table_name: str, columns: dict[str, str]) -> None:
    existing = {field.name for field in spark.table(table_name).schema.fields}
    for column_name, definition in columns.items():
        if column_name not in existing:
            spark.sql(f"ALTER TABLE {table_name} ADD COLUMNS ({definition})")


def _portable_row_hashes(frame, columns: list[str]) -> list[str]:
    return [
        row.row_hash
        for row in frame.select(
            F.sha2(
                F.concat_ws(
                    "\u001f",
                    *[F.col(column).cast(StringType()) for column in columns],
                ),
                256,
            ).alias("row_hash")
        ).orderBy("row_hash").collect()
    ]


def _portable_snapshot_fingerprint(decision_frame, evidence_frame) -> str:
    decision_columns = [
        "decision_id", "decision_type", "security_sk", "date_sk", "output_status",
        "input_snapshot_hash", "model_version", "output_json", "evidence_pack_json",
        "event_date", "knowledge_date",
    ]
    evidence_columns = [
        "decision_id", "evidence_ordinal", "document_id", "input_snapshot_hash",
        "model_version", "event_date", "knowledge_date",
    ]
    decision_hashes = "|".join(_portable_row_hashes(decision_frame, decision_columns))
    evidence_hashes = "|".join(_portable_row_hashes(evidence_frame, evidence_columns))
    return hashlib.sha256(f"{decision_hashes}|{evidence_hashes}".encode("ascii")).hexdigest()


def _canonical_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


for required_table in [
    "fact_fundamental_anchor",
    "fundamental_anchor_snapshot_manifest",
    "fact_narrative_intensity",
    "narrative_snapshot_manifest",
]:
    _require_table(required_table)

engine_source = mssparkutils.fs.head(ENGINE_LAKEHOUSE_PATH, 1024 * 1024)
engine_bytes = engine_source.encode("utf-8")
if hashlib.sha256(engine_bytes).hexdigest() != ENGINE_SHA256:
    raise RuntimeError("E22 engine resource hash mismatch")
engine_path = os.path.join(tempfile.gettempdir(), "narrative_premium_e22_v4.py")
with open(engine_path, "wb") as engine_file:
    engine_file.write(engine_bytes)
engine_spec = importlib.util.spec_from_file_location("narrative_premium", engine_path)
if engine_spec is None or engine_spec.loader is None:
    raise RuntimeError(f"Could not load E22 engine resource: {engine_path}")
engine_module = importlib.util.module_from_spec(engine_spec)
sys.modules[engine_spec.name] = engine_module
engine_spec.loader.exec_module(engine_module)
os.remove(engine_path)
PremiumObservation = engine_module.PremiumObservation
PreviousPremiumState = engine_module.PreviousPremiumState
build_narrative_premiums = engine_module.build_narrative_premiums

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

completed_e21_manifests = spark.table("narrative_snapshot_manifest").filter(
    F.col("status") == F.lit("completed")
)
if completed_e21_manifests.isEmpty():
    raise RuntimeError("E22 requires a completed E21 manifest")

as_of_date = str(as_of_date).strip()
if not as_of_date:
    as_of_date = completed_e21_manifests.agg(
        F.max("as_of_date").alias("as_of_date")
    ).first().as_of_date.isoformat()
parsed_as_of_date = date.fromisoformat(as_of_date)
if parsed_as_of_date > datetime.now(timezone.utc).date():
    raise ValueError("as_of_date cannot be in the future")
date_sk = int(parsed_as_of_date.strftime("%Y%m%d"))

selected_e21_manifest = (
    completed_e21_manifests
    .filter(F.col("as_of_date") == F.lit(parsed_as_of_date))
    .orderBy(F.col("completed_at").desc(), F.col("generation").desc())
    .limit(1)
    .collect()
)
if len(selected_e21_manifest) != 1:
    raise RuntimeError(f"No completed E21 manifest exists for {as_of_date}")
e21_generation = selected_e21_manifest[0].generation
e21_manifest_fingerprint = selected_e21_manifest[0].fingerprint

selected_e20_manifest = (
    spark.table("fundamental_anchor_snapshot_manifest")
    .filter(
        (F.col("status") == F.lit("completed"))
        & (F.col("as_of_date") == F.lit(parsed_as_of_date))
        & (F.col("model_version") == F.lit(E20_MODEL_VERSION))
    )
    .orderBy(F.col("completed_at").desc(), F.col("generation").desc())
    .limit(1)
    .collect()
)
if len(selected_e20_manifest) != 1:
    raise RuntimeError(f"No completed E20 manifest exists for {as_of_date}")
e20_generation = selected_e20_manifest[0].generation
e20_manifest_fingerprint = selected_e20_manifest[0].fingerprint

e21_rows = (
    spark.table("fact_narrative_intensity")
    .filter(
        (F.col("date_sk") == F.lit(date_sk))
        & (F.col("extraction_generation") == F.lit(e21_generation))
        & (F.col("model_version") == F.lit(E21_MODEL_VERSION))
        & (F.col("prompt_version") == F.lit(E21_PROMPT_VERSION))
    )
)
if e21_rows.isEmpty():
    raise RuntimeError(f"E22 found no E21 rows for {as_of_date}")

duplicate_e21_rows = e21_rows.groupBy("security_sk").count().filter(F.col("count") > 1).count()
e21_pit_violations = e21_rows.filter(
    F.col("event_date").isNull()
    | F.col("knowledge_date").isNull()
    | (F.col("event_date") > F.col("knowledge_date"))
    | (F.col("knowledge_date") > F.lit(parsed_as_of_date))
).count()
if duplicate_e21_rows or e21_pit_violations:
    raise RuntimeError(
        "E22 E21 input validation failed: "
        f"duplicate_rows={duplicate_e21_rows}, pit_violations={e21_pit_violations}"
    )

e20_rows = spark.table("fact_fundamental_anchor").filter(
    (F.col("date_sk") == F.lit(date_sk))
    & (F.col("model_version") == F.lit(E20_MODEL_VERSION))
)
e20_fingerprint_columns = [
    "security_sk", "date_sk", "ev_sales", "ev_ebitda", "p_fcf",
    "expected_ev_sales", "residual_evs", "residual_evebitda", "residual_pfcf",
    "anchor_residual", "fundamental_anchor_z", "anchor_method", "n_peers",
    "r2_sector", "uses_forward", "imputed_flags", "model_version", "source_sk",
    "event_date", "knowledge_date",
]
e20_actual_row_count = e20_rows.count()
e20_actual_fingerprint = (
    e20_rows
    .withColumn(
        "row_hash",
        F.sha2(F.to_json(F.struct(*[F.col(column) for column in e20_fingerprint_columns])), 256),
    )
    .agg(F.sha2(F.concat_ws("|", F.sort_array(F.collect_list("row_hash"))), 256).alias("fingerprint"))
    .first().fingerprint
)
if (
    e20_actual_row_count != selected_e20_manifest[0].row_count
    or e20_actual_fingerprint != e20_manifest_fingerprint
):
    raise RuntimeError("E22 E20 completed snapshot does not match current fact rows")
duplicate_e20_rows = e20_rows.groupBy("security_sk").count().filter(F.col("count") > 1).count()
e20_pit_violations = e20_rows.filter(
    F.col("event_date").isNull()
    | F.col("knowledge_date").isNull()
    | (F.col("event_date") > F.col("knowledge_date"))
    | (F.col("knowledge_date") > F.lit(parsed_as_of_date))
).count()
if duplicate_e20_rows or e20_pit_violations:
    raise RuntimeError(
        "E22 E20 input validation failed: "
        f"duplicate_rows={duplicate_e20_rows}, pit_violations={e20_pit_violations}"
    )

cohort = (
    e21_rows.alias("n")
    .join(
        e20_rows.alias("a"),
        (F.col("n.security_sk") == F.col("a.security_sk"))
        & (F.col("n.date_sk") == F.col("a.date_sk")),
        "left",
    )
    .select(
        F.col("n.security_sk").alias("security_sk"),
        F.col("n.narrative_intensity").alias("narrative_intensity"),
        F.col("n.coverage_status").alias("narrative_coverage_status"),
        F.col("n.available_weight").alias("narrative_available_weight"),
        F.col("n.extraction_coverage").alias("narrative_extraction_coverage"),
        F.col("n.coverage_reasons_json").alias("narrative_coverage_reasons_json"),
        F.col("n.sentiment_strength").alias("sentiment_strength"),
        F.col("n.sentiment_velocity_strength").alias("sentiment_velocity_strength"),
        F.col("n.theme_concentration").alias("theme_concentration"),
        F.col("n.forward_promise_ratio").alias("forward_promise_ratio"),
        F.col("n.hype_density").alias("hype_density"),
        F.col("n.news_attention").alias("news_attention"),
        F.col("n.insider_divergence").alias("insider_divergence"),
        F.col("n.evidence_document_ids_json").alias("evidence_document_ids_json"),
        F.col("n.model_version").alias("e21_model_version"),
        F.col("n.prompt_version").alias("prompt_version"),
        F.col("n.input_generation").alias("input_generation"),
        F.col("n.extraction_generation").alias("extraction_generation"),
        F.col("n.event_date").alias("narrative_event_date"),
        F.col("n.knowledge_date").alias("narrative_knowledge_date"),
        F.col("a.fundamental_anchor_z").alias("fundamental_anchor_z"),
        F.col("a.anchor_method").alias("anchor_method"),
        F.col("a.n_peers").alias("anchor_n_peers"),
        F.col("a.r2_sector").alias("anchor_r2_sector"),
        F.col("a.imputed_flags").alias("anchor_imputed_flags"),
        F.col("a.model_version").alias("e20_model_version"),
        F.col("a.event_date").alias("anchor_event_date"),
        F.col("a.knowledge_date").alias("anchor_knowledge_date"),
    )
)
if cohort.count() != e21_rows.count():
    raise RuntimeError("E22 cohort row count does not reconcile to E21")

observations = []
for row in cohort.orderBy("security_sk").collect():
    try:
        evidence_ids = json.loads(row.evidence_document_ids_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"E22 invalid evidence_document_ids_json for {row.security_sk}"
        ) from exc
    if not isinstance(evidence_ids, list) or any(
        not isinstance(value, str) or not value for value in evidence_ids
    ):
        raise RuntimeError(f"E22 invalid evidence IDs for {row.security_sk}")
    try:
        narrative_coverage_reasons = json.loads(row.narrative_coverage_reasons_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"E22 invalid narrative coverage reasons for {row.security_sk}"
        ) from exc
    if not isinstance(narrative_coverage_reasons, list):
        raise RuntimeError(f"E22 narrative coverage reasons must be an array for {row.security_sk}")
    component_names = (
        "sentiment_strength",
        "sentiment_velocity_strength",
        "theme_concentration",
        "forward_promise_ratio",
        "hype_density",
        "news_attention",
        "insider_divergence",
    )
    observations.append(PremiumObservation(
        security_sk=row.security_sk,
        as_of=parsed_as_of_date,
        fundamental_anchor_z=row.fundamental_anchor_z,
        anchor_method=row.anchor_method,
        narrative_intensity=row.narrative_intensity,
        narrative_coverage_status=row.narrative_coverage_status,
        narrative_available_weight=row.narrative_available_weight,
        narrative_extraction_coverage=row.narrative_extraction_coverage,
        narrative_component_mask=tuple(
            name for name in component_names if getattr(row, name) is not None
        ),
        narrative_coverage_reasons=tuple(narrative_coverage_reasons),
        anchor_event_date=row.anchor_event_date,
        anchor_knowledge_date=row.anchor_knowledge_date,
        anchor_n_peers=row.anchor_n_peers,
        anchor_r2_sector=row.anchor_r2_sector,
        anchor_imputed_flags=row.anchor_imputed_flags,
        narrative_event_date=row.narrative_event_date,
        narrative_knowledge_date=row.narrative_knowledge_date,
        evidence_document_ids=tuple(evidence_ids),
        e20_model_version=row.e20_model_version,
        e20_generation=e20_generation,
        e20_manifest_fingerprint=e20_manifest_fingerprint,
        e21_model_version=row.e21_model_version,
        prompt_version=row.prompt_version,
        input_generation=row.input_generation,
        extraction_generation=row.extraction_generation,
        e21_manifest_fingerprint=e21_manifest_fingerprint,
    ))

if spark.catalog.tableExists("fact_narrative_premium"):
    DeltaTable.forName(spark, "fact_narrative_premium").delete(
        F.col("model_version") != F.lit(MODEL_VERSION)
    )
if spark.catalog.tableExists("decision_log"):
    DeltaTable.forName(spark, "decision_log").delete(
        (F.col("decision_type") == F.lit("NARRATIVE_PREMIUM"))
        & (F.col("model_version") != F.lit(MODEL_VERSION))
    )
if spark.catalog.tableExists("fact_narrative_premium_evidence"):
    DeltaTable.forName(spark, "fact_narrative_premium_evidence").delete(
        F.col("model_version") != F.lit(MODEL_VERSION)
    )
if spark.catalog.tableExists("narrative_premium_snapshot_manifest"):
    active_snapshot_hashes = {
        row.input_snapshot_hash
        for row in spark.table("fact_narrative_premium")
        .filter(F.col("model_version") == F.lit(MODEL_VERSION))
        .select("input_snapshot_hash").distinct().collect()
    }
    if active_snapshot_hashes:
        quoted_hashes = ",".join(f"'{value}'" for value in sorted(active_snapshot_hashes))
        DeltaTable.forName(spark, "narrative_premium_snapshot_manifest").delete(
            f"input_snapshot_hash NOT IN ({quoted_hashes})"
        )
    else:
        DeltaTable.forName(spark, "narrative_premium_snapshot_manifest").delete()
for obsolete_projection in ("v_narrative_premium",):
    for drop_sql in (
        f"DROP VIEW IF EXISTS {obsolete_projection}",
        f"DROP TABLE IF EXISTS {obsolete_projection}",
    ):
        try:
            spark.sql(drop_sql)
        except Exception:
            pass

previous_premiums = {}
if (
    spark.catalog.tableExists("fact_narrative_premium")
    and spark.catalog.tableExists("narrative_premium_snapshot_manifest")
):
    _ensure_columns("fact_narrative_premium", {
        "fit_context_hash": "fit_context_hash STRING",
    })
    previous_manifest_window = Window.partitionBy("as_of_date").orderBy(
        F.col("completed_at").desc(), F.col("generation").desc()
    )
    latest_previous_manifests = (
        spark.table("narrative_premium_snapshot_manifest")
        .filter(
            (F.col("status") == F.lit("completed"))
            & (F.col("as_of_date") < F.lit(parsed_as_of_date))
        )
        .withColumn("manifest_row_number", F.row_number().over(previous_manifest_window))
        .filter(F.col("manifest_row_number") == 1)
        .drop("manifest_row_number")
    )
    previous_as_of = latest_previous_manifests.agg(
        F.max("as_of_date").alias("as_of_date")
    ).first().as_of_date
    if previous_as_of is not None:
        latest_previous_manifests = latest_previous_manifests.filter(
            F.col("as_of_date") == F.lit(previous_as_of)
        )
    previous_window = Window.partitionBy("security_sk").orderBy(
        F.col("p.date_sk").desc(), F.col("p.created_at").desc(), F.col("p.decision_id").desc()
    )
    previous_rows = (
        spark.table("fact_narrative_premium").alias("p")
        .join(
            latest_previous_manifests.alias("m"),
            (F.col("p.generation") == F.col("m.generation"))
            & (F.col("p.date_sk") == F.date_format(F.col("m.as_of_date"), "yyyyMMdd").cast(IntegerType())),
            "inner",
        )
        .filter(
            (F.col("p.model_version") == F.lit(MODEL_VERSION))
            & F.col("p.narrative_premium").isNotNull()
        )
        .withColumn("row_number", F.row_number().over(previous_window))
        .filter(F.col("row_number") == 1)
        .select(
            F.col("p.security_sk").alias("security_sk"),
            F.col("p.decision_id").alias("decision_id"),
            F.col("m.as_of_date").alias("previous_as_of"),
            F.col("p.generation").alias("generation"),
            F.col("p.narrative_premium").alias("narrative_premium"),
            F.col("p.fit_context_hash").alias("fit_context_hash"),
        )
        .collect()
    )
    previous_premiums = {
        row.security_sk: PreviousPremiumState(
            decision_id=row.decision_id,
            as_of=row.previous_as_of,
            generation=row.generation,
            narrative_premium=row.narrative_premium,
            fit_context_hash=row.fit_context_hash,
        )
        for row in previous_rows
    }

premium_results = build_narrative_premiums(
    observations,
    previous_premiums=previous_premiums,
)
if len(premium_results) != len(observations):
    raise RuntimeError("E22 engine output count does not reconcile to input")

input_snapshot_hashes = {result.input_snapshot_hash for result in premium_results}
if len(input_snapshot_hashes) != 1:
    raise RuntimeError("E22 engine returned multiple input snapshot hashes")
input_snapshot_hash = next(iter(input_snapshot_hashes))
decision_set_hash = hashlib.sha256(
    "|".join(sorted(result.decision_id for result in premium_results)).encode("ascii")
).hexdigest()
generation = f"e22-{decision_set_hash[:32]}"
run_started_at = datetime.now(timezone.utc)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

premium_schema = StructType([
    StructField("decision_id", StringType(), False),
    StructField("generation", StringType(), False),
    StructField("security_sk", LongType(), False),
    StructField("date_sk", IntegerType(), False),
    StructField("fundamental_anchor_z", DoubleType()),
    StructField("narrative_intensity", DoubleType()),
    StructField("narrative_intensity_z", DoubleType()),
    StructField("attribution_intercept", DoubleType()),
    StructField("attribution_beta", DoubleType()),
    StructField("attribution_r2", DoubleType()),
    StructField("narrative_premium", DoubleType()),
    StructField("unexplained_residual", DoubleType()),
    StructField("anchor_support_z", DoubleType()),
    StructField("divergence_state", StringType()),
    StructField("is_converging", BooleanType()),
    StructField("eligible_security_count", IntegerType(), False),
    StructField("coverage_status", StringType(), False),
    StructField("coverage_reasons_json", StringType(), False),
    StructField("evidence_pack_json", StringType(), False),
    StructField("input_snapshot_hash", StringType(), False),
    StructField("fit_context_hash", StringType(), False),
    StructField("e20_model_version", StringType()),
    StructField("e20_generation", StringType(), False),
    StructField("e20_manifest_fingerprint", StringType(), False),
    StructField("e21_model_version", StringType(), False),
    StructField("e21_manifest_fingerprint", StringType(), False),
    StructField("prompt_version", StringType(), False),
    StructField("input_generation", StringType(), False),
    StructField("extraction_generation", StringType(), False),
    StructField("model_version", StringType(), False),
    StructField("event_date", DateType(), False),
    StructField("knowledge_date", DateType(), False),
    StructField("created_at", TimestampType(), False),
])

premium_rows = []
decision_rows = []
evidence_rows = []
for result in premium_results:
    source = next(row for row in observations if row.security_sk == result.security_sk)
    coverage_reasons_json = _canonical_json(list(result.coverage_reasons))
    evidence_pack_json = _canonical_json(result.evidence_pack)
    output_payload = {
        "decision_id": result.decision_id,
        "security_sk": result.security_sk,
        "date_sk": date_sk,
        "fundamental_anchor_z": result.fundamental_anchor_z,
        "narrative_intensity": result.narrative_intensity,
        "narrative_intensity_z": result.narrative_intensity_z,
        "attribution_intercept": result.attribution_intercept,
        "attribution_beta": result.attribution_beta,
        "attribution_r2": result.attribution_r2,
        "narrative_premium": result.narrative_premium,
        "unexplained_residual": result.unexplained_residual,
        "anchor_support_z": result.anchor_support_z,
        "divergence_state": result.divergence_state,
        "is_converging": result.is_converging,
        "eligible_security_count": result.eligible_security_count,
        "coverage_status": result.coverage_status,
        "coverage_reasons": list(result.coverage_reasons),
        "model_version": result.model_version,
        "fit_context_hash": result.fit_context_hash,
        "e20_generation": source.e20_generation,
        "e20_manifest_fingerprint": source.e20_manifest_fingerprint,
        "e21_manifest_fingerprint": source.e21_manifest_fingerprint,
    }
    premium_rows.append((
        result.decision_id,
        generation,
        result.security_sk,
        date_sk,
        result.fundamental_anchor_z,
        result.narrative_intensity,
        result.narrative_intensity_z,
        result.attribution_intercept,
        result.attribution_beta,
        result.attribution_r2,
        result.narrative_premium,
        result.unexplained_residual,
        result.anchor_support_z,
        result.divergence_state,
        result.is_converging,
        result.eligible_security_count,
        result.coverage_status,
        coverage_reasons_json,
        evidence_pack_json,
        result.input_snapshot_hash,
        result.fit_context_hash,
        source.e20_model_version,
        source.e20_generation,
        source.e20_manifest_fingerprint,
        source.e21_model_version,
        source.e21_manifest_fingerprint,
        source.prompt_version,
        source.input_generation,
        source.extraction_generation,
        result.model_version,
        result.event_date,
        result.knowledge_date,
        run_started_at,
    ))
    decision_rows.append((
        result.decision_id,
        "NARRATIVE_PREMIUM",
        result.security_sk,
        date_sk,
        result.coverage_status,
        result.input_snapshot_hash,
        result.model_version,
        _canonical_json(output_payload),
        evidence_pack_json,
        result.event_date,
        result.knowledge_date,
        run_started_at,
    ))
    for evidence_ordinal, document_id in enumerate(sorted(set(source.evidence_document_ids))):
        evidence_rows.append((
            result.decision_id,
            evidence_ordinal,
            document_id,
            result.input_snapshot_hash,
            result.model_version,
            result.event_date,
            result.knowledge_date,
            run_started_at,
        ))

premium_frame = spark.createDataFrame(premium_rows, premium_schema)

decision_schema = StructType([
    StructField("decision_id", StringType(), False),
    StructField("decision_type", StringType(), False),
    StructField("security_sk", LongType(), False),
    StructField("date_sk", IntegerType(), False),
    StructField("output_status", StringType(), False),
    StructField("input_snapshot_hash", StringType(), False),
    StructField("model_version", StringType(), False),
    StructField("output_json", StringType(), False),
    StructField("evidence_pack_json", StringType(), False),
    StructField("event_date", DateType(), False),
    StructField("knowledge_date", DateType(), False),
    StructField("created_at", TimestampType(), False),
])
decision_frame = spark.createDataFrame(decision_rows, decision_schema)

evidence_schema = StructType([
    StructField("decision_id", StringType(), False),
    StructField("evidence_ordinal", IntegerType(), False),
    StructField("document_id", StringType(), False),
    StructField("input_snapshot_hash", StringType(), False),
    StructField("model_version", StringType(), False),
    StructField("event_date", DateType(), False),
    StructField("knowledge_date", DateType(), False),
    StructField("created_at", TimestampType(), False),
])
evidence_frame = spark.createDataFrame(evidence_rows, evidence_schema)

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_narrative_premium (
        decision_id STRING NOT NULL,
        generation STRING NOT NULL,
        security_sk BIGINT NOT NULL,
        date_sk INT NOT NULL,
        fundamental_anchor_z DOUBLE,
        narrative_intensity DOUBLE,
        narrative_intensity_z DOUBLE,
        attribution_intercept DOUBLE,
        attribution_beta DOUBLE,
        attribution_r2 DOUBLE,
        narrative_premium DOUBLE,
        unexplained_residual DOUBLE,
        anchor_support_z DOUBLE,
        divergence_state STRING,
        is_converging BOOLEAN,
        eligible_security_count INT NOT NULL,
        coverage_status STRING NOT NULL,
        coverage_reasons_json STRING NOT NULL,
        evidence_pack_json STRING NOT NULL,
        input_snapshot_hash STRING NOT NULL,
        fit_context_hash STRING,
        e20_model_version STRING,
        e20_generation STRING,
        e20_manifest_fingerprint STRING,
        e21_model_version STRING NOT NULL,
        e21_manifest_fingerprint STRING,
        prompt_version STRING NOT NULL,
        input_generation STRING NOT NULL,
        extraction_generation STRING NOT NULL,
        model_version STRING NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        created_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
_ensure_columns("fact_narrative_premium", {
    "fit_context_hash": "fit_context_hash STRING",
    "e20_generation": "e20_generation STRING",
    "e20_manifest_fingerprint": "e20_manifest_fingerprint STRING",
    "e21_manifest_fingerprint": "e21_manifest_fingerprint STRING",
})
spark.sql("""
    CREATE TABLE IF NOT EXISTS decision_log (
        decision_id STRING NOT NULL,
        decision_type STRING NOT NULL,
        security_sk BIGINT NOT NULL,
        date_sk INT NOT NULL,
        output_status STRING NOT NULL,
        input_snapshot_hash STRING NOT NULL,
        model_version STRING NOT NULL,
        output_json STRING NOT NULL,
        evidence_pack_json STRING NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        created_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_narrative_premium_evidence (
        decision_id STRING NOT NULL,
        evidence_ordinal INT NOT NULL,
        document_id STRING NOT NULL,
        input_snapshot_hash STRING NOT NULL,
        model_version STRING NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        created_at TIMESTAMP NOT NULL
    ) USING DELTA
""")
spark.sql("""
    CREATE TABLE IF NOT EXISTS narrative_premium_snapshot_manifest (
        generation STRING NOT NULL,
        as_of_date DATE NOT NULL,
        input_snapshot_hash STRING NOT NULL,
        status STRING NOT NULL,
        row_count BIGINT NOT NULL,
        evidence_count BIGINT,
        ready_count BIGINT NOT NULL,
        partial_count BIGINT NOT NULL,
        withheld_count BIGINT NOT NULL,
        fingerprint STRING NOT NULL,
        created_at TIMESTAMP NOT NULL,
        completed_at TIMESTAMP
    ) USING DELTA
""")
_ensure_columns("narrative_premium_snapshot_manifest", {
    "evidence_count": "evidence_count BIGINT",
})

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

fact_immutable_columns = [column for column in premium_frame.columns if column != "created_at"]
existing_fact_matches = (
    spark.table("fact_narrative_premium").alias("t")
    .join(premium_frame.alias("s"), "decision_id", "inner")
)
fact_conflict_condition = reduce(
    lambda left, right: left | right,
    [
        ~F.col(f"t.{column}").eqNullSafe(F.col(f"s.{column}"))
        for column in fact_immutable_columns
        if column != "decision_id"
    ],
)
fact_conflicts = existing_fact_matches.filter(fact_conflict_condition).count()

decision_immutable_columns = [column for column in decision_frame.columns if column != "created_at"]
existing_decision_matches = (
    spark.table("decision_log").alias("t")
    .join(decision_frame.alias("s"), "decision_id", "inner")
)
decision_conflict_condition = reduce(
    lambda left, right: left | right,
    [
        ~F.col(f"t.{column}").eqNullSafe(F.col(f"s.{column}"))
        for column in decision_immutable_columns
        if column != "decision_id"
    ],
)
decision_conflicts = existing_decision_matches.filter(decision_conflict_condition).count()
evidence_immutable_columns = [column for column in evidence_frame.columns if column != "created_at"]
existing_evidence_matches = (
    spark.table("fact_narrative_premium_evidence").alias("t")
    .join(
        evidence_frame.alias("s"),
        (F.col("t.decision_id") == F.col("s.decision_id"))
        & (F.col("t.evidence_ordinal") == F.col("s.evidence_ordinal")),
        "inner",
    )
)
evidence_conflict_condition = reduce(
    lambda left, right: left | right,
    [
        ~F.col(f"t.{column}").eqNullSafe(F.col(f"s.{column}"))
        for column in evidence_immutable_columns
        if column not in {"decision_id", "evidence_ordinal"}
    ],
)
evidence_conflicts = existing_evidence_matches.filter(evidence_conflict_condition).count()
if fact_conflicts or decision_conflicts or evidence_conflicts:
    raise RuntimeError(
        "E22 immutable replay conflict: "
        f"fact_conflicts={fact_conflicts}, decision_conflicts={decision_conflicts}, "
        f"evidence_conflicts={evidence_conflicts}"
    )

fact_insert_count = premium_frame.join(
    spark.table("fact_narrative_premium").select("decision_id"),
    "decision_id",
    "left_anti",
).count()
decision_log_insert_count = decision_frame.join(
    spark.table("decision_log").select("decision_id"),
    "decision_id",
    "left_anti",
).count()
evidence_insert_count = evidence_frame.alias("s").join(
    spark.table("fact_narrative_premium_evidence").alias("t"),
    (F.col("s.decision_id") == F.col("t.decision_id"))
    & (F.col("s.evidence_ordinal") == F.col("t.evidence_ordinal")),
    "left_anti",
).count()

snapshot_fingerprint = _portable_snapshot_fingerprint(decision_frame, evidence_frame)
row_count = premium_frame.count()
evidence_count = evidence_frame.count()
coverage_counts = {
    row.coverage_status: row["count"]
    for row in premium_frame.groupBy("coverage_status").count().collect()
}
ready_count = coverage_counts.get("READY", 0)
partial_count = coverage_counts.get("PARTIAL", 0)
withheld_count = coverage_counts.get("WITHHELD", 0)

manifest_schema = StructType([
    StructField("generation", StringType(), False),
    StructField("as_of_date", DateType(), False),
    StructField("input_snapshot_hash", StringType(), False),
    StructField("status", StringType(), False),
    StructField("row_count", LongType(), False),
    StructField("evidence_count", LongType(), False),
    StructField("ready_count", LongType(), False),
    StructField("partial_count", LongType(), False),
    StructField("withheld_count", LongType(), False),
    StructField("fingerprint", StringType(), False),
    StructField("created_at", TimestampType(), False),
    StructField("completed_at", TimestampType()),
])


def _manifest_frame(status: str, completed_at):
    return spark.createDataFrame([(
        generation,
        parsed_as_of_date,
        input_snapshot_hash,
        status,
        row_count,
        evidence_count,
        ready_count,
        partial_count,
        withheld_count,
        snapshot_fingerprint,
        run_started_at,
        completed_at,
    )], manifest_schema)


existing_manifest_conflicts = (
    spark.table("narrative_premium_snapshot_manifest").alias("t")
    .join(
        _manifest_frame("running", None).alias("s"),
        (F.col("t.generation") == F.col("s.generation"))
        & (F.col("t.as_of_date") == F.col("s.as_of_date")),
        "inner",
    )
    .filter(
        ~F.col("t.input_snapshot_hash").eqNullSafe(F.col("s.input_snapshot_hash"))
        | ~F.col("t.row_count").eqNullSafe(F.col("s.row_count"))
        | ~F.col("t.evidence_count").eqNullSafe(F.col("s.evidence_count"))
        | ~F.col("t.ready_count").eqNullSafe(F.col("s.ready_count"))
        | ~F.col("t.partial_count").eqNullSafe(F.col("s.partial_count"))
        | ~F.col("t.withheld_count").eqNullSafe(F.col("s.withheld_count"))
        | ~F.col("t.fingerprint").eqNullSafe(F.col("s.fingerprint"))
    )
    .count()
)
if existing_manifest_conflicts:
    raise RuntimeError("E22 manifest replay conflict")

manifest_target = DeltaTable.forName(spark, "narrative_premium_snapshot_manifest")
(
    manifest_target.alias("t")
    .merge(
        _manifest_frame("running", None).alias("s"),
        "t.generation = s.generation AND t.as_of_date = s.as_of_date",
    )
    .whenMatchedUpdate(
        condition="t.status <> 'completed'",
        set={
            "input_snapshot_hash": "s.input_snapshot_hash",
            "status": "s.status",
            "row_count": "s.row_count",
            "evidence_count": "s.evidence_count",
            "ready_count": "s.ready_count",
            "partial_count": "s.partial_count",
            "withheld_count": "s.withheld_count",
            "fingerprint": "s.fingerprint",
            "created_at": "s.created_at",
            "completed_at": "s.completed_at",
        },
    )
    .whenNotMatchedInsertAll()
    .execute()
)

(
    DeltaTable.forName(spark, "fact_narrative_premium").alias("t")
    .merge(premium_frame.alias("s"), "t.decision_id = s.decision_id")
    .whenNotMatchedInsertAll()
    .execute()
)
(
    DeltaTable.forName(spark, "decision_log").alias("t")
    .merge(decision_frame.alias("s"), "t.decision_id = s.decision_id")
    .whenNotMatchedInsertAll()
    .execute()
)
(
    DeltaTable.forName(spark, "fact_narrative_premium_evidence").alias("t")
    .merge(
        evidence_frame.alias("s"),
        "t.decision_id = s.decision_id AND t.evidence_ordinal = s.evidence_ordinal",
    )
    .whenNotMatchedInsertAll()
    .execute()
)

target_facts = spark.table("fact_narrative_premium").filter(
    (F.col("generation") == F.lit(generation))
    & (F.col("date_sk") == F.lit(date_sk))
    & (F.col("model_version") == F.lit(MODEL_VERSION))
)
target_decisions = spark.table("decision_log").filter(
    (F.col("input_snapshot_hash") == F.lit(input_snapshot_hash))
    & (F.col("date_sk") == F.lit(date_sk))
    & (F.col("model_version") == F.lit(MODEL_VERSION))
    & (F.col("decision_type") == F.lit("NARRATIVE_PREMIUM"))
)
target_evidence = spark.table("fact_narrative_premium_evidence").filter(
    (F.col("input_snapshot_hash") == F.lit(input_snapshot_hash))
    & (F.col("model_version") == F.lit(MODEL_VERSION))
)
duplicate_fact_decisions = target_facts.groupBy("decision_id").count().filter(F.col("count") > 1).count()
duplicate_log_decisions = target_decisions.groupBy("decision_id").count().filter(F.col("count") > 1).count()
duplicate_evidence_grains = target_evidence.groupBy(
    "decision_id", "evidence_ordinal"
).count().filter(F.col("count") > 1).count()
pit_violations = target_facts.filter(
    F.col("event_date").isNull()
    | F.col("knowledge_date").isNull()
    | (F.col("event_date") > F.col("knowledge_date"))
    | (F.col("knowledge_date") > F.lit(parsed_as_of_date))
).count()
version_violations = target_facts.filter(
    (F.col("model_version") != F.lit(MODEL_VERSION))
    | (F.col("e21_model_version") != F.lit(E21_MODEL_VERSION))
    | (F.col("prompt_version") != F.lit(E21_PROMPT_VERSION))
    | (F.col("input_snapshot_hash") != F.lit(input_snapshot_hash))
    | (F.length("decision_id") != 64)
    | (F.length("input_snapshot_hash") != 64)
    | F.col("fit_context_hash").isNull()
    | (F.length("fit_context_hash") != 64)
).count()
evidence_pit_version_violations = target_evidence.filter(
    F.col("decision_id").isNull()
    | F.col("document_id").isNull()
    | (F.col("evidence_ordinal") < 0)
    | (F.col("input_snapshot_hash") != F.lit(input_snapshot_hash))
    | (F.col("model_version") != F.lit(MODEL_VERSION))
    | (F.col("event_date") > F.col("knowledge_date"))
    | (F.col("knowledge_date") > F.lit(parsed_as_of_date))
).count()
coverage_violations = target_facts.filter(
    ~F.col("coverage_status").isin("READY", "PARTIAL", "WITHHELD")
    | F.col("coverage_reasons_json").isNull()
    | F.col("evidence_pack_json").isNull()
    | (
        (F.col("coverage_status") == "WITHHELD")
        & (
            F.col("narrative_intensity_z").isNotNull()
            | F.col("attribution_intercept").isNotNull()
            | F.col("attribution_beta").isNotNull()
            | F.col("attribution_r2").isNotNull()
            | F.col("narrative_premium").isNotNull()
            | F.col("unexplained_residual").isNotNull()
            | F.col("anchor_support_z").isNotNull()
            | F.col("divergence_state").isNotNull()
            | F.col("is_converging").isNotNull()
        )
    )
    | (
        F.col("coverage_status").isin("READY", "PARTIAL")
        & (
            F.col("fundamental_anchor_z").isNull()
            | F.col("narrative_intensity_z").isNull()
            | F.col("attribution_intercept").isNull()
            | F.col("attribution_beta").isNull()
            | F.col("attribution_r2").isNull()
            | F.col("narrative_premium").isNull()
            | F.col("unexplained_residual").isNull()
            | F.col("anchor_support_z").isNull()
            | F.col("divergence_state").isNull()
            | (F.col("eligible_security_count") < F.lit(8))
        )
    )
).count()
reconciliation_violations = target_facts.filter(
    F.col("narrative_premium").isNotNull()
    & (
        F.abs(
            F.col("fundamental_anchor_z")
            - F.col("attribution_intercept")
            - F.col("narrative_premium")
            - F.col("unexplained_residual")
        ) > F.lit(1e-8)
    )
).count()
anchor_support_violations = target_facts.filter(
    F.col("anchor_support_z").isNotNull()
    & (F.abs(F.col("anchor_support_z") + F.col("fundamental_anchor_z")) > F.lit(1e-10))
).count()
decision_log_missing = target_facts.alias("f").join(
    target_decisions.alias("d"), "decision_id", "left_anti"
).count()
decision_top_level_mismatches = (
    target_facts.alias("f")
    .join(target_decisions.alias("d"), "decision_id", "inner")
    .filter(
        ~F.col("f.security_sk").eqNullSafe(F.col("d.security_sk"))
        | ~F.col("f.date_sk").eqNullSafe(F.col("d.date_sk"))
        | ~F.col("f.coverage_status").eqNullSafe(F.col("d.output_status"))
        | ~F.col("f.input_snapshot_hash").eqNullSafe(F.col("d.input_snapshot_hash"))
        | ~F.col("f.model_version").eqNullSafe(F.col("d.model_version"))
        | ~F.col("f.evidence_pack_json").eqNullSafe(F.col("d.evidence_pack_json"))
        | ~F.col("f.event_date").eqNullSafe(F.col("d.event_date"))
        | ~F.col("f.knowledge_date").eqNullSafe(F.col("d.knowledge_date"))
    )
    .count()
)
evidence_orphans = target_evidence.alias("e").join(
    target_facts.select("decision_id").alias("f"), "decision_id", "left_anti"
).count()
empty_evidence_hash = hashlib.sha256(b"[]").hexdigest()
evidence_summary = target_evidence.groupBy("decision_id").agg(
    F.count("document_id").alias("stored_evidence_count"),
    F.sha2(F.to_json(F.sort_array(F.collect_list("document_id"))), 256).alias("stored_evidence_hash"),
)
evidence_contract_violations = (
    target_facts.alias("f")
    .join(evidence_summary.alias("e"), "decision_id", "left")
    .withColumn(
        "pack_evidence_count",
        F.get_json_object("f.evidence_pack_json", "$.narrative.evidence_document_count").cast(LongType()),
    )
    .withColumn(
        "pack_evidence_hash",
        F.get_json_object("f.evidence_pack_json", "$.narrative.evidence_document_hash"),
    )
    .filter(
        F.col("pack_evidence_count").isNull()
        | F.col("pack_evidence_hash").isNull()
        | (F.col("pack_evidence_count") != F.coalesce(F.col("e.stored_evidence_count"), F.lit(0)))
        | (
            F.col("pack_evidence_hash")
            != F.coalesce(F.col("e.stored_evidence_hash"), F.lit(empty_evidence_hash))
        )
    )
    .count()
)
payload_size_violations = target_decisions.filter(
    (F.length("output_json") > 8000)
    | (F.length("evidence_pack_json") > 8000)
).count()
fact_count = target_facts.count()
decision_count = target_decisions.count()
stored_evidence_count = target_evidence.count()
validation_counts = {
    "duplicate_fact_decisions": duplicate_fact_decisions,
    "duplicate_log_decisions": duplicate_log_decisions,
    "duplicate_evidence_grains": duplicate_evidence_grains,
    "pit_violations": pit_violations,
    "version_violations": version_violations,
    "evidence_pit_version_violations": evidence_pit_version_violations,
    "coverage_violations": coverage_violations,
    "reconciliation_violations": reconciliation_violations,
    "anchor_support_violations": anchor_support_violations,
    "decision_log_missing": decision_log_missing,
    "decision_top_level_mismatches": decision_top_level_mismatches,
    "evidence_orphans": evidence_orphans,
    "evidence_contract_violations": evidence_contract_violations,
    "payload_size_violations": payload_size_violations,
    "fact_count_mismatch": int(fact_count != row_count),
    "decision_count_mismatch": int(decision_count != row_count),
    "evidence_count_mismatch": int(stored_evidence_count != evidence_count),
}
if any(validation_counts.values()):
    raise RuntimeError(f"E22 post-write validation failed: {_canonical_json(validation_counts)}")

completed_at = datetime.now(timezone.utc)
latest_e21_identity = (
    spark.table("narrative_snapshot_manifest")
    .filter(
        (F.col("status") == F.lit("completed"))
        & (F.col("as_of_date") == F.lit(parsed_as_of_date))
    )
    .orderBy(F.col("completed_at").desc(), F.col("generation").desc())
    .limit(1)
    .first()
)
latest_e20_identity = (
    spark.table("fundamental_anchor_snapshot_manifest")
    .filter(
        (F.col("status") == F.lit("completed"))
        & (F.col("as_of_date") == F.lit(parsed_as_of_date))
        & (F.col("model_version") == F.lit(E20_MODEL_VERSION))
    )
    .orderBy(F.col("completed_at").desc(), F.col("generation").desc())
    .limit(1)
    .first()
)
validation_counts["upstream_manifest_changed"] = int(
    latest_e21_identity is None
    or latest_e21_identity.generation != e21_generation
    or latest_e21_identity.fingerprint != e21_manifest_fingerprint
    or latest_e20_identity is None
    or latest_e20_identity.generation != e20_generation
    or latest_e20_identity.fingerprint != e20_manifest_fingerprint
)
if validation_counts["upstream_manifest_changed"]:
    raise RuntimeError(f"E22 upstream manifest changed during execution: {_canonical_json(validation_counts)}")

(
    manifest_target.alias("t")
    .merge(
        _manifest_frame("completed", completed_at).alias("s"),
        "t.generation = s.generation AND t.as_of_date = s.as_of_date",
    )
    .whenMatchedUpdate(
        condition="t.status <> 'completed'",
        set={
            "status": "s.status",
            "completed_at": "s.completed_at",
        },
    )
    .whenNotMatchedInsertAll()
    .execute()
)

run_summary = {
    "generation": generation,
    "as_of_date": as_of_date,
    "input_snapshot_hash": input_snapshot_hash,
    "fingerprint": snapshot_fingerprint,
    "row_count": row_count,
    "coverage_status_counts": coverage_counts,
    "fact_insert_count": fact_insert_count,
    "decision_log_insert_count": decision_log_insert_count,
    "evidence_count": evidence_count,
    "evidence_insert_count": evidence_insert_count,
    "status": "completed",
    "validation": validation_counts,
}
run_summary_json = _canonical_json(run_summary)
print(run_summary_json)
mssparkutils.notebook.exit(run_summary_json)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }