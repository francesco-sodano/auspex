-- Auspex E8 gold fact tables and serving views (Fabric Warehouse T-SQL)
-- Mirrors the E8 Lakehouse tables produced by nb_05/06/07.

IF OBJECT_ID('dbo.fact_fundamentals', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_fundamentals (
        security_sk            BIGINT        NOT NULL,
        date_sk                INT           NOT NULL,
        fundamentals_kind      VARCHAR(32)   NOT NULL,
        currency               VARCHAR(3)    NULL,
        sector                 VARCHAR(64)   NULL,
        industry               VARCHAR(128)  NULL,
        market_cap             DECIMAL(20,2) NULL,
        shares_outstanding     DECIMAL(20,4) NULL,
        ebitda                 DECIMAL(20,2) NULL,
        pe_ratio               DECIMAL(18,6) NULL,
        peg_ratio              DECIMAL(18,6) NULL,
        ps_ratio               DECIMAL(18,6) NULL,
        ev_ebitda              DECIMAL(18,6) NULL,
        gross_profit_ttm       DECIMAL(20,2) NULL,
        profit_margin          DECIMAL(18,6) NULL,
        rev_growth_yoy         DECIMAL(18,6) NULL,
        cash_and_equivalents   DECIMAL(20,2) NULL,
        total_debt             DECIMAL(20,2) NULL,
        operating_cashflow     DECIMAL(20,2) NULL,
        capital_expenditures   DECIMAL(20,2) NULL,
        fcf_yield              DECIMAL(18,6) NULL,
        net_debt_to_ebitda     DECIMAL(18,6) NULL,
        fundamentals_revision_hash CHAR(64)  NOT NULL,
        silver_natural_key     VARCHAR(64)   NOT NULL,
        silver_batch_id        VARCHAR(256)  NOT NULL,
        silver_ingest_ts       DATETIME2(6)  NOT NULL,
        silver_source_record_hash CHAR(64)   NOT NULL,
        silver_loaded_at       DATETIME2(6)  NOT NULL,
        source_sk              INT           NULL,
        event_date             DATE          NOT NULL,
        knowledge_date         DATE          NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_fundamentals') AND name = 'shares_outstanding')
    ALTER TABLE dbo.fact_fundamentals ADD shares_outstanding DECIMAL(20,4) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_fundamentals') AND name = 'fundamentals_kind')
    ALTER TABLE dbo.fact_fundamentals ADD fundamentals_kind VARCHAR(32) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_fundamentals') AND name = 'fundamentals_revision_hash')
    ALTER TABLE dbo.fact_fundamentals ADD fundamentals_revision_hash CHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_fundamentals') AND name = 'silver_natural_key')
    ALTER TABLE dbo.fact_fundamentals ADD silver_natural_key VARCHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_fundamentals') AND name = 'silver_batch_id')
    ALTER TABLE dbo.fact_fundamentals ADD silver_batch_id VARCHAR(256) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_fundamentals') AND name = 'silver_ingest_ts')
    ALTER TABLE dbo.fact_fundamentals ADD silver_ingest_ts DATETIME2(6) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_fundamentals') AND name = 'silver_source_record_hash')
    ALTER TABLE dbo.fact_fundamentals ADD silver_source_record_hash CHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_fundamentals') AND name = 'silver_loaded_at')
    ALTER TABLE dbo.fact_fundamentals ADD silver_loaded_at DATETIME2(6) NULL;
GO

IF EXISTS (
    SELECT 1 FROM dbo.fact_fundamentals
    WHERE security_sk IS NULL OR date_sk IS NULL OR fundamentals_kind IS NULL
       OR fundamentals_revision_hash IS NULL OR silver_natural_key IS NULL
       OR silver_batch_id IS NULL OR silver_ingest_ts IS NULL
       OR silver_source_record_hash IS NULL OR silver_loaded_at IS NULL
       OR event_date IS NULL OR knowledge_date IS NULL
)
    THROW 50007, 'fact_fundamentals requires a Silver-backed staged reload before revision provenance can be enforced.', 1;

BEGIN
    BEGIN TRANSACTION;
    DROP TABLE IF EXISTS dbo.fact_fundamentals_revisioned;
    CREATE TABLE dbo.fact_fundamentals_revisioned (
        security_sk            BIGINT        NOT NULL,
        date_sk                INT           NOT NULL,
        fundamentals_kind      VARCHAR(32)   NOT NULL,
        currency               VARCHAR(3)    NULL,
        sector                 VARCHAR(64)   NULL,
        industry               VARCHAR(128)  NULL,
        market_cap             DECIMAL(20,2) NULL,
        shares_outstanding     DECIMAL(20,4) NULL,
        ebitda                 DECIMAL(20,2) NULL,
        pe_ratio               DECIMAL(18,6) NULL,
        peg_ratio              DECIMAL(18,6) NULL,
        ps_ratio               DECIMAL(18,6) NULL,
        ev_ebitda              DECIMAL(18,6) NULL,
        gross_profit_ttm       DECIMAL(20,2) NULL,
        profit_margin          DECIMAL(18,6) NULL,
        rev_growth_yoy         DECIMAL(18,6) NULL,
        cash_and_equivalents   DECIMAL(20,2) NULL,
        total_debt             DECIMAL(20,2) NULL,
        operating_cashflow     DECIMAL(20,2) NULL,
        capital_expenditures   DECIMAL(20,2) NULL,
        fcf_yield              DECIMAL(18,6) NULL,
        net_debt_to_ebitda     DECIMAL(18,6) NULL,
        fundamentals_revision_hash CHAR(64)  NOT NULL,
        silver_natural_key     VARCHAR(64)   NOT NULL,
        silver_batch_id        VARCHAR(256)  NOT NULL,
        silver_ingest_ts       DATETIME2(6)  NOT NULL,
        silver_source_record_hash CHAR(64)   NOT NULL,
        silver_loaded_at       DATETIME2(6)  NOT NULL,
        source_sk              INT           NULL,
        event_date             DATE          NOT NULL,
        knowledge_date         DATE          NOT NULL
    );
    INSERT INTO dbo.fact_fundamentals_revisioned
        SELECT security_sk, date_sk, fundamentals_kind, currency, sector, industry,
            market_cap, shares_outstanding, ebitda, pe_ratio, peg_ratio, ps_ratio, ev_ebitda,
           gross_profit_ttm, profit_margin, rev_growth_yoy, cash_and_equivalents,
           total_debt, operating_cashflow, capital_expenditures, fcf_yield,
           net_debt_to_ebitda, fundamentals_revision_hash, silver_natural_key,
           silver_batch_id, silver_ingest_ts, silver_source_record_hash,
           silver_loaded_at, source_sk, event_date, knowledge_date
    FROM dbo.fact_fundamentals;
    EXEC sp_rename 'dbo.fact_fundamentals', 'fact_fundamentals_legacy';
    EXEC sp_rename 'dbo.fact_fundamentals_revisioned', 'fact_fundamentals';
    DROP TABLE dbo.fact_fundamentals_legacy;
    COMMIT TRANSACTION;
END;
GO

DECLARE @fact_company_news_existed BIT =
    CASE WHEN OBJECT_ID('dbo.fact_company_news', 'U') IS NULL THEN 0 ELSE 1 END;

IF OBJECT_ID('dbo.fact_company_news', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_company_news (
        news_sk        BIGINT         NOT NULL,
        security_sk    BIGINT         NOT NULL,
        date_sk        INT            NOT NULL,
        published_at   DATETIME2(6)   NOT NULL,
        title          VARCHAR(512)   NULL,
        summary        VARCHAR(4000)  NULL,
        url            VARCHAR(1024)  NULL,
        source         VARCHAR(128)   NULL,
        news_revision_hash CHAR(64)   NOT NULL,
        silver_natural_key VARCHAR(64) NOT NULL,
        silver_batch_id VARCHAR(256)  NOT NULL,
        silver_ingest_ts DATETIME2(6) NOT NULL,
        silver_source_record_hash CHAR(64) NOT NULL,
        silver_loaded_at DATETIME2(6) NOT NULL,
        source_sk      INT            NULL,
        event_date     DATE           NOT NULL,
        knowledge_date DATE           NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_company_news') AND name = 'published_at')
    ALTER TABLE dbo.fact_company_news ADD published_at DATETIME2(6) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_company_news') AND name = 'news_revision_hash')
    ALTER TABLE dbo.fact_company_news ADD news_revision_hash CHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_company_news') AND name = 'silver_natural_key')
    ALTER TABLE dbo.fact_company_news ADD silver_natural_key VARCHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_company_news') AND name = 'silver_batch_id')
    ALTER TABLE dbo.fact_company_news ADD silver_batch_id VARCHAR(256) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_company_news') AND name = 'silver_ingest_ts')
    ALTER TABLE dbo.fact_company_news ADD silver_ingest_ts DATETIME2(6) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_company_news') AND name = 'silver_source_record_hash')
    ALTER TABLE dbo.fact_company_news ADD silver_source_record_hash CHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_company_news') AND name = 'silver_loaded_at')
    ALTER TABLE dbo.fact_company_news ADD silver_loaded_at DATETIME2(6) NULL;

IF EXISTS (
    SELECT 1 FROM dbo.fact_company_news
    WHERE news_sk IS NULL OR security_sk IS NULL OR date_sk IS NULL
       OR published_at IS NULL OR news_revision_hash IS NULL
       OR silver_natural_key IS NULL OR silver_batch_id IS NULL
       OR silver_ingest_ts IS NULL OR silver_source_record_hash IS NULL
       OR silver_loaded_at IS NULL OR event_date IS NULL OR knowledge_date IS NULL
)
    THROW 50008, 'fact_company_news requires a Silver-backed staged reload before news revision provenance can be enforced.', 1;

IF @fact_company_news_existed = 1
BEGIN
    BEGIN TRANSACTION;
    DROP TABLE IF EXISTS dbo.fact_company_news_revisioned;
    CREATE TABLE dbo.fact_company_news_revisioned (
        news_sk                   BIGINT         NOT NULL,
        security_sk               BIGINT         NOT NULL,
        date_sk                   INT            NOT NULL,
        published_at              DATETIME2(6)   NOT NULL,
        title                     VARCHAR(512)   NULL,
        summary                   VARCHAR(4000)  NULL,
        url                       VARCHAR(1024)  NULL,
        source                    VARCHAR(128)   NULL,
        news_revision_hash        CHAR(64)       NOT NULL,
        silver_natural_key        VARCHAR(64)    NOT NULL,
        silver_batch_id           VARCHAR(256)   NOT NULL,
        silver_ingest_ts          DATETIME2(6)   NOT NULL,
        silver_source_record_hash CHAR(64)       NOT NULL,
        silver_loaded_at          DATETIME2(6)   NOT NULL,
        source_sk                 INT            NULL,
        event_date                DATE           NOT NULL,
        knowledge_date            DATE           NOT NULL
    );
    INSERT INTO dbo.fact_company_news_revisioned
    SELECT news_sk, security_sk, date_sk, published_at, title, summary, url,
           source, news_revision_hash, silver_natural_key, silver_batch_id,
           silver_ingest_ts, silver_source_record_hash, silver_loaded_at,
           source_sk, event_date, knowledge_date
    FROM dbo.fact_company_news;
    EXEC sp_rename 'dbo.fact_company_news', 'fact_company_news_legacy';
    EXEC sp_rename 'dbo.fact_company_news_revisioned', 'fact_company_news';
    DROP TABLE dbo.fact_company_news_legacy;
    COMMIT TRANSACTION;
END;
GO

IF OBJECT_ID('dbo.fact_theme_membership', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_theme_membership (
        theme_id        VARCHAR(128)  NOT NULL,
        etf_symbol      VARCHAR(16)   NOT NULL,
        security_sk     BIGINT        NOT NULL,
        weight          DECIMAL(9,6)  NULL,
        is_ground_truth BIT           NULL,
        theme_revision_hash CHAR(64)  NOT NULL,
        snapshot_batch_id VARCHAR(256) NOT NULL,
        snapshot_ingest_ts DATETIME2(6) NOT NULL,
        source_sk       INT           NULL,
        event_date      DATE          NOT NULL,
        knowledge_date  DATE          NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_theme_membership') AND name = 'theme_revision_hash')
    ALTER TABLE dbo.fact_theme_membership ADD theme_revision_hash CHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_theme_membership') AND name = 'snapshot_batch_id')
    ALTER TABLE dbo.fact_theme_membership ADD snapshot_batch_id VARCHAR(256) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_theme_membership') AND name = 'snapshot_ingest_ts')
    ALTER TABLE dbo.fact_theme_membership ADD snapshot_ingest_ts DATETIME2(6) NULL;
GO

IF EXISTS (SELECT 1 FROM dbo.fact_theme_membership WHERE theme_revision_hash IS NULL)
    THROW 50004, 'fact_theme_membership requires a Silver-backed staged reload before theme_revision_hash can be made NOT NULL.', 1;

BEGIN
    BEGIN TRANSACTION;

    DROP TABLE IF EXISTS dbo.fact_theme_membership_revisioned;

    CREATE TABLE dbo.fact_theme_membership_revisioned (
        theme_id           VARCHAR(128) NOT NULL,
        etf_symbol         VARCHAR(16)  NOT NULL,
        security_sk        BIGINT       NOT NULL,
        weight             DECIMAL(9,6) NULL,
        is_ground_truth    BIT          NULL,
        theme_revision_hash CHAR(64)    NOT NULL,
        snapshot_batch_id VARCHAR(256)  NULL,
        snapshot_ingest_ts DATETIME2(6) NULL,
        source_sk          INT          NULL,
        event_date         DATE         NOT NULL,
        knowledge_date     DATE         NOT NULL
    );

    INSERT INTO dbo.fact_theme_membership_revisioned
        SELECT theme_id, etf_symbol, security_sk, weight, is_ground_truth,
            theme_revision_hash, snapshot_batch_id, snapshot_ingest_ts,
            source_sk, event_date, knowledge_date
    FROM dbo.fact_theme_membership;

    EXEC sp_rename 'dbo.fact_theme_membership', 'fact_theme_membership_legacy';
    EXEC sp_rename 'dbo.fact_theme_membership_revisioned', 'fact_theme_membership';
    DROP TABLE dbo.fact_theme_membership_legacy;

    COMMIT TRANSACTION;
END;
GO

DROP TABLE IF EXISTS dbo.fact_broad_market_membership;
CREATE TABLE dbo.fact_broad_market_membership (
    security_sk          BIGINT        NOT NULL,
    broad_market_weight  DECIMAL(9,6)  NULL,
    theme_revision_hash  CHAR(64)      NOT NULL,
    snapshot_batch_id    VARCHAR(256)  NOT NULL,
    snapshot_ingest_ts   DATETIME2(6)  NOT NULL,
    source_sk            INT           NULL,
    event_date           DATE          NOT NULL,
    knowledge_date       DATE          NOT NULL
);
GO

DECLARE @fact_material_event_existed BIT =
    CASE WHEN OBJECT_ID('dbo.fact_material_event', 'U') IS NULL THEN 0 ELSE 1 END;

IF OBJECT_ID('dbo.fact_material_event', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_material_event (
        event_sk       BIGINT        NOT NULL,
        security_sk    BIGINT        NULL,
        date_sk        INT           NULL,
        accession_no   VARCHAR(25)   NOT NULL,
        filing_type    VARCHAR(16)   NULL,
        description    VARCHAR(1024) NULL,
        material_event_revision_hash CHAR(64) NOT NULL,
        source_sk      INT           NULL,
        event_date     DATE          NOT NULL,
        knowledge_date DATE          NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_material_event') AND name = 'material_event_revision_hash')
    ALTER TABLE dbo.fact_material_event ADD material_event_revision_hash CHAR(64) NULL;

IF EXISTS (
    SELECT 1 FROM dbo.fact_material_event
    WHERE event_sk IS NULL OR accession_no IS NULL
       OR material_event_revision_hash IS NULL
       OR event_date IS NULL OR knowledge_date IS NULL
)
    THROW 50009, 'fact_material_event requires a Silver-backed staged reload before material_event_revision_hash can be enforced.', 1;

IF @fact_material_event_existed = 1
BEGIN
    BEGIN TRANSACTION;
    DROP TABLE IF EXISTS dbo.fact_material_event_revisioned;
    CREATE TABLE dbo.fact_material_event_revisioned (
        event_sk                    BIGINT        NOT NULL,
        security_sk                 BIGINT        NULL,
        date_sk                     INT           NULL,
        accession_no                VARCHAR(25)   NOT NULL,
        filing_type                 VARCHAR(16)   NULL,
        description                 VARCHAR(1024) NULL,
        material_event_revision_hash CHAR(64)    NOT NULL,
        source_sk                   INT           NULL,
        event_date                  DATE          NOT NULL,
        knowledge_date              DATE          NOT NULL
    );
    INSERT INTO dbo.fact_material_event_revisioned
    SELECT event_sk, security_sk, date_sk, accession_no, filing_type,
           description, material_event_revision_hash, source_sk, event_date,
           knowledge_date
    FROM dbo.fact_material_event;
    EXEC sp_rename 'dbo.fact_material_event', 'fact_material_event_legacy';
    EXEC sp_rename 'dbo.fact_material_event_revisioned', 'fact_material_event';
    DROP TABLE dbo.fact_material_event_legacy;
    COMMIT TRANSACTION;
END;
GO

DECLARE @fact_sec_filing_event_existed BIT =
    CASE WHEN OBJECT_ID('dbo.fact_sec_filing_event', 'U') IS NULL THEN 0 ELSE 1 END;

IF OBJECT_ID('dbo.fact_sec_filing_event', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_sec_filing_event (
        filing_event_sk BIGINT        NOT NULL,
        accession_no    VARCHAR(25)   NOT NULL,
        filing_type     VARCHAR(32)   NULL,
        filer_name      VARCHAR(8000) NULL,
        filing_revision_hash CHAR(64) NOT NULL,
        source_sk       INT           NULL,
        event_date      DATE          NOT NULL,
        knowledge_date  DATE          NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_sec_filing_event') AND name = 'filing_revision_hash')
    ALTER TABLE dbo.fact_sec_filing_event ADD filing_revision_hash CHAR(64) NULL;

IF EXISTS (
    SELECT 1 FROM dbo.fact_sec_filing_event
    WHERE filing_event_sk IS NULL OR accession_no IS NULL
       OR filing_revision_hash IS NULL
       OR event_date IS NULL OR knowledge_date IS NULL
)
    THROW 50010, 'fact_sec_filing_event requires a Silver-backed staged reload before filing_revision_hash can be enforced.', 1;

IF @fact_sec_filing_event_existed = 1
BEGIN
    BEGIN TRANSACTION;
    DROP TABLE IF EXISTS dbo.fact_sec_filing_event_revisioned;
    CREATE TABLE dbo.fact_sec_filing_event_revisioned (
        filing_event_sk      BIGINT       NOT NULL,
        accession_no         VARCHAR(25)  NOT NULL,
        filing_type          VARCHAR(32)  NULL,
        filer_name           VARCHAR(8000) NULL,
        filing_revision_hash CHAR(64)     NOT NULL,
        source_sk            INT          NULL,
        event_date           DATE         NOT NULL,
        knowledge_date       DATE         NOT NULL
    );
    INSERT INTO dbo.fact_sec_filing_event_revisioned
    SELECT filing_event_sk, accession_no, filing_type, filer_name,
           filing_revision_hash, source_sk, event_date, knowledge_date
    FROM dbo.fact_sec_filing_event;
    EXEC sp_rename 'dbo.fact_sec_filing_event', 'fact_sec_filing_event_legacy';
    EXEC sp_rename 'dbo.fact_sec_filing_event_revisioned', 'fact_sec_filing_event';
    DROP TABLE dbo.fact_sec_filing_event_legacy;
    COMMIT TRANSACTION;
END;
GO

CREATE OR ALTER VIEW dbo.v_fundamentals_daily_asof AS
WITH asof_axis AS (
    SELECT DISTINCT f.security_sk, d.cal_date AS as_of
    FROM dbo.fact_fundamentals f
    JOIN dbo.dim_date d
      ON d.cal_date >= f.event_date
     AND d.cal_date >= f.knowledge_date
),
eligible_metric_revisions AS (
    SELECT
        a.security_sk,
        a.as_of,
        m.metric_name,
        m.metric_value,
        f.knowledge_date,
        f.event_date,
        f.silver_loaded_at,
        f.fundamentals_revision_hash,
        ROW_NUMBER() OVER (
            PARTITION BY a.security_sk, a.as_of, m.metric_name
            ORDER BY f.knowledge_date DESC, f.event_date DESC,
                     f.silver_loaded_at DESC, f.fundamentals_revision_hash DESC
        ) AS revision_rank
    FROM asof_axis a
    JOIN dbo.fact_fundamentals f
      ON f.security_sk = a.security_sk
     AND f.event_date <= a.as_of
     AND f.knowledge_date <= a.as_of
    CROSS APPLY (VALUES
        ('market_cap', CAST(f.market_cap AS DECIMAL(38,8))),
        ('ebitda', CAST(f.ebitda AS DECIMAL(38,8))),
        ('pe_ratio', CAST(f.pe_ratio AS DECIMAL(38,8))),
        ('peg_ratio', CAST(f.peg_ratio AS DECIMAL(38,8))),
        ('ps_ratio', CAST(f.ps_ratio AS DECIMAL(38,8))),
        ('ev_ebitda', CAST(f.ev_ebitda AS DECIMAL(38,8))),
        ('gross_profit_ttm', CAST(f.gross_profit_ttm AS DECIMAL(38,8))),
        ('profit_margin', CAST(f.profit_margin AS DECIMAL(38,8))),
        ('rev_growth_yoy', CAST(f.rev_growth_yoy AS DECIMAL(38,8))),
        ('cash_and_equivalents', CAST(f.cash_and_equivalents AS DECIMAL(38,8))),
        ('total_debt', CAST(f.total_debt AS DECIMAL(38,8))),
        ('operating_cashflow', CAST(f.operating_cashflow AS DECIMAL(38,8))),
        ('capital_expenditures', CAST(f.capital_expenditures AS DECIMAL(38,8))),
        ('fcf_yield', CAST(f.fcf_yield AS DECIMAL(38,8))),
        ('net_debt_to_ebitda', CAST(f.net_debt_to_ebitda AS DECIMAL(38,8)))
    ) m(metric_name, metric_value)
    WHERE m.metric_value IS NOT NULL
),
latest_metrics AS (
    SELECT *
    FROM eligible_metric_revisions
    WHERE revision_rank = 1
)
SELECT
    security_sk,
    as_of,
    YEAR(as_of) * 10000 + MONTH(as_of) * 100 + DAY(as_of) AS date_sk,
    MAX(CASE WHEN metric_name = 'market_cap' THEN metric_value END) AS market_cap,
    MAX(CASE WHEN metric_name = 'ebitda' THEN metric_value END) AS ebitda,
    MAX(CASE WHEN metric_name = 'pe_ratio' THEN metric_value END) AS pe_ratio,
    MAX(CASE WHEN metric_name = 'peg_ratio' THEN metric_value END) AS peg_ratio,
    MAX(CASE WHEN metric_name = 'ps_ratio' THEN metric_value END) AS ps_ratio,
    MAX(CASE WHEN metric_name = 'ev_ebitda' THEN metric_value END) AS ev_ebitda,
    MAX(CASE WHEN metric_name = 'gross_profit_ttm' THEN metric_value END) AS gross_profit_ttm,
    MAX(CASE WHEN metric_name = 'profit_margin' THEN metric_value END) AS profit_margin,
    MAX(CASE WHEN metric_name = 'rev_growth_yoy' THEN metric_value END) AS rev_growth_yoy,
    MAX(CASE WHEN metric_name = 'cash_and_equivalents' THEN metric_value END) AS cash_and_equivalents,
    MAX(CASE WHEN metric_name = 'total_debt' THEN metric_value END) AS total_debt,
    MAX(CASE WHEN metric_name = 'operating_cashflow' THEN metric_value END) AS operating_cashflow,
    MAX(CASE WHEN metric_name = 'capital_expenditures' THEN metric_value END) AS capital_expenditures,
    MAX(CASE WHEN metric_name = 'fcf_yield' THEN metric_value END) AS fcf_yield,
    MAX(CASE WHEN metric_name = 'net_debt_to_ebitda' THEN metric_value END) AS net_debt_to_ebitda,
    MAX(knowledge_date) AS max_knowledge_date
FROM latest_metrics
GROUP BY security_sk, as_of;
GO

CREATE OR ALTER VIEW dbo.v_fundamentals_latest AS
WITH ranked AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY security_sk ORDER BY as_of DESC) AS asof_rank
    FROM dbo.v_fundamentals_daily_asof
)
SELECT security_sk, as_of, date_sk, market_cap, ebitda, pe_ratio, peg_ratio,
       ps_ratio, ev_ebitda, gross_profit_ttm, profit_margin, rev_growth_yoy,
       cash_and_equivalents, total_debt, operating_cashflow,
       capital_expenditures, fcf_yield, net_debt_to_ebitda, max_knowledge_date
FROM ranked
WHERE asof_rank = 1;
GO

CREATE OR ALTER VIEW dbo.v_company_news AS
SELECT *
FROM dbo.fact_company_news;
GO

CREATE OR ALTER VIEW dbo.v_news_sentiment_daily_asof AS
WITH asof_axis AS (
    SELECT DISTINCT n.security_sk, d.cal_date AS as_of
    FROM dbo.fact_news_sentiment n
    JOIN dbo.dim_date d
      ON d.cal_date >= n.event_date
     AND d.cal_date >= n.knowledge_date
),
eligible_revisions AS (
    SELECT
        a.security_sk,
        a.as_of,
        n.news_sk,
        n.sentiment,
        n.knowledge_date,
        ROW_NUMBER() OVER (
            PARTITION BY a.security_sk, a.as_of, n.news_sk
            ORDER BY n.knowledge_date DESC, n.event_date DESC,
                     n.silver_loaded_at DESC, n.news_revision_hash DESC
        ) AS revision_rank
    FROM asof_axis a
    JOIN dbo.fact_news_sentiment n
      ON n.security_sk = a.security_sk
     AND n.event_date BETWEEN DATEADD(DAY, -29, a.as_of) AND a.as_of
     AND n.knowledge_date <= a.as_of
)
SELECT
    security_sk,
    as_of,
    AVG(CAST(sentiment AS FLOAT)) AS news_sentiment_30d,
    MAX(knowledge_date) AS max_knowledge_date
FROM eligible_revisions
WHERE revision_rank = 1
GROUP BY security_sk, as_of;
GO

CREATE OR ALTER VIEW dbo.v_news_sentiment_30d AS
WITH ranked AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY security_sk ORDER BY as_of DESC) AS asof_rank
    FROM dbo.v_news_sentiment_daily_asof
)
SELECT security_sk, as_of, news_sentiment_30d, max_knowledge_date
FROM ranked
WHERE asof_rank = 1;
GO
