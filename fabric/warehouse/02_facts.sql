-- Auspex E5 gold fact tables (Fabric Warehouse T-SQL)
-- Every fact table carries event_date and knowledge_date for PIT correctness.

IF OBJECT_ID('dbo.fact_market_daily', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_market_daily (
        security_sk    BIGINT        NOT NULL,
        date_sk        INT           NOT NULL,
        [open]         DECIMAL(18,6) NULL,
        high           DECIMAL(18,6) NULL,
        low            DECIMAL(18,6) NULL,
        [close]        DECIMAL(18,6) NULL,
        adj_close      DECIMAL(18,6) NULL,
        volume         BIGINT        NULL,
        ret_1d         DECIMAL(12,8) NULL,
        source_sk      INT           NULL,
        event_date     DATE          NOT NULL,
        knowledge_date DATE          NOT NULL
    );
END;

IF OBJECT_ID('dbo.fact_insider_txn', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_insider_txn (
        insider_txn_sk BIGINT        NOT NULL,
        security_sk    BIGINT        NOT NULL,
        entity_sk      BIGINT        NULL,
        date_sk        INT           NULL,
        line_no        INT           NOT NULL,
        txn_code       VARCHAR(2)    NULL,
        is_buy         BIT           NULL,
        shares         DECIMAL(20,4) NULL,
        price          DECIMAL(18,6) NULL,
        value_usd      DECIMAL(20,2) NULL,
        shares_after   DECIMAL(20,4) NULL,
        accession_no   VARCHAR(25)   NOT NULL,
        source_sk      INT           NULL,
        event_date     DATE          NOT NULL,
        knowledge_date DATE          NOT NULL
    );
END;

IF OBJECT_ID('dbo.fact_institutional_holding', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_institutional_holding (
        security_sk       BIGINT        NULL,
        entity_sk         BIGINT        NULL,
        date_sk           INT           NULL,
        shares            DECIMAL(20,4) NULL,
        value_usd         DECIMAL(20,2) NULL,
        shares_delta_qoq  DECIMAL(20,4) NULL,
        pct_of_portfolio  DECIMAL(9,6)  NULL,
        accession_no      VARCHAR(25)   NOT NULL,
        source_sk         INT           NULL,
        event_date        DATE          NOT NULL,
        knowledge_date    DATE          NOT NULL
    );
END;

IF OBJECT_ID('dbo.fact_ownership_event', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_ownership_event (
        security_sk    BIGINT       NULL,
        entity_sk      BIGINT       NULL,
        date_sk        INT          NULL,
        pct_owned      DECIMAL(9,6) NULL,
        filing_type    VARCHAR(4)   NULL,
        is_activist    BIT          NULL,
        accession_no   VARCHAR(25)  NOT NULL,
        source_sk      INT          NULL,
        event_date     DATE         NOT NULL,
        knowledge_date DATE         NOT NULL
    );
END;

IF OBJECT_ID('dbo.fact_news_sentiment', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_news_sentiment (
        news_sk        BIGINT         NULL,
        security_sk    BIGINT         NULL,
        date_sk        INT            NULL,
        sentiment      DECIMAL(5,4)   NULL,
        relevance      DECIMAL(5,4)   NULL,
        title_hash     CHAR(64)       NOT NULL,
        url            VARCHAR(1024)  NULL,
        source_sk      INT            NULL,
        event_date     DATE           NOT NULL,
        knowledge_date DATE           NOT NULL
    );
END;

IF OBJECT_ID('dbo.fact_contract_award', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_contract_award (
        award_sk         BIGINT         NULL,
        security_sk      BIGINT         NULL,
        date_sk          INT            NULL,
        agency           VARCHAR(128)   NULL,
        amount_usd       DECIMAL(20,2)  NULL,
        description_hash CHAR(64)       NULL,
        source_sk        INT            NULL,
        event_date       DATE           NOT NULL,
        knowledge_date   DATE           NOT NULL
    );
END;

IF OBJECT_ID('dbo.fact_macro', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_macro (
        indicator_code VARCHAR(32)   NOT NULL,
        date_sk        INT           NOT NULL,
        [value]        DECIMAL(20,6) NULL,
        source_sk      INT           NULL,
        event_date     DATE          NOT NULL,
        knowledge_date DATE          NOT NULL
    );
END;