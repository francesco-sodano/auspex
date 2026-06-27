# Fabric Notebook: nb_03_silver_to_gold
# Reads E4 silver Delta tables and writes the first E5 gold star-schema tables.
# Attaches to: auspex_bronze (default lakehouse)
#
# Current implemented source coverage:
# - silver_insider_txn -> dim_entity, fact_insider_txn
# - silver_prices      -> dim_date, fact_market_daily
# - dim_security       -> reused as the conformed security dimension

# COMMAND ----------
from delta.tables import DeltaTable
from decimal import Decimal
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql.types import (
    BooleanType, DateType, DecimalType, IntegerType, LongType,
    StringType, StructField, StructType,
)

# COMMAND ----------
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
    (
        DeltaTable.forName(spark, table_name)
        .alias("t")
        .merge(source_df.alias("s"), condition)
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )
    print(f"Merged {source_df.count()} rows into {table_name}")


for required in ["dim_security", "silver_insider_txn", "silver_prices"]:
    _require_table(required)

# COMMAND ----------
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

# COMMAND ----------
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

# COMMAND ----------
# --- Build dim_date from silver fact dates ---
date_df = (
    spark.sql("""
        SELECT date AS cal_date FROM silver_prices WHERE date IS NOT NULL
        UNION
        SELECT event_date AS cal_date FROM silver_prices WHERE event_date IS NOT NULL
        UNION
        SELECT knowledge_date AS cal_date FROM silver_prices WHERE knowledge_date IS NOT NULL
        UNION
        SELECT event_date AS cal_date FROM silver_insider_txn WHERE event_date IS NOT NULL
        UNION
        SELECT knowledge_date AS cal_date FROM silver_insider_txn WHERE knowledge_date IS NOT NULL
    """)
    .dropDuplicates(["cal_date"])
    .withColumn("date_sk", _date_sk("cal_date"))
    .withColumn("year", F.year("cal_date"))
    .withColumn("quarter", F.quarter("cal_date"))
    .withColumn("month", F.month("cal_date"))
    .withColumn("day", F.dayofmonth("cal_date"))
    .withColumn("is_trading_day", F.dayofweek("cal_date").between(2, 6))
    .withColumn("fiscal_quarter", F.concat(F.year("cal_date").cast("string"), F.lit("Q"), F.quarter("cal_date").cast("string")))
    .select("date_sk", "cal_date", "year", "quarter", "month", "day", "is_trading_day", "fiscal_quarter")
)
_merge_all("dim_date", date_df, "t.date_sk = s.date_sk")

# COMMAND ----------
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

# COMMAND ----------
# --- Gold facts: implemented source tables ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_market_daily (
        security_sk    BIGINT NOT NULL,
        date_sk        INT    NOT NULL,
        open           DECIMAL(18,6),
        high           DECIMAL(18,6),
        low            DECIMAL(18,6),
        close          DECIMAL(18,6),
        adj_close      DECIMAL(18,6),
        volume         BIGINT,
        ret_1d         DECIMAL(12,8),
        source_sk      INT,
        event_date     DATE,
        knowledge_date DATE
    )
    USING DELTA
""")

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
        .filter(F.col("event_date").isNull() | F.col("knowledge_date").isNull())
        .count()
    )
    if bad_pit_rows:
        DeltaTable.forName(spark, fact_table).delete("event_date IS NULL OR knowledge_date IS NULL")
        print(f"Removed {bad_pit_rows} legacy {fact_table} rows with missing PIT fields")

# COMMAND ----------
# --- Load fact_market_daily from silver_prices ---
price_window = Window.partitionBy("security_sk").orderBy("date")
market_df = (
    spark.table("silver_prices")
    .filter(F.col("event_date").isNotNull() & F.col("knowledge_date").isNotNull())
    .withColumn("prev_close", F.lag("close").over(price_window))
    .withColumn(
        "ret_1d",
        F.when(
            F.col("prev_close").isNotNull() & (F.col("prev_close") > 0),
            (F.col("close").cast("double") / F.col("prev_close").cast("double")) - F.lit(1.0),
        ).cast(DecimalType(12, 8)),
    )
    .withColumn("date_sk", F.date_format("date", "yyyyMMdd").cast(IntegerType()))
    .withColumn("source_sk", F.lit(2))
    .select(
        "security_sk", "date_sk", "open", "high", "low", "close", "adj_close",
        "volume", "ret_1d", "source_sk", "event_date", "knowledge_date",
    )
    .dropDuplicates(["security_sk", "date_sk"])
)
_merge_all("fact_market_daily", market_df, "t.security_sk = s.security_sk AND t.date_sk = s.date_sk")

# COMMAND ----------
# --- Load fact_insider_txn from silver_insider_txn ---
entity_lookup = spark.table("dim_entity").select("entity_sk", "entity_natural_id")
insider_source = (
    spark.table("silver_insider_txn")
    .filter(F.col("event_date").isNotNull() & F.col("knowledge_date").isNotNull())
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

# COMMAND ----------
# --- E5 validation summary ---
for table_name in [
    "dim_security", "dim_date", "dim_source", "dim_entity",
    "fact_market_daily", "fact_insider_txn",
    "fact_institutional_holding", "fact_ownership_event", "fact_news_sentiment",
    "fact_contract_award", "fact_macro", "fact_fx_rate",
]:
    print(f"{table_name}: {spark.table(table_name).count()} rows")

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
        SELECT COUNT(*) AS n FROM fact_market_daily WHERE event_date IS NULL OR knowledge_date IS NULL
        UNION ALL
        SELECT COUNT(*) AS n FROM fact_insider_txn WHERE event_date IS NULL OR knowledge_date IS NULL
    ) x
""").collect()[0].n

print(f"E5 validation: orphan_market={orphan_market}, orphan_insider={orphan_insider}, missing_pit={pit_missing}")
if orphan_market or orphan_insider or pit_missing:
    raise RuntimeError(
        f"E5 validation failed: orphan_market={orphan_market}, "
        f"orphan_insider={orphan_insider}, missing_pit={pit_missing}"
    )