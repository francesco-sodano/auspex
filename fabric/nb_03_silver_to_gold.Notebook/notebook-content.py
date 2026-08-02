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

# Fabric Notebook: nb_03_silver_to_gold
# Reads E4 silver Delta tables and writes the first E5 gold star-schema tables.
# Attaches to: auspex_bronze (default lakehouse)
#
# Current implemented source coverage:
# - silver_insider_txn -> dim_entity, fact_insider_txn
# - silver_prices      -> dim_date, fact_market_daily
# - dim_security       -> reused as the conformed security dimension

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from delta.tables import DeltaTable
from decimal import Decimal
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql.types import (
    BooleanType, DateType, DecimalType, IntegerType, LongType,
    StringType, StructField, StructType,
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Helpers ---
_MAX_BIGINT = 9223372036854775807


def _require_table(table_name: str) -> None:
    if not spark.catalog.tableExists(table_name):
        raise RuntimeError(f"Required E4 table is missing: {table_name}")


def _ensure_columns(table_name: str, column_specs: dict[str, str]) -> None:
    existing = set(spark.table(table_name).columns)
    for column_name, ddl in column_specs.items():
        if column_name not in existing:
            spark.sql(f"ALTER TABLE {table_name} ADD COLUMNS ({ddl})")


def _positive_sk(*cols):
    return F.pmod(F.xxhash64(F.concat_ws("|", *cols)), F.lit(_MAX_BIGINT)).cast(LongType())


def _date_sk(col_name: str):
    return F.date_format(F.col(col_name), "yyyyMMdd").cast(IntegerType())


def _merge_all(table_name: str, source_df, condition: str) -> None:
    target = DeltaTable.forName(spark, table_name)
    (
        target
        .alias("t")
        .merge(source_df.alias("s"), condition)
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )
    metrics = target.history(1).select("operationMetrics").first().operationMetrics or {}
    print(f"Merged source_rows={metrics.get('numSourceRows', 'unknown')} into {table_name}")


def _ensure_not_null_constraints(table_name: str, column_names: list[str]) -> None:
    properties = {
        row.key for row in spark.sql(f"SHOW TBLPROPERTIES {table_name}").collect()
    }
    for column_name in column_names:
        constraint_name = f"{table_name}_{column_name}_not_null"
        property_name = f"delta.constraints.{constraint_name}"
        if property_name not in properties:
            spark.sql(
                f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} "
                f"CHECK ({column_name} IS NOT NULL)"
            )


for required in ["dim_security", "silver_insider_txn", "silver_prices"]:
    _require_table(required)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Gold dimensions: dim_date, dim_source, dim_entity ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS dim_date (
        date_sk        INT      NOT NULL,
        cal_date       DATE     NOT NULL,
        year           INT,
        quarter        INT,
        month          INT,
        day            INT,
        is_trading_day BOOLEAN,
        fiscal_quarter STRING
    )
    USING DELTA
""")

spark.sql("""
    CREATE TABLE IF NOT EXISTS dim_source (
        source_sk          INT           NOT NULL,
        source_id          STRING        NOT NULL,
        source_type        STRING,
        latency_class      STRING,
        reliability_weight DECIMAL(3,2),
        source_class       STRING
    )
    USING DELTA
""")

spark.sql("""
    CREATE TABLE IF NOT EXISTS dim_entity (
        entity_sk          BIGINT NOT NULL,
        entity_natural_id  STRING NOT NULL,
        entity_type        STRING NOT NULL,
        name               STRING,
        role               STRING,
        cik                STRING
    )
    USING DELTA
