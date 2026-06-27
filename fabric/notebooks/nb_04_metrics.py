# Fabric Notebook: nb_04_metrics
# Builds the E6a metric layer and stable security feature contract from E5 gold facts.
# Attaches to: auspex_bronze (default lakehouse)
#
# Current implemented source coverage:
# - fact_market_daily -> momentum, realized risk, risk-adjusted context
# - fact_insider_txn  -> smart-money insider metrics
#
# E6b final Opportunity Score is intentionally not published here; it depends on
# E8 source completion and the E14 valuation-brake leg.

# COMMAND ----------
from datetime import date
from decimal import Decimal
from math import sqrt
from delta.tables import DeltaTable
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql.types import (
    BooleanType, DateType, DecimalType, DoubleType, IntegerType,
    StringType, StructField, StructType, TimestampType,
)

# COMMAND ----------
# --- Helpers ---
_RF_ANNUAL_DEFAULT = 0.02
spark.conf.set("spark.advise.divisionExprConvertRule.enable", "true")


def _require_table(table_name: str) -> None:
    if not spark.catalog.tableExists(table_name):
        raise RuntimeError(f"Required E5 table is missing: {table_name}")


def _ensure_columns(table_name: str, column_specs: dict[str, str]) -> None:
    existing = set(spark.table(table_name).columns)
    for column_name, ddl in column_specs.items():
        if column_name not in existing:
            spark.sql(f"ALTER TABLE {table_name} ADD COLUMNS ({ddl})")


def _merge_all(table_name: str, source_df, condition: str) -> None:
    (
        DeltaTable.forName(spark, table_name)
        .alias("t")
        .merge(source_df.alias("s"), condition)
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )
    print(f"Merged {source_df.count()} rows into {table_name}")


def _replace_delta_projection(table_name: str, select_sql: str) -> None:
    for drop_sql in (f"DROP VIEW IF EXISTS {table_name}", f"DROP TABLE IF EXISTS {table_name}"):
        try:
            spark.sql(drop_sql)
        except Exception:
            pass

    spark.sql(f"CREATE TABLE {table_name} USING DELTA AS {select_sql}")
    print(f"Materialized {table_name}: {spark.table(table_name).count()} rows")


for required in ["dim_security", "dim_date", "fact_market_daily", "fact_insider_txn"]:
    _require_table(required)

# COMMAND ----------
# --- Metric config ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS metric_weights (
        metric_name    STRING        NOT NULL,
        metric_group   STRING        NOT NULL,
        weight         DECIMAL(9,6)  NOT NULL,
        direction      INT           NOT NULL,
        is_active      BOOLEAN       NOT NULL,
        required_epic  STRING,
        version        STRING        NOT NULL,
        effective_from DATE          NOT NULL,
        effective_to   DATE          NOT NULL,
        updated_at     TIMESTAMP
    )
    USING DELTA
