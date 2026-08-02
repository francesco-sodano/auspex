-- Promote the latest completed E21 Lakehouse snapshot into Fabric Warehouse.

CREATE OR ALTER PROCEDURE dbo.usp_promote_narrative_snapshot
    @as_of_date DATE = NULL
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @generation VARCHAR(64);
    DECLARE @snapshot_as_of DATE;
    DECLARE @expected_feature_count BIGINT;
    DECLARE @expected_intensity_count BIGINT;
    DECLARE @fingerprint VARCHAR(64);
    DECLARE @input_generation VARCHAR(64);

    SELECT TOP 1
        @generation = generation,
        @snapshot_as_of = as_of_date,
        @expected_feature_count = feature_count,
        @expected_intensity_count = intensity_count,
        @fingerprint = fingerprint
    FROM auspex_bronze.dbo.narrative_snapshot_manifest
    WHERE status = 'completed'
      AND (@as_of_date IS NULL OR as_of_date = @as_of_date)
    ORDER BY completed_at DESC, generation DESC;

    IF @generation IS NULL
        THROW 51300, 'No completed E21 Lakehouse manifest is available.', 1;

    IF @fingerprint IS NULL OR LEN(@fingerprint) <> 64
        THROW 51301, 'E21 Lakehouse manifest fingerprint is invalid.', 1;

    IF @expected_feature_count <> (
        SELECT COUNT_BIG(*)
        FROM auspex_bronze.dbo.fact_narrative_features
        WHERE extraction_generation = @generation
    )
       OR @expected_intensity_count <> (
        SELECT COUNT_BIG(*)
        FROM auspex_bronze.dbo.fact_narrative_intensity
        WHERE extraction_generation = @generation
          AND date_sk = YEAR(@snapshot_as_of) * 10000
              + MONTH(@snapshot_as_of) * 100
              + DAY(@snapshot_as_of)
    )
        THROW 51302, 'E21 Lakehouse manifest row counts do not reconcile.', 1;

    IF EXISTS (
        SELECT 1
        FROM auspex_bronze.dbo.fact_narrative_features f
        LEFT JOIN auspex_bronze.dbo.narrative_snapshot_manifest m
          ON f.extraction_generation = m.generation
         AND m.status = 'completed'
                WHERE f.extraction_generation = @generation
                    AND m.generation IS NULL
    )
       OR EXISTS (
        SELECT 1
        FROM auspex_bronze.dbo.fact_narrative_intensity i
        LEFT JOIN auspex_bronze.dbo.narrative_snapshot_manifest m
          ON i.extraction_generation = m.generation
         AND m.status = 'completed'
                WHERE i.extraction_generation = @generation
                    AND i.date_sk = YEAR(@snapshot_as_of) * 10000
                            + MONTH(@snapshot_as_of) * 100
                            + DAY(@snapshot_as_of)
                    AND m.generation IS NULL
    )
        THROW 51303, 'E21 Lakehouse rows reference an incomplete snapshot.', 1;

    IF (
        SELECT COUNT_BIG(*)
        FROM (
            SELECT input_generation
            FROM auspex_bronze.dbo.fact_narrative_features
            WHERE extraction_generation = @generation
            GROUP BY input_generation
        ) g
    ) <> 1
        THROW 51304, 'E21 Lakehouse snapshot has an invalid input generation.', 1;

    SELECT TOP 1 @input_generation = input_generation
    FROM auspex_bronze.dbo.fact_narrative_features
    WHERE extraction_generation = @generation;

    IF EXISTS (
        SELECT 1
        FROM auspex_bronze.dbo.fact_narrative_intensity
        WHERE extraction_generation = @generation
          AND input_generation <> @input_generation
    )
        THROW 51305, 'E21 Lakehouse feature and intensity generations do not match.', 1;

    BEGIN TRY
        BEGIN TRANSACTION;

        DELETE FROM dbo.fact_narrative_features;
        DELETE FROM dbo.fact_narrative_intensity;

        INSERT INTO dbo.fact_narrative_features (
            cache_key, document_id, security_sk, symbol, source_id, source_type,
            document_revision_hash, sentiment, relevance, forward_promise_ratio,
            hype_density, themes_json, evidence_quotes_json, theme_evidence_json,
            model_version, prompt_version, prompt_sha256, input_generation, extraction_generation,
            extracted_at, event_date, knowledge_date
        )
        SELECT
            cache_key, document_id, security_sk, symbol, source_id, source_type,
            document_revision_hash, sentiment, relevance, forward_promise_ratio,
            hype_density, themes_json, evidence_quotes_json, theme_evidence_json,
            model_version, prompt_version, prompt_sha256, input_generation, extraction_generation,
            extracted_at, event_date, knowledge_date
        FROM auspex_bronze.dbo.fact_narrative_features
        WHERE extraction_generation = @generation;

        INSERT INTO dbo.fact_narrative_intensity (
            security_sk, date_sk, eligible_document_count, extracted_document_count,
            extraction_coverage, sentiment_level, sentiment_strength,
            sentiment_velocity_z, sentiment_velocity_strength, theme_concentration,
            forward_promise_ratio, hype_density, news_volume_z_30d, news_attention,
            insider_net_buy_ratio_90d, insider_divergence, mgmt_reality_gap,
            revision_dispersion_z, options_skew, narrative_intensity, available_weight,
            coverage_status, coverage_reasons_json, evidence_document_ids_json,
            model_version, prompt_version, input_generation, extraction_generation,
            event_date, knowledge_date
        )
        SELECT
            security_sk, date_sk, eligible_document_count, extracted_document_count,
            extraction_coverage, sentiment_level, sentiment_strength,
            sentiment_velocity_z, sentiment_velocity_strength, theme_concentration,
            forward_promise_ratio, hype_density, news_volume_z_30d, news_attention,
            insider_net_buy_ratio_90d, insider_divergence, mgmt_reality_gap,
            revision_dispersion_z, options_skew, narrative_intensity, available_weight,
            coverage_status, coverage_reasons_json, evidence_document_ids_json,
            model_version, prompt_version, input_generation, extraction_generation,
            event_date, knowledge_date
                FROM auspex_bronze.dbo.fact_narrative_intensity
                WHERE extraction_generation = @generation
                    AND date_sk = YEAR(@snapshot_as_of) * 10000
                            + MONTH(@snapshot_as_of) * 100
                            + DAY(@snapshot_as_of);

        IF (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_features)
                     <> @expected_feature_count
           OR (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_intensity)
                     <> @expected_intensity_count
            THROW 51306, 'E21 Warehouse replacement row counts do not reconcile.', 1;

        IF EXISTS (
            SELECT cache_key
            FROM dbo.fact_narrative_features
            GROUP BY cache_key
            HAVING COUNT_BIG(*) > 1
        )
           OR EXISTS (
            SELECT document_id, document_revision_hash
            FROM dbo.fact_narrative_features
            GROUP BY document_id, document_revision_hash
            HAVING COUNT_BIG(*) > 1
        )
           OR EXISTS (
            SELECT security_sk, date_sk, model_version, prompt_version
            FROM dbo.fact_narrative_intensity
            GROUP BY security_sk, date_sk, model_version, prompt_version
            HAVING COUNT_BIG(*) > 1
        )
            THROW 51307, 'E21 Warehouse duplicate grain validation failed.', 1;

        IF EXISTS (
            SELECT 1
              FROM dbo.fact_narrative_features f
              WHERE f.event_date > f.knowledge_date
                  OR f.knowledge_date > CAST(SYSUTCDATETIME() AS DATE)
        )
           OR EXISTS (
            SELECT 1
            FROM dbo.fact_narrative_intensity
            WHERE event_date > knowledge_date
               OR knowledge_date > DATEFROMPARTS(
                    date_sk / 10000,
                    (date_sk / 100) % 100,
                    date_sk % 100
               )
        )
            THROW 51308, 'E21 Warehouse PIT validation failed.', 1;

        IF EXISTS (
            SELECT 1
            FROM dbo.fact_narrative_features
            WHERE source_type <> 'news'
               OR security_sk IS NULL
               OR sentiment NOT BETWEEN -1.0 AND 1.0
               OR relevance NOT BETWEEN 0.0 AND 1.0
               OR forward_promise_ratio NOT BETWEEN 0.0 AND 1.0
               OR hype_density NOT BETWEEN 0.0 AND 1.0
               OR model_version <> 'gpt-4o:2024-11-20'
               OR prompt_version <> 'e21_narrative_v1'
               OR prompt_sha256 <> '70987525ba240b9008ec684c5cab346cfd02b10f8315d7c2f66adff381c930a5'
               OR input_generation IS NULL
               OR extraction_generation IS NULL
        )
            THROW 51309, 'E21 Warehouse document feature validation failed.', 1;

        IF EXISTS (
            SELECT 1
            FROM dbo.fact_narrative_intensity
            WHERE eligible_document_count < 0
               OR extracted_document_count < 0
               OR extracted_document_count > eligible_document_count
               OR extraction_coverage NOT BETWEEN 0.0 AND 1.0
               OR available_weight NOT BETWEEN 0.0 AND 1.0
               OR (sentiment_level IS NOT NULL AND sentiment_level NOT BETWEEN -1.0 AND 1.0)
               OR (sentiment_strength IS NOT NULL AND sentiment_strength NOT BETWEEN 0.0 AND 1.0)
               OR (sentiment_velocity_strength IS NOT NULL AND sentiment_velocity_strength NOT BETWEEN 0.0 AND 1.0)
               OR (theme_concentration IS NOT NULL AND theme_concentration NOT BETWEEN 0.0 AND 1.0)
               OR (forward_promise_ratio IS NOT NULL AND forward_promise_ratio NOT BETWEEN 0.0 AND 1.0)
               OR (hype_density IS NOT NULL AND hype_density NOT BETWEEN 0.0 AND 1.0)
               OR (news_attention IS NOT NULL AND news_attention NOT BETWEEN 0.0 AND 1.0)
               OR (insider_divergence IS NOT NULL AND insider_divergence NOT BETWEEN 0.0 AND 1.0)
               OR (narrative_intensity IS NOT NULL AND narrative_intensity NOT BETWEEN 0.0 AND 100.0)
               OR model_version <> 'gpt-4o:2024-11-20'
               OR prompt_version <> 'e21_narrative_v1'
               OR input_generation IS NULL
               OR extraction_generation IS NULL
        )
            THROW 51310, 'E21 Warehouse intensity range validation failed.', 1;

        IF EXISTS (
            SELECT 1
            FROM dbo.fact_narrative_intensity
            WHERE coverage_status NOT IN ('WITHHELD', 'PARTIAL')
               OR (coverage_status = 'WITHHELD' AND narrative_intensity IS NOT NULL)
               OR (coverage_status = 'PARTIAL' AND narrative_intensity IS NULL)
               OR coverage_status = 'READY'
               OR mgmt_reality_gap IS NOT NULL
               OR revision_dispersion_z IS NOT NULL
               OR options_skew IS NOT NULL
               OR coverage_reasons_json IS NULL
               OR coverage_reasons_json NOT LIKE '%mgmt_reality_gap:source_unavailable%'
               OR coverage_reasons_json NOT LIKE '%revision_dispersion_z:source_unavailable%'
               OR coverage_reasons_json NOT LIKE '%options_skew:source_unavailable%'
        )
            THROW 51311, 'E21 Warehouse coverage validation failed.', 1;

        IF EXISTS (
            SELECT 1
            FROM dbo.fact_narrative_features
            WHERE extraction_generation = @generation
              AND input_generation <> @input_generation
        )
           OR EXISTS (
            SELECT 1
            FROM dbo.fact_narrative_intensity
            WHERE extraction_generation = @generation
              AND input_generation <> @input_generation
        )
            THROW 51312, 'E21 Warehouse selected generation validation failed.', 1;

        IF @expected_feature_count <> (
            SELECT COUNT_BIG(*)
            FROM dbo.fact_narrative_features
            WHERE extraction_generation = @generation
        )
           OR @expected_intensity_count <> (
            SELECT COUNT_BIG(*)
            FROM dbo.fact_narrative_intensity
            WHERE extraction_generation = @generation
              AND date_sk = YEAR(@snapshot_as_of) * 10000
                  + MONTH(@snapshot_as_of) * 100
                  + DAY(@snapshot_as_of)
        )
            THROW 51313, 'E21 Warehouse manifest counts do not reconcile after promotion.', 1;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO