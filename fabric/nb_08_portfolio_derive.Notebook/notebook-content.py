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

# Fabric Notebook: nb_08_portfolio_derive
# Mirrors immutable owner-scoped portfolio transactions from Bronze, derives
# positions and valuation, and exports canonical security/market projections.
# Attaches to: auspex_bronze (default lakehouse)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from datetime import date, timedelta
from decimal import Decimal
from delta.tables import DeltaTable
from pyspark.sql import functions as F
from pyspark.sql import Window
from pyspark.sql.types import BooleanType, DecimalType, LongType, StringType, StructField, StructType

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# PARAMETERS CELL ********************

# --- Parameters: mark this cell as the Fabric parameter cell ---
from_date = ""
to_date = ""

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Normalize and validate injected parameter values ---
_today = date.today().isoformat()
from_date = str(from_date).strip() or (date.today() - timedelta(days=30)).isoformat()
to_date = str(to_date).strip() or _today
if date.fromisoformat(from_date) > date.fromisoformat(to_date):
    raise ValueError("from_date must be on or before to_date")

print(f"Window: {from_date} to {to_date}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

def _require_table(table_name: str) -> None:
    if not spark.catalog.tableExists(table_name):
        raise RuntimeError(f"Required upstream table is missing: {table_name}")


def _merge_all(table_name: str, source_df, condition: str) -> None:
    if source_df.isEmpty():
        return
    (
        DeltaTable.forName(spark, table_name)
        .alias("t")
        .merge(source_df.alias("s"), condition)
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )
    metrics = DeltaTable.forName(spark, table_name).history(1).select("operationMetrics").first()
    print(f"{table_name} operationMetrics={metrics.operationMetrics}")


for required_table in [
    "dim_security", "fact_market_daily", "fact_fx_rate",
    "fact_theme_opportunity_score", "security_daily_features",
]:
    _require_table(required_table)

spark.sql("""
    CREATE TABLE IF NOT EXISTS portfolio_snapshot_manifest (
        snapshot_id STRING NOT NULL,
        snapshot_date DATE NOT NULL,
        status STRING NOT NULL,
        transaction_count BIGINT NOT NULL,
        position_count BIGINT,
        valuation_count BIGINT,
        completed_at TIMESTAMP
    ) USING DELTA
""")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Export the canonical current security catalogue as point-read documents.
current_securities = (
    spark.table("dim_security")
    .filter(F.col("is_current") & F.col("is_active") & F.col("ticker").isNotNull())
    .select(
        "security_sk",
        F.upper(F.trim("ticker")).alias("ticker"),
        F.upper(F.trim("isin")).alias("isin"),
        "company_name",
        F.upper(F.trim("currency")).alias("currency"),
        "exchange",
        "gics_sector",
        "country",
    )
)
invalid_security_currencies = current_securities.filter(
    F.col("currency").isNull() | ~F.col("currency").isin("USD", "CHF", "EUR", "GBP")
)
if not invalid_security_currencies.isEmpty():
    raise RuntimeError("Canonical security projection contains missing or unsupported currencies")
ambiguous_current_tickers = (
    current_securities.groupBy("ticker")
    .agg(F.countDistinct("security_sk").alias("security_count"))
    .filter(F.col("security_count") > 1)
)
ambiguous_current_isins = (
    current_securities.filter(F.col("isin").isNotNull())
    .groupBy("isin")
    .agg(F.countDistinct("security_sk").alias("security_count"))
    .filter(F.col("security_count") > 1)
)
if not ambiguous_current_isins.isEmpty():
    raise RuntimeError("Canonical security projection contains ambiguous current ISIN identifiers")

unique_current_tickers = current_securities.join(
    ambiguous_current_tickers.select("ticker"), "ticker", "left_anti"
)
projection_generation = to_date
security_projection_columns = [
    "security_sk", "ticker", "isin", "company_name", "currency", "exchange",
    "gics_sector", "country",
]
security_by_sk = (
    current_securities.withColumn("id", F.concat(F.lit("security:"), F.col("security_sk")))
    .withColumn("source_id", F.lit("fabric"))
    .withColumn("generation", F.lit(projection_generation))
)
security_by_ticker = (
    unique_current_tickers.withColumn("id", F.concat(F.lit("ticker:"), F.col("ticker")))
    .withColumn("source_id", F.lit("fabric"))
    .withColumn("generation", F.lit(projection_generation))
)
security_by_isin = (
    current_securities.filter(F.col("isin").isNotNull())
    .withColumn("id", F.concat(F.lit("isin:"), F.col("isin")))
    .withColumn("source_id", F.lit("fabric"))
    .withColumn("generation", F.lit(projection_generation))
)
(
    security_by_sk.unionByName(security_by_ticker).unionByName(security_by_isin)
    .coalesce(1)
    .write.mode("overwrite")
    .json("Files/serving/security_catalog")
)

# Export one deterministic latest-known quote per current security.
price_window = Window.partitionBy("security_sk").orderBy(
    F.col("event_date").desc(),
    F.col("knowledge_date").desc(),
    F.col("revision_loaded_at").desc_nulls_last(),
    F.col("price_revision_hash").desc(),
)
latest_quotes = (
    spark.table("fact_market_daily")
    .filter(F.col("knowledge_date") <= F.to_date(F.lit(to_date)))
    .withColumn("row_number", F.row_number().over(price_window))
    .filter(F.col("row_number") == 1)
    .join(current_securities.select("security_sk", "ticker", "currency"), "security_sk", "inner")
    .select(
        "ticker",
        "security_sk",
        F.col("close").cast(StringType()).alias("price"),
        "currency",
        F.col("event_date").cast(StringType()).alias("as_of"),
        F.lit("fabric").alias("source_id"),
        F.lit(projection_generation).alias("generation"),
    )
)
latest_quotes_by_security = latest_quotes.withColumn(
    "id", F.concat(F.lit("quote:security:"), F.col("security_sk"))
)
latest_quotes_by_ticker = (
    latest_quotes.join(unique_current_tickers.select("security_sk"), "security_sk", "inner")
    .withColumn("id", F.concat(F.lit("quote:"), F.col("ticker")))
)

# Export the latest seven distinct point-in-time closes for compact Home sparklines.
history_revision_window = Window.partitionBy("security_sk", "event_date").orderBy(
    F.col("knowledge_date").desc(),
    F.col("revision_loaded_at").desc_nulls_last(),
    F.col("price_revision_hash").desc(),
)
history_session_window = Window.partitionBy("security_sk").orderBy(
    F.col("event_date").desc()
)
recent_price_points = (
    spark.table("fact_market_daily")
    .filter(F.col("knowledge_date") <= F.to_date(F.lit(to_date)))
    .withColumn("revision_number", F.row_number().over(history_revision_window))
    .filter(F.col("revision_number") == 1)
    .withColumn("session_number", F.row_number().over(history_session_window))
    .filter(F.col("session_number") <= 7)
    .join(current_securities.select("security_sk", "ticker", "currency"), "security_sk", "inner")
    .select(
        "security_sk", "ticker", "currency",
        F.col("event_date").cast(StringType()).alias("date"),
        F.col("close").cast(StringType()).alias("price"),
    )
)
price_histories = (
    recent_price_points.withColumn(
        "price_point_json",
        F.concat(
            F.lit('{"date":"'), F.col("date"),
            F.lit('","price":"'), F.col("price"), F.lit('"}'),
        ),
    )
    .groupBy("security_sk", "ticker", "currency")
    .agg(
        F.concat(
            F.lit("["),
            F.concat_ws(",", F.sort_array(F.collect_list("price_point_json"))),
            F.lit("]"),
        ).alias("prices_json")
    )
    .withColumn("kind", F.lit("price_history"))
    .withColumn("source_id", F.lit("fabric"))
    .withColumn("generation", F.lit(projection_generation))
)
price_histories_by_security = price_histories.withColumn(
    "id", F.concat(F.lit("history:security:"), F.col("security_sk"))
)
price_histories_by_ticker = (
    price_histories.join(unique_current_tickers.select("security_sk"), "security_sk", "inner")
    .withColumn("id", F.concat(F.lit("history:"), F.col("ticker")))
)

fx_window = Window.partitionBy("ccy_pair").orderBy(
    F.col("event_date").desc(),
    F.col("knowledge_date").desc(),
    F.col("fx_revision_hash").desc(),
)
latest_fx = (
    spark.table("fact_fx_rate")
    .filter(F.col("knowledge_date") <= F.to_date(F.lit(to_date)))
    .withColumn("row_number", F.row_number().over(fx_window))
    .filter(F.col("row_number") == 1)
    .select(
        F.concat(F.lit("fx:"), F.upper("ccy_pair")).alias("id"),
        F.lit("fx_alias").alias("kind"),
        F.upper("ccy_pair").alias("pair"),
        F.col("rate").cast(StringType()).alias("rate"),
        F.col("event_date").cast(StringType()).alias("as_of"),
        F.lit("fabric").alias("source_id"),
        F.lit(projection_generation).alias("generation"),
    )
)
score_window = Window.partitionBy("security_sk").orderBy(
    F.col("as_of").desc(),
    F.col("opportunity_score").desc(),
    F.col("theme_id"),
)
score_attribution = F.array(*[
    F.struct(
        F.lit(leg_name).alias("key"),
        F.col(f"s.{contribution_column}").cast(StringType()).alias("contribution"),
        F.when(F.col(f"s.{contribution_column}") > 0, F.lit("RAISED"))
        .when(F.col(f"s.{contribution_column}") < 0, F.lit("LOWERED"))
        .otherwise(F.lit("NEUTRAL"))
        .alias("direction"),
    )
    for leg_name, contribution_column in (
        ("thesis_linkage", "thesis_linkage_contribution"),
        ("attention_acceleration", "attention_acceleration_contribution"),
        ("smart_money", "smart_money_contribution"),
        ("fundamental_health", "fundamental_health_contribution"),
        ("valuation_brake", "valuation_brake_contribution"),
        ("crowding_positioning", "crowding_positioning_contribution"),
    )
])
latest_scores = (
    spark.table("fact_theme_opportunity_score").alias("s")
    .filter(
        (F.col("s.as_of") <= F.to_date(F.lit(to_date)))
        & F.col("s.coverage_status").isin("READY", "PARTIAL")
    )
    .withColumn("row_number", F.row_number().over(score_window))
    .filter(F.col("row_number") == 1)
    .join(
        current_securities.select("security_sk", "ticker", "country"),
        "security_sk",
        "inner",
    )
    .join(
        spark.table("security_daily_features").select(
            "security_sk", "date_sk", "illiquidity",
        ).alias("f"),
        (F.col("s.security_sk") == F.col("f.security_sk"))
        & (F.col("s.date_sk") == F.col("f.date_sk")),
        "left",
    )
    .withColumn("id", F.concat(F.lit("score:security:"), F.col("s.security_sk")))
    .withColumn("kind", F.lit("opportunity_score"))
    .withColumn(
        "spread_bps",
        F.least(
            F.lit(Decimal("100")),
            F.greatest(
                F.lit(Decimal("5")),
                F.abs(F.coalesce(F.col("f.illiquidity"), F.lit(Decimal("0.0005"))))
                * F.lit(Decimal("10000")),
            ),
        ).cast(StringType()),
    )
    .withColumn(
        "coverage_reasons",
        F.from_json("coverage_reasons_json", "ARRAY<STRING>"),
    )
    .withColumn("attribution", score_attribution)
    .select(
        "id", "kind", F.col("s.security_sk").alias("security_sk"),
        "ticker", "country", F.col("s.theme_id").alias("theme_id"),
        F.col("s.as_of").cast(StringType()).alias("as_of"),
        F.col("s.opportunity_score").cast(StringType()).alias("opportunity_score"),
        F.col("s.coverage_status").alias("coverage_status"),
        "coverage_reasons", "attribution", "spread_bps",
        F.col("s.model_version").alias("score_model_version"),
        F.col("s.weight_version").alias("score_weight_version"),
        F.lit("fabric").alias("source_id"),
        F.lit(projection_generation).alias("generation"),
    )
)
invalid_score_attribution = latest_scores.filter(
    (F.size("attribution") != 6)
    | F.exists(
        "attribution",
        lambda leg: leg["key"].isNull()
        | leg["contribution"].isNull()
        | ~leg["direction"].isin("RAISED", "LOWERED", "NEUTRAL"),
    )
)
if not invalid_score_attribution.isEmpty():
    raise RuntimeError("Opportunity Score projection contains incomplete attribution")
dated_fx = (
    spark.table("fact_fx_rate")
    .filter(F.col("knowledge_date") <= F.to_date(F.lit(to_date)))
    .withColumn(
        "dated_row_number",
        F.row_number().over(
            Window.partitionBy("ccy_pair", "event_date").orderBy(
                F.col("knowledge_date").desc(),
                F.col("fx_revision_hash").desc(),
            )
        ),
    )
    .filter(F.col("dated_row_number") == 1)
    .select(
        F.concat(
            F.lit("fx:"), F.upper("ccy_pair"), F.lit(":"), F.col("event_date").cast(StringType())
        ).alias("id"),
        F.lit("fx").alias("kind"),
        F.upper("ccy_pair").alias("pair"),
        F.col("rate").cast(StringType()).alias("rate"),
        F.col("event_date").cast(StringType()).alias("as_of"),
        F.lit("fabric").alias("source_id"),
        F.lit(projection_generation).alias("generation"),
    )
)
(
    latest_quotes_by_security.unionByName(latest_quotes_by_ticker, allowMissingColumns=True)
    .unionByName(price_histories_by_security, allowMissingColumns=True)
    .unionByName(price_histories_by_ticker, allowMissingColumns=True)
    .unionByName(latest_fx, allowMissingColumns=True)
    .unionByName(dated_fx, allowMissingColumns=True)
    .unionByName(latest_scores, allowMissingColumns=True)
    .coalesce(1)
    .write.mode("overwrite")
    .json("Files/serving/market_data")
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

spark.sql("""
    CREATE TABLE IF NOT EXISTS silver_portfolio_transaction (
        transaction_id STRING NOT NULL,
        owner_user_sk STRING NOT NULL,
        client_request_id STRING NOT NULL,
        account_id STRING NOT NULL,
        transaction_type STRING NOT NULL,
        security_sk BIGINT,
        ticker STRING,
        security_currency STRING,
        quantity DECIMAL(20,8),
        price DECIMAL(20,8),
        currency STRING NOT NULL,
        fees DECIMAL(20,2) NOT NULL,
        cash_amount DECIMAL(20,2) NOT NULL,
        base_currency STRING,
        fx_rate_to_base DECIMAL(20,8),
        corrects_transaction_id STRING,
        gross_amount DECIMAL(20,2),
        source_currency STRING,
        source_amount DECIMAL(20,2),
        fx_rate_to_settlement DECIMAL(20,8),
        linked_transaction_id STRING,
        cost_category STRING,
        affects_cash BOOLEAN,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        created_at TIMESTAMP NOT NULL,
        payload_hash STRING NOT NULL
    ) USING DELTA
""")
portfolio_transaction_columns = spark.table("silver_portfolio_transaction").columns
if "base_currency" not in portfolio_transaction_columns:
    spark.sql("ALTER TABLE silver_portfolio_transaction ADD COLUMNS (base_currency STRING)")
if "fx_rate_to_base" not in portfolio_transaction_columns:
    spark.sql("ALTER TABLE silver_portfolio_transaction ADD COLUMNS (fx_rate_to_base DECIMAL(20,8))")
if "corrects_transaction_id" not in portfolio_transaction_columns:
    spark.sql("ALTER TABLE silver_portfolio_transaction ADD COLUMNS (corrects_transaction_id STRING)")
if "gross_amount" not in portfolio_transaction_columns:
    spark.sql("ALTER TABLE silver_portfolio_transaction ADD COLUMNS (gross_amount DECIMAL(20,2))")
if "source_currency" not in portfolio_transaction_columns:
    spark.sql("ALTER TABLE silver_portfolio_transaction ADD COLUMNS (source_currency STRING)")
if "source_amount" not in portfolio_transaction_columns:
    spark.sql("ALTER TABLE silver_portfolio_transaction ADD COLUMNS (source_amount DECIMAL(20,2))")
if "fx_rate_to_settlement" not in portfolio_transaction_columns:
    spark.sql("ALTER TABLE silver_portfolio_transaction ADD COLUMNS (fx_rate_to_settlement DECIMAL(20,8))")
if "linked_transaction_id" not in portfolio_transaction_columns:
    spark.sql("ALTER TABLE silver_portfolio_transaction ADD COLUMNS (linked_transaction_id STRING)")
if "cost_category" not in portfolio_transaction_columns:
    spark.sql("ALTER TABLE silver_portfolio_transaction ADD COLUMNS (cost_category STRING)")
if "affects_cash" not in portfolio_transaction_columns:
    spark.sql("ALTER TABLE silver_portfolio_transaction ADD COLUMNS (affects_cash BOOLEAN)")

portfolio_record_schema = StructType([
    StructField("record_type", StringType()),
    StructField("snapshot_id", StringType()),
    StructField("snapshot_date", StringType()),
    StructField("transaction_count", LongType()),
    StructField("transaction_id", StringType()),
    StructField("owner_user_sk", StringType()),
    StructField("client_request_id", StringType()),
    StructField("account_id", StringType()),
    StructField("transaction_type", StringType()),
    StructField("security_sk", LongType()),
    StructField("security_code", StringType()),
    StructField("security_currency", StringType()),
    StructField("quantity", StringType()),
    StructField("price", StringType()),
    StructField("currency", StringType()),
    StructField("fees", StringType()),
    StructField("cash_amount", StringType()),
    StructField("base_currency", StringType()),
    StructField("fx_rate_to_base", StringType()),
    StructField("corrects_transaction_id", StringType()),
    StructField("gross_amount", StringType()),
    StructField("source_currency", StringType()),
    StructField("source_amount", StringType()),
    StructField("fx_rate_to_settlement", StringType()),
    StructField("linked_transaction_id", StringType()),
    StructField("cost_category", StringType()),
    StructField("affects_cash", BooleanType()),
    StructField("event_date", StringType()),
    StructField("created_at", StringType()),
    StructField("payload_hash", StringType()),
])
portfolio_envelope_schema = StructType([
    StructField("ingest_ts", StringType()),
    StructField("batch_id", StringType()),
    StructField("record", portfolio_record_schema),
])
try:
    mssparkutils.fs.ls("Files/bronze/portfolio")
    portfolio_bronze_exists = True
except Exception:
    portfolio_bronze_exists = False

if portfolio_bronze_exists:
    portfolio_raw = (
        spark.read.option("recursiveFileLookup", "true").text("Files/bronze/portfolio")
        .withColumn("envelope", F.from_json("value", portfolio_envelope_schema))
        .select(
            F.to_timestamp("envelope.ingest_ts").alias("bronze_ingest_ts"),
            F.col("envelope.batch_id").alias("bronze_batch_id"),
            "envelope.record.*",
        )
    )
    invalid_portfolio_rows = portfolio_raw.filter(
        F.col("record_type").isNull() | F.col("snapshot_id").isNull()
    )
    if not invalid_portfolio_rows.isEmpty():
        raise RuntimeError("Portfolio Bronze contains malformed snapshot rows")
    latest_manifest = (
        portfolio_raw.filter(F.col("record_type") == "snapshot_manifest")
        .withColumn("snapshot_date_parsed", F.to_date("snapshot_date"))
        .filter(F.col("snapshot_date_parsed").isNotNull())
        .orderBy(
            F.col("bronze_ingest_ts").desc(),
            F.col("bronze_batch_id").desc(),
        )
        .first()
    )
    if latest_manifest is None:
        raise RuntimeError("Portfolio Bronze has no complete snapshot manifest")
    selected_snapshot_id = latest_manifest.snapshot_id
    expected_transaction_count = int(latest_manifest.transaction_count or 0)
    running_manifest = spark.sql(f"""
        SELECT
            '{selected_snapshot_id}' AS snapshot_id,
            DATE('{latest_manifest.snapshot_date_parsed}') AS snapshot_date,
            'running' AS status,
            CAST({expected_transaction_count} AS BIGINT) AS transaction_count,
            CAST(NULL AS BIGINT) AS position_count,
            CAST(NULL AS BIGINT) AS valuation_count,
            CAST(NULL AS TIMESTAMP) AS completed_at
    """)
    DeltaTable.forName(spark, "portfolio_snapshot_manifest").delete("true")
    running_manifest.write.mode("append").saveAsTable("portfolio_snapshot_manifest")
    portfolio_rows = (
        portfolio_raw
        .filter(
            (F.col("record_type") == "transaction")
            & (F.col("snapshot_id") == selected_snapshot_id)
        )
        .withColumn("event_date", F.to_date("event_date"))
        .withColumn("created_at", F.to_timestamp("created_at"))
        .withColumn("knowledge_date", F.to_date("created_at"))
        .withColumn("security_sk", F.col("security_sk").cast(LongType()))
        .withColumn("quantity", F.col("quantity").cast(DecimalType(20, 8)))
        .withColumn("price", F.col("price").cast(DecimalType(20, 8)))
        .withColumn("fees", F.col("fees").cast(DecimalType(20, 2)))
        .withColumn("cash_amount", F.col("cash_amount").cast(DecimalType(20, 2)))
        .withColumn("fx_rate_to_base", F.col("fx_rate_to_base").cast(DecimalType(20, 8)))
        .withColumn("gross_amount", F.col("gross_amount").cast(DecimalType(20, 2)))
        .withColumn("source_amount", F.col("source_amount").cast(DecimalType(20, 2)))
        .withColumn("fx_rate_to_settlement", F.col("fx_rate_to_settlement").cast(DecimalType(20, 8)))
        .withColumn("affects_cash", F.coalesce(F.col("affects_cash"), F.lit(True)))
        .select(
            "transaction_id", "owner_user_sk", "client_request_id", "account_id",
            "transaction_type", "security_sk", F.col("security_code").alias("ticker"),
            "security_currency", "quantity", "price", "currency", "fees",
            "cash_amount", "base_currency", "fx_rate_to_base", "event_date",
            "corrects_transaction_id", "gross_amount", "source_currency",
            "source_amount", "fx_rate_to_settlement", "linked_transaction_id",
            "cost_category", "affects_cash", "knowledge_date", "created_at", "payload_hash",
        )
    )
    invalid_portfolio_rows = portfolio_rows.filter(
        F.col("transaction_id").isNull()
        | F.col("owner_user_sk").isNull()
        | F.col("client_request_id").isNull()
        | F.col("account_id").isNull()
        | F.col("transaction_type").isNull()
        | F.col("currency").isNull()
        | F.col("fees").isNull()
        | F.col("cash_amount").isNull()
        | F.col("affects_cash").isNull()
        | F.col("event_date").isNull()
        | F.col("knowledge_date").isNull()
        | (F.col("event_date") > F.col("knowledge_date"))
        | F.col("payload_hash").isNull()
    )
    if not invalid_portfolio_rows.isEmpty():
        raise RuntimeError("Portfolio snapshot contains invalid transaction rows")
    portfolio_rows = portfolio_rows.filter(~F.col("transaction_id").isNull())
    conflicting_portfolio_revisions = (
        portfolio_rows.groupBy("owner_user_sk", "transaction_id")
        .agg(F.countDistinct("payload_hash").alias("payload_hash_count"))
        .filter(F.col("payload_hash_count") > 1)
    )
    if not conflicting_portfolio_revisions.isEmpty():
        raise RuntimeError("Portfolio snapshot contains conflicting transaction payloads")
    portfolio_rows = (
        portfolio_rows.orderBy(F.col("created_at").desc(), F.col("payload_hash").desc())
        .dropDuplicates(["owner_user_sk", "transaction_id"])
    )
    if portfolio_rows.count() != expected_transaction_count:
        raise RuntimeError("Portfolio snapshot transaction count does not match its manifest")
    unresolved_security_events = portfolio_rows.filter(
        F.col("transaction_type").isin("OPENING_POSITION", "BUY", "SELL", "DIVIDEND")
        & F.col("security_sk").isNull()
    )
    if not unresolved_security_events.isEmpty():
        raise RuntimeError("Portfolio snapshot contains unresolved security events")
    DeltaTable.forName(spark, "silver_portfolio_transaction").delete("true")
    if not portfolio_rows.isEmpty():
        portfolio_rows.write.mode("append").saveAsTable("silver_portfolio_transaction")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

ledger_asof = (
    spark.table("silver_portfolio_transaction")
    .filter(
        (F.col("event_date") <= F.to_date(F.lit(to_date)))
        & (F.col("knowledge_date") <= F.to_date(F.lit(to_date)))
    )
)
superseded_transaction_ids = (
    ledger_asof.filter(F.col("corrects_transaction_id").isNotNull())
    .select(
        "owner_user_sk",
        F.col("corrects_transaction_id").alias("transaction_id"),
    )
)
missing_correction_targets = (
    superseded_transaction_ids.alias("c")
    .join(
        ledger_asof.select("owner_user_sk", "transaction_id").alias("t"),
        ["owner_user_sk", "transaction_id"],
        "left_anti",
    )
)
duplicate_corrections = (
    superseded_transaction_ids.groupBy("owner_user_sk", "transaction_id")
    .count().filter(F.col("count") > 1)
)
correction_chains = (
    ledger_asof.filter(F.col("corrects_transaction_id").isNotNull()).alias("c")
    .join(
        ledger_asof.filter(F.col("corrects_transaction_id").isNotNull()).alias("t"),
        (F.col("c.owner_user_sk") == F.col("t.owner_user_sk"))
        & (F.col("c.corrects_transaction_id") == F.col("t.transaction_id")),
        "inner",
    )
)
if (
    not missing_correction_targets.isEmpty()
    or not duplicate_corrections.isEmpty()
    or not correction_chains.isEmpty()
):
    raise RuntimeError("Portfolio snapshot contains an invalid correction graph")
effective_ledger = ledger_asof.join(
    superseded_transaction_ids,
    ["owner_user_sk", "transaction_id"],
    "left_anti",
)
effective_ledger = effective_ledger.join(
    superseded_transaction_ids.select(
        "owner_user_sk",
        F.col("transaction_id").alias("linked_transaction_id"),
    ),
    ["owner_user_sk", "linked_transaction_id"],
    "left_anti",
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_portfolio_position (
        owner_user_sk STRING NOT NULL,
        account_id STRING NOT NULL,
        security_sk BIGINT NOT NULL,
        ticker STRING NOT NULL,
        security_currency STRING,
        gics_sector STRING,
        country STRING,
        quantity DECIMAL(20,8) NOT NULL,
        market_value_base DECIMAL(20,2),
        position_weight DECIMAL(12,8),
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL
    ) USING DELTA
""")
portfolio_position_columns = spark.table("fact_portfolio_position").columns
for column_name, ddl in {
    "security_currency": "security_currency STRING",
    "gics_sector": "gics_sector STRING",
    "country": "country STRING",
    "market_value_base": "market_value_base DECIMAL(20,2)",
    "position_weight": "position_weight DECIMAL(12,8)",
}.items():
    if column_name not in portfolio_position_columns:
        spark.sql(f"ALTER TABLE fact_portfolio_position ADD COLUMNS ({ddl})")

position_events = (
    effective_ledger
    .filter(
        F.col("security_sk").isNotNull()
        & F.col("transaction_type").isin("OPENING_POSITION", "BUY", "SELL")
    )
    .withColumn(
        "signed_quantity",
        F.when(F.col("transaction_type") == "SELL", -F.col("quantity")).otherwise(F.col("quantity")),
    )
)
positions = (
    position_events.groupBy("owner_user_sk", "account_id", "security_sk", "ticker")
    .agg(
        F.sum("signed_quantity").alias("quantity"),
        F.max("event_date").alias("event_date"),
        F.max("knowledge_date").alias("knowledge_date"),
    )
    .filter(F.col("quantity") != 0)
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_portfolio_valuation (
        owner_user_sk STRING NOT NULL,
        valuation_date DATE NOT NULL,
        base_currency STRING NOT NULL,
        total_cash_base DECIMAL(20,2),
        total_stocks_base DECIMAL(20,2),
        total_value_base DECIMAL(20,2),
        cash_weight DECIMAL(12,8),
        missing_prices INT NOT NULL,
        missing_fx INT NOT NULL,
        coverage_complete BOOLEAN NOT NULL,
        knowledge_date DATE NOT NULL
    ) USING DELTA
""")

# Materialize USD as the canonical daily valuation currency. The Web API can
# convert to the user's configured base currency from the exported FX projection.
cash = (
    effective_ledger
    .groupBy("owner_user_sk", "currency")
    .agg(F.sum("cash_amount").alias("cash_amount"))
)
usd_fx = latest_fx.select(
    F.substring("pair", 4, 3).alias("currency"),
    F.col("rate").cast(DecimalType(18, 8)).alias("usd_to_currency"),
).alias("usd_fx")
cash_usd = (
    cash.join(usd_fx, "currency", "left")
    .withColumn(
        "cash_base",
        F.when(F.col("currency") == "USD", F.col("cash_amount"))
        .otherwise(F.col("cash_amount") / F.col("usd_to_currency")),
    )
    .withColumn("fx_missing", F.col("cash_base").isNull().cast("int"))
    .groupBy("owner_user_sk")
    .agg(F.sum("cash_base").alias("total_cash_base"), F.sum("fx_missing").alias("cash_missing_fx"))
)
position_values_detail = (
    positions.join(
        latest_quotes.select(
            "security_sk",
            F.col("price").cast(DecimalType(20, 8)).alias("latest_price"),
            F.col("currency").alias("price_currency"),
        ),
        "security_sk",
        "left",
    )
    .join(
        current_securities.select(
            "security_sk", "gics_sector", "country",
            F.col("currency").alias("security_currency"),
        ),
        "security_sk",
        "left",
    )
    .join(usd_fx, F.col("price_currency") == F.col("usd_fx.currency"), "left")
    .withColumn(
        "position_usd_value",
        F.when(
            F.col("price_currency") == "USD",
            F.col("quantity") * F.col("latest_price"),
        ).otherwise(
            (F.col("quantity") * F.col("latest_price")) / F.col("usd_to_currency")
        ),
    )
    .withColumn("price_missing", F.col("latest_price").isNull().cast("int"))
    .withColumn(
        "position_missing_fx",
        (
            F.col("latest_price").isNotNull()
            & (F.col("price_currency") != "USD")
            & F.col("usd_to_currency").isNull()
        ).cast("int"),
    )
)
position_values = (
    position_values_detail.groupBy("owner_user_sk")
    .agg(
        F.sum("position_usd_value").alias("total_stocks_base"),
        F.sum("price_missing").alias("missing_prices"),
        F.sum("position_missing_fx").alias("position_missing_fx"),
    )
)
owners = ledger_asof.select("owner_user_sk").distinct()
valuation = (
    owners.join(cash_usd, "owner_user_sk", "left")
    .join(position_values, "owner_user_sk", "left")
    .fillna({"total_cash_base": 0, "total_stocks_base": 0, "cash_missing_fx": 0, "missing_prices": 0, "position_missing_fx": 0})
    .withColumn("missing_fx", F.col("cash_missing_fx") + F.col("position_missing_fx"))
    .withColumn("coverage_complete", (F.col("missing_prices") == 0) & (F.col("missing_fx") == 0))
    .withColumn(
        "total_value_base",
        F.when(F.col("coverage_complete"), F.col("total_cash_base") + F.col("total_stocks_base")),
    )
    .withColumn(
        "cash_weight",
        F.when(F.col("total_value_base") != 0, F.col("total_cash_base") / F.col("total_value_base")),
    )
    .withColumn("valuation_date", F.to_date(F.lit(to_date)))
    .withColumn("base_currency", F.lit("USD"))
    .withColumn("knowledge_date", F.to_date(F.lit(to_date)))
    .select(
        "owner_user_sk", "valuation_date", "base_currency", "total_cash_base",
        "total_stocks_base", "total_value_base", "cash_weight", "missing_prices",
        "missing_fx", "coverage_complete", "knowledge_date",
    )
)
_merge_all(
    "fact_portfolio_valuation",
    valuation,
    "t.owner_user_sk = s.owner_user_sk AND t.valuation_date = s.valuation_date AND t.base_currency = s.base_currency",
)

positions_enriched = (
    position_values_detail.alias("p")
    .join(
        valuation.select("owner_user_sk", "total_value_base").alias("v"),
        "owner_user_sk",
        "left",
    )
    .withColumn("market_value_base", F.col("position_usd_value").cast(DecimalType(20, 2)))
    .withColumn(
        "position_weight",
        F.when(
            F.col("v.total_value_base").isNotNull() & (F.col("v.total_value_base") != 0),
            F.col("position_usd_value") / F.col("v.total_value_base"),
        ).cast(DecimalType(12, 8)),
    )
    .select(
        "owner_user_sk", "account_id", "security_sk", "ticker",
        "security_currency", "gics_sector", "country", "quantity",
        "market_value_base", "position_weight", "event_date", "knowledge_date",
    )
)
DeltaTable.forName(spark, "fact_portfolio_position").delete("true")
if not positions_enriched.isEmpty():
    positions_enriched.write.mode("append").saveAsTable("fact_portfolio_position")

if portfolio_bronze_exists:
    completed_position_count = positions_enriched.count()
    completed_valuation_count = valuation.count()
    completed_manifest = spark.sql(f"""
        SELECT
            '{selected_snapshot_id}' AS snapshot_id,
            DATE('{latest_manifest.snapshot_date_parsed}') AS snapshot_date,
            'completed' AS status,
            CAST({expected_transaction_count} AS BIGINT) AS transaction_count,
            CAST({completed_position_count} AS BIGINT) AS position_count,
            CAST({completed_valuation_count} AS BIGINT) AS valuation_count,
            current_timestamp() AS completed_at
    """)
    DeltaTable.forName(spark, "portfolio_snapshot_manifest").delete("true")
    completed_manifest.write.mode("append").saveAsTable("portfolio_snapshot_manifest")

summary = {
    "from_date": from_date,
    "to_date": to_date,
    "security_documents": security_by_sk.count() + security_by_ticker.count() + security_by_isin.count(),
    "quote_documents": latest_quotes_by_security.count() + latest_quotes_by_ticker.count(),
    "price_history_documents": price_histories_by_security.count() + price_histories_by_ticker.count(),
    "fx_documents": latest_fx.count() + dated_fx.count(),
    "score_documents": latest_scores.count(),
    "transactions": spark.table("silver_portfolio_transaction").count(),
    "positions": spark.table("fact_portfolio_position").count(),
    "valuations": valuation.count(),
    "incomplete_valuations": valuation.filter(~F.col("coverage_complete")).count(),
}
print(summary)
mssparkutils.notebook.exit(str(summary))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