""")

_ensure_columns("metric_weights", {
    "required_epic": "required_epic STRING",
    "version": "version STRING",
    "effective_from": "effective_from DATE",
    "effective_to": "effective_to DATE",
    "updated_at": "updated_at TIMESTAMP",
})

weights_schema = StructType([
    StructField("metric_name", StringType(), False),
    StructField("metric_group", StringType(), False),
    StructField("weight", DecimalType(9, 6), False),
    StructField("direction", IntegerType(), False),
    StructField("is_active", BooleanType(), False),
    StructField("required_epic", StringType(), True),
    StructField("version", StringType(), False),
    StructField("effective_from", DateType(), False),
    StructField("effective_to", DateType(), False),
    StructField("updated_at", TimestampType(), True),
])

weights_rows = [
    ("momentum_3m", "composite_growth_score", Decimal("0.250000"), 1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
    ("momentum_6m", "composite_growth_score", Decimal("0.150000"), 1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
    ("momentum_12m", "composite_growth_score", Decimal("0.100000"), 1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
    ("realized_vol_30d", "composite_growth_score", Decimal("0.100000"), -1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
    ("insider_net_buy_ratio_90d", "composite_growth_score", Decimal("0.250000"), 1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
    ("insider_cluster_buy_30d", "composite_growth_score", Decimal("0.150000"), 1, True, "E6a", "e6a_v1", date(1900, 1, 1), date(9999, 12, 31), None),
]
weights_df = spark.createDataFrame(weights_rows, weights_schema).withColumn("updated_at", F.current_timestamp())
_merge_all(
    "metric_weights",
    weights_df,
    "t.metric_name = s.metric_name AND t.version = s.version AND t.effective_from = s.effective_from",
)

weight_sum = (
    spark.table("metric_weights")
    .filter((F.col("metric_group") == "composite_growth_score") & (F.col("is_active") == True) & (F.col("version") == "e6a_v1"))
    .agg(F.round(F.sum("weight"), 6).alias("weight_sum"))
    .collect()[0]
    .weight_sum
)
if weight_sum != Decimal("1.000000"):
    raise RuntimeError(f"metric_weights for composite_growth_score must sum to 1.000000, got {weight_sum}")

# COMMAND ----------
# --- Feature contract table ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS security_daily_features (
        security_sk                   BIGINT       NOT NULL,
        date_sk                       INT          NOT NULL,
        ticker                        STRING,
        company_name                  STRING,
        gics_sector                   STRING,
        country                       STRING,
        as_of                         DATE         NOT NULL,
        close                         DECIMAL(18,6),
        ret_1d                        DECIMAL(12,8),
        momentum_3m                   DOUBLE,
        momentum_6m                   DOUBLE,
        momentum_12m                  DOUBLE,
        rel_strength_sector           DOUBLE,
        realized_vol_30d              DOUBLE,
        realized_vol_90d              DOUBLE,
        realized_vol_252d             DOUBLE,
        downside_deviation_252d       DOUBLE,
        max_drawdown_252d             DOUBLE,
        beta_252d                     DOUBLE,
        illiquidity                   DOUBLE,
        ann_return_252d               DOUBLE,
        sharpe_252d                   DOUBLE,
        sortino_252d                  DOUBLE,
        calmar_252d                   DOUBLE,
        info_ratio_252d               DOUBLE,
        insider_net_buy_ratio_90d     DOUBLE,
        insider_cluster_buy_30d       INT,
        inst_net_flow_qoq             DOUBLE,
        inst_new_initiations          INT,
        activist_13d_flag             BOOLEAN,
        news_sentiment_ewma_14d       DOUBLE,
        news_volume_z_30d             DOUBLE,
        contract_award_usd_trailing_90d DOUBLE,
        fundamental_anchor_z          DOUBLE,
        narrative_intensity           DOUBLE,
        narrative_premium             DOUBLE,
        divergence_state              STRING,
        composite_growth_score        DOUBLE,
        opportunity_score             DOUBLE,
        score_status                  STRING,
        max_knowledge_date            DATE,
        stale_sources_json            STRING
    )
    USING DELTA
""")

_ensure_columns("security_daily_features", {
    "realized_vol_30d": "realized_vol_30d DOUBLE",
    "realized_vol_90d": "realized_vol_90d DOUBLE",
    "score_status": "score_status STRING",
    "max_knowledge_date": "max_knowledge_date DATE",
    "stale_sources_json": "stale_sources_json STRING",
})

# COMMAND ----------
# --- Market metrics from PIT-clean market facts ---
price_window = Window.partitionBy("security_sk").orderBy("price_event_date")
rows_30 = price_window.rowsBetween(-29, 0)
rows_90 = price_window.rowsBetween(-89, 0)
rows_252 = price_window.rowsBetween(-251, 0)
asof_latest_window = Window.partitionBy("security_sk", "date_sk").orderBy(
    F.col("price_event_date").desc(), F.col("market_knowledge_date").desc()
)

