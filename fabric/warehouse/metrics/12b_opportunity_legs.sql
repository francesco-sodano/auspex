-- Auspex E6a opportunity leg-source contract.
-- The six-leg final Opportunity Score is completed in E6b after E8 and E14.

CREATE OR ALTER VIEW dbo.v_opportunity_legs AS
SELECT
    security_sk,
    date_sk,
    as_of,
    CAST(NULL AS FLOAT) AS thesis_linkage_z,
    CAST(NULL AS FLOAT) AS attention_acceleration_z,
    CAST(NULL AS FLOAT) AS smart_money_z,
    CAST(NULL AS FLOAT) AS fundamental_health_z,
    CAST(NULL AS FLOAT) AS valuation_brake_z,
    CAST(NULL AS FLOAT) AS crowding_positioning_z,
    score_status,
    max_knowledge_date
FROM dbo.security_daily_features
WHERE max_knowledge_date <= as_of;
GO
