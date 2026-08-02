-- Auspex E20 PIT-safe fair-multiple anchor.

IF OBJECT_ID('dbo.fact_fundamental_anchor', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_fundamental_anchor (
        security_sk BIGINT NOT NULL,
        date_sk INT NOT NULL,
        ev_sales DECIMAL(18,6) NULL,
        ev_ebitda DECIMAL(18,6) NULL,
        p_fcf DECIMAL(18,6) NULL,
        expected_ev_sales DECIMAL(18,6) NULL,
        residual_evs DECIMAL(12,8) NULL,
        residual_evebitda DECIMAL(12,8) NULL,
        residual_pfcf DECIMAL(12,8) NULL,
        anchor_residual DECIMAL(12,8) NULL,
        fundamental_anchor_z DECIMAL(12,8) NULL,
        anchor_method VARCHAR(16) NOT NULL,
        n_peers INT NOT NULL,
        r2_sector DECIMAL(9,6) NULL,
        uses_forward BIT NOT NULL,
        imputed_flags VARCHAR(512) NULL,
        model_version VARCHAR(32) NOT NULL,
        source_sk INT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL
    );
END;
GO

CREATE OR ALTER VIEW dbo.v_fundamental_anchor AS
WITH latest AS (
    SELECT
        a.*,
        ROW_NUMBER() OVER (
            PARTITION BY a.security_sk
            ORDER BY a.date_sk DESC, a.knowledge_date DESC, a.model_version DESC
        ) AS row_number
    FROM dbo.fact_fundamental_anchor a
        WHERE a.model_version = 'e20_v2'
            AND a.knowledge_date <= DATEFROMPARTS(a.date_sk / 10000, (a.date_sk / 100) % 100, a.date_sk % 100)
)
SELECT
    a.security_sk,
    s.ticker,
    s.company_name,
    s.gics_sector,
    a.date_sk,
    a.ev_sales,
    a.ev_ebitda,
    a.p_fcf,
    a.expected_ev_sales,
    a.residual_evs,
    a.residual_evebitda,
    a.residual_pfcf,
    a.anchor_residual,
    a.fundamental_anchor_z,
    a.anchor_method,
    a.n_peers,
    a.r2_sector,
    a.uses_forward,
    a.imputed_flags,
    a.model_version,
    a.event_date,
    a.knowledge_date
FROM latest a
INNER JOIN dbo.dim_security s ON a.security_sk = s.security_sk
WHERE a.row_number = 1 AND s.is_current = 1;
GO