market_base = (
    spark.table("fact_market_daily")
    .filter(
        F.col("security_sk").isNotNull()
        & F.col("date_sk").isNotNull()
        & F.col("event_date").isNotNull()
        & F.col("knowledge_date").isNotNull()
    )
    .select(
        "security_sk", F.col("event_date").alias("price_event_date"), "close", "ret_1d", "volume",
        F.col("knowledge_date").alias("market_knowledge_date"),
    )
    .dropDuplicates(["security_sk", "price_event_date"])
    .withColumn("as_of", F.greatest(F.col("price_event_date"), F.col("market_knowledge_date")))
    .withColumn("date_sk", F.date_format("as_of", "yyyyMMdd").cast(IntegerType()))
    .withColumn("close_d", F.col("close").cast(DoubleType()))
    .withColumn("ret_1d_d", F.col("ret_1d").cast(DoubleType()))
    .withColumn("dollar_volume", F.col("close_d") * F.col("volume").cast(DoubleType()))
    .withColumn("close_lag_63", F.lag("close_d", 63).over(price_window))
    .withColumn("close_lag_126", F.lag("close_d", 126).over(price_window))
    .withColumn("close_lag_252", F.lag("close_d", 252).over(price_window))
    .withColumn("momentum_3m", F.when(F.col("close_lag_63") > 0, F.col("close_d") / F.col("close_lag_63") - F.lit(1.0)))
    .withColumn("momentum_6m", F.when(F.col("close_lag_126") > 0, F.col("close_d") / F.col("close_lag_126") - F.lit(1.0)))
    .withColumn("momentum_12m", F.when(F.col("close_lag_252") > 0, F.col("close_d") / F.col("close_lag_252") - F.lit(1.0)))
    .withColumn("realized_vol_30d", F.stddev_samp("ret_1d_d").over(rows_30) * F.lit(sqrt(252.0)))
    .withColumn("realized_vol_90d", F.stddev_samp("ret_1d_d").over(rows_90) * F.lit(sqrt(252.0)))
    .withColumn("realized_vol_252d", F.stddev_samp("ret_1d_d").over(rows_252) * F.lit(sqrt(252.0)))
    .withColumn("downside_sq", F.pow(F.least(F.coalesce(F.col("ret_1d_d"), F.lit(0.0)) - F.lit(_RF_ANNUAL_DEFAULT / 252.0), F.lit(0.0)), 2))
    .withColumn("downside_deviation_252d", F.sqrt(F.avg("downside_sq").over(rows_252)) * F.lit(sqrt(252.0)))
    .withColumn("running_max_252d", F.max("close_d").over(rows_252))
    .withColumn("drawdown", F.when(F.col("running_max_252d") > 0, F.col("close_d") / F.col("running_max_252d") - F.lit(1.0)))
    .withColumn("max_drawdown_252d", F.min("drawdown").over(rows_252))
    .withColumn("avg_dollar_volume_30d", F.avg("dollar_volume").over(rows_30))
    .withColumn("illiquidity", F.when(F.col("avg_dollar_volume_30d") > 0, F.lit(1.0) / F.col("avg_dollar_volume_30d")))
    .withColumn("log_return", F.when(F.col("ret_1d_d") > -1, F.log1p("ret_1d_d")))
    .withColumn("ret_count_252d", F.count("log_return").over(rows_252))
    .withColumn("sum_log_return_252d", F.sum("log_return").over(rows_252))
    .withColumn(
        "ann_return_252d",
        F.when(F.col("ret_count_252d") > 1, F.exp(F.col("sum_log_return_252d") * F.lit(252.0) / F.col("ret_count_252d")) - F.lit(1.0)),
    )
    .withColumn("sharpe_252d", F.when(F.col("realized_vol_252d") > 0, (F.col("ann_return_252d") - F.lit(_RF_ANNUAL_DEFAULT)) / F.col("realized_vol_252d")))
    .withColumn("sortino_252d", F.when(F.col("downside_deviation_252d") > 0, (F.col("ann_return_252d") - F.lit(_RF_ANNUAL_DEFAULT)) / F.col("downside_deviation_252d")))
    .withColumn("calmar_252d", F.when(F.col("max_drawdown_252d") < 0, F.col("ann_return_252d") / F.abs(F.col("max_drawdown_252d"))))
)

sector_lookup = (
    spark.table("dim_security")
    .filter(F.col("is_current") == True)
    .select("security_sk", "ticker", "company_name", "gics_sector", "country")
)
market_with_security = market_base.join(sector_lookup, on="security_sk", how="left")
market_latest = (
    market_with_security
    .withColumn("asof_row_number", F.row_number().over(asof_latest_window))
    .filter(F.col("asof_row_number") == 1)
    .drop("asof_row_number")
)
sector_medians = (
    market_latest
    .withColumn("sector_bucket", F.coalesce(F.col("gics_sector"), F.lit("UNKNOWN")))
    .groupBy("date_sk", "sector_bucket")
    .agg(F.expr("percentile_approx(momentum_3m, 0.5)").alias("sector_median_momentum_3m"))
)
market_metrics = (
    market_latest
    .withColumn("sector_bucket", F.coalesce(F.col("gics_sector"), F.lit("UNKNOWN")))
    .join(sector_medians, ["date_sk", "sector_bucket"], "left")
    .withColumn("rel_strength_sector", F.col("momentum_3m") - F.col("sector_median_momentum_3m"))
)

# COMMAND ----------
# --- Smart-money metrics from PIT-clean Form 4 facts ---
asof_df = market_metrics.select("security_sk", "date_sk", "as_of")
insider = (
    spark.table("fact_insider_txn")
    .filter(
        F.col("security_sk").isNotNull()
        & F.col("event_date").isNotNull()
        & F.col("knowledge_date").isNotNull()
    )
    .select("security_sk", "entity_sk", "is_buy", "value_usd", "event_date", "knowledge_date")
)

insider_join = asof_df.alias("d").join(
    insider.alias("i"),
    (F.col("d.security_sk") == F.col("i.security_sk"))
    & (F.col("i.event_date") <= F.col("d.as_of"))
    & (F.col("i.knowledge_date") <= F.col("d.as_of"))
    & (F.col("i.event_date") >= F.date_sub(F.col("d.as_of"), 89)),
    "left",
)
insider_90d = (
    insider_join
    .groupBy(F.col("d.security_sk").alias("security_sk"), F.col("d.date_sk").alias("date_sk"))
    .agg(
        F.sum(F.when(F.col("i.is_buy") == True, F.coalesce(F.col("i.value_usd").cast(DoubleType()), F.lit(0.0))).otherwise(F.lit(0.0))).alias("buy_value_90d"),
        F.sum(F.when(F.col("i.is_buy") == False, F.coalesce(F.col("i.value_usd").cast(DoubleType()), F.lit(0.0))).otherwise(F.lit(0.0))).alias("sell_value_90d"),
        F.max(F.col("i.knowledge_date")).alias("insider_knowledge_date_90d"),
    )
    .withColumn(
        "insider_net_buy_ratio_90d",
        F.when(
            (F.col("buy_value_90d") + F.col("sell_value_90d")) > 0,
            (F.col("buy_value_90d") - F.col("sell_value_90d")) / (F.col("buy_value_90d") + F.col("sell_value_90d")),
        ),
    )
)

insider_join_30d = asof_df.alias("d").join(
    insider.alias("i"),
    (F.col("d.security_sk") == F.col("i.security_sk"))
    & (F.col("i.event_date") <= F.col("d.as_of"))
    & (F.col("i.knowledge_date") <= F.col("d.as_of"))
    & (F.col("i.event_date") >= F.date_sub(F.col("d.as_of"), 29)),
    "left",
)
insider_30d = (
    insider_join_30d
    .groupBy(F.col("d.security_sk").alias("security_sk"), F.col("d.date_sk").alias("date_sk"))
    .agg(
        F.countDistinct(F.when((F.col("i.is_buy") == True) & F.col("i.entity_sk").isNotNull(), F.col("i.entity_sk"))).cast(IntegerType()).alias("insider_cluster_buy_30d"),
        F.max(F.col("i.knowledge_date")).alias("insider_knowledge_date_30d"),
    )
)