""")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Seed dim_source for currently implemented sources ---
source_schema = StructType([
    StructField("source_sk", IntegerType(), False),
    StructField("source_id", StringType(), False),
    StructField("source_type", StringType(), True),
    StructField("latency_class", StringType(), True),
    StructField("reliability_weight", DecimalType(3, 2), True),
    StructField("source_class", StringType(), True),
])
source_rows = [
    (1, "sec_form4", "filing", "daily", Decimal("1.00"), "public_official"),
    (2, "prices_eod", "price", "daily", Decimal("0.85"), "provider_api"),
]
dim_source_df = spark.createDataFrame(source_rows, source_schema)
_merge_all("dim_source", dim_source_df, "t.source_sk = s.source_sk")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Build a continuous conformed calendar across all available facts ---
date_candidates = (
    spark.table("silver_prices").select(F.col("date").alias("cal_date"))
    .unionByName(spark.table("silver_prices").select(F.col("event_date").alias("cal_date")))
    .unionByName(spark.table("silver_prices").select(F.col("knowledge_date").alias("cal_date")))
    .unionByName(spark.table("silver_insider_txn").select(F.col("event_date").alias("cal_date")))
    .unionByName(spark.table("silver_insider_txn").select(F.col("knowledge_date").alias("cal_date")))
)
for fact_table in [
    "fact_market_daily", "fact_insider_txn", "fact_institutional_holding",
    "fact_ownership_event", "fact_news_sentiment", "fact_contract_award",
    "fact_macro", "fact_fx_rate", "fact_fundamentals", "fact_company_news",
    "fact_theme_membership", "fact_material_event", "fact_sec_filing_event",
]:
    if spark.catalog.tableExists(fact_table):
        fact_dates = spark.table(fact_table)
        date_candidates = date_candidates.unionByName(
            fact_dates.select(F.col("event_date").alias("cal_date"))
        ).unionByName(
            fact_dates.select(F.col("knowledge_date").alias("cal_date"))
        )

date_bounds = date_candidates.filter(F.col("cal_date").isNotNull()).agg(
    F.min("cal_date").alias("min_date"),
    F.max("cal_date").alias("max_date"),
).first()
if date_bounds.min_date is None or date_bounds.max_date is None:
    raise RuntimeError("dim_date requires at least one valid fact date")

trading_dates = (
    spark.table("silver_prices")
    .select(F.col("date").alias("cal_date"))
    .filter(F.col("cal_date").isNotNull())
    .distinct()
    .withColumn("is_trading_day", F.lit(True))
)
date_df = (
    spark.range(1)
    .select(F.explode(F.sequence(F.lit(date_bounds.min_date), F.lit(date_bounds.max_date))).alias("cal_date"))
    .join(trading_dates, "cal_date", "left")
    .withColumn("date_sk", _date_sk("cal_date"))
    .withColumn("year", F.year("cal_date"))
    .withColumn("quarter", F.quarter("cal_date"))
    .withColumn("month", F.month("cal_date"))
    .withColumn("day", F.dayofmonth("cal_date"))
    .withColumn("is_trading_day", F.coalesce(F.col("is_trading_day"), F.lit(False)))
    .withColumn("fiscal_quarter", F.concat(F.year("cal_date").cast("string"), F.lit("Q"), F.quarter("cal_date").cast("string")))
    .select("date_sk", "cal_date", "year", "quarter", "month", "day", "is_trading_day", "fiscal_quarter")
)
DeltaTable.forName(spark, "dim_date").delete(
    (F.col("cal_date") < F.lit(date_bounds.min_date))
    | (F.col("cal_date") > F.lit(date_bounds.max_date))
)
_merge_all("dim_date", date_df, "t.date_sk = s.date_sk")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Build dim_entity from Form 4 reporters ---
reporters = spark.table("silver_insider_txn").filter(F.col("reporter_name").isNotNull())
entity_df = (
    reporters
    .withColumn(
        "entity_natural_id",
        F.when(F.col("reporter_cik").isNotNull(), F.concat(F.lit("insider:"), F.col("reporter_cik")))
        .otherwise(F.concat(F.lit("insider_name:"), F.sha2(F.lower(F.col("reporter_name")), 256))),
    )
    .withColumn("entity_sk", _positive_sk(F.col("entity_natural_id")))
    .withColumn("entity_type", F.lit("insider"))
    .withColumn(
        "role",
        F.when(F.col("officer_title").isNotNull(), F.col("officer_title"))
        .when(F.col("is_director") == True, F.lit("Director"))
        .when(F.col("is_ten_pct") == True, F.lit("10% owner"))
        .when(F.col("is_officer") == True, F.lit("Officer"))
        .otherwise(F.lit(None).cast(StringType())),
    )
    .groupBy("entity_sk", "entity_natural_id", "entity_type")
    .agg(
        F.min("reporter_name").alias("name"),
        F.min("role").alias("role"),
        F.min("reporter_cik").alias("cik"),
    )
)
_merge_all("dim_entity", entity_df, "t.entity_sk = s.entity_sk")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Gold facts: implemented source tables ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_market_daily (
        security_sk    BIGINT NOT NULL,
        date_sk        INT    NOT NULL,
        price_revision_hash STRING NOT NULL,
        open           DECIMAL(18,6),
        high           DECIMAL(18,6),
        low            DECIMAL(18,6),
        close          DECIMAL(18,6),
        adj_close      DECIMAL(18,6),
        volume         BIGINT,
        ret_1d         DECIMAL(12,8),
        source_sk      INT,
        event_date     DATE,
        knowledge_date DATE,
        ingest_ts      TIMESTAMP NOT NULL,
        revision_loaded_at TIMESTAMP NOT NULL
    )
    USING DELTA
""")

