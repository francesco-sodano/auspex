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

# CELL ********************

# Fabric Notebook: nb_05_alpha_vantage_to_gold
# Reads Alpha Vantage E8 bronze payloads and writes gold fundamentals, news,
# macro risk-free, FX, institutional holding, and ETF theme-membership facts.
# Attaches to: auspex_bronze (default lakehouse)

from datetime import date, timedelta
from delta.tables import DeltaTable
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType, DecimalType, IntegerType, LongType,
    MapType, StringType, StructField, StructType,
)


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Parameters ---
def _widget(name, default):
    try:
        return mssparkutils.widgets.get(name)
    except Exception:
        return default


_today = date.today().isoformat()
from_date = _widget("from_date", (date.today() - timedelta(days=7)).isoformat())
to_date = _widget("to_date", _today)


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
        raise RuntimeError(f"Required upstream table is missing: {table_name}")


def _date_paths(source: str):
    d = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    paths = []
    while d <= end:
        paths.append(f"Files/bronze/{source}/{d.year}/{d.month:02d}/{d.day:02d}/*.ndjson")
        d += timedelta(days=1)
    return paths


def _existing_paths(paths):
    result = []
    for p in paths:
        try:
            mssparkutils.fs.ls(p.rsplit("/", 1)[0])
            result.append(p)
        except Exception:
            pass
    return result


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


for required in ["dim_security", "dim_date", "dim_source"]:
    _require_table(required)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Read Alpha Vantage, ETF, and Finnhub news bronze records ---
av_paths = _existing_paths(_date_paths("alpha_vantage") + _date_paths("etf_holdings"))
news_paths = _existing_paths(_date_paths("news"))
paths = av_paths + news_paths
if not paths:
    raise RuntimeError("No Alpha Vantage/ETF/news bronze files found in window")

raw_lines = spark.read.text(paths).select(F.col("value").alias("raw_json"))
raw = raw_lines.select(
    F.get_json_object("raw_json", "$.source_id").alias("source_id"),
    F.get_json_object("raw_json", "$.batch_id").alias("batch_id"),
    F.to_timestamp(F.get_json_object("raw_json", "$.ingest_ts")).alias("ingest_ts"),
    F.get_json_object("raw_json", "$.record.function").alias("function"),
    F.get_json_object("raw_json", "$.record.context.symbol").alias("symbol"),
    F.get_json_object("raw_json", "$.record.context.maturity").alias("maturity"),
    F.get_json_object("raw_json", "$.record.context.ccy_pair").alias("ccy_pair"),
    F.to_timestamp(F.get_json_object("raw_json", "$.record.fetched_at")).alias("fetched_at"),
    F.get_json_object("raw_json", "$.record.payload").alias("payload_json"),
    F.upper(F.get_json_object("raw_json", "$.record.symbol")).alias("finnhub_symbol"),
    F.get_json_object("raw_json", "$.record.article").alias("article_json"),
)
print(f"Alpha Vantage bronze records: {raw.count()}")