# COMMAND ----------
# --- Assemble raw feature rows ---
raw_features = (
    market_metrics
    .join(insider_90d, ["security_sk", "date_sk"], "left")
    .join(insider_30d, ["security_sk", "date_sk"], "left")
    .withColumn("beta_252d", F.lit(None).cast(DoubleType()))
    .withColumn("info_ratio_252d", F.lit(None).cast(DoubleType()))
    .withColumn("inst_net_flow_qoq", F.lit(None).cast(DoubleType()))
    .withColumn("inst_new_initiations", F.lit(None).cast(IntegerType()))
    .withColumn("activist_13d_flag", F.lit(False).cast(BooleanType()))
    .withColumn("news_sentiment_ewma_14d", F.lit(None).cast(DoubleType()))
    .withColumn("news_volume_z_30d", F.lit(None).cast(DoubleType()))
    .withColumn("contract_award_usd_trailing_90d", F.lit(None).cast(DoubleType()))
    .withColumn("fundamental_anchor_z", F.lit(None).cast(DoubleType()))
    .withColumn("narrative_intensity", F.lit(None).cast(DoubleType()))
    .withColumn("narrative_premium", F.lit(None).cast(DoubleType()))
    .withColumn("divergence_state", F.lit(None).cast(StringType()))
    .withColumn("insider_cluster_buy_30d", F.coalesce(F.col("insider_cluster_buy_30d"), F.lit(0)).cast(IntegerType()))
    .withColumn(
        "max_knowledge_date",
        F.greatest(F.col("market_knowledge_date"), F.col("insider_knowledge_date_90d"), F.col("insider_knowledge_date_30d")),
    )
)

# COMMAND ----------
# --- Deterministic composite metric recipe: winsorize -> z -> sign-align -> blend -> 0-100 ---
metric_names = [
    "momentum_3m",
    "momentum_6m",
    "momentum_12m",
    "realized_vol_30d",
    "insider_net_buy_ratio_90d",
    "insider_cluster_buy_30d",
]

bounds_exprs = []
for metric_name in metric_names:
    bounds_exprs.append(F.expr(f"percentile_approx({metric_name}, 0.01)").alias(f"{metric_name}_p01"))
    bounds_exprs.append(F.expr(f"percentile_approx({metric_name}, 0.99)").alias(f"{metric_name}_p99"))

bounds_df = raw_features.groupBy("date_sk").agg(*bounds_exprs)
scored = raw_features.join(bounds_df, "date_sk", "left")

for metric_name in metric_names:
    scored = scored.withColumn(
        f"{metric_name}_winsor",
        F.when(F.col(metric_name).isNull(), F.lit(None).cast(DoubleType()))
        .when(F.col(f"{metric_name}_p01").isNotNull() & (F.col(metric_name) < F.col(f"{metric_name}_p01")), F.col(f"{metric_name}_p01"))
        .when(F.col(f"{metric_name}_p99").isNotNull() & (F.col(metric_name) > F.col(f"{metric_name}_p99")), F.col(f"{metric_name}_p99"))
        .otherwise(F.col(metric_name).cast(DoubleType())),
    )

stats_exprs = []
for metric_name in metric_names:
    stats_exprs.append(F.avg(f"{metric_name}_winsor").alias(f"{metric_name}_mean"))
    stats_exprs.append(F.stddev_samp(f"{metric_name}_winsor").alias(f"{metric_name}_stddev"))

stats_df = scored.groupBy("date_sk").agg(*stats_exprs)
scored = scored.join(stats_df, "date_sk", "left")

for metric_name in metric_names:
    scored = scored.withColumn(
        f"{metric_name}_z",
        F.when(F.col(f"{metric_name}_winsor").isNull(), F.lit(0.0))
        .when(F.col(f"{metric_name}_stddev") > 0, (F.col(f"{metric_name}_winsor") - F.col(f"{metric_name}_mean")) / F.col(f"{metric_name}_stddev"))
        .otherwise(F.lit(0.0)),
    )

active_weights = {
    row.metric_name: {"weight": float(row.weight), "direction": int(row.direction)}
    for row in spark.table("metric_weights")
    .filter((F.col("metric_group") == "composite_growth_score") & (F.col("is_active") == True) & (F.col("version") == "e6a_v1"))
    .select("metric_name", "weight", "direction")
    .collect()
}

