-- Auspex E6b final Opportunity Score serving view.
-- Score grain is theme/security/date; E22 context is explanation-only.

CREATE OR ALTER VIEW dbo.v_opportunity_score AS
SELECT
    s.score_id,
    s.generation,
    s.theme_id,
    s.security_sk,
    d.ticker,
    d.company_name,
    s.date_sk,
    s.as_of,
    s.classification_provenance,
    s.classification_id,
    s.classification_updated_at,
    s.candidate_count,
    s.opportunity_score_raw,
    s.opportunity_score,
    s.coverage_status,
    s.coverage_reasons_json,
    s.model_version,
    s.weight_version,
    s.max_knowledge_date,
    f.narrative_premium,
    f.divergence_state,
    f.narrative_decision_id
FROM dbo.fact_theme_opportunity_score s
JOIN dbo.opportunity_score_snapshot_manifest m
  ON m.generation = s.generation
 AND m.as_of_date = s.as_of
 AND m.model_version = s.model_version
 AND m.weight_version = s.weight_version
 AND m.status = 'completed'
JOIN dbo.dim_security d
    ON d.security_sk = s.security_sk
LEFT JOIN dbo.security_daily_features f
  ON f.security_sk = s.security_sk AND f.date_sk = s.date_sk
WHERE s.max_knowledge_date <= s.as_of
        AND s.model_version = 'opportunity_v1'
    AND s.weight_version = 'balanced_v1';
GO

CREATE OR ALTER VIEW dbo.v_security_score_attribution AS
WITH active_scores AS (
    SELECT s.*, d.ticker, d.company_name,
        f.narrative_premium, f.divergence_state, f.narrative_decision_id
    FROM dbo.fact_theme_opportunity_score s
    JOIN dbo.opportunity_score_snapshot_manifest m
      ON m.generation = s.generation
     AND m.as_of_date = s.as_of
     AND m.model_version = s.model_version
     AND m.weight_version = s.weight_version
     AND m.status = 'completed'
        JOIN dbo.dim_security d
            ON d.security_sk = s.security_sk
        LEFT JOIN dbo.security_daily_features f
      ON f.security_sk = s.security_sk AND f.date_sk = s.date_sk
    WHERE s.max_knowledge_date <= s.as_of
        AND s.model_version = 'opportunity_v1'
            AND s.weight_version = 'balanced_v1'
), attribution AS (
    SELECT score_id, generation, theme_id, security_sk, ticker, company_name,
        date_sk, as_of, 'thesis_linkage' AS leg_name,
        thesis_linkage_z AS leg_z, thesis_linkage_contribution AS leg_contribution,
        opportunity_score, coverage_status, coverage_reasons_json,
        narrative_premium, divergence_state, narrative_decision_id,
        model_version, weight_version, max_knowledge_date
    FROM active_scores
    UNION ALL
    SELECT score_id, generation, theme_id, security_sk, ticker, company_name,
        date_sk, as_of, 'attention_acceleration',
        attention_acceleration_z, attention_acceleration_contribution,
        opportunity_score, coverage_status, coverage_reasons_json,
        narrative_premium, divergence_state, narrative_decision_id,
        model_version, weight_version, max_knowledge_date
    FROM active_scores
    UNION ALL
    SELECT score_id, generation, theme_id, security_sk, ticker, company_name,
        date_sk, as_of, 'smart_money', smart_money_z, smart_money_contribution,
        opportunity_score, coverage_status, coverage_reasons_json,
        narrative_premium, divergence_state, narrative_decision_id,
        model_version, weight_version, max_knowledge_date
    FROM active_scores
    UNION ALL
    SELECT score_id, generation, theme_id, security_sk, ticker, company_name,
        date_sk, as_of, 'fundamental_health', fundamental_health_z, fundamental_health_contribution,
        opportunity_score, coverage_status, coverage_reasons_json,
        narrative_premium, divergence_state, narrative_decision_id,
        model_version, weight_version, max_knowledge_date
    FROM active_scores
    UNION ALL
    SELECT score_id, generation, theme_id, security_sk, ticker, company_name,
        date_sk, as_of, 'valuation_brake', valuation_brake_z, valuation_brake_contribution,
        opportunity_score, coverage_status, coverage_reasons_json,
        narrative_premium, divergence_state, narrative_decision_id,
        model_version, weight_version, max_knowledge_date
    FROM active_scores
    UNION ALL
    SELECT score_id, generation, theme_id, security_sk, ticker, company_name,
        date_sk, as_of, 'crowding_positioning', crowding_positioning_z, crowding_positioning_contribution,
        opportunity_score, coverage_status, coverage_reasons_json,
        narrative_premium, divergence_state, narrative_decision_id,
        model_version, weight_version, max_knowledge_date
    FROM active_scores
)
SELECT
    a.*,
    CAST(w.weight AS FLOAT) AS leg_weight,
    CASE
     WHEN a.leg_contribution > 0 THEN 'RAISED'
     WHEN a.leg_contribution < 0 THEN 'LOWERED'
     ELSE 'NEUTRAL'
    END AS contribution_direction
FROM attribution a
JOIN dbo.metric_weights w
  ON w.metric_name = a.leg_name
 AND w.metric_group = 'opportunity_score'
 AND w.version = a.weight_version
 AND w.is_active = 1;
GO
