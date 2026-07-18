-- Auspex E5 gold dimensions (Fabric Warehouse T-SQL)
-- These DDL files mirror the Lakehouse gold tables produced by nb_03_silver_to_gold.py
-- and provide the Warehouse promotion contract for E5.

IF OBJECT_ID('dbo.dim_security', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.dim_security (
        security_sk       BIGINT       NOT NULL,
        cik               VARCHAR(10)  NULL,
        ticker            VARCHAR(16)  NULL,
        isin              VARCHAR(12)  NULL,
        figi              VARCHAR(12)  NULL,
        company_name      VARCHAR(256) NOT NULL,
        gics_sector       VARCHAR(64)  NULL,
        gics_industry     VARCHAR(64)  NULL,
        country           VARCHAR(2)   NULL,
        exchange          VARCHAR(16)  NULL,
        currency          VARCHAR(3)   NULL,
        mcap_band         VARCHAR(16)  NULL,
        is_active         BIT          NOT NULL,
        valid_from        DATE         NOT NULL,
        valid_to          DATE         NOT NULL,
        is_current        BIT          NOT NULL,
        resolution_method VARCHAR(64)  NULL,
        source_id         VARCHAR(64)  NULL,
        updated_at        DATETIME2(3) NULL
    );
END;

IF OBJECT_ID('dbo.dim_date', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.dim_date (
        date_sk        INT        NOT NULL,
        cal_date       DATE       NOT NULL,
        [year]         INT        NULL,
        [quarter]      INT        NULL,
        [month]        INT        NULL,
        [day]          INT        NULL,
        is_trading_day BIT        NULL,
        fiscal_quarter VARCHAR(7) NULL
    );
END;

IF OBJECT_ID('dbo.dim_entity', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.dim_entity (
        entity_sk         BIGINT       NOT NULL,
        entity_natural_id VARCHAR(64)  NOT NULL,
        entity_type       VARCHAR(16)  NOT NULL,
        [name]            VARCHAR(256) NULL,
        [role]            VARCHAR(64)  NULL,
        cik               VARCHAR(10)  NULL
    );
END;

IF OBJECT_ID('dbo.dim_source', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.dim_source (
        source_sk          INT          NOT NULL,
        source_id          VARCHAR(64)  NOT NULL,
        source_type        VARCHAR(32)  NULL,
        latency_class      VARCHAR(16)  NULL,
        reliability_weight DECIMAL(3,2) NULL,
        source_class       VARCHAR(32)  NULL
    );
END;