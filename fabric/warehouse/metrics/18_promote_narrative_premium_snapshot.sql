-- Promote one completed E22 snapshot and append its immutable decision log.

CREATE OR ALTER PROCEDURE dbo.usp_promote_narrative_premium_snapshot
    @as_of_date DATE = NULL,
    @emit_result BIT = 1
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @generation VARCHAR(64);
    DECLARE @snapshot_as_of DATE;
    DECLARE @input_snapshot_hash VARCHAR(64);
    DECLARE @expected_row_count BIGINT;
    DECLARE @expected_evidence_count BIGINT;
    DECLARE @expected_ready_count BIGINT;
    DECLARE @expected_partial_count BIGINT;
    DECLARE @expected_withheld_count BIGINT;
    DECLARE @fingerprint VARCHAR(64);
    DECLARE @date_sk INT;
    DECLARE @decision_log_inserted BIGINT = 0;
    DECLARE @evidence_inserted BIGINT = 0;
    DECLARE @decision_row_hashes VARCHAR(MAX);
    DECLARE @evidence_row_hashes VARCHAR(MAX);
    DECLARE @computed_fingerprint VARCHAR(64);

    SELECT TOP 1
        @generation = generation,
        @snapshot_as_of = as_of_date,
        @input_snapshot_hash = input_snapshot_hash,
        @expected_row_count = row_count,
        @expected_evidence_count = evidence_count,
        @expected_ready_count = ready_count,
        @expected_partial_count = partial_count,
        @expected_withheld_count = withheld_count,
        @fingerprint = fingerprint
    FROM auspex_bronze.dbo.narrative_premium_snapshot_manifest
    WHERE status = 'completed'
      AND (@as_of_date IS NULL OR as_of_date = @as_of_date)
    ORDER BY completed_at DESC, generation DESC;

    IF @generation IS NULL
        THROW 51400, 'No completed E22 Lakehouse manifest is available.', 1;

    SET @date_sk = YEAR(@snapshot_as_of) * 10000
        + MONTH(@snapshot_as_of) * 100
        + DAY(@snapshot_as_of);

    IF @fingerprint IS NULL OR LEN(@fingerprint) <> 64
       OR @input_snapshot_hash IS NULL OR LEN(@input_snapshot_hash) <> 64
        THROW 51401, 'E22 Lakehouse manifest hash contract is invalid.', 1;

    IF @expected_row_count <> (
        SELECT COUNT_BIG(*)
        FROM auspex_bronze.dbo.fact_narrative_premium
        WHERE generation = @generation AND date_sk = @date_sk
    )
       OR @expected_ready_count <> (
        SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_narrative_premium
        WHERE generation = @generation AND date_sk = @date_sk AND coverage_status = 'READY'
    )
       OR @expected_partial_count <> (
        SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_narrative_premium
        WHERE generation = @generation AND date_sk = @date_sk AND coverage_status = 'PARTIAL'
    )
       OR @expected_withheld_count <> (
        SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_narrative_premium
        WHERE generation = @generation AND date_sk = @date_sk AND coverage_status = 'WITHHELD'
    )
        THROW 51402, 'E22 Lakehouse manifest counts do not reconcile.', 1;

    IF @expected_evidence_count <> (
        SELECT COUNT_BIG(*)
        FROM auspex_bronze.dbo.fact_narrative_premium_evidence
        WHERE input_snapshot_hash = @input_snapshot_hash
          AND model_version = 'e22_v4'
    )
        THROW 51410, 'E22 Lakehouse evidence count does not reconcile.', 1;

    IF EXISTS (
        SELECT 1
        FROM auspex_bronze.dbo.fact_narrative_premium f
        OUTER APPLY (
            SELECT
                COUNT_BIG(*) AS evidence_count,
                LOWER(CONVERT(VARCHAR(64), HASHBYTES(
                    'SHA2_256',
                    CONCAT(
                        '[',
                        COALESCE(
                            STRING_AGG(
                                CAST(CONCAT('"', STRING_ESCAPE(e.document_id, 'json'), '"') AS VARCHAR(MAX)),
                                ','
                            ) WITHIN GROUP (ORDER BY e.evidence_ordinal),
                            ''
                        ),
                        ']'
                    )
                ), 2)) AS evidence_hash
            FROM auspex_bronze.dbo.fact_narrative_premium_evidence e
            WHERE e.decision_id = f.decision_id
              AND e.input_snapshot_hash = @input_snapshot_hash
              AND e.model_version = 'e22_v4'
        ) e
        WHERE f.generation = @generation
          AND f.date_sk = @date_sk
          AND (
              TRY_CAST(JSON_VALUE(f.evidence_pack_json, '$.narrative.evidence_document_count') AS BIGINT) IS NULL
              OR JSON_VALUE(f.evidence_pack_json, '$.narrative.evidence_document_hash') IS NULL
              OR TRY_CAST(JSON_VALUE(f.evidence_pack_json, '$.narrative.evidence_document_count') AS BIGINT)
                  <> e.evidence_count
              OR JSON_VALUE(f.evidence_pack_json, '$.narrative.evidence_document_hash')
                  <> e.evidence_hash
          )
    )
        THROW 51414, 'E22 Lakehouse evidence hash does not reconcile.', 1;

    SELECT @decision_row_hashes = STRING_AGG(
        CAST(row_hash AS VARCHAR(MAX)), '|'
    ) WITHIN GROUP (ORDER BY row_hash)
    FROM (
        SELECT LOWER(CONVERT(VARCHAR(64), HASHBYTES(
            'SHA2_256',
            CONCAT_WS(
                CHAR(31),
                decision_id,
                decision_type,
                CONVERT(VARCHAR(20), security_sk),
                CONVERT(VARCHAR(10), date_sk),
                output_status,
                input_snapshot_hash,
                model_version,
                output_json,
                evidence_pack_json,
                CONVERT(CHAR(10), event_date, 23),
                CONVERT(CHAR(10), knowledge_date, 23)
            )
        ), 2)) AS row_hash
        FROM auspex_bronze.dbo.decision_log
        WHERE decision_type = 'NARRATIVE_PREMIUM'
          AND input_snapshot_hash = @input_snapshot_hash
          AND date_sk = @date_sk
          AND model_version = 'e22_v4'
    ) h;

    SELECT @evidence_row_hashes = STRING_AGG(
        CAST(row_hash AS VARCHAR(MAX)), '|'
    ) WITHIN GROUP (ORDER BY row_hash)
    FROM (
        SELECT LOWER(CONVERT(VARCHAR(64), HASHBYTES(
            'SHA2_256',
            CONCAT_WS(
                CHAR(31),
                decision_id,
                CONVERT(VARCHAR(10), evidence_ordinal),
                document_id,
                input_snapshot_hash,
                model_version,
                CONVERT(CHAR(10), event_date, 23),
                CONVERT(CHAR(10), knowledge_date, 23)
            )
        ), 2)) AS row_hash
        FROM auspex_bronze.dbo.fact_narrative_premium_evidence
        WHERE input_snapshot_hash = @input_snapshot_hash
          AND model_version = 'e22_v4'
    ) h;

    SET @computed_fingerprint = LOWER(CONVERT(VARCHAR(64), HASHBYTES(
        'SHA2_256',
        CONCAT(COALESCE(@decision_row_hashes, ''), '|', COALESCE(@evidence_row_hashes, ''))
    ), 2));
    IF @computed_fingerprint <> @fingerprint
        THROW 51416, 'E22 Lakehouse snapshot fingerprint does not reconcile.', 1;

    IF EXISTS (
        SELECT 1
        FROM auspex_bronze.dbo.fact_narrative_premium f
        LEFT JOIN auspex_bronze.dbo.decision_log d
          ON d.decision_id = f.decision_id
         AND d.decision_type = 'NARRATIVE_PREMIUM'
        WHERE f.generation = @generation
          AND f.date_sk = @date_sk
          AND (
              d.decision_id IS NULL
              OR d.security_sk <> f.security_sk
              OR d.date_sk <> f.date_sk
              OR d.output_status <> f.coverage_status
              OR d.input_snapshot_hash <> f.input_snapshot_hash
              OR d.model_version <> f.model_version
              OR d.evidence_pack_json <> f.evidence_pack_json
              OR d.event_date <> f.event_date
              OR d.knowledge_date <> f.knowledge_date
              OR EXISTS (
                  SELECT
                      f.decision_id,
                      f.security_sk,
                      f.date_sk,
                      f.fundamental_anchor_z,
                      f.narrative_intensity,
                      f.narrative_intensity_z,
                      f.attribution_intercept,
                      f.attribution_beta,
                      f.attribution_r2,
                      f.narrative_premium,
                      f.unexplained_residual,
                      f.anchor_support_z,
                      f.divergence_state,
                      f.is_converging,
                      f.eligible_security_count,
                      f.coverage_status,
                      f.coverage_reasons_json,
                      f.model_version,
                      f.fit_context_hash
                  EXCEPT
                  SELECT
                      JSON_VALUE(d.output_json, '$.decision_id'),
                      TRY_CAST(JSON_VALUE(d.output_json, '$.security_sk') AS BIGINT),
                      TRY_CAST(JSON_VALUE(d.output_json, '$.date_sk') AS INT),
                      TRY_CAST(JSON_VALUE(d.output_json, '$.fundamental_anchor_z') AS FLOAT),
                      TRY_CAST(JSON_VALUE(d.output_json, '$.narrative_intensity') AS FLOAT),
                      TRY_CAST(JSON_VALUE(d.output_json, '$.narrative_intensity_z') AS FLOAT),
                      TRY_CAST(JSON_VALUE(d.output_json, '$.attribution_intercept') AS FLOAT),
                      TRY_CAST(JSON_VALUE(d.output_json, '$.attribution_beta') AS FLOAT),
                      TRY_CAST(JSON_VALUE(d.output_json, '$.attribution_r2') AS FLOAT),
                      TRY_CAST(JSON_VALUE(d.output_json, '$.narrative_premium') AS FLOAT),
                      TRY_CAST(JSON_VALUE(d.output_json, '$.unexplained_residual') AS FLOAT),
                      TRY_CAST(JSON_VALUE(d.output_json, '$.anchor_support_z') AS FLOAT),
                      JSON_VALUE(d.output_json, '$.divergence_state'),
                      CASE JSON_VALUE(d.output_json, '$.is_converging')
                          WHEN 'true' THEN CAST(1 AS BIT)
                          WHEN 'false' THEN CAST(0 AS BIT)
                          ELSE NULL
                      END,
                      TRY_CAST(JSON_VALUE(d.output_json, '$.eligible_security_count') AS INT),
                      JSON_VALUE(d.output_json, '$.coverage_status'),
                      JSON_QUERY(d.output_json, '$.coverage_reasons'),
                      JSON_VALUE(d.output_json, '$.model_version'),
                      JSON_VALUE(d.output_json, '$.fit_context_hash')
              )
          )
    )
        THROW 51417, 'E22 Lakehouse fact and decision payloads do not reconcile.', 1;

    IF @expected_row_count <> (
        SELECT COUNT_BIG(*)
        FROM auspex_bronze.dbo.decision_log
        WHERE decision_type = 'NARRATIVE_PREMIUM'
          AND input_snapshot_hash = @input_snapshot_hash
          AND date_sk = @date_sk
          AND model_version = 'e22_v4'
    )
        THROW 51403, 'E22 Lakehouse decision log count does not reconcile.', 1;

    IF EXISTS (
        SELECT 1
        FROM auspex_bronze.dbo.decision_log s
        JOIN dbo.decision_log t ON t.decision_id = s.decision_id
        WHERE s.decision_type = 'NARRATIVE_PREMIUM'
          AND s.input_snapshot_hash = @input_snapshot_hash
          AND s.date_sk = @date_sk
          AND EXISTS (
              SELECT
                  s.decision_type, s.security_sk, s.date_sk, s.output_status,
                  s.input_snapshot_hash, s.model_version, s.output_json,
                  s.evidence_pack_json, s.event_date, s.knowledge_date
              EXCEPT
              SELECT
                  t.decision_type, t.security_sk, t.date_sk, t.output_status,
                  t.input_snapshot_hash, t.model_version, t.output_json,
                  t.evidence_pack_json, t.event_date, t.knowledge_date
          )
    )
        THROW 51404, 'E22 Warehouse decision log immutable conflict.', 1;

    IF EXISTS (
        SELECT 1
        FROM auspex_bronze.dbo.fact_narrative_premium_evidence s
        JOIN dbo.fact_narrative_premium_evidence t
          ON t.decision_id = s.decision_id
         AND t.evidence_ordinal = s.evidence_ordinal
        WHERE s.input_snapshot_hash = @input_snapshot_hash
          AND s.model_version = 'e22_v4'
          AND EXISTS (
              SELECT
                  s.document_id, s.input_snapshot_hash, s.model_version,
                  s.event_date, s.knowledge_date
              EXCEPT
              SELECT
                  t.document_id, t.input_snapshot_hash, t.model_version,
                  t.event_date, t.knowledge_date
          )
    )
        THROW 51411, 'E22 Warehouse evidence immutable conflict.', 1;

    BEGIN TRY
        BEGIN TRANSACTION;

        DELETE FROM dbo.fact_narrative_premium;

        INSERT INTO dbo.fact_narrative_premium (
            decision_id, generation, security_sk, date_sk, fundamental_anchor_z,
            narrative_intensity, narrative_intensity_z, attribution_intercept,
            attribution_beta, attribution_r2, narrative_premium, unexplained_residual,
            anchor_support_z, divergence_state, is_converging, eligible_security_count,
            coverage_status, coverage_reasons_json, evidence_pack_json,
            input_snapshot_hash, fit_context_hash, e20_model_version,
            e20_generation, e20_manifest_fingerprint, e21_model_version,
            e21_manifest_fingerprint, prompt_version,
            input_generation, extraction_generation, model_version, event_date,
            knowledge_date, created_at
        )
        SELECT
            decision_id, generation, security_sk, date_sk, fundamental_anchor_z,
            narrative_intensity, narrative_intensity_z, attribution_intercept,
            attribution_beta, attribution_r2, narrative_premium, unexplained_residual,
            anchor_support_z, divergence_state, is_converging, eligible_security_count,
            coverage_status, coverage_reasons_json, evidence_pack_json,
            input_snapshot_hash, fit_context_hash, e20_model_version,
            e20_generation, e20_manifest_fingerprint, e21_model_version,
            e21_manifest_fingerprint, prompt_version,
            input_generation, extraction_generation, model_version, event_date,
            knowledge_date, created_at
        FROM auspex_bronze.dbo.fact_narrative_premium
        WHERE generation = @generation AND date_sk = @date_sk;

        INSERT INTO dbo.decision_log (
            decision_id, decision_type, security_sk, date_sk, output_status,
            input_snapshot_hash, model_version, output_json, evidence_pack_json,
            event_date, knowledge_date, created_at
        )
        SELECT
            s.decision_id, s.decision_type, s.security_sk, s.date_sk, s.output_status,
            s.input_snapshot_hash, s.model_version, s.output_json, s.evidence_pack_json,
            s.event_date, s.knowledge_date, s.created_at
        FROM auspex_bronze.dbo.decision_log s
        WHERE s.decision_type = 'NARRATIVE_PREMIUM'
          AND s.input_snapshot_hash = @input_snapshot_hash
          AND s.date_sk = @date_sk
          AND NOT EXISTS (
              SELECT 1 FROM dbo.decision_log t WHERE t.decision_id = s.decision_id
          );
        SET @decision_log_inserted = @@ROWCOUNT;

                INSERT INTO dbo.fact_narrative_premium_evidence (
                        decision_id, evidence_ordinal, document_id, input_snapshot_hash,
                        model_version, event_date, knowledge_date, created_at
                )
                SELECT
                        s.decision_id, s.evidence_ordinal, s.document_id, s.input_snapshot_hash,
                        s.model_version, s.event_date, s.knowledge_date, s.created_at
                FROM auspex_bronze.dbo.fact_narrative_premium_evidence s
                WHERE s.input_snapshot_hash = @input_snapshot_hash
                      AND s.model_version = 'e22_v4'
                    AND NOT EXISTS (
                            SELECT 1
                            FROM dbo.fact_narrative_premium_evidence t
                            WHERE t.decision_id = s.decision_id
                                AND t.evidence_ordinal = s.evidence_ordinal
                    );
                SET @evidence_inserted = @@ROWCOUNT;

        IF (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_premium) <> @expected_row_count
            THROW 51405, 'E22 Warehouse fact count does not reconcile.', 1;

        IF EXISTS (
            SELECT decision_id FROM dbo.fact_narrative_premium
            GROUP BY decision_id HAVING COUNT_BIG(*) > 1
        )
           OR EXISTS (
            SELECT decision_id FROM dbo.decision_log
            GROUP BY decision_id HAVING COUNT_BIG(*) > 1
        )
           OR EXISTS (
            SELECT decision_id, evidence_ordinal
            FROM dbo.fact_narrative_premium_evidence
            GROUP BY decision_id, evidence_ordinal HAVING COUNT_BIG(*) > 1
        )
            THROW 51406, 'E22 Warehouse decision grain validation failed.', 1;

        IF EXISTS (
            SELECT 1 FROM dbo.fact_narrative_premium
            WHERE event_date > knowledge_date
               OR knowledge_date > @snapshot_as_of
               OR LEN(decision_id) <> 64
               OR LEN(input_snapshot_hash) <> 64
               OR fit_context_hash IS NULL
               OR LEN(fit_context_hash) <> 64
               OR e20_generation IS NULL
               OR LEN(e20_manifest_fingerprint) <> 64
               OR LEN(e21_manifest_fingerprint) <> 64
               OR model_version <> 'e22_v4'
               OR e21_model_version <> 'gpt-4o:2024-11-20'
               OR prompt_version <> 'e21_narrative_v1'
        )
            THROW 51407, 'E22 Warehouse PIT/version validation failed.', 1;

        IF EXISTS (
            SELECT 1 FROM dbo.fact_narrative_premium
            WHERE coverage_status NOT IN ('READY', 'PARTIAL', 'WITHHELD')
               OR coverage_reasons_json IS NULL
               OR evidence_pack_json IS NULL
               OR (coverage_status = 'WITHHELD' AND (
                    narrative_intensity_z IS NOT NULL
                    OR attribution_intercept IS NOT NULL
                    OR attribution_beta IS NOT NULL
                    OR attribution_r2 IS NOT NULL
                    OR narrative_premium IS NOT NULL
                    OR unexplained_residual IS NOT NULL
                    OR anchor_support_z IS NOT NULL
                    OR divergence_state IS NOT NULL
                    OR is_converging IS NOT NULL
               ))
               OR (coverage_status IN ('READY', 'PARTIAL') AND (
                    fundamental_anchor_z IS NULL
                    OR narrative_intensity_z IS NULL
                    OR attribution_intercept IS NULL
                    OR attribution_beta IS NULL
                    OR attribution_r2 IS NULL
                    OR narrative_premium IS NULL
                    OR unexplained_residual IS NULL
                    OR anchor_support_z IS NULL
                    OR divergence_state IS NULL
                    OR eligible_security_count < 8
               ))
        )
            THROW 51408, 'E22 Warehouse coverage validation failed.', 1;

        IF EXISTS (
            SELECT 1 FROM dbo.fact_narrative_premium
            WHERE narrative_premium IS NOT NULL
              AND ABS(
                    fundamental_anchor_z - attribution_intercept
                    - narrative_premium - unexplained_residual
              ) > 0.00000001
        )
            THROW 51409, 'E22 Warehouse attribution reconciliation failed.', 1;

        IF EXISTS (
            SELECT 1 FROM dbo.decision_log
            WHERE decision_type = 'NARRATIVE_PREMIUM'
              AND input_snapshot_hash = @input_snapshot_hash
              AND (LEN(output_json) > 8000 OR LEN(evidence_pack_json) > 8000)
        )
            THROW 51412, 'E22 Warehouse compact payload validation failed.', 1;

        IF @expected_evidence_count <> (
            SELECT COUNT_BIG(*) FROM dbo.fact_narrative_premium_evidence
            WHERE input_snapshot_hash = @input_snapshot_hash
              AND model_version = 'e22_v4'
        )
            THROW 51413, 'E22 Warehouse evidence replacement count does not reconcile.', 1;

        IF EXISTS (
            SELECT 1
            FROM dbo.fact_narrative_premium f
            OUTER APPLY (
                SELECT
                    COUNT_BIG(*) AS evidence_count,
                    LOWER(CONVERT(VARCHAR(64), HASHBYTES(
                        'SHA2_256',
                        CONCAT(
                            '[',
                            COALESCE(
                                STRING_AGG(
                                    CAST(CONCAT('"', STRING_ESCAPE(e.document_id, 'json'), '"') AS VARCHAR(MAX)),
                                    ','
                                ) WITHIN GROUP (ORDER BY e.evidence_ordinal),
                                ''
                            ),
                            ']'
                        )
                    ), 2)) AS evidence_hash
                FROM dbo.fact_narrative_premium_evidence e
                WHERE e.decision_id = f.decision_id
                  AND e.input_snapshot_hash = @input_snapshot_hash
                  AND e.model_version = 'e22_v4'
            ) e
            WHERE f.generation = @generation
              AND f.date_sk = @date_sk
              AND (
                  TRY_CAST(JSON_VALUE(f.evidence_pack_json, '$.narrative.evidence_document_count') AS BIGINT) IS NULL
                  OR JSON_VALUE(f.evidence_pack_json, '$.narrative.evidence_document_hash') IS NULL
                  OR TRY_CAST(JSON_VALUE(f.evidence_pack_json, '$.narrative.evidence_document_count') AS BIGINT)
                      <> e.evidence_count
                  OR JSON_VALUE(f.evidence_pack_json, '$.narrative.evidence_document_hash')
                      <> e.evidence_hash
              )
        )
            THROW 51415, 'E22 Warehouse evidence hash does not reconcile.', 1;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
        THROW;
    END CATCH;

    IF @emit_result = 1
        SELECT
            @generation AS generation,
            @snapshot_as_of AS as_of_date,
            @input_snapshot_hash AS input_snapshot_hash,
            @fingerprint AS fingerprint,
            (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_premium) AS row_count,
            (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_premium WHERE coverage_status = 'READY') AS ready_count,
            (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_premium WHERE coverage_status = 'PARTIAL') AS partial_count,
            (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_premium WHERE coverage_status = 'WITHHELD') AS withheld_count,
            @decision_log_inserted AS decision_log_inserted,
            @expected_evidence_count AS evidence_count,
            @evidence_inserted AS evidence_inserted;
