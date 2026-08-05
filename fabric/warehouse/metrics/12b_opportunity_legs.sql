-- Auspex E14/E6b per-theme opportunity leg contract.

IF OBJECT_ID('dbo.fact_theme_opportunity_score', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_theme_opportunity_score (
        score_id VARCHAR(64) NOT NULL,
        generation VARCHAR(64) NOT NULL,
        cohort_snapshot_hash VARCHAR(64) NOT NULL,
        theme_id VARCHAR(128) NOT NULL,
        security_sk BIGINT NOT NULL,
        date_sk INT NOT NULL,
        as_of DATE NOT NULL,
        candidate_source VARCHAR(16) NOT NULL,
        candidate_snapshot_id VARCHAR(256) NULL,
        candidate_snapshot_ingest_ts DATETIME2(6) NULL,
        candidate_count INT NOT NULL,
        thesis_linkage_z FLOAT NULL,
        attention_acceleration_z FLOAT NULL,
        smart_money_z FLOAT NULL,
        fundamental_health_z FLOAT NULL,
        valuation_brake_z FLOAT NULL,
        crowding_positioning_z FLOAT NULL,
        thesis_linkage_contribution FLOAT NULL,
        attention_acceleration_contribution FLOAT NULL,
        smart_money_contribution FLOAT NULL,
        fundamental_health_contribution FLOAT NULL,
        valuation_brake_contribution FLOAT NULL,
        crowding_positioning_contribution FLOAT NULL,
        opportunity_score_raw FLOAT NULL,
        opportunity_score FLOAT NULL,
        coverage_status VARCHAR(16) NOT NULL,
        coverage_reasons_json VARCHAR(4000) NOT NULL,
        max_knowledge_date DATE NOT NULL,
        model_version VARCHAR(32) NOT NULL,
        weight_version VARCHAR(32) NOT NULL,
        created_at DATETIME2(6) NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_theme_opportunity_score') AND name = 'candidate_snapshot_id')
    ALTER TABLE dbo.fact_theme_opportunity_score ADD candidate_snapshot_id VARCHAR(256) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_theme_opportunity_score') AND name = 'candidate_snapshot_ingest_ts')
    ALTER TABLE dbo.fact_theme_opportunity_score ADD candidate_snapshot_ingest_ts DATETIME2(6) NULL;

IF OBJECT_ID('dbo.opportunity_score_snapshot_manifest', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.opportunity_score_snapshot_manifest (
        generation VARCHAR(64) NOT NULL,
        as_of_date DATE NOT NULL,
        model_version VARCHAR(32) NOT NULL,
        weight_version VARCHAR(32) NOT NULL,
        status VARCHAR(16) NOT NULL,
        row_count BIGINT NOT NULL,
        ready_count BIGINT NOT NULL,
        partial_count BIGINT NOT NULL,
        withheld_count BIGINT NOT NULL,
        fingerprint VARCHAR(64) NOT NULL,
        created_at DATETIME2(6) NOT NULL,
        completed_at DATETIME2(6) NULL
    );
END;
GO

CREATE OR ALTER VIEW dbo.v_opportunity_legs AS
SELECT
    s.score_id,
    s.generation,
    s.theme_id,
    s.security_sk,
    s.date_sk,
    s.as_of,
    s.candidate_source,
    s.candidate_snapshot_id,
    s.candidate_snapshot_ingest_ts,
    s.candidate_count,
    s.thesis_linkage_z,
    s.attention_acceleration_z,
    s.smart_money_z,
    s.fundamental_health_z,
    s.valuation_brake_z,
    s.crowding_positioning_z,
    s.coverage_status,
    s.coverage_reasons_json,
    s.cohort_snapshot_hash,
    s.model_version,
    s.weight_version,
    s.max_knowledge_date
FROM dbo.fact_theme_opportunity_score s
JOIN dbo.opportunity_score_snapshot_manifest m
  ON m.generation = s.generation
 AND m.as_of_date = s.as_of
 AND m.model_version = s.model_version
 AND m.weight_version = s.weight_version
 AND m.status = 'completed'
WHERE s.max_knowledge_date <= s.as_of
    AND s.model_version = 'e6b_v2'
  AND s.weight_version = 'e6b_balanced_v1';
GO
