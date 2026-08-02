-- Auspex E5 FX fact table (Fabric Warehouse T-SQL)

DECLARE @fact_fx_rate_existed BIT =
    CASE WHEN OBJECT_ID('dbo.fact_fx_rate', 'U') IS NULL THEN 0 ELSE 1 END;

IF OBJECT_ID('dbo.fact_fx_rate', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_fx_rate (
        ccy_pair       VARCHAR(7)    NOT NULL,
        date_sk        INT           NOT NULL,
        rate           DECIMAL(18,8) NOT NULL,
        fx_revision_hash CHAR(64)    NOT NULL,
        source_sk      INT           NULL,
        event_date     DATE          NOT NULL,
        knowledge_date DATE          NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_fx_rate') AND name = 'fx_revision_hash')
    ALTER TABLE dbo.fact_fx_rate ADD fx_revision_hash CHAR(64) NULL;

IF EXISTS (SELECT 1 FROM dbo.fact_fx_rate WHERE fx_revision_hash IS NULL)
    THROW 50003, 'fact_fx_rate requires a Silver-backed staged reload before fx_revision_hash can be made NOT NULL.', 1;

IF @fact_fx_rate_existed = 1
BEGIN
    BEGIN TRANSACTION;

    DROP TABLE IF EXISTS dbo.fact_fx_rate_revisioned;

    CREATE TABLE dbo.fact_fx_rate_revisioned (
        ccy_pair        VARCHAR(7)    NOT NULL,
        date_sk         INT           NOT NULL,
        rate            DECIMAL(18,8) NOT NULL,
        fx_revision_hash CHAR(64)     NOT NULL,
        source_sk       INT           NULL,
        event_date      DATE          NOT NULL,
        knowledge_date  DATE          NOT NULL
    );

    INSERT INTO dbo.fact_fx_rate_revisioned
    SELECT ccy_pair, date_sk, rate, fx_revision_hash, source_sk,
           event_date, knowledge_date
    FROM dbo.fact_fx_rate;

    EXEC sp_rename 'dbo.fact_fx_rate', 'fact_fx_rate_legacy';
    EXEC sp_rename 'dbo.fact_fx_rate_revisioned', 'fact_fx_rate';
    DROP TABLE dbo.fact_fx_rate_legacy;

    COMMIT TRANSACTION;
END;