-- E22 Narrative Premium serving tables and view.

IF OBJECT_ID('dbo.fact_narrative_premium', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_narrative_premium (
        decision_id VARCHAR(64) NOT NULL,
        generation VARCHAR(64) NOT NULL,
        security_sk BIGINT NOT NULL,
        date_sk INT NOT NULL,
        fundamental_anchor_z FLOAT NULL,
        narrative_intensity FLOAT NULL,
        narrative_intensity_z FLOAT NULL,
        attribution_intercept FLOAT NULL,
        attribution_beta FLOAT NULL,
        attribution_r2 FLOAT NULL,
        narrative_premium FLOAT NULL,
        unexplained_residual FLOAT NULL,
        anchor_support_z FLOAT NULL,
        divergence_state VARCHAR(32) NULL,
        is_converging BIT NULL,
        eligible_security_count INT NOT NULL,
        coverage_status VARCHAR(16) NOT NULL,
        coverage_reasons_json VARCHAR(2048) NOT NULL,
        evidence_pack_json VARCHAR(8000) NOT NULL,
        input_snapshot_hash VARCHAR(64) NOT NULL,
        fit_context_hash VARCHAR(64) NULL,
        e20_model_version VARCHAR(32) NULL,
        e20_generation VARCHAR(64) NULL,
        e20_manifest_fingerprint VARCHAR(64) NULL,
        e21_model_version VARCHAR(64) NOT NULL,
        e21_manifest_fingerprint VARCHAR(64) NULL,
        prompt_version VARCHAR(64) NOT NULL,
        input_generation VARCHAR(64) NOT NULL,
        extraction_generation VARCHAR(64) NOT NULL,
        model_version VARCHAR(32) NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        created_at DATETIME2(6) NOT NULL
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_narrative_premium') AND name = 'fit_context_hash')
    ALTER TABLE dbo.fact_narrative_premium ADD fit_context_hash VARCHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_narrative_premium') AND name = 'e20_generation')
    ALTER TABLE dbo.fact_narrative_premium ADD e20_generation VARCHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_narrative_premium') AND name = 'e20_manifest_fingerprint')
    ALTER TABLE dbo.fact_narrative_premium ADD e20_manifest_fingerprint VARCHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_narrative_premium') AND name = 'e21_manifest_fingerprint')
    ALTER TABLE dbo.fact_narrative_premium ADD e21_manifest_fingerprint VARCHAR(64) NULL;
GO

IF OBJECT_ID('dbo.fact_narrative_premium_evidence', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_narrative_premium_evidence (
        decision_id VARCHAR(64) NOT NULL,
        evidence_ordinal INT NOT NULL,
        document_id VARCHAR(256) NOT NULL,
        input_snapshot_hash VARCHAR(64) NOT NULL,
        model_version VARCHAR(32) NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        created_at DATETIME2(6) NOT NULL
    );
END;
GO

IF OBJECT_ID('dbo.decision_log', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.decision_log (
        decision_id VARCHAR(64) NOT NULL,
        decision_type VARCHAR(32) NOT NULL,
        security_sk BIGINT NOT NULL,
        date_sk INT NOT NULL,
        output_status VARCHAR(16) NOT NULL,
        input_snapshot_hash VARCHAR(64) NOT NULL,
        model_version VARCHAR(32) NOT NULL,
        output_json VARCHAR(8000) NOT NULL,
        evidence_pack_json VARCHAR(8000) NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL,
        created_at DATETIME2(6) NOT NULL
    );
END;
GO

CREATE OR ALTER VIEW dbo.v_narrative_premium AS
SELECT
    p.decision_id,
    p.generation,
    p.security_sk,
    s.ticker,
    s.company_name,
    p.date_sk,
    p.fundamental_anchor_z,
    p.narrative_intensity,
    p.narrative_intensity_z,
    p.attribution_intercept,
    p.attribution_beta,
    p.attribution_r2,
    p.narrative_premium,
    p.unexplained_residual,
    p.anchor_support_z,
    p.divergence_state,
    p.is_converging,
    p.eligible_security_count,
    p.coverage_status,
    p.coverage_reasons_json,
    p.evidence_pack_json,
    p.input_snapshot_hash,
    p.fit_context_hash,
    p.e20_model_version,
    p.e20_generation,
    p.e20_manifest_fingerprint,
    p.e21_model_version,
    p.e21_manifest_fingerprint,
    p.prompt_version,
    p.input_generation,
    p.extraction_generation,
    p.model_version,
    p.event_date,
    p.knowledge_date,
    p.created_at
FROM dbo.fact_narrative_premium p
LEFT JOIN dbo.dim_security s
  ON s.security_sk = p.security_sk
WHERE p.event_date <= p.knowledge_date
  AND p.knowledge_date <= DATEFROMPARTS(
        p.date_sk / 10000,
        (p.date_sk / 100) % 100,
        p.date_sk % 100
  );
GO