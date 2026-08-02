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

# Fabric Notebook: nb_09_fundamental_anchor
# Materializes the E20 PIT-safe fair-multiple anchor before nb_04_metrics.

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

import importlib.util
import hashlib
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from delta.tables import DeltaTable
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql.types import (
    BooleanType, DateType, DoubleType, IntegerType, LongType, StringType,
    StructField, StructType, TimestampType,
)

MIN_PEERS = 8
MIN_RESIDUAL_DF = 5
MODEL_VERSION = "e20_v2"
ENGINE_LAKEHOUSE_PATH = "Files/config/e20/84641443bde957496881c8cce27b4c8a0dda7f2b5b94eca79b4fdd6213a9a14b.py"
ENGINE_SHA256 = "84641443bde957496881c8cce27b4c8a0dda7f2b5b94eca79b4fdd6213a9a14b"

engine_source = mssparkutils.fs.head(ENGINE_LAKEHOUSE_PATH, 1024 * 1024)
engine_bytes = engine_source.encode("utf-8")
if hashlib.sha256(engine_bytes).hexdigest() != ENGINE_SHA256:
    raise RuntimeError("E20 engine resource hash mismatch")
engine_path = os.path.join(tempfile.gettempdir(), "fundamental_anchor_e20_v2.py")
with open(engine_path, "wb") as engine_file:
    engine_file.write(engine_bytes)
engine_spec = importlib.util.spec_from_file_location("fundamental_anchor", engine_path)
if engine_spec is None or engine_spec.loader is None:
    raise RuntimeError(f"Could not load E20 engine resource: {engine_path}")
engine_module = importlib.util.module_from_spec(engine_spec)
sys.modules[engine_spec.name] = engine_module
engine_spec.loader.exec_module(engine_module)
os.remove(engine_path)
AnchorObservation = engine_module.AnchorObservation
build_anchors = engine_module.build_anchors

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# PARAMETERS CELL ********************

# --- Parameters: mark this cell as the Fabric parameter cell ---
from_date = ""
to_date = ""
max_anchor_dates = 7

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Normalize and validate injected parameter values ---
from_date = str(from_date).strip()
to_date = str(to_date).strip()
max_anchor_dates = int(max_anchor_dates)
if from_date and to_date and date.fromisoformat(from_date) > date.fromisoformat(to_date):
    raise ValueError("from_date must be on or before to_date")
if max_anchor_dates < 1 or max_anchor_dates > 366:
    raise ValueError("max_anchor_dates must be between 1 and 366")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

def _require_table(table_name: str) -> None:
    if not spark.catalog.tableExists(table_name):
        raise RuntimeError(f"Required E20 table is missing: {table_name}")


for required_table in ["dim_security", "dim_source", "fact_market_daily", "fact_fundamentals"]:
    _require_table(required_table)

spark.sql("""
    MERGE INTO dim_source AS t
    USING (
        SELECT 11 AS source_sk, 'e20_fundamental_anchor' AS source_id,
               'derived_model' AS source_type, 'daily' AS latency_class,
               CAST(1.00 AS DECIMAL(3,2)) AS reliability_weight,
               'derived' AS source_class
    ) AS s
    ON t.source_sk = s.source_sk
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_fundamental_anchor (
        security_sk BIGINT NOT NULL,
        date_sk INT NOT NULL,
        ev_sales DECIMAL(18,6),
        ev_ebitda DECIMAL(18,6),
        p_fcf DECIMAL(18,6),
        expected_ev_sales DECIMAL(18,6),
        residual_evs DECIMAL(12,8),
        residual_evebitda DECIMAL(12,8),
        residual_pfcf DECIMAL(12,8),
        anchor_residual DECIMAL(12,8),
        fundamental_anchor_z DECIMAL(12,8),
        anchor_method STRING NOT NULL,
        n_peers INT NOT NULL,
        r2_sector DECIMAL(9,6),
        uses_forward BOOLEAN NOT NULL,
        imputed_flags STRING,
        model_version STRING NOT NULL,
        source_sk INT,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL
    ) USING DELTA
""")
spark.sql("""
    CREATE TABLE IF NOT EXISTS fundamental_anchor_snapshot_manifest (
        generation STRING NOT NULL,
        as_of_date DATE NOT NULL,
        model_version STRING NOT NULL,
        status STRING NOT NULL,
        row_count BIGINT NOT NULL,
        fingerprint STRING NOT NULL,
        created_at TIMESTAMP NOT NULL,
        completed_at TIMESTAMP
    ) USING DELTA
""")
DeltaTable.forName(spark, "fact_fundamental_anchor").delete()
DeltaTable.forName(spark, "fundamental_anchor_snapshot_manifest").delete()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

market_revisions = (
    spark.table("fact_market_daily")
    .filter(
        F.col("security_sk").isNotNull()
        & F.col("event_date").isNotNull()
        & F.col("knowledge_date").isNotNull()
        & (F.col("event_date") <= F.col("knowledge_date"))
        & (F.col("knowledge_date") <= F.current_date())
    )
)
requested_anchor_dates = (
    spark.range(1)
    .select(
        F.explode(
            F.sequence(
                F.to_date(F.lit(from_date)),
                F.to_date(F.lit(to_date)),
            )
        ).alias("as_of")
    )
    if from_date and to_date
    else spark.range(0).select(F.lit(None).cast(DateType()).alias("as_of"))
)
anchor_dates = (
    market_revisions.select(F.col("event_date").alias("as_of"))
    .unionByName(requested_anchor_dates)
    .distinct()
    .filter(
        (F.lit(not from_date) | (F.col("as_of") >= F.to_date(F.lit(from_date))))
        & (F.lit(not to_date) | (F.col("as_of") <= F.to_date(F.lit(to_date))))
    )
    .orderBy(F.col("as_of").desc())
    .limit(max_anchor_dates)
)
market_candidates = anchor_dates.alias("d").join(
    market_revisions.alias("p"),
    (F.col("p.event_date") <= F.col("d.as_of"))
    & (F.col("p.knowledge_date") <= F.col("d.as_of")),
    "inner",
)
market_window = Window.partitionBy(
    F.col("d.as_of"), F.col("p.security_sk"),
).orderBy(
    F.col("p.event_date").desc(),
    F.col("p.knowledge_date").desc(),
    F.col("p.revision_loaded_at").desc_nulls_last(),
    F.col("p.price_revision_hash").desc(),
)
dates = (
    market_candidates.withColumn("market_row_number", F.row_number().over(market_window))
    .filter(F.col("market_row_number") == 1)
    .select(
        F.col("p.security_sk").alias("security_sk"),
        F.date_format(F.col("d.as_of"), "yyyyMMdd").cast(IntegerType()).alias("date_sk"),
        F.col("d.as_of").alias("as_of"),
        F.col("p.close").cast(DoubleType()).alias("close"),
        F.col("p.event_date").alias("price_event_date"),
        F.col("p.knowledge_date").alias("price_knowledge_date"),
    )
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

fundamentals = spark.table("fact_fundamentals").alias("f")
overview_window = Window.partitionBy(
    F.col("d.security_sk"), F.col("d.date_sk"),
).orderBy(
    F.col("f.knowledge_date").desc_nulls_last(),
    F.col("f.event_date").desc_nulls_last(),
    F.col("f.silver_loaded_at").desc_nulls_last(),
    F.col("f.fundamentals_revision_hash").desc_nulls_last(),
)
overview_snapshot = (
    dates.alias("d")
    .join(
        fundamentals,
        (F.col("f.security_sk") == F.col("d.security_sk"))
        & (F.col("f.knowledge_date") <= F.col("d.as_of"))
        & (F.col("f.event_date") <= F.col("d.as_of"))
        & (F.col("f.fundamentals_kind") == "OVERVIEW_SNAPSHOT")
        & (F.col("f.market_cap") > 0)
        & (F.col("f.ps_ratio") > 0),
        "left",
    )
    .withColumn("overview_row_number", F.row_number().over(overview_window))
    .filter(F.col("overview_row_number") == 1)
    .select(
        F.col("d.security_sk").alias("security_sk"),
        F.col("d.date_sk").alias("date_sk"),
        F.col("f.market_cap").alias("market_cap"),
        F.col("f.currency").alias("overview_currency"),
        F.col("f.sector").alias("sector"),
        F.col("f.shares_outstanding").alias("shares_outstanding"),
        F.col("f.ebitda").alias("ebitda"),
        F.col("f.ps_ratio").alias("ps_ratio"),
        F.col("f.gross_profit_ttm").alias("gross_profit_ttm"),
        F.col("f.profit_margin").alias("profit_margin"),
        F.col("f.rev_growth_yoy").alias("rev_growth_yoy"),
        F.col("f.event_date").alias("overview_event_date"),
        F.col("f.knowledge_date").alias("overview_knowledge_date"),
    )
)

statement_revision_window = Window.partitionBy(
    F.col("d.security_sk"), F.col("d.date_sk"), F.col("f.event_date"),
).orderBy(
    F.col("f.knowledge_date").desc_nulls_last(),
    F.col("f.silver_loaded_at").desc_nulls_last(),
    F.col("f.fundamentals_revision_hash").desc_nulls_last(),
)
statement_revisions = (
    dates.alias("d")
    .join(
        fundamentals,
        (F.col("f.security_sk") == F.col("d.security_sk"))
        & (F.col("f.knowledge_date") <= F.col("d.as_of"))
        & (F.col("f.event_date") <= F.col("d.as_of"))
        & (F.col("f.fundamentals_kind") == "STATEMENT"),
        "left",
    )
    .withColumn("statement_revision_row_number", F.row_number().over(statement_revision_window))
    .filter(F.col("statement_revision_row_number") == 1)
    .select(
        F.col("d.security_sk").alias("security_sk"),
        F.col("d.date_sk").alias("date_sk"),
        F.col("f.currency").alias("statement_currency"),
        F.col("f.cash_and_equivalents").alias("cash_and_equivalents"),
        F.col("f.total_debt").alias("total_debt"),
        F.col("f.operating_cashflow").alias("operating_cashflow"),
        F.col("f.capital_expenditures").alias("capital_expenditures"),
        F.col("f.event_date").alias("statement_event_date"),
        F.col("f.knowledge_date").alias("statement_knowledge_date"),
    )
)
statement_quarter_window = Window.partitionBy(
    "security_sk", "date_sk",
).orderBy(
    F.col("statement_event_date").desc_nulls_last(),
    F.col("statement_knowledge_date").desc_nulls_last(),
)
statement_quarters = statement_revisions.withColumn(
    "statement_quarter_number", F.row_number().over(statement_quarter_window),
)
latest_balance_snapshot = (
    statement_quarters.filter(F.col("statement_quarter_number") == 1)
    .select(
        "security_sk", "date_sk", "statement_currency",
        "cash_and_equivalents", "total_debt",
        F.col("statement_event_date").alias("balance_event_date"),
        F.col("statement_knowledge_date").alias("balance_knowledge_date"),
    )
)
ttm_cashflow = (
    statement_quarters.filter(F.col("statement_quarter_number") <= 4)
    .groupBy("security_sk", "date_sk")
    .agg(
        F.countDistinct("statement_event_date").alias("ttm_quarters"),
        F.countDistinct("statement_currency").alias("ttm_currency_count"),
        F.first("statement_currency", ignorenulls=True).alias("ttm_currency"),
        F.count("operating_cashflow").alias("ttm_operating_cashflow_quarters"),
        F.count("capital_expenditures").alias("ttm_capex_quarters"),
        F.sum("operating_cashflow").alias("ttm_operating_cashflow_raw"),
        F.sum(F.abs(F.col("capital_expenditures"))).alias("ttm_capex_outflow_raw"),
        F.max("statement_event_date").alias("ttm_event_date"),
        F.max("statement_knowledge_date").alias("ttm_knowledge_date"),
    )
    .withColumn(
        "ttm_operating_cashflow",
        F.when(
            (F.col("ttm_quarters") == 4)
            & (F.col("ttm_currency_count") == 1)
            & (F.col("ttm_operating_cashflow_quarters") == 4),
            F.col("ttm_operating_cashflow_raw"),
        ),
    )
    .withColumn(
        "ttm_capex_outflow",
        F.when(
            (F.col("ttm_quarters") == 4)
            & (F.col("ttm_currency_count") == 1)
            & (F.col("ttm_capex_quarters") == 4),
            F.col("ttm_capex_outflow_raw"),
        ),
    )
)
statement_snapshot = latest_balance_snapshot.join(
    ttm_cashflow, ["security_sk", "date_sk"], "left",
)

panel = (
    dates
    .join(overview_snapshot, ["security_sk", "date_sk"], "left")
    .join(statement_snapshot, ["security_sk", "date_sk"], "left")
)
event_columns = [
    F.col("price_event_date"), F.col("overview_event_date"),
    F.col("balance_event_date"), F.col("ttm_event_date"),
]
knowledge_columns = [
    F.col("price_knowledge_date"), F.col("overview_knowledge_date"),
    F.col("balance_knowledge_date"), F.col("ttm_knowledge_date"),
]
security_sector_candidates = (
    dates.alias("d")
    .join(
        spark.table("dim_security").alias("s"),
        (F.col("s.security_sk") == F.col("d.security_sk"))
        & (F.col("s.valid_from") <= F.col("d.as_of"))
        & (F.col("s.valid_to").isNull() | (F.col("d.as_of") < F.col("s.valid_to"))),
        "left",
    )
    .select(
        F.col("d.security_sk").alias("security_sk"),
        F.col("d.date_sk").alias("date_sk"),
        F.col("s.gics_sector").alias("dimension_sector"),
        F.col("s.currency").alias("dimension_currency"),
        F.col("s.valid_from").alias("sector_valid_from"),
        F.col("s.updated_at").alias("sector_updated_at"),
    )
)
sector_overlap_count = security_sector_candidates.groupBy(
    "security_sk", "date_sk",
).count().filter(F.col("count") > 1).count()
security_sector_window = Window.partitionBy("security_sk", "date_sk").orderBy(
    F.col("sector_valid_from").desc_nulls_last(),
    F.col("sector_updated_at").desc_nulls_last(),
)
security_sectors = (
    security_sector_candidates
    .withColumn("sector_row_number", F.row_number().over(security_sector_window))
    .filter(F.col("sector_row_number") == 1)
    .select("security_sk", "date_sk", "dimension_sector", "dimension_currency")
)
panel = (
    panel.join(security_sectors, ["security_sk", "date_sk"], "left")
    .withColumn("sector", F.coalesce(F.col("sector"), F.col("dimension_sector"), F.lit("Unknown")))
    .withColumn(
        "currency_coherent",
        F.col("overview_currency").isNotNull()
        & F.col("statement_currency").isNotNull()
        & F.col("dimension_currency").isNotNull()
        & (F.col("overview_currency") == F.col("statement_currency"))
        & (
            F.col("ttm_currency").isNull()
            | (F.col("overview_currency") == F.col("ttm_currency"))
        )
        & (F.col("overview_currency") == F.col("dimension_currency")),
    )
    .withColumn(
        "current_market_cap",
        F.when(
            F.col("currency_coherent")
            & (F.col("close") > 0) & (F.col("shares_outstanding") > 0),
            F.col("close") * F.col("shares_outstanding"),
        ).when(
            F.col("currency_coherent") & (F.col("market_cap") > 0),
            F.col("market_cap").cast(DoubleType()),
        ),
    )
    .withColumn(
        "market_cap_snapshot_fallback",
        F.col("currency_coherent")
        & ~((F.col("close") > 0) & (F.col("shares_outstanding") > 0)),
    )
    .withColumn(
        "enterprise_value",
        F.when(
            (F.col("current_market_cap") > 0)
            & F.col("total_debt").isNotNull()
            & F.col("cash_and_equivalents").isNotNull(),
            F.col("current_market_cap")
            + F.col("total_debt").cast(DoubleType())
            - F.col("cash_and_equivalents").cast(DoubleType()),
        ),
    )
    .withColumn(
        "revenue_ttm",
        F.when(
            (F.col("market_cap") > 0) & (F.col("ps_ratio") > 0),
            F.col("market_cap").cast(DoubleType()) / F.col("ps_ratio").cast(DoubleType()),
        ),
    )
    .withColumn(
        "free_cash_flow_ttm",
        F.when(
            F.col("ttm_operating_cashflow").isNotNull()
            & F.col("ttm_capex_outflow").isNotNull(),
            F.col("ttm_operating_cashflow") - F.col("ttm_capex_outflow"),
        ),
    )
    .withColumn("ev_sales", F.when(F.col("revenue_ttm") > 0, F.col("enterprise_value") / F.col("revenue_ttm")))
    .withColumn("derived_ev_ebitda", F.when(F.col("ebitda") > 0, F.col("enterprise_value") / F.col("ebitda")))
    .withColumn("p_fcf", F.when(F.col("free_cash_flow_ttm") > 0, F.col("current_market_cap") / F.col("free_cash_flow_ttm")))
    .withColumn("gross_margin", F.when(F.col("revenue_ttm") > 0, F.col("gross_profit_ttm") / F.col("revenue_ttm")))
    .withColumn("derived_fcf_yield", F.when(F.col("current_market_cap") > 0, F.col("free_cash_flow_ttm") / F.col("current_market_cap")))
    .withColumn("derived_leverage", F.when(F.col("ebitda") > 0, (F.col("total_debt") - F.col("cash_and_equivalents")) / F.col("ebitda")))
    .withColumn("cash_burn_flag", F.when(F.col("free_cash_flow_ttm").isNotNull(), F.col("free_cash_flow_ttm") < 0))
    .withColumn("anchor_event_date", F.greatest(*event_columns))
    .withColumn("anchor_knowledge_date", F.greatest(*knowledge_columns))
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

model_panel = panel.filter(F.col("overview_knowledge_date").isNotNull())
observations = [
    AnchorObservation(
        security_sk=int(row.security_sk),
        as_of=row.as_of,
        sector=row.sector,
        ev_sales=float(row.ev_sales) if row.ev_sales is not None else None,
        ev_ebitda=float(row.derived_ev_ebitda) if row.derived_ev_ebitda is not None else None,
        p_fcf=float(row.p_fcf) if row.p_fcf is not None else None,
        rev_growth_yoy=float(row.rev_growth_yoy) if row.rev_growth_yoy is not None else None,
        gross_margin=float(row.gross_margin) if row.gross_margin is not None else None,
        profit_margin=float(row.profit_margin) if row.profit_margin is not None else None,
        net_debt_to_ebitda=float(row.derived_leverage) if row.derived_leverage is not None else None,
        fcf_yield=float(row.derived_fcf_yield) if row.derived_fcf_yield is not None else None,
        cash_burn_flag=bool(row.cash_burn_flag) if row.cash_burn_flag is not None else None,
        event_date=row.anchor_event_date or row.as_of,
        knowledge_date=row.anchor_knowledge_date or row.as_of,
    )
    for row in model_panel.select(
        "security_sk", "as_of", "sector", "ev_sales", "derived_ev_ebitda", "p_fcf",
        "rev_growth_yoy", "gross_margin", "profit_margin", "derived_leverage",
        "derived_fcf_yield", "cash_burn_flag", "anchor_event_date", "anchor_knowledge_date",
    ).collect()
]
results = build_anchors(observations)
panel_flags = {
    (int(row.security_sk), row.as_of): list(filter(None, [
        "market_cap_snapshot_fallback" if row.market_cap_snapshot_fallback else None,
        "missing_total_debt" if row.total_debt is None else None,
        "missing_cash_equivalents" if row.cash_and_equivalents is None else None,
        "ttm_fcf_unavailable" if row.free_cash_flow_ttm is None else None,
        "currency_mismatch" if not row.currency_coherent else None,
    ]))
    for row in model_panel.select(
        "security_sk", "as_of", "market_cap_snapshot_fallback", "total_debt",
        "cash_and_equivalents", "free_cash_flow_ttm", "currency_coherent",
    ).collect()
}

result_schema = StructType([
    StructField("security_sk", LongType(), False), StructField("as_of", DateType(), False),
    StructField("ev_sales", DoubleType()), StructField("ev_ebitda", DoubleType()),
    StructField("p_fcf", DoubleType()), StructField("expected_ev_sales", DoubleType()),
    StructField("residual_evs", DoubleType()), StructField("residual_evebitda", DoubleType()),
    StructField("residual_pfcf", DoubleType()), StructField("anchor_residual", DoubleType()),
    StructField("fundamental_anchor_z", DoubleType()), StructField("anchor_method", StringType(), False),
    StructField("n_peers", IntegerType(), False), StructField("r2_sector", DoubleType()),
    StructField("uses_forward", BooleanType(), False), StructField("imputed_flags", StringType()),
    StructField("model_version", StringType(), False), StructField("event_date", DateType(), False),
    StructField("knowledge_date", DateType(), False),
])
result_rows = [(
    result.security_sk, result.as_of, result.ev_sales, result.ev_ebitda, result.p_fcf,
    result.expected_ev_sales, result.residual_evs, result.residual_evebitda,
    result.residual_pfcf, result.anchor_residual, result.fundamental_anchor_z,
    result.anchor_method, result.n_peers, result.r2_sector, result.uses_forward,
    ",".join(filter(None, [
        result.imputed_flags,
        *panel_flags.get((result.security_sk, result.as_of), []),
    ])),
    result.model_version, result.event_date, result.knowledge_date,
) for result in results]

anchors = (
    spark.createDataFrame(result_rows, result_schema)
    .withColumn("date_sk", F.date_format("as_of", "yyyyMMdd").cast(IntegerType()))
    .withColumn("source_sk", F.lit(11).cast(IntegerType()))
    .withColumn("ev_sales", F.col("ev_sales").cast("decimal(18,6)"))
    .withColumn("ev_ebitda", F.col("ev_ebitda").cast("decimal(18,6)"))
    .withColumn("p_fcf", F.col("p_fcf").cast("decimal(18,6)"))
    .withColumn("expected_ev_sales", F.col("expected_ev_sales").cast("decimal(18,6)"))
    .withColumn("residual_evs", F.col("residual_evs").cast("decimal(12,8)"))
    .withColumn("residual_evebitda", F.col("residual_evebitda").cast("decimal(12,8)"))
    .withColumn("residual_pfcf", F.col("residual_pfcf").cast("decimal(12,8)"))
    .withColumn("anchor_residual", F.col("anchor_residual").cast("decimal(12,8)"))
    .withColumn("fundamental_anchor_z", F.col("fundamental_anchor_z").cast("decimal(12,8)"))
    .withColumn("r2_sector", F.col("r2_sector").cast("decimal(9,6)"))
    .select(
        "security_sk", "date_sk", "ev_sales", "ev_ebitda", "p_fcf",
        "expected_ev_sales", "residual_evs", "residual_evebitda", "residual_pfcf",
        "anchor_residual", "fundamental_anchor_z", "anchor_method", "n_peers",
        "r2_sector", "uses_forward", "imputed_flags", "model_version", "source_sk",
        "event_date", "knowledge_date",
    )
)

anchor_fingerprint_columns = [
    "security_sk", "date_sk", "ev_sales", "ev_ebitda", "p_fcf",
    "expected_ev_sales", "residual_evs", "residual_evebitda", "residual_pfcf",
    "anchor_residual", "fundamental_anchor_z", "anchor_method", "n_peers",
    "r2_sector", "uses_forward", "imputed_flags", "model_version", "source_sk",
    "event_date", "knowledge_date",
]
anchor_snapshots = (
    anchors
    .withColumn(
        "row_hash",
        F.sha2(F.to_json(F.struct(*[F.col(column) for column in anchor_fingerprint_columns])), 256),
    )
    .groupBy("date_sk")
    .agg(
        F.count(F.lit(1)).alias("row_count"),
        F.sha2(F.concat_ws("|", F.sort_array(F.collect_list("row_hash"))), 256).alias("fingerprint"),
    )
    .withColumn("as_of_date", F.to_date(F.col("date_sk").cast(StringType()), "yyyyMMdd"))
    .withColumn("generation", F.concat(F.lit("e20-"), F.substring("fingerprint", 1, 32)))
    .withColumn("model_version", F.lit(MODEL_VERSION))
)
if anchor_snapshots.isEmpty():
    raise RuntimeError("E20 produced no anchor snapshot rows")
run_started_at = datetime.now(timezone.utc)
manifest_schema = StructType([
    StructField("generation", StringType(), False),
    StructField("as_of_date", DateType(), False),
    StructField("model_version", StringType(), False),
    StructField("status", StringType(), False),
    StructField("row_count", LongType(), False),
    StructField("fingerprint", StringType(), False),
    StructField("created_at", TimestampType(), False),
    StructField("completed_at", TimestampType()),
])


def _manifest_frame(status: str, completed_at):
    return spark.createDataFrame([
        (
            row.generation,
            row.as_of_date,
            MODEL_VERSION,
            status,
            row.row_count,
            row.fingerprint,
            run_started_at,
            completed_at,
        )
        for row in anchor_snapshots.select(
            "generation", "as_of_date", "row_count", "fingerprint"
        ).collect()
    ], manifest_schema)


manifest_target = DeltaTable.forName(spark, "fundamental_anchor_snapshot_manifest")
running_manifests = _manifest_frame("running", None)
existing_manifest_conflicts = (
    spark.table("fundamental_anchor_snapshot_manifest").alias("t")
    .join(
        running_manifests.alias("s"),
        (F.col("t.generation") == F.col("s.generation"))
        & (F.col("t.as_of_date") == F.col("s.as_of_date"))
        & (F.col("t.model_version") == F.col("s.model_version")),
        "inner",
    )
    .filter(
        ~F.col("t.row_count").eqNullSafe(F.col("s.row_count"))
        | ~F.col("t.fingerprint").eqNullSafe(F.col("s.fingerprint"))
    )
    .count()
)
if existing_manifest_conflicts:
    raise RuntimeError("E20 manifest replay conflict")
(
    manifest_target.alias("t")
    .merge(
        running_manifests.alias("s"),
        "t.generation = s.generation AND t.as_of_date = s.as_of_date "
        "AND t.model_version = s.model_version",
    )
    .whenMatchedUpdate(
        condition="t.status <> 'completed'",
        set={
            "status": "s.status",
            "row_count": "s.row_count",
            "fingerprint": "s.fingerprint",
            "created_at": "s.created_at",
            "completed_at": "s.completed_at",
        },
    )
    .whenNotMatchedInsertAll()
    .execute()
)

target = DeltaTable.forName(spark, "fact_fundamental_anchor")
processed_date_sks = [row.date_sk for row in dates.select("date_sk").distinct().collect()]
anchor_merge = (
    target.alias("t")
    .merge(
        anchors.alias("s"),
        "t.security_sk = s.security_sk AND t.date_sk = s.date_sk AND t.model_version = s.model_version",
    )
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
)
if processed_date_sks:
    processed_dates_sql = ",".join(str(value) for value in sorted(processed_date_sks))
    anchor_merge = anchor_merge.whenNotMatchedBySourceDelete(
        f"t.model_version = '{MODEL_VERSION}' AND t.date_sk IN ({processed_dates_sql})"
    )
anchor_merge.execute()

for drop_sql in ("DROP VIEW IF EXISTS v_fundamental_anchor", "DROP TABLE IF EXISTS v_fundamental_anchor"):
    try:
        spark.sql(drop_sql)
    except Exception:
        pass
spark.sql("""
    CREATE TABLE v_fundamental_anchor USING DELTA AS
    WITH ranked AS (
        SELECT a.*,
               ROW_NUMBER() OVER (
                   PARTITION BY a.security_sk
                   ORDER BY a.date_sk DESC, a.knowledge_date DESC
               ) AS anchor_row_number
        FROM fact_fundamental_anchor a
        WHERE a.model_version = 'e20_v2'
    )
    SELECT
        a.security_sk, a.date_sk, a.ev_sales, a.ev_ebitda, a.p_fcf,
        a.expected_ev_sales, a.residual_evs, a.residual_evebitda,
        a.residual_pfcf, a.anchor_residual, a.fundamental_anchor_z,
        a.anchor_method, a.n_peers, a.r2_sector, a.uses_forward,
        a.imputed_flags, a.model_version, a.source_sk, a.event_date,
        a.knowledge_date, s.ticker, s.company_name, s.gics_sector
    FROM ranked a
    INNER JOIN dim_security s ON a.security_sk = s.security_sk
    WHERE a.anchor_row_number = 1 AND s.is_current = true
""")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

duplicate_rows = spark.sql("""
    SELECT COUNT(*) AS n FROM (
        SELECT security_sk, date_sk, model_version
        FROM fact_fundamental_anchor
        GROUP BY security_sk, date_sk, model_version
        HAVING COUNT(*) > 1
    ) duplicates
""").collect()[0].n
future_rows = spark.table("fact_fundamental_anchor").filter(
    (F.col("event_date") > F.col("knowledge_date"))
    | (F.col("knowledge_date") > F.to_date(F.col("date_sk").cast(StringType()), "yyyyMMdd"))
).count()
missing_contract = spark.table("fact_fundamental_anchor").filter(
    F.col("anchor_method").isNull()
    | F.col("model_version").isNull()
    | F.col("uses_forward").isNull()
).count()
if duplicate_rows or future_rows or missing_contract or sector_overlap_count:
    raise RuntimeError(
        "E20 validation failed: "
        f"duplicates={duplicate_rows}, future={future_rows}, missing_contract={missing_contract}, "
        f"sector_overlaps={sector_overlap_count}"
    )

persisted_snapshots = (
    spark.table("fact_fundamental_anchor")
    .filter(F.col("model_version") == F.lit(MODEL_VERSION))
    .join(anchor_snapshots.select("date_sk"), "date_sk", "inner")
    .withColumn(
        "row_hash",
        F.sha2(F.to_json(F.struct(*[F.col(column) for column in anchor_fingerprint_columns])), 256),
    )
    .groupBy("date_sk")
    .agg(
        F.count(F.lit(1)).alias("persisted_row_count"),
        F.sha2(F.concat_ws("|", F.sort_array(F.collect_list("row_hash"))), 256).alias("persisted_fingerprint"),
    )
)
snapshot_mismatches = (
    anchor_snapshots.alias("s")
    .join(persisted_snapshots.alias("p"), "date_sk", "left")
    .filter(
        ~F.col("s.row_count").eqNullSafe(F.col("p.persisted_row_count"))
        | ~F.col("s.fingerprint").eqNullSafe(F.col("p.persisted_fingerprint"))
    )
    .count()
)
if snapshot_mismatches:
    raise RuntimeError(f"E20 persisted snapshot validation failed: {snapshot_mismatches}")

completed_at = datetime.now(timezone.utc)
(
    manifest_target.alias("t")
    .merge(
        _manifest_frame("completed", completed_at).alias("s"),
        "t.generation = s.generation AND t.as_of_date = s.as_of_date "
        "AND t.model_version = s.model_version",
    )
    .whenMatchedUpdate(
        condition="t.status <> 'completed'",
        set={"status": "s.status", "completed_at": "s.completed_at"},
    )
    .whenNotMatchedInsertAll()
    .execute()
)

method_counts = {
    row.anchor_method: row["count"]
    for row in spark.table("fact_fundamental_anchor").groupBy("anchor_method").count().collect()
}
print({
    "model_version": MODEL_VERSION,
    "anchor_rows": spark.table("fact_fundamental_anchor").count(),
    "method_counts": method_counts,
    "min_peers": MIN_PEERS,
    "min_residual_df": MIN_RESIDUAL_DF,
})

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