_ensure_columns("fact_market_daily", {
    "price_revision_hash": "price_revision_hash STRING",
    "ingest_ts": "ingest_ts TIMESTAMP",
    "revision_loaded_at": "revision_loaded_at TIMESTAMP",
})

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_insider_txn (
        insider_txn_sk BIGINT NOT NULL,
        security_sk    BIGINT NOT NULL,
        entity_sk      BIGINT,
        date_sk        INT,
        line_no        INT    NOT NULL,
        txn_code       STRING,
        is_buy         BOOLEAN,
        shares         DECIMAL(20,4),
        price          DECIMAL(18,6),
        value_usd      DECIMAL(20,2),
        shares_after   DECIMAL(20,4),
        accession_no   STRING NOT NULL,
        source_sk      INT,
        event_date     DATE,
        knowledge_date DATE
    )
    USING DELTA
""")

# Empty schema placeholders for E8 facts. They keep the E5 star schema stable;
# source-specific E8 notebooks will populate them later.
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_institutional_holding (
        security_sk BIGINT, entity_sk BIGINT, date_sk INT,
        shares DECIMAL(20,4), value_usd DECIMAL(20,2),
        shares_delta_qoq DECIMAL(20,4), pct_of_portfolio DECIMAL(9,6),
        accession_no STRING NOT NULL,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_ownership_event (
        security_sk BIGINT, entity_sk BIGINT, date_sk INT,
        pct_owned DECIMAL(9,6), filing_type STRING, is_activist BOOLEAN,
        accession_no STRING NOT NULL,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_news_sentiment (
        news_sk BIGINT, security_sk BIGINT, date_sk INT,
        sentiment DECIMAL(5,4), relevance DECIMAL(5,4),
        title_hash STRING NOT NULL, url STRING,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_contract_award (
        award_sk BIGINT, security_sk BIGINT, date_sk INT,
        agency STRING, amount_usd DECIMAL(20,2), description_hash STRING,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_macro (
        indicator_code STRING, date_sk INT, value DECIMAL(20,6),
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_fx_rate (
        ccy_pair STRING NOT NULL, date_sk INT NOT NULL, rate DECIMAL(18,8) NOT NULL,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")

for fact_table in ["fact_market_daily", "fact_insider_txn"]:
    bad_pit_rows = (
        spark.table(fact_table)
        .filter(
            F.col("event_date").isNull()
            | F.col("knowledge_date").isNull()
            | (F.col("event_date") > F.col("knowledge_date"))
        )
        .count()
    )
    if bad_pit_rows:
        DeltaTable.forName(spark, fact_table).delete(
            "event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date"
        )
        print(f"Removed {bad_pit_rows} legacy {fact_table} rows with invalid PIT fields")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Load revision-preserving fact_market_daily from silver_prices ---
market_df = (
    spark.table("silver_prices")
    .filter(F.col("event_date").isNotNull() & F.col("knowledge_date").isNotNull())
    .filter(F.col("price_revision_hash").isNotNull())
    .withColumn("ret_1d", F.lit(None).cast(DecimalType(12, 8)))
    .withColumn("date_sk", F.date_format("date", "yyyyMMdd").cast(IntegerType()))
    .withColumn("source_sk", F.lit(2))
    .select(
        "security_sk", "date_sk", "price_revision_hash", "open", "high", "low",
        "close", "adj_close", "volume", "ret_1d", "source_sk", "event_date",
        "knowledge_date", "ingest_ts", "revision_loaded_at",
    )
)
_merge_all(
    "fact_market_daily",
    market_df,
    "t.security_sk = s.security_sk AND t.date_sk = s.date_sk "
    "AND t.price_revision_hash = s.price_revision_hash",
)

legacy_market_rows = (
    spark.table("fact_market_daily")
    .filter(F.col("price_revision_hash").isNull())
    .count()
)
if legacy_market_rows:
    legacy_market_keys = (
        spark.table("fact_market_daily")
        .filter(F.col("price_revision_hash").isNull())
        .select("security_sk", "date_sk")
        .distinct()
    )
    revisioned_market_keys = (
        spark.table("fact_market_daily")
        .filter(F.col("price_revision_hash").isNotNull())
        .select("security_sk", "date_sk")
        .distinct()
    )
    uncovered_legacy_market_keys = legacy_market_keys.join(
        revisioned_market_keys,
        ["security_sk", "date_sk"],
        "left_anti",
    )
    uncovered_legacy_market_key_count = uncovered_legacy_market_keys.count()
    if uncovered_legacy_market_key_count:
        display(uncovered_legacy_market_keys.limit(100))
        raise RuntimeError(
            "MARKET FACT MIGRATION FAILED: "
            f"uncovered_legacy_keys={uncovered_legacy_market_key_count}"
        )
    DeltaTable.forName(spark, "fact_market_daily").delete("price_revision_hash IS NULL")
    print(
        f"Removed {legacy_market_rows} superseded unversioned fact_market_daily rows "
        "after the revisioned merge completed"
    )

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Load fact_insider_txn from silver_insider_txn ---
entity_lookup = spark.table("dim_entity").select("entity_sk", "entity_natural_id")
insider_source = (
    spark.table("silver_insider_txn")
    .filter(
        F.col("event_date").isNotNull()
        & F.col("knowledge_date").isNotNull()
        & (F.col("event_date") <= F.col("knowledge_date"))
    )
    .withColumn(
        "entity_natural_id",
        F.when(F.col("reporter_cik").isNotNull(), F.concat(F.lit("insider:"), F.col("reporter_cik")))
        .otherwise(F.concat(F.lit("insider_name:"), F.sha2(F.lower(F.col("reporter_name")), 256))),
    )
)

insider_df = (
    insider_source
    .join(entity_lookup, on="entity_natural_id", how="left")
    .withColumn("insider_txn_sk", _positive_sk(F.lit("sec_form4"), F.col("accession_no"), F.col("line_no").cast("string")))
    .withColumn("date_sk", F.date_format("event_date", "yyyyMMdd").cast(IntegerType()))
    .withColumn("source_sk", F.lit(1))
    .select(
        "insider_txn_sk", "security_sk", "entity_sk", "date_sk", "line_no",
        "txn_code", "is_buy", "shares", "price", "value_usd", "shares_after",
        "accession_no", "source_sk", "event_date", "knowledge_date",
    )
    .dropDuplicates(["insider_txn_sk"])
)
_merge_all("fact_insider_txn", insider_df, "t.insider_txn_sk = s.insider_txn_sk")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- E5 validation summary ---
updated_tables = [
    "dim_security", "dim_date", "dim_source", "dim_entity",
    "fact_market_daily", "fact_insider_txn",
    "fact_institutional_holding", "fact_ownership_event", "fact_news_sentiment",
    "fact_contract_award", "fact_macro", "fact_fx_rate",
]
print(f"E5 tables ready: {', '.join(updated_tables)}")

orphan_market = spark.sql("""
    SELECT COUNT(*) AS n
    FROM fact_market_daily f
    LEFT JOIN dim_security s ON f.security_sk = s.security_sk
    WHERE s.security_sk IS NULL
