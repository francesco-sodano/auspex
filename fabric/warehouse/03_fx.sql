-- Auspex E5 FX fact table (Fabric Warehouse T-SQL)

IF OBJECT_ID('dbo.fact_fx_rate', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_fx_rate (
        ccy_pair       VARCHAR(7)    NOT NULL,
        date_sk        INT           NOT NULL,
        rate           DECIMAL(18,8) NOT NULL,
        source_sk      INT           NULL,
        event_date     DATE          NOT NULL,
        knowledge_date DATE          NOT NULL
    );
END;