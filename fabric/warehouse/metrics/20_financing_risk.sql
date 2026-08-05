-- Current PIT financing-risk record used by deterministic recommendation policy.
-- Rebuildable from Bronze/Silver; no historical migration contract.

DROP VIEW IF EXISTS dbo.v_financing_risk;
DROP TABLE IF EXISTS dbo.fact_financing_risk;
GO

CREATE TABLE dbo.fact_financing_risk (
    security_sk BIGINT NOT NULL,
    date_sk INT NOT NULL,
    as_of DATE NOT NULL,
    diluted_share_growth_yoy FLOAT NULL,
    cash_runway_years FLOAT NULL,
    is_burning_cash BIT NULL,
    days_since_shelf_filing INT NULL,
    shelf_form VARCHAR(16) NULL,
    shelf_accession VARCHAR(25) NULL,
    financing_coverage_status VARCHAR(16) NOT NULL,
    financing_coverage_reasons_json VARCHAR(4000) NOT NULL,
    max_knowledge_date DATE NULL,
    created_at DATETIME2(6) NOT NULL
);
GO

CREATE OR ALTER VIEW dbo.v_financing_risk AS
SELECT
    security_sk, date_sk, as_of,
    diluted_share_growth_yoy, cash_runway_years, is_burning_cash,
    days_since_shelf_filing, shelf_form, shelf_accession,
    financing_coverage_status, financing_coverage_reasons_json,
    max_knowledge_date
FROM dbo.fact_financing_risk
WHERE max_knowledge_date IS NULL OR max_knowledge_date <= as_of;
GO