security_lookup = (
    spark.table("dim_security")
    .filter(F.col("is_current") == True)
    .select("security_sk", F.upper("ticker").alias("symbol"), "gics_sector")
)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Source dimension upsert ---
source_schema = StructType([
    StructField("source_sk", IntegerType(), False),
    StructField("source_id", StringType(), False),
    StructField("source_type", StringType(), True),
    StructField("latency_class", StringType(), True),
    StructField("reliability_weight", DecimalType(3, 2), True),
    StructField("source_class", StringType(), True),
])
source_rows = [
    (3, "alpha_vantage", "fundamental", "daily", None, "provider_api"),
    (4, "news", "news", "daily", None, "provider_api"),
    (5, "etf_holdings", "etf", "weekly", None, "provider_api"),
    (6, "contracts", "contract", "weekly", None, "public_official"),
    (7, "sec_13f", "filing", "quarterly", None, "public_official"),
    (8, "sec_13dg", "filing", "daily", None, "public_official"),
    (9, "sec_8k", "filing", "daily", None, "public_official"),
    (10, "sec_s1", "filing", "daily", None, "public_official"),
]
source_df = spark.createDataFrame(source_rows, source_schema)
_merge_all("dim_source", source_df, "t.source_sk = s.source_sk")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- fact_fundamentals ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_fundamentals (
        security_sk BIGINT NOT NULL, date_sk INT NOT NULL,
        currency STRING, sector STRING, industry STRING,
        market_cap DECIMAL(20,2), ebitda DECIMAL(20,2), pe_ratio DECIMAL(18,6), peg_ratio DECIMAL(18,6),
        ps_ratio DECIMAL(18,6), ev_ebitda DECIMAL(18,6), gross_profit_ttm DECIMAL(20,2),
        profit_margin DECIMAL(18,6), rev_growth_yoy DECIMAL(18,6),
        cash_and_equivalents DECIMAL(20,2), total_debt DECIMAL(20,2),
        operating_cashflow DECIMAL(20,2), capital_expenditures DECIMAL(20,2),
        fcf_yield DECIMAL(18,6), net_debt_to_ebitda DECIMAL(18,6),
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")

overview = (
    raw.filter(F.col("function") == "OVERVIEW")
    .withColumn("payload", F.from_json("payload_json", MapType(StringType(), StringType())))
    .select(
        F.upper("symbol").alias("symbol"),
        F.to_date("fetched_at").alias("event_date"),
        F.to_date("fetched_at").alias("knowledge_date"),
        F.element_at("payload", "Currency").alias("currency"),
        F.element_at("payload", "Sector").alias("sector"),
        F.element_at("payload", "Industry").alias("industry"),
        F.element_at("payload", "MarketCapitalization").cast(DecimalType(20, 2)).alias("market_cap"),
        F.element_at("payload", "EBITDA").cast(DecimalType(20, 2)).alias("ebitda"),
        F.element_at("payload", "PERatio").cast(DecimalType(18, 6)).alias("pe_ratio"),
        F.element_at("payload", "PEGRatio").cast(DecimalType(18, 6)).alias("peg_ratio"),
        F.element_at("payload", "PriceToSalesRatioTTM").cast(DecimalType(18, 6)).alias("ps_ratio"),
        F.element_at("payload", "EVToEBITDA").cast(DecimalType(18, 6)).alias("ev_ebitda"),
        F.element_at("payload", "GrossProfitTTM").cast(DecimalType(20, 2)).alias("gross_profit_ttm"),
        F.element_at("payload", "ProfitMargin").cast(DecimalType(18, 6)).alias("profit_margin"),
        F.element_at("payload", "QuarterlyRevenueGrowthYOY").cast(DecimalType(18, 6)).alias("rev_growth_yoy"),
    )
)

balance = (
    raw.filter(F.col("function") == "BALANCE_SHEET")
    .select(
        F.upper("symbol").alias("symbol"),
        F.coalesce(F.get_json_object("payload_json", "$.quarterlyReports[0].fiscalDateEnding"), F.to_date("fetched_at").cast("string")).alias("fiscal_date_ending"),
        F.get_json_object("payload_json", "$.quarterlyReports[0].cashAndCashEquivalentsAtCarryingValue").cast(DecimalType(20, 2)).alias("cash_and_equivalents"),
        F.coalesce(
            F.get_json_object("payload_json", "$.quarterlyReports[0].shortLongTermDebtTotal").cast(DecimalType(20, 2)),
            F.get_json_object("payload_json", "$.quarterlyReports[0].shortTermDebt").cast(DecimalType(20, 2))
            + F.get_json_object("payload_json", "$.quarterlyReports[0].longTermDebt").cast(DecimalType(20, 2)),
        ).alias("total_debt"),
    )
)

cashflow = (
    raw.filter(F.col("function") == "CASH_FLOW")
    .select(
        F.upper("symbol").alias("symbol"),
        F.coalesce(F.get_json_object("payload_json", "$.quarterlyReports[0].fiscalDateEnding"), F.to_date("fetched_at").cast("string")).alias("fiscal_date_ending"),
        F.get_json_object("payload_json", "$.quarterlyReports[0].operatingCashflow").cast(DecimalType(20, 2)).alias("operating_cashflow"),
        F.get_json_object("payload_json", "$.quarterlyReports[0].capitalExpenditures").cast(DecimalType(20, 2)).alias("capital_expenditures"),
    )
)

fundamentals_df = (
    overview
    .join(security_lookup, "symbol", "inner")
    .join(balance, "symbol", "left")
    .join(cashflow, "symbol", "left")
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("source_sk", F.lit(3))
    .withColumn(
        "fcf_yield",
        F.when(F.col("market_cap") > 0, (F.col("operating_cashflow") + F.col("capital_expenditures")) / F.col("market_cap")).cast(DecimalType(18, 6)),
    )
    .withColumn(
        "net_debt_to_ebitda",
        F.when(F.col("ebitda") != 0, (F.col("total_debt") - F.col("cash_and_equivalents")) / F.col("ebitda")).cast(DecimalType(18, 6)),
    )
    .select(
        "security_sk", "date_sk", "currency", "sector", "industry", "market_cap", "ebitda",
        "pe_ratio", "peg_ratio", "ps_ratio", "ev_ebitda", "gross_profit_ttm", "profit_margin",
        "rev_growth_yoy", "cash_and_equivalents", "total_debt", "operating_cashflow",
        "capital_expenditures", "fcf_yield", "net_debt_to_ebitda", "source_sk", "event_date", "knowledge_date",
    )
    .dropDuplicates(["security_sk", "date_sk"])
)
if fundamentals_df.count():
    _merge_all("fact_fundamentals", fundamentals_df, "t.security_sk = s.security_sk AND t.date_sk = s.date_sk")

spark.sql("""
    CREATE TABLE IF NOT EXISTS v_fundamentals_latest
    USING DELTA AS
    SELECT * FROM fact_fundamentals WHERE 1 = 0
""")
DeltaTable.forName(spark, "v_fundamentals_latest").delete("1 = 1")
latest_fundamentals_df = spark.sql("""
    SELECT f.*
    FROM fact_fundamentals f
    JOIN (
        SELECT security_sk, MAX(date_sk) AS date_sk
        FROM fact_fundamentals
        GROUP BY security_sk
    ) latest
      ON f.security_sk = latest.security_sk AND f.date_sk = latest.date_sk
""")
if latest_fundamentals_df.count():
    latest_fundamentals_df.write.format("delta").mode("append").saveAsTable("v_fundamentals_latest")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- fact_company_news + fact_news_sentiment ---
spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_company_news (
        news_sk BIGINT NOT NULL, security_sk BIGINT NOT NULL, date_sk INT NOT NULL,
        title STRING, summary STRING, url STRING, source STRING,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")

news_payload_schema = StructType([
    StructField("feed", ArrayType(StructType([
        StructField("title", StringType()),
        StructField("url", StringType()),
        StructField("source", StringType()),
        StructField("summary", StringType()),
        StructField("time_published", StringType()),
        StructField("overall_sentiment_score", StringType()),
        StructField("ticker_sentiment", ArrayType(MapType(StringType(), StringType()))),
    ])))
])

av_news = (
    raw.filter(F.col("function") == "NEWS_SENTIMENT")
    .withColumn("payload", F.from_json("payload_json", news_payload_schema))
    .withColumn("article", F.explode_outer("payload.feed"))
    .withColumn("ticker_sentiment", F.explode_outer("article.ticker_sentiment"))
    .filter((F.upper(F.element_at("ticker_sentiment", "ticker")) == F.upper(F.col("symbol"))) | F.col("ticker_sentiment").isNull())
    .select(
        F.upper("symbol").alias("symbol"),
        F.col("article.title").alias("title"),
        F.col("article.summary").alias("summary"),
        F.col("article.url").alias("url"),
        F.col("article.source").alias("source"),
        F.coalesce(F.to_date(F.to_timestamp("article.time_published", "yyyyMMdd'T'HHmmss")), F.to_date("fetched_at")).alias("event_date"),
        F.to_date("fetched_at").alias("knowledge_date"),
        F.coalesce(F.element_at("ticker_sentiment", "ticker_sentiment_score"), F.col("article.overall_sentiment_score")).cast(DecimalType(5, 4)).alias("sentiment"),
        F.element_at("ticker_sentiment", "relevance_score").cast(DecimalType(5, 4)).alias("relevance"),
    )
    .filter(F.col("url").isNotNull())
    .join(security_lookup, "symbol", "inner")
    .withColumn("news_sk", _positive_sk(F.lit("alpha_vantage"), F.col("url"), F.col("event_date").cast("string")))
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("source_sk", F.lit(3))
    .dropDuplicates(["news_sk", "security_sk"])
)
finnhub_news = (
    raw.filter(F.col("source_id") == "news")
    .select(
        F.col("finnhub_symbol").alias("symbol"),
        F.get_json_object("article_json", "$.headline").alias("title"),
        F.get_json_object("article_json", "$.summary").alias("summary"),
        F.get_json_object("article_json", "$.url").alias("url"),
        F.get_json_object("article_json", "$.source").alias("source"),
        F.to_date(F.from_unixtime(F.get_json_object("article_json", "$.datetime").cast("long"))).alias("event_date"),
        F.to_date("ingest_ts").alias("knowledge_date"),
    )
    .filter(F.col("url").isNotNull())
    .join(security_lookup, "symbol", "inner")
    .withColumn("news_sk", _positive_sk(F.lit("finnhub"), F.col("url"), F.col("event_date").cast("string")))
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("source_sk", F.lit(4))
    .withColumn("sentiment", F.lit(None).cast(DecimalType(5, 4)))
    .withColumn("relevance", F.lit(None).cast(DecimalType(5, 4)))
    .dropDuplicates(["news_sk", "security_sk"])
)
news = av_news.unionByName(finnhub_news.select(av_news.columns), allowMissingColumns=True)
company_news_df = news.select("news_sk", "security_sk", "date_sk", "title", "summary", "url", "source", "source_sk", "event_date", "knowledge_date")
if company_news_df.count():
    _merge_all("fact_company_news", company_news_df, "t.news_sk = s.news_sk AND t.security_sk = s.security_sk")

news_sentiment_df = (
    news
    .withColumn("title_hash", F.sha2(F.concat_ws("|", F.col("title"), F.col("url")), 256))
    .select("news_sk", "security_sk", "date_sk", "sentiment", "relevance", "title_hash", "url", "source_sk", "event_date", "knowledge_date")
)
if news_sentiment_df.count():
    _merge_all("fact_news_sentiment", news_sentiment_df, "t.news_sk = s.news_sk AND t.security_sk = s.security_sk")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- fact_macro + fact_fx_rate ---
macro_schema = StructType([StructField("data", ArrayType(MapType(StringType(), StringType())))])
macro_df = (
    raw.filter(F.col("function") == "TREASURY_YIELD")
    .withColumn("payload", F.from_json("payload_json", macro_schema))
    .withColumn("point", F.explode_outer("payload.data"))
    .select(
        F.concat(F.lit("US_TREASURY_"), F.upper(F.coalesce(F.col("maturity"), F.lit("3month")))).alias("indicator_code"),
        F.to_date(F.element_at("point", "date")).alias("event_date"),
        F.element_at("point", "value").cast(DecimalType(20, 6)).alias("value"),
        F.to_date("fetched_at").alias("knowledge_date"),
    )
    .filter(F.col("event_date").isNotNull() & F.col("value").isNotNull())
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("source_sk", F.lit(3))
    .select("indicator_code", "date_sk", "value", "source_sk", "event_date", "knowledge_date")
    .dropDuplicates(["indicator_code", "date_sk"])
)
if macro_df.count():
    _merge_all("fact_macro", macro_df, "t.indicator_code = s.indicator_code AND t.date_sk = s.date_sk")

fx_df = (
    raw.filter(F.col("function") == "CURRENCY_EXCHANGE_RATE")
    .select(
        F.coalesce(F.col("ccy_pair"), F.lit("USDCHF")).alias("ccy_pair"),
        F.to_date(F.coalesce(
            F.get_json_object("payload_json", "$['Realtime Currency Exchange Rate']['6. Last Refreshed']"),
            F.to_date("fetched_at").cast("string"),
        )).alias("event_date"),
        F.get_json_object("payload_json", "$['Realtime Currency Exchange Rate']['5. Exchange Rate']").cast(DecimalType(18, 8)).alias("rate"),
        F.to_date("fetched_at").alias("knowledge_date"),
    )
    .filter(F.col("rate").isNotNull())
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("source_sk", F.lit(3))
    .select("ccy_pair", "date_sk", "rate", "source_sk", "event_date", "knowledge_date")
    .dropDuplicates(["ccy_pair", "date_sk"])
)
if fx_df.count():
    _merge_all("fact_fx_rate", fx_df, "t.ccy_pair = s.ccy_pair AND t.date_sk = s.date_sk")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Institutional holdings and ETF theme membership ---
holdings_schema = StructType([StructField("data", ArrayType(MapType(StringType(), StringType())))])
inst_df = (
    raw.filter(F.col("function") == "INSTITUTIONAL_HOLDINGS")
    .withColumn("payload", F.from_json("payload_json", holdings_schema))
    .withColumn("holding", F.explode_outer("payload.data"))
    .select(
        F.upper("symbol").alias("symbol"),
        F.coalesce(F.to_date(F.element_at("holding", "date_reported")), F.to_date("fetched_at")).alias("event_date"),
        F.element_at("holding", "holder").alias("holder"),
        F.element_at("holding", "shares").cast(DecimalType(20, 4)).alias("shares"),
        F.element_at("holding", "value").cast(DecimalType(20, 2)).alias("value_usd"),
        F.to_date("fetched_at").alias("knowledge_date"),
    )
    .filter(F.col("holder").isNotNull())
    .join(security_lookup, "symbol", "inner")
    .withColumn("entity_sk", _positive_sk(F.lit("institution"), F.col("holder")))
    .withColumn("date_sk", _date_sk("event_date"))
    .withColumn("shares_delta_qoq", F.lit(None).cast(DecimalType(20, 4)))
    .withColumn("pct_of_portfolio", F.lit(None).cast(DecimalType(9, 6)))
    .withColumn("accession_no", F.sha2(F.concat_ws("|", F.lit("av_inst"), F.col("symbol"), F.col("holder"), F.col("event_date")), 256))
    .withColumn("accession_no", F.substring(F.col("accession_no"), 1, 25))
    .withColumn("source_sk", F.lit(3))
    .select(
        "security_sk", "entity_sk", "date_sk", "shares", "value_usd", "shares_delta_qoq",
        "pct_of_portfolio", "accession_no", "source_sk", "event_date", "knowledge_date",
    )
    .dropDuplicates(["accession_no"])
)
if inst_df.count():
    _merge_all("fact_institutional_holding", inst_df, "t.accession_no = s.accession_no")

spark.sql("""
    CREATE TABLE IF NOT EXISTS fact_theme_membership (
        theme_id STRING NOT NULL, etf_symbol STRING NOT NULL, security_sk BIGINT NOT NULL,
        weight DECIMAL(9,6), is_ground_truth BOOLEAN,
        source_sk INT, event_date DATE, knowledge_date DATE
    )
    USING DELTA
""")

etf_schema = StructType([StructField("holdings", ArrayType(MapType(StringType(), StringType())))])
etf_df = (
    raw.filter(F.col("function") == "ETF_PROFILE")
    .withColumn("payload", F.from_json("payload_json", etf_schema))
    .withColumn("holding", F.explode_outer("payload.holdings"))
    .select(
        F.concat(F.lit("etf:"), F.upper("symbol")).alias("theme_id"),
        F.upper("symbol").alias("etf_symbol"),
        F.upper(F.element_at("holding", "symbol")).alias("symbol"),
        F.element_at("holding", "weight").cast(DecimalType(9, 6)).alias("weight"),
        F.to_date("fetched_at").alias("event_date"),
        F.to_date("fetched_at").alias("knowledge_date"),
    )
    .filter(F.col("symbol").isNotNull())
    .join(security_lookup, "symbol", "inner")
    .withColumn("is_ground_truth", F.lit(True))
    .withColumn("source_sk", F.lit(5))
    .select("theme_id", "etf_symbol", "security_sk", "weight", "is_ground_truth", "source_sk", "event_date", "knowledge_date")
    .dropDuplicates(["theme_id", "security_sk"])
)
if etf_df.count():
    _merge_all("fact_theme_membership", etf_df, "t.theme_id = s.theme_id AND t.security_sk = s.security_sk")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# --- Validation summary ---
for table_name in [
    "fact_fundamentals", "fact_company_news", "fact_news_sentiment", "fact_macro",
    "fact_fx_rate", "fact_institutional_holding", "fact_theme_membership",
]:
    print(f"{table_name}: {spark.table(table_name).count()} rows")

missing_pit = spark.sql("""
    SELECT SUM(n) AS n
    FROM (
        SELECT COUNT(*) AS n FROM fact_fundamentals WHERE event_date IS NULL OR knowledge_date IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_company_news WHERE event_date IS NULL OR knowledge_date IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_news_sentiment WHERE event_date IS NULL OR knowledge_date IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_macro WHERE event_date IS NULL OR knowledge_date IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_fx_rate WHERE event_date IS NULL OR knowledge_date IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_institutional_holding WHERE event_date IS NULL OR knowledge_date IS NULL
        UNION ALL SELECT COUNT(*) AS n FROM fact_theme_membership WHERE event_date IS NULL OR knowledge_date IS NULL
    ) x
""").collect()[0].n
print(f"E8 Alpha Vantage validation: missing_pit={missing_pit}")
if missing_pit:
    raise RuntimeError(f"E8 Alpha Vantage validation failed: missing_pit={missing_pit}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
