-- Auspex E21 PIT narrative document features and daily intensity.

IF OBJECT_ID('dbo.fact_narrative_features', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_narrative_features (
        cache_key VARCHAR(64) NOT NULL,
        document_id VARCHAR(128) NOT NULL,
        security_sk BIGINT NOT NULL,
        symbol VARCHAR(32) NULL,
        source_id VARCHAR(512) NOT NULL,
        source_type VARCHAR(32) NOT NULL,
        document_revision_hash VARCHAR(64) NOT NULL,
        sentiment DECIMAL(12,8) NOT NULL,
        relevance DECIMAL(12,8) NOT NULL,
        forward_promise_ratio DECIMAL(12,8) NOT NULL,
        hype_density DECIMAL(12,8) NOT NULL,
        themes_json VARCHAR(8000) NOT NULL,
        evidence_quotes_json VARCHAR(8000) NOT NULL,
        theme_evidence_json VARCHAR(8000) NOT NULL,
        model_version VARCHAR(64) NOT NULL,
        prompt_version VARCHAR(64) NOT NULL,
        prompt_sha256 CHAR(64) NOT NULL,
        input_generation VARCHAR(64) NOT NULL,
        extraction_generation VARCHAR(64) NOT NULL,
        extracted_at DATETIME2(6) NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL
    );
END;
GO

IF OBJECT_ID('dbo.fact_narrative_intensity', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_narrative_intensity (
        security_sk BIGINT NOT NULL,
        date_sk INT NOT NULL,
        eligible_document_count BIGINT NOT NULL,
        extracted_document_count BIGINT NOT NULL,
        extraction_coverage DECIMAL(12,8) NOT NULL,
        sentiment_level DECIMAL(12,8) NULL,
        sentiment_strength DECIMAL(12,8) NULL,
        sentiment_velocity_z DECIMAL(18,8) NULL,
        sentiment_velocity_strength DECIMAL(12,8) NULL,
        theme_concentration DECIMAL(12,8) NULL,
        forward_promise_ratio DECIMAL(12,8) NULL,
        hype_density DECIMAL(12,8) NULL,
        news_volume_z_30d DECIMAL(18,8) NULL,
        news_attention DECIMAL(12,8) NULL,
        insider_net_buy_ratio_90d DECIMAL(18,8) NULL,
        insider_divergence DECIMAL(12,8) NULL,
        mgmt_reality_gap DECIMAL(18,8) NULL,
        revision_dispersion_z DECIMAL(18,8) NULL,
        options_skew DECIMAL(18,8) NULL,
        narrative_intensity DECIMAL(12,6) NULL,
        available_weight DECIMAL(12,8) NOT NULL,
        coverage_status VARCHAR(16) NOT NULL,
        coverage_reasons_json VARCHAR(8000) NOT NULL,
        evidence_document_ids_json VARCHAR(8000) NOT NULL,
        model_version VARCHAR(64) NOT NULL,
        prompt_version VARCHAR(64) NOT NULL,
        input_generation VARCHAR(64) NOT NULL,
        extraction_generation VARCHAR(64) NOT NULL,
        event_date DATE NOT NULL,
        knowledge_date DATE NOT NULL
    );
END;
GO

CREATE OR ALTER VIEW dbo.v_narrative_intensity AS
WITH latest AS (
    SELECT
        i.*,
        ROW_NUMBER() OVER (
            PARTITION BY i.security_sk, i.model_version, i.prompt_version
            ORDER BY i.date_sk DESC, i.knowledge_date DESC, i.extraction_generation DESC
        ) AS row_number
    FROM dbo.fact_narrative_intensity i
    WHERE i.knowledge_date <= DATEFROMPARTS(
        i.date_sk / 10000,
        (i.date_sk / 100) % 100,
        i.date_sk % 100
    )
)
SELECT
    i.security_sk,
    s.ticker,
    s.company_name,
    i.date_sk,
    i.eligible_document_count,
    i.extracted_document_count,
    i.extraction_coverage,
    i.sentiment_level,
    i.sentiment_strength,
    i.sentiment_velocity_z,
    i.sentiment_velocity_strength,
    i.theme_concentration,
    i.forward_promise_ratio,
    i.hype_density,
    i.news_volume_z_30d,
    i.news_attention,
    i.insider_net_buy_ratio_90d,
    i.insider_divergence,
    i.mgmt_reality_gap,
    i.revision_dispersion_z,
    i.options_skew,
    i.narrative_intensity,
    i.available_weight,
    i.coverage_status,
    i.coverage_reasons_json,
    i.evidence_document_ids_json,
    i.model_version,
    i.prompt_version,
    i.input_generation,
    i.extraction_generation,
    i.event_date,
    i.knowledge_date
FROM latest i
INNER JOIN dbo.dim_security s ON i.security_sk = s.security_sk
WHERE i.row_number = 1
  AND s.is_current = 1;
GO