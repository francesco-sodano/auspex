-- Auspex E6b final Opportunity Score serving view.
-- E6a exposes the contract but keeps opportunity_score NULL until E8/E14 provide all six legs.

CREATE OR ALTER VIEW dbo.v_opportunity_score AS
SELECT
    security_sk,
    date_sk,
    as_of,
    opportunity_score,
    CASE
        WHEN opportunity_score IS NULL THEN 'INCOMPLETE_E6A_WAITING_E8_E14'
        ELSE score_status
    END AS score_status,
    max_knowledge_date
FROM dbo.security_daily_features
WHERE max_knowledge_date <= as_of;
GO