END;
GO

IF OBJECT_ID('dbo.e22_release_audit', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.e22_release_audit (
        release_run_id VARCHAR(64) NOT NULL,
        as_of_date DATE NOT NULL,
        e22_generation VARCHAR(64) NOT NULL,
        e22_fingerprint VARCHAR(64) NOT NULL,
        e22_row_count BIGINT NOT NULL,
        gold_source_row_count BIGINT NOT NULL,
        gold_target_row_count BIGINT NOT NULL,
        completed_at DATETIME2(6) NOT NULL,
        status VARCHAR(16) NOT NULL
    );
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_promote_e22_release
    @as_of_date DATE,
    @release_run_id VARCHAR(64),
    @expected_fingerprint VARCHAR(64)
AS
BEGIN
    SET NOCOUNT ON;

    IF EXISTS (SELECT 1 FROM dbo.e22_release_audit WHERE release_run_id = @release_run_id)
        THROW 51430, 'E22 release run ID already exists.', 1;

    DECLARE @generation VARCHAR(64);
    DECLARE @manifest_fingerprint VARCHAR(64);
    DECLARE @premium_row_count BIGINT;
    DECLARE @gold_source_row_count BIGINT;
    DECLARE @gold_target_row_count BIGINT;

    SELECT TOP 1
        @generation = generation,
        @manifest_fingerprint = fingerprint
    FROM auspex_bronze.dbo.narrative_premium_snapshot_manifest
    WHERE status = 'completed' AND as_of_date = @as_of_date
    ORDER BY completed_at DESC, generation DESC;

    IF @generation IS NULL OR @manifest_fingerprint <> @expected_fingerprint
        THROW 51431, 'E22 release fingerprint does not match the completed manifest.', 1;

    BEGIN TRY
        BEGIN TRANSACTION;

        EXEC dbo.usp_promote_narrative_premium_snapshot
            @as_of_date = @as_of_date,
            @emit_result = 0;

        EXEC dbo.usp_promote_lakehouse_gold @promotion_run_id = @release_run_id;

        IF EXISTS (
            SELECT 1
            FROM dbo.fact_narrative_premium p
            LEFT JOIN dbo.security_daily_features f
              ON f.narrative_decision_id = p.decision_id
             AND f.security_sk = p.security_sk
             AND f.date_sk = p.date_sk
            WHERE p.generation = @generation
              AND (
                  f.narrative_decision_id IS NULL
                  OR EXISTS (
                      SELECT
                          p.fundamental_anchor_z,
                          p.narrative_intensity,
                          p.narrative_premium,
                          p.coverage_status,
                          p.coverage_reasons_json,
                          p.anchor_support_z,
                          p.divergence_state,
                          p.is_converging
                      EXCEPT
                      SELECT
                          f.fundamental_anchor_z,
                          f.narrative_intensity,
                          f.narrative_premium,
                          f.narrative_premium_coverage_status,
                          f.narrative_premium_coverage_reasons_json,
                          f.anchor_support_z,
                          f.divergence_state,
                          f.narrative_is_converging
                  )
                  OR f.opportunity_score IS NOT NULL
              )
        )
           OR EXISTS (
            SELECT 1
            FROM dbo.security_daily_features f
            LEFT JOIN dbo.fact_narrative_premium p
              ON p.decision_id = f.narrative_decision_id
            WHERE f.narrative_decision_id IS NOT NULL
              AND p.decision_id IS NULL
        )
            THROW 51432, 'E22 release daily features do not reconcile to premium facts.', 1;

        SELECT @premium_row_count = COUNT_BIG(*)
        FROM dbo.fact_narrative_premium
        WHERE generation = @generation;
        SELECT
            @gold_source_row_count = source_row_count,
            @gold_target_row_count = target_row_count
        FROM dbo.gold_promotion_audit
        WHERE promotion_run_id = @release_run_id AND status = 'SUCCEEDED';

        IF @manifest_fingerprint <> @expected_fingerprint
           OR @gold_source_row_count IS NULL
           OR @gold_source_row_count <> @gold_target_row_count
            THROW 51433, 'E22 release audit reconciliation failed.', 1;

        INSERT INTO dbo.e22_release_audit (
            release_run_id, as_of_date, e22_generation, e22_fingerprint,
            e22_row_count, gold_source_row_count, gold_target_row_count,
            completed_at, status
        ) VALUES (
            @release_run_id, @as_of_date, @generation, @manifest_fingerprint,
            @premium_row_count, @gold_source_row_count, @gold_target_row_count,
            SYSUTCDATETIME(), 'SUCCEEDED'
        );

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
        THROW;
    END CATCH;

    SELECT
        release_run_id,
        as_of_date,
        e22_generation AS generation,
        e22_fingerprint AS fingerprint,
        e22_row_count AS row_count,
        gold_source_row_count,
        gold_target_row_count,
        status
    FROM dbo.e22_release_audit
    WHERE release_run_id = @release_run_id;
END;
GO