-- Auspex E6a base metric layer (Fabric Warehouse T-SQL)
-- Mirrors nb_04_metrics.py output. Views are PIT-safe through max_knowledge_date <= as_of.

IF OBJECT_ID('dbo.metric_weights', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.metric_weights (
        metric_name    VARCHAR(64)  NOT NULL,
        metric_group   VARCHAR(64)  NOT NULL,
        [weight]       DECIMAL(9,6) NOT NULL,
        direction      INT          NOT NULL,
        is_active      BIT          NOT NULL,
        required_epic  VARCHAR(16)  NULL,
        [version]      VARCHAR(32)  NOT NULL,
        effective_from DATE         NOT NULL,
        effective_to   DATE         NOT NULL,
        updated_at     DATETIME2(3) NULL
    );
END;
GO

MERGE dbo.metric_weights AS t
USING (VALUES
    ('momentum_3m', 'composite_growth_score', CAST(0.250000 AS DECIMAL(9,6)), 1, 1, 'E6a', 'e6a_v1', CAST('1900-01-01' AS DATE), CAST('9999-12-31' AS DATE)),
    ('momentum_6m', 'composite_growth_score', CAST(0.150000 AS DECIMAL(9,6)), 1, 1, 'E6a', 'e6a_v1', CAST('1900-01-01' AS DATE), CAST('9999-12-31' AS DATE)),
    ('momentum_12m', 'composite_growth_score', CAST(0.100000 AS DECIMAL(9,6)), 1, 1, 'E6a', 'e6a_v1', CAST('1900-01-01' AS DATE), CAST('9999-12-31' AS DATE)),
    ('realized_vol_30d', 'composite_growth_score', CAST(0.100000 AS DECIMAL(9,6)), -1, 1, 'E6a', 'e6a_v1', CAST('1900-01-01' AS DATE), CAST('9999-12-31' AS DATE)),
    ('insider_net_buy_ratio_90d', 'composite_growth_score', CAST(0.250000 AS DECIMAL(9,6)), 1, 1, 'E6a', 'e6a_v1', CAST('1900-01-01' AS DATE), CAST('9999-12-31' AS DATE)),
    ('insider_cluster_buy_30d', 'composite_growth_score', CAST(0.150000 AS DECIMAL(9,6)), 1, 1, 'E6a', 'e6a_v1', CAST('1900-01-01' AS DATE), CAST('9999-12-31' AS DATE))
) AS s(metric_name, metric_group, [weight], direction, is_active, required_epic, [version], effective_from, effective_to)
ON t.metric_name = s.metric_name AND t.[version] = s.[version] AND t.effective_from = s.effective_from
WHEN MATCHED THEN UPDATE SET
    metric_group = s.metric_group,
    [weight] = s.[weight],
    direction = s.direction,
    is_active = s.is_active,
    required_epic = s.required_epic,
    effective_to = s.effective_to,
    updated_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (
    metric_name, metric_group, [weight], direction, is_active, required_epic, [version], effective_from, effective_to, updated_at
) VALUES (
    s.metric_name, s.metric_group, s.[weight], s.direction, s.is_active, s.required_epic, s.[version], s.effective_from, s.effective_to, SYSUTCDATETIME()
);
GO

IF OBJECT_ID('dbo.security_daily_features', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.security_daily_features (
        security_sk                      BIGINT         NOT NULL,
        date_sk                          INT            NOT NULL,
        ticker                           VARCHAR(16)    NULL,
        company_name                     VARCHAR(256)   NULL,
        gics_sector                      VARCHAR(64)    NULL,
        country                          VARCHAR(2)     NULL,
        as_of                            DATE           NOT NULL,
        [close]                          DECIMAL(18,6)  NULL,
        ret_1d                           DECIMAL(12,8)  NULL,
        momentum_3m                      FLOAT          NULL,
        momentum_6m                      FLOAT          NULL,
        momentum_12m                     FLOAT          NULL,
        rel_strength_sector              FLOAT          NULL,
        realized_vol_30d                 FLOAT          NULL,
        realized_vol_90d                 FLOAT          NULL,
        realized_vol_252d                FLOAT          NULL,
        downside_deviation_252d          FLOAT          NULL,
        max_drawdown_252d                FLOAT          NULL,
        beta_252d                        FLOAT          NULL,
        illiquidity                      FLOAT          NULL,
        ann_return_252d                  FLOAT          NULL,
        sharpe_252d                      FLOAT          NULL,
        sortino_252d                     FLOAT          NULL,
        calmar_252d                      FLOAT          NULL,
        info_ratio_252d                  FLOAT          NULL,
        insider_net_buy_ratio_90d        FLOAT          NULL,
        insider_cluster_buy_30d          INT            NULL,
        inst_net_flow_qoq                FLOAT          NULL,
        inst_new_initiations             INT            NULL,
        activist_13d_flag                BIT            NULL,
        news_sentiment_ewma_14d          FLOAT          NULL,
        news_volume_z_30d                FLOAT          NULL,
        contract_award_usd_trailing_90d  FLOAT          NULL,
        fundamental_anchor_z             FLOAT          NULL,
        narrative_intensity              FLOAT          NULL,
        narrative_premium                FLOAT          NULL,
        divergence_state                 VARCHAR(32)    NULL,
        composite_growth_score           FLOAT          NULL,
        opportunity_score                FLOAT          NULL,
        score_status                     VARCHAR(64)    NULL,
        max_knowledge_date               DATE           NULL,
        stale_sources_json               VARCHAR(2048)  NULL
    );
END;
GO

CREATE OR ALTER VIEW dbo.v_market_momentum AS
SELECT
    security_sk,
    date_sk,
    as_of,
    [close],
    ret_1d,
    momentum_3m,
    momentum_6m,
    momentum_12m,
    rel_strength_sector,
    max_knowledge_date
FROM dbo.security_daily_features
WHERE max_knowledge_date <= as_of;
GO

CREATE OR ALTER VIEW dbo.v_market_risk AS
SELECT
    security_sk,
    date_sk,
    as_of,
    realized_vol_30d,
    realized_vol_90d,
    realized_vol_252d,
    downside_deviation_252d,
    max_drawdown_252d,
    beta_252d,
    illiquidity,
    max_knowledge_date
FROM dbo.security_daily_features
WHERE max_knowledge_date <= as_of;
GO

CREATE OR ALTER VIEW dbo.v_risk_adjusted AS
SELECT
    security_sk,
    date_sk,
    as_of,
    ann_return_252d,
    sharpe_252d,
    sortino_252d,
    calmar_252d,
    info_ratio_252d,
    max_knowledge_date
FROM dbo.security_daily_features
WHERE max_knowledge_date <= as_of;
GO

CREATE OR ALTER VIEW dbo.v_smart_money AS
SELECT
    security_sk,
    date_sk,
    as_of,
    insider_net_buy_ratio_90d,
    insider_cluster_buy_30d,
    inst_net_flow_qoq,
    inst_new_initiations,
    activist_13d_flag,
    max_knowledge_date
FROM dbo.security_daily_features
WHERE max_knowledge_date <= as_of;
GO

CREATE OR ALTER VIEW dbo.v_security_daily_features AS
SELECT
    security_sk,
    ticker,
    company_name,
    gics_sector,
    country,
    as_of,
    date_sk,
    [close],
    ret_1d,
    momentum_3m,
    momentum_6m,
    momentum_12m,
    rel_strength_sector,
    realized_vol_252d,
    downside_deviation_252d,
    max_drawdown_252d,
    beta_252d,
    illiquidity,
    ann_return_252d,
    sharpe_252d,
    sortino_252d,
    calmar_252d,
    info_ratio_252d,
    insider_net_buy_ratio_90d,
    insider_cluster_buy_30d,
    inst_net_flow_qoq,
    inst_new_initiations,
    activist_13d_flag,
    news_sentiment_ewma_14d,
    news_volume_z_30d,
    contract_award_usd_trailing_90d,
    fundamental_anchor_z,
    narrative_intensity,
    narrative_premium,
    divergence_state,
    composite_growth_score,
    opportunity_score,
    score_status,
    max_knowledge_date,
    stale_sources_json
FROM dbo.security_daily_features
WHERE max_knowledge_date <= as_of;
GO
