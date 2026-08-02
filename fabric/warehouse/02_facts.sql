-- Auspex E5 gold fact tables (Fabric Warehouse T-SQL)
-- Every fact table carries event_date and knowledge_date for PIT correctness.

DECLARE @fact_market_daily_existed BIT =
    CASE WHEN OBJECT_ID('dbo.fact_market_daily', 'U') IS NULL THEN 0 ELSE 1 END;

IF OBJECT_ID('dbo.fact_market_daily', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_market_daily (
        security_sk    BIGINT        NOT NULL,
        date_sk        INT           NOT NULL,
        price_revision_hash CHAR(64) NOT NULL,
        [open]         DECIMAL(18,6) NULL,
        high           DECIMAL(18,6) NULL,
        low            DECIMAL(18,6) NULL,
        [close]        DECIMAL(18,6) NULL,
        adj_close      DECIMAL(18,6) NULL,
        volume         BIGINT        NULL,
        ret_1d         DECIMAL(12,8) NULL,
        source_sk      INT           NULL,
        event_date     DATE          NOT NULL,
        knowledge_date DATE          NOT NULL,
        ingest_ts      DATETIME2(6)  NOT NULL,
        revision_loaded_at DATETIME2(6) NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_market_daily') AND name = 'price_revision_hash')
    ALTER TABLE dbo.fact_market_daily ADD price_revision_hash CHAR(64) NULL;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_market_daily') AND name = 'ingest_ts')
    ALTER TABLE dbo.fact_market_daily ADD ingest_ts DATETIME2(6) NULL;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_market_daily') AND name = 'revision_loaded_at')
    ALTER TABLE dbo.fact_market_daily ADD revision_loaded_at DATETIME2(6) NULL;

IF EXISTS (
    SELECT 1
    FROM dbo.fact_market_daily
    WHERE price_revision_hash IS NULL
       OR ingest_ts IS NULL
       OR revision_loaded_at IS NULL
)
    THROW 50001, 'fact_market_daily requires a staged reload before revision columns can be made NOT NULL.', 1;

IF @fact_market_daily_existed = 1
BEGIN
    BEGIN TRANSACTION;

    DROP TABLE IF EXISTS dbo.fact_market_daily_revisioned;

    CREATE TABLE dbo.fact_market_daily_revisioned (
        security_sk        BIGINT        NOT NULL,
        date_sk            INT           NOT NULL,
        price_revision_hash CHAR(64)     NOT NULL,
        [open]             DECIMAL(18,6) NULL,
        high               DECIMAL(18,6) NULL,
        low                DECIMAL(18,6) NULL,
        [close]            DECIMAL(18,6) NULL,
        adj_close          DECIMAL(18,6) NULL,
        volume             BIGINT        NULL,
        ret_1d             DECIMAL(12,8) NULL,
        source_sk          INT           NULL,
        event_date         DATE          NOT NULL,
        knowledge_date     DATE          NOT NULL,
        ingest_ts          DATETIME2(6)  NOT NULL,
        revision_loaded_at DATETIME2(6)  NOT NULL
    );

    INSERT INTO dbo.fact_market_daily_revisioned
    SELECT security_sk, date_sk, price_revision_hash, [open], high, low, [close],
           adj_close, volume, ret_1d, source_sk, event_date, knowledge_date,
           ingest_ts, revision_loaded_at
    FROM dbo.fact_market_daily;

    EXEC sp_rename 'dbo.fact_market_daily', 'fact_market_daily_legacy';
    EXEC sp_rename 'dbo.fact_market_daily_revisioned', 'fact_market_daily';
    DROP TABLE dbo.fact_market_daily_legacy;

    COMMIT TRANSACTION;
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

DECLARE @fact_institutional_holding_existed BIT =
    CASE WHEN OBJECT_ID('dbo.fact_institutional_holding', 'U') IS NULL THEN 0 ELSE 1 END;

IF OBJECT_ID('dbo.fact_institutional_holding', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_institutional_holding (
        security_sk               BIGINT        NULL,
        entity_sk                 BIGINT        NULL,
        date_sk                   INT           NULL,
        shares                    DECIMAL(20,4) NULL,
        value_usd                 DECIMAL(20,2) NULL,
        shares_delta_qoq          DECIMAL(20,4) NULL,
        pct_of_portfolio          DECIMAL(9,6)  NULL,
        accession_no              VARCHAR(25)   NOT NULL,
        holding_revision_hash     CHAR(64)      NOT NULL,
        silver_natural_key        VARCHAR(64)   NULL,
        silver_batch_id           VARCHAR(256)  NULL,
        silver_ingest_ts          DATETIME2(6)  NULL,
        silver_source_record_hash CHAR(64)      NULL,
        silver_loaded_at          DATETIME2(6)  NULL,
        source_sk                 INT           NULL,
        event_date                DATE          NOT NULL,
        knowledge_date            DATE          NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_institutional_holding') AND name = 'holding_revision_hash')
    ALTER TABLE dbo.fact_institutional_holding ADD holding_revision_hash CHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_institutional_holding') AND name = 'silver_natural_key')
    ALTER TABLE dbo.fact_institutional_holding ADD silver_natural_key VARCHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_institutional_holding') AND name = 'silver_batch_id')
    ALTER TABLE dbo.fact_institutional_holding ADD silver_batch_id VARCHAR(256) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_institutional_holding') AND name = 'silver_ingest_ts')
    ALTER TABLE dbo.fact_institutional_holding ADD silver_ingest_ts DATETIME2(6) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_institutional_holding') AND name = 'silver_source_record_hash')
    ALTER TABLE dbo.fact_institutional_holding ADD silver_source_record_hash CHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_institutional_holding') AND name = 'silver_loaded_at')
    ALTER TABLE dbo.fact_institutional_holding ADD silver_loaded_at DATETIME2(6) NULL;

IF EXISTS (
    SELECT 1 FROM dbo.fact_institutional_holding
    WHERE accession_no IS NULL OR holding_revision_hash IS NULL
       OR event_date IS NULL OR knowledge_date IS NULL
       OR (
           source_sk = 3
           AND (silver_natural_key IS NULL OR silver_batch_id IS NULL
                OR silver_ingest_ts IS NULL OR silver_source_record_hash IS NULL
                OR silver_loaded_at IS NULL)
       )
)
    THROW 50002, 'fact_institutional_holding requires a Silver-backed staged reload before revision provenance can be enforced.', 1;

IF @fact_institutional_holding_existed = 1
BEGIN
    BEGIN TRANSACTION;
    DROP TABLE IF EXISTS dbo.fact_institutional_holding_revisioned;
    CREATE TABLE dbo.fact_institutional_holding_revisioned (
        security_sk               BIGINT        NULL,
        entity_sk                 BIGINT        NULL,
        date_sk                   INT           NULL,
        shares                    DECIMAL(20,4) NULL,
        value_usd                 DECIMAL(20,2) NULL,
        shares_delta_qoq          DECIMAL(20,4) NULL,
        pct_of_portfolio          DECIMAL(9,6)  NULL,
        accession_no              VARCHAR(25)   NOT NULL,
        holding_revision_hash     CHAR(64)      NOT NULL,
        silver_natural_key        VARCHAR(64)   NULL,
        silver_batch_id           VARCHAR(256)  NULL,
        silver_ingest_ts          DATETIME2(6)  NULL,
        silver_source_record_hash CHAR(64)      NULL,
        silver_loaded_at          DATETIME2(6)  NULL,
        source_sk                 INT           NULL,
        event_date                DATE          NOT NULL,
        knowledge_date            DATE          NOT NULL
    );
    INSERT INTO dbo.fact_institutional_holding_revisioned
    SELECT security_sk, entity_sk, date_sk, shares, value_usd, shares_delta_qoq,
           pct_of_portfolio, accession_no, holding_revision_hash,
           silver_natural_key, silver_batch_id, silver_ingest_ts,
           silver_source_record_hash, silver_loaded_at, source_sk, event_date,
           knowledge_date
    FROM dbo.fact_institutional_holding;
    EXEC sp_rename 'dbo.fact_institutional_holding', 'fact_institutional_holding_legacy';
    EXEC sp_rename 'dbo.fact_institutional_holding_revisioned', 'fact_institutional_holding';
    DROP TABLE dbo.fact_institutional_holding_legacy;
    COMMIT TRANSACTION;
END;

DECLARE @fact_ownership_event_existed BIT =
    CASE WHEN OBJECT_ID('dbo.fact_ownership_event', 'U') IS NULL THEN 0 ELSE 1 END;

IF OBJECT_ID('dbo.fact_ownership_event', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_ownership_event (
        security_sk             BIGINT       NULL,
        entity_sk               BIGINT       NULL,
        date_sk                 INT          NULL,
        pct_owned               DECIMAL(9,6) NULL,
        filing_type             VARCHAR(16)  NULL,
        is_activist             BIT          NULL,
        accession_no            VARCHAR(25)  NOT NULL,
        ownership_revision_hash CHAR(64)     NOT NULL,
        source_sk               INT          NULL,
        event_date              DATE         NOT NULL,
        knowledge_date          DATE         NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_ownership_event') AND name = 'ownership_revision_hash')
    ALTER TABLE dbo.fact_ownership_event ADD ownership_revision_hash CHAR(64) NULL;

IF EXISTS (
    SELECT 1 FROM dbo.fact_ownership_event
    WHERE accession_no IS NULL OR ownership_revision_hash IS NULL
       OR event_date IS NULL OR knowledge_date IS NULL
)
    THROW 50003, 'fact_ownership_event requires a Silver-backed staged reload before ownership_revision_hash can be enforced.', 1;

IF @fact_ownership_event_existed = 1
BEGIN
    BEGIN TRANSACTION;
    DROP TABLE IF EXISTS dbo.fact_ownership_event_revisioned;
    CREATE TABLE dbo.fact_ownership_event_revisioned (
        security_sk             BIGINT       NULL,
        entity_sk               BIGINT       NULL,
        date_sk                 INT          NULL,
        pct_owned               DECIMAL(9,6) NULL,
        filing_type             VARCHAR(16)  NULL,
        is_activist             BIT          NULL,
        accession_no            VARCHAR(25)  NOT NULL,
        ownership_revision_hash CHAR(64)     NOT NULL,
        source_sk               INT          NULL,
        event_date              DATE         NOT NULL,
        knowledge_date          DATE         NOT NULL
    );
    INSERT INTO dbo.fact_ownership_event_revisioned
    SELECT security_sk, entity_sk, date_sk, pct_owned, filing_type, is_activist,
           accession_no, ownership_revision_hash, source_sk, event_date, knowledge_date
    FROM dbo.fact_ownership_event;
    EXEC sp_rename 'dbo.fact_ownership_event', 'fact_ownership_event_legacy';
    EXEC sp_rename 'dbo.fact_ownership_event_revisioned', 'fact_ownership_event';
    DROP TABLE dbo.fact_ownership_event_legacy;
    COMMIT TRANSACTION;
END;

DECLARE @fact_news_sentiment_existed BIT =
    CASE WHEN OBJECT_ID('dbo.fact_news_sentiment', 'U') IS NULL THEN 0 ELSE 1 END;

IF OBJECT_ID('dbo.fact_news_sentiment', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_news_sentiment (
        news_sk                   BIGINT         NULL,
        security_sk               BIGINT         NULL,
        date_sk                   INT            NULL,
        published_at              DATETIME2(6)   NOT NULL,
        sentiment                 DECIMAL(5,4)   NULL,
        relevance                 DECIMAL(5,4)   NULL,
        title_hash                CHAR(64)       NOT NULL,
        url                       VARCHAR(1024)  NULL,
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
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_news_sentiment') AND name = 'published_at')
    ALTER TABLE dbo.fact_news_sentiment ADD published_at DATETIME2(6) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_news_sentiment') AND name = 'news_revision_hash')
    ALTER TABLE dbo.fact_news_sentiment ADD news_revision_hash CHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_news_sentiment') AND name = 'silver_natural_key')
    ALTER TABLE dbo.fact_news_sentiment ADD silver_natural_key VARCHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_news_sentiment') AND name = 'silver_batch_id')
    ALTER TABLE dbo.fact_news_sentiment ADD silver_batch_id VARCHAR(256) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_news_sentiment') AND name = 'silver_ingest_ts')
    ALTER TABLE dbo.fact_news_sentiment ADD silver_ingest_ts DATETIME2(6) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_news_sentiment') AND name = 'silver_source_record_hash')
    ALTER TABLE dbo.fact_news_sentiment ADD silver_source_record_hash CHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_news_sentiment') AND name = 'silver_loaded_at')
    ALTER TABLE dbo.fact_news_sentiment ADD silver_loaded_at DATETIME2(6) NULL;

IF EXISTS (
    SELECT 1 FROM dbo.fact_news_sentiment
    WHERE published_at IS NULL OR title_hash IS NULL OR news_revision_hash IS NULL
       OR silver_natural_key IS NULL OR silver_batch_id IS NULL
       OR silver_ingest_ts IS NULL OR silver_source_record_hash IS NULL
       OR silver_loaded_at IS NULL OR event_date IS NULL OR knowledge_date IS NULL
)
    THROW 50004, 'fact_news_sentiment requires a Silver-backed staged reload before news revision provenance can be enforced.', 1;

IF @fact_news_sentiment_existed = 1
BEGIN
    BEGIN TRANSACTION;
    DROP TABLE IF EXISTS dbo.fact_news_sentiment_revisioned;
    CREATE TABLE dbo.fact_news_sentiment_revisioned (
        news_sk                   BIGINT         NULL,
        security_sk               BIGINT         NULL,
        date_sk                   INT            NULL,
        published_at              DATETIME2(6)   NOT NULL,
        sentiment                 DECIMAL(5,4)   NULL,
        relevance                 DECIMAL(5,4)   NULL,
        title_hash                CHAR(64)       NOT NULL,
        url                       VARCHAR(1024)  NULL,
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
    INSERT INTO dbo.fact_news_sentiment_revisioned
    SELECT news_sk, security_sk, date_sk, published_at, sentiment, relevance,
           title_hash, url, news_revision_hash, silver_natural_key,
           silver_batch_id, silver_ingest_ts, silver_source_record_hash,
           silver_loaded_at, source_sk, event_date, knowledge_date
    FROM dbo.fact_news_sentiment;
    EXEC sp_rename 'dbo.fact_news_sentiment', 'fact_news_sentiment_legacy';
    EXEC sp_rename 'dbo.fact_news_sentiment_revisioned', 'fact_news_sentiment';
    DROP TABLE dbo.fact_news_sentiment_legacy;
    COMMIT TRANSACTION;
END;

DECLARE @fact_contract_award_existed BIT =
    CASE WHEN OBJECT_ID('dbo.fact_contract_award', 'U') IS NULL THEN 0 ELSE 1 END;

IF OBJECT_ID('dbo.fact_contract_award', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_contract_award (
        award_sk               BIGINT         NOT NULL,
        transaction_id         CHAR(64)       NOT NULL,
        award_id               VARCHAR(128)   NOT NULL,
        contract_revision_hash CHAR(64)       NOT NULL,
        security_sk            BIGINT         NOT NULL,
        entity_sk              BIGINT         NOT NULL,
        date_sk                INT            NOT NULL,
        agency                 VARCHAR(128)   NULL,
        amount_usd             DECIMAL(20,2)  NOT NULL,
        description_hash       CHAR(64)       NOT NULL,
        source_sk              INT            NULL,
        event_date             DATE           NOT NULL,
        knowledge_date         DATE           NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_contract_award') AND name = 'award_id')
    ALTER TABLE dbo.fact_contract_award ADD award_id VARCHAR(128) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_contract_award') AND name = 'transaction_id')
    ALTER TABLE dbo.fact_contract_award ADD transaction_id CHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_contract_award') AND name = 'contract_revision_hash')
    ALTER TABLE dbo.fact_contract_award ADD contract_revision_hash CHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_contract_award') AND name = 'entity_sk')
    ALTER TABLE dbo.fact_contract_award ADD entity_sk BIGINT NULL;

IF EXISTS (
    SELECT 1 FROM dbo.fact_contract_award
    WHERE award_sk IS NULL OR transaction_id IS NULL OR award_id IS NULL OR contract_revision_hash IS NULL
       OR security_sk IS NULL OR entity_sk IS NULL OR date_sk IS NULL
       OR amount_usd IS NULL OR description_hash IS NULL
       OR event_date IS NULL OR knowledge_date IS NULL
)
    THROW 50005, 'fact_contract_award requires a Silver-backed staged reload before contract revision provenance can be enforced.', 1;

IF @fact_contract_award_existed = 1
BEGIN
    BEGIN TRANSACTION;
    DROP TABLE IF EXISTS dbo.fact_contract_award_revisioned;
    CREATE TABLE dbo.fact_contract_award_revisioned (
        award_sk               BIGINT         NOT NULL,
        transaction_id         CHAR(64)       NOT NULL,
        award_id               VARCHAR(128)   NOT NULL,
        contract_revision_hash CHAR(64)       NOT NULL,
        security_sk            BIGINT         NOT NULL,
        entity_sk              BIGINT         NOT NULL,
        date_sk                INT            NOT NULL,
        agency                 VARCHAR(128)   NULL,
        amount_usd             DECIMAL(20,2)  NOT NULL,
        description_hash       CHAR(64)       NOT NULL,
        source_sk              INT            NULL,
        event_date             DATE           NOT NULL,
        knowledge_date         DATE           NOT NULL
    );
    INSERT INTO dbo.fact_contract_award_revisioned
    SELECT award_sk, transaction_id, award_id, contract_revision_hash, security_sk, entity_sk,
           date_sk, agency, amount_usd, description_hash, source_sk, event_date,
           knowledge_date
    FROM dbo.fact_contract_award;
    EXEC sp_rename 'dbo.fact_contract_award', 'fact_contract_award_legacy';
    EXEC sp_rename 'dbo.fact_contract_award_revisioned', 'fact_contract_award';
    DROP TABLE dbo.fact_contract_award_legacy;
    COMMIT TRANSACTION;
END;

DECLARE @fact_macro_existed BIT =
    CASE WHEN OBJECT_ID('dbo.fact_macro', 'U') IS NULL THEN 0 ELSE 1 END;

IF OBJECT_ID('dbo.fact_macro', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_macro (
        indicator_code VARCHAR(32)   NOT NULL,
        date_sk        INT           NOT NULL,
        [value]        DECIMAL(20,6) NULL,
        macro_revision_hash CHAR(64) NOT NULL,
        source_sk      INT           NULL,
        event_date     DATE          NOT NULL,
        knowledge_date DATE          NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_macro') AND name = 'macro_revision_hash')
    ALTER TABLE dbo.fact_macro ADD macro_revision_hash CHAR(64) NULL;

IF EXISTS (SELECT 1 FROM dbo.fact_macro WHERE macro_revision_hash IS NULL)
    THROW 50002, 'fact_macro requires a Silver-backed staged reload before macro_revision_hash can be made NOT NULL.', 1;

IF @fact_macro_existed = 1
BEGIN
    BEGIN TRANSACTION;

    DROP TABLE IF EXISTS dbo.fact_macro_revisioned;

    CREATE TABLE dbo.fact_macro_revisioned (
        indicator_code      VARCHAR(32)   NOT NULL,
        date_sk             INT           NOT NULL,
        [value]             DECIMAL(20,6) NULL,
        macro_revision_hash CHAR(64)      NOT NULL,
        source_sk           INT           NULL,
        event_date          DATE          NOT NULL,
        knowledge_date      DATE          NOT NULL
    );

    INSERT INTO dbo.fact_macro_revisioned
    SELECT indicator_code, date_sk, [value], macro_revision_hash, source_sk,
           event_date, knowledge_date
    FROM dbo.fact_macro;

    EXEC sp_rename 'dbo.fact_macro', 'fact_macro_legacy';
    EXEC sp_rename 'dbo.fact_macro_revisioned', 'fact_macro';
    DROP TABLE dbo.fact_macro_legacy;

    COMMIT TRANSACTION;
END;