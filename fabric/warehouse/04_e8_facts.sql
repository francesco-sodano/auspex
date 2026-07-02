-- Auspex E8 gold fact tables and serving views (Fabric Warehouse T-SQL)
-- Mirrors the E8 Lakehouse tables produced by nb_05/06/07.

IF OBJECT_ID('dbo.fact_fundamentals', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_fundamentals (
        security_sk            BIGINT        NOT NULL,
        date_sk                INT           NOT NULL,
        currency               VARCHAR(3)    NULL,
        sector                 VARCHAR(64)   NULL,
        industry               VARCHAR(128)  NULL,
        market_cap             DECIMAL(20,2) NULL,
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
        source_sk              INT           NULL,
        event_date             DATE          NOT NULL,
        knowledge_date         DATE          NOT NULL
    );
END;
GO

IF OBJECT_ID('dbo.fact_company_news', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_company_news (
        news_sk        BIGINT         NOT NULL,
        security_sk    BIGINT         NOT NULL,
        date_sk        INT            NOT NULL,
        title          VARCHAR(512)   NULL,
        summary        VARCHAR(4000)  NULL,
        url            VARCHAR(1024)  NULL,
        source         VARCHAR(128)   NULL,
        source_sk      INT            NULL,
        event_date     DATE           NOT NULL,
        knowledge_date DATE           NOT NULL
    );
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
        source_sk       INT           NULL,
        event_date      DATE          NOT NULL,
        knowledge_date  DATE          NOT NULL
    );
END;
GO

IF OBJECT_ID('dbo.fact_material_event', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_material_event (
        event_sk       BIGINT        NOT NULL,
        security_sk    BIGINT        NULL,
        date_sk        INT           NULL,
        accession_no   VARCHAR(25)   NOT NULL,
        filing_type    VARCHAR(16)   NULL,
        description    VARCHAR(1024) NULL,
        source_sk      INT           NULL,
        event_date     DATE          NOT NULL,
        knowledge_date DATE          NOT NULL
    );
END;
GO

IF OBJECT_ID('dbo.fact_sec_filing_event', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_sec_filing_event (
        filing_event_sk BIGINT        NOT NULL,
        accession_no    VARCHAR(25)   NOT NULL,
        filing_type     VARCHAR(32)   NULL,
        filer_name      VARCHAR(512)  NULL,
        source_sk       INT           NULL,
        event_date      DATE          NOT NULL,
        knowledge_date  DATE          NOT NULL
    );
END;
GO

CREATE OR ALTER VIEW dbo.v_fundamentals_latest AS
SELECT f.*
FROM dbo.fact_fundamentals f
JOIN (
    SELECT security_sk, MAX(date_sk) AS date_sk
    FROM dbo.fact_fundamentals
    GROUP BY security_sk
) latest
  ON f.security_sk = latest.security_sk AND f.date_sk = latest.date_sk;
GO

CREATE OR ALTER VIEW dbo.v_company_news AS
SELECT *
FROM dbo.fact_company_news
WHERE knowledge_date <= event_date;
GO

CREATE OR ALTER VIEW dbo.v_news_sentiment_30d AS
SELECT
    security_sk,
    MAX(event_date) AS as_of,
    AVG(CAST(sentiment AS FLOAT)) AS news_sentiment_30d,
    MAX(knowledge_date) AS max_knowledge_date
FROM dbo.fact_news_sentiment
GROUP BY security_sk;
GO