score_expr = F.lit(0.0)
for metric_name in metric_names:
    config = active_weights[metric_name]
    score_expr = score_expr + F.lit(config["weight"] * config["direction"]) * F.col(f"{metric_name}_z")

rank_window = Window.partitionBy("date_sk").orderBy(F.col("composite_growth_score_raw"))
count_window = Window.partitionBy("date_sk")
features_df = (
    scored
    .withColumn("composite_growth_score_raw", score_expr)
    .withColumn("date_security_count", F.count("security_sk").over(count_window))
    .withColumn(
        "composite_growth_score",
        F.when(F.col("date_security_count") <= 1, F.lit(50.0))
        .otherwise(F.round(F.percent_rank().over(rank_window) * F.lit(100.0), 4)),
    )
    .withColumn("opportunity_score", F.lit(None).cast(DoubleType()))
    .withColumn("score_status", F.lit("INCOMPLETE_E6A_WAITING_E8_E14"))
    .withColumn(
        "stale_sources_json",
        F.to_json(F.struct(
            F.col("momentum_3m").isNull().alias("momentum_3m"),
            F.col("momentum_6m").isNull().alias("momentum_6m"),
            F.col("momentum_12m").isNull().alias("momentum_12m"),
            F.col("beta_252d").isNull().alias("benchmark"),
            F.col("inst_net_flow_qoq").isNull().alias("institutional"),
            F.col("news_sentiment_ewma_14d").isNull().alias("news"),
            F.col("contract_award_usd_trailing_90d").isNull().alias("contracts"),
            F.col("fundamental_anchor_z").isNull().alias("fundamentals"),
            F.col("narrative_intensity").isNull().alias("narrative"),
            F.lit(True).alias("valuation_brake"),
        )),
    )
    .select(
        "security_sk", "date_sk", "ticker", "company_name", "gics_sector", "country", "as_of",
        "close", "ret_1d", "momentum_3m", "momentum_6m", "momentum_12m", "rel_strength_sector",
        "realized_vol_30d", "realized_vol_90d", "realized_vol_252d", "downside_deviation_252d",
        "max_drawdown_252d", "beta_252d", "illiquidity", "ann_return_252d", "sharpe_252d",
        "sortino_252d", "calmar_252d", "info_ratio_252d", "insider_net_buy_ratio_90d",
        "insider_cluster_buy_30d", "inst_net_flow_qoq", "inst_new_initiations", "activist_13d_flag",
        "news_sentiment_ewma_14d", "news_volume_z_30d", "contract_award_usd_trailing_90d",
        "fundamental_anchor_z", "narrative_intensity", "narrative_premium", "divergence_state",
        "composite_growth_score", "opportunity_score", "score_status", "max_knowledge_date", "stale_sources_json",
    )
    .dropDuplicates(["security_sk", "date_sk"])
)

_merge_all("security_daily_features", features_df, "t.security_sk = s.security_sk AND t.date_sk = s.date_sk")

# COMMAND ----------
# --- Lakehouse serving projections for the API/agent contract ---
# Fabric SQL endpoints do not reliably expose Spark views, so the notebook
# materializes the v_* projections as Delta tables. The Warehouse SQL files
# still define the promoted Warehouse objects as true SQL views.
_replace_delta_projection("v_market_momentum", """
    SELECT security_sk, date_sk, as_of, close, ret_1d, momentum_3m, momentum_6m, momentum_12m, rel_strength_sector, max_knowledge_date
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

_replace_delta_projection("v_market_risk", """
    SELECT security_sk, date_sk, as_of, realized_vol_30d, realized_vol_90d, realized_vol_252d,
           downside_deviation_252d, max_drawdown_252d, beta_252d, illiquidity, max_knowledge_date
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

_replace_delta_projection("v_risk_adjusted", """
    SELECT security_sk, date_sk, as_of, ann_return_252d, sharpe_252d, sortino_252d, calmar_252d, info_ratio_252d, max_knowledge_date
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

_replace_delta_projection("v_smart_money", """
    SELECT security_sk, date_sk, as_of, insider_net_buy_ratio_90d, insider_cluster_buy_30d,
           inst_net_flow_qoq, inst_new_initiations, activist_13d_flag, max_knowledge_date
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

_replace_delta_projection("v_opportunity_legs", """
    SELECT security_sk, date_sk, as_of,
           CAST(NULL AS DOUBLE) AS thesis_linkage_z,
           CAST(NULL AS DOUBLE) AS attention_acceleration_z,
           CAST(NULL AS DOUBLE) AS smart_money_z,
           CAST(NULL AS DOUBLE) AS fundamental_health_z,
           CAST(NULL AS DOUBLE) AS valuation_brake_z,
           CAST(NULL AS DOUBLE) AS crowding_positioning_z,
           score_status,
           max_knowledge_date
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

_replace_delta_projection("v_opportunity_score", """
    SELECT security_sk, date_sk, as_of, opportunity_score, score_status, max_knowledge_date
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

_replace_delta_projection("v_security_daily_features", """
    SELECT security_sk, ticker, company_name, gics_sector, country, as_of, date_sk,
           close, ret_1d, momentum_3m, momentum_6m, momentum_12m, rel_strength_sector,
           realized_vol_252d, downside_deviation_252d, max_drawdown_252d, beta_252d, illiquidity,
           ann_return_252d, sharpe_252d, sortino_252d, calmar_252d, info_ratio_252d,
           insider_net_buy_ratio_90d, insider_cluster_buy_30d, inst_net_flow_qoq, inst_new_initiations,
           activist_13d_flag, news_sentiment_ewma_14d, news_volume_z_30d, contract_award_usd_trailing_90d,
           fundamental_anchor_z, narrative_intensity, narrative_premium, divergence_state,
           composite_growth_score, opportunity_score, score_status, max_knowledge_date, stale_sources_json
    FROM security_daily_features
    WHERE max_knowledge_date <= as_of
""")

# COMMAND ----------
# --- E6 validation summary ---
row_count = spark.table("security_daily_features").count()
serving_row_count = spark.table("v_security_daily_features").count()
missing_pit = spark.sql("""
    SELECT COUNT(*) AS n
    FROM security_daily_features
    WHERE as_of IS NULL OR max_knowledge_date IS NULL OR max_knowledge_date > as_of
""").collect()[0].n
weight_validation = spark.sql("""
    SELECT ROUND(SUM(weight), 6) AS weight_sum
    FROM metric_weights
    WHERE metric_group = 'composite_growth_score' AND is_active = true AND version = 'e6a_v1'
""").collect()[0].weight_sum
duplicate_features = spark.sql("""
    SELECT COUNT(*) AS n
    FROM (
        SELECT security_sk, date_sk, COUNT(*) AS duplicate_count
        FROM security_daily_features
        GROUP BY security_sk, date_sk
        HAVING COUNT(*) > 1
    ) d
""").collect()[0].n
invalid_scores = spark.sql("""
    SELECT COUNT(*) AS n
    FROM security_daily_features
    WHERE composite_growth_score < 0 OR composite_growth_score > 100 OR opportunity_score IS NOT NULL
""").collect()[0].n

print(
    f"E6 validation: security_daily_features={row_count}, "
    f"v_security_daily_features={serving_row_count}, "
    f"missing_or_future_pit={missing_pit}, duplicate_features={duplicate_features}, "
    f"invalid_scores={invalid_scores}, weight_sum={weight_validation}"
)
if row_count == 0 or serving_row_count != row_count or missing_pit or duplicate_features or invalid_scores or weight_validation != Decimal("1.000000"):
    raise RuntimeError(
        f"E6 validation failed: security_daily_features={row_count}, "
        f"v_security_daily_features={serving_row_count}, "
        f"missing_or_future_pit={missing_pit}, "
        f"duplicate_features={duplicate_features}, invalid_scores={invalid_scores}, "
        f"weight_sum={weight_validation}"
    )