""").collect()[0].n
orphan_insider = spark.sql("""
    SELECT COUNT(*) AS n
    FROM fact_insider_txn f
    LEFT JOIN dim_security s ON f.security_sk = s.security_sk
    WHERE s.security_sk IS NULL
""").collect()[0].n
pit_missing = spark.sql("""
    SELECT SUM(n) AS n
    FROM (
        SELECT COUNT(*) AS n FROM fact_market_daily WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
        UNION ALL
        SELECT COUNT(*) AS n FROM fact_insider_txn WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
    ) x
""").collect()[0].n
market_revision_duplicates = spark.sql("""
    SELECT COUNT(*) AS n
    FROM (
        SELECT security_sk, date_sk, price_revision_hash
        FROM fact_market_daily
        GROUP BY security_sk, date_sk, price_revision_hash
        HAVING COUNT(*) > 1
    ) x
""").collect()[0].n
market_missing_revision_hash = spark.sql("""
    SELECT COUNT(*) AS n
    FROM fact_market_daily
    WHERE price_revision_hash IS NULL
""").collect()[0].n
market_missing_ingest_ts = spark.sql("""
    SELECT COUNT(*) AS n
    FROM fact_market_daily
    WHERE ingest_ts IS NULL
""").collect()[0].n
market_missing_loaded_at = spark.sql("""
    SELECT COUNT(*) AS n
    FROM fact_market_daily
    WHERE revision_loaded_at IS NULL
""").collect()[0].n

print(
    f"E5 validation: orphan_market={orphan_market}, orphan_insider={orphan_insider}, "
    f"missing_pit={pit_missing}, market_revision_duplicates={market_revision_duplicates}, "
    f"market_missing_revision_hash={market_missing_revision_hash}, "
    f"market_missing_ingest_ts={market_missing_ingest_ts}, "
    f"market_missing_revision_loaded_at={market_missing_loaded_at}"
)
if (
    orphan_market
    or orphan_insider
    or pit_missing
    or market_revision_duplicates
    or market_missing_revision_hash
    or market_missing_ingest_ts
    or market_missing_loaded_at
):
    raise RuntimeError(
        f"E5 validation failed: orphan_market={orphan_market}, "
        f"orphan_insider={orphan_insider}, missing_pit={pit_missing}, "
        f"market_revision_duplicates={market_revision_duplicates}, "
        f"market_missing_revision_hash={market_missing_revision_hash}, "
        f"market_missing_ingest_ts={market_missing_ingest_ts}, "
        f"market_missing_revision_loaded_at={market_missing_loaded_at}"
    )

_ensure_not_null_constraints(
    "fact_market_daily",
    ["price_revision_hash", "ingest_ts", "revision_loaded_at"],
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
