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

IF OBJECT_ID('dbo.dim_theme', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.dim_theme (
        theme_id          VARCHAR(128) NOT NULL,
        theme_name        VARCHAR(128) NOT NULL,
        benchmark_symbol  VARCHAR(16)  NOT NULL,
        is_active         BIT          NOT NULL,
        catalog_version   INT          NOT NULL,
        updated_at        DATETIME2(6) NOT NULL
    );
END;

IF OBJECT_ID('dbo.bridge_theme_etf', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.bridge_theme_etf (
        theme_id          VARCHAR(128) NOT NULL,
        etf_symbol        VARCHAR(16)  NOT NULL,
        blend_weight      DECIMAL(9,6) NOT NULL,
        is_active         BIT          NOT NULL,
        catalog_version   INT          NOT NULL,
        updated_at        DATETIME2(6) NOT NULL
    );
END;

IF OBJECT_ID('dbo.security_theme_classification', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.security_theme_classification (
        classification_id       CHAR(64)      NOT NULL,
        security_sk             BIGINT        NOT NULL,
        ticker                  VARCHAR(16)   NOT NULL,
        theme_id                VARCHAR(128)  NOT NULL,
        provenance              VARCHAR(16)   NOT NULL,
        confidence              FLOAT         NOT NULL,
        rationale               VARCHAR(1000) NOT NULL,
        effective_from          DATE          NOT NULL,
        effective_to            DATE          NULL,
        classification_version  VARCHAR(64)   NOT NULL,
        updated_at              DATETIME2(6)  NOT NULL
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

DECLARE @dim_entity_needs_width_upgrade BIT =
    CASE WHEN EXISTS (
        SELECT 1
        FROM sys.columns
        WHERE object_id = OBJECT_ID('dbo.dim_entity')
          AND name = 'entity_natural_id'
          AND max_length < 128
    ) THEN 1 ELSE 0 END;

IF OBJECT_ID('dbo.dim_entity', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.dim_entity (
        entity_sk         BIGINT       NOT NULL,
        entity_natural_id VARCHAR(128) NOT NULL,
        entity_type       VARCHAR(16)  NOT NULL,
        [name]            VARCHAR(256) NULL,
        [role]            VARCHAR(64)  NULL,
        cik               VARCHAR(10)  NULL
    );
END;

IF @dim_entity_needs_width_upgrade = 1
BEGIN
    BEGIN TRANSACTION;

    DROP TABLE IF EXISTS dbo.dim_entity_wide;

    CREATE TABLE dbo.dim_entity_wide (
        entity_sk         BIGINT       NOT NULL,
        entity_natural_id VARCHAR(128) NOT NULL,
        entity_type       VARCHAR(16)  NOT NULL,
        [name]            VARCHAR(256) NULL,
        [role]            VARCHAR(64)  NULL,
        cik               VARCHAR(10)  NULL
    );

    INSERT INTO dbo.dim_entity_wide
    SELECT entity_sk, entity_natural_id, entity_type, [name], [role], cik
    FROM dbo.dim_entity;

    EXEC sp_rename 'dbo.dim_entity', 'dim_entity_narrow';
    EXEC sp_rename 'dbo.dim_entity_wide', 'dim_entity';
    DROP TABLE dbo.dim_entity_narrow;

    COMMIT TRANSACTION;
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