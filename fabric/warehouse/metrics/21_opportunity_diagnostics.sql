-- Current Opportunity Score dependence and movement diagnostics.

DROP VIEW IF EXISTS dbo.v_opportunity_leg_diagnostics;
DROP VIEW IF EXISTS dbo.v_opportunity_score_movement;
DROP TABLE IF EXISTS dbo.opportunity_leg_diagnostics;
DROP TABLE IF EXISTS dbo.opportunity_score_movement;
GO

CREATE TABLE dbo.opportunity_leg_diagnostics (
    theme_id VARCHAR(128) NOT NULL,
    date_sk INT NOT NULL,
    as_of DATE NOT NULL,
    leg_x VARCHAR(64) NOT NULL,
    leg_y VARCHAR(64) NOT NULL,
    pair_count INT NOT NULL,
    correlation FLOAT NULL,
    complete_case_count INT NOT NULL,
    pc1_variance_share FLOAT NULL,
    model_version VARCHAR(32) NOT NULL,
    weight_version VARCHAR(32) NOT NULL,
    created_at DATETIME2(6) NOT NULL
);

CREATE TABLE dbo.opportunity_score_movement (
    theme_id VARCHAR(128) NOT NULL,
    security_sk BIGINT NOT NULL,
    date_sk INT NOT NULL,
    as_of DATE NOT NULL,
    previous_as_of DATE NOT NULL,
    previous_score FLOAT NOT NULL,
    current_score FLOAT NOT NULL,
    counterfactual_score FLOAT NOT NULL,
    score_delta FLOAT NOT NULL,
    own_composite_effect FLOAT NOT NULL,
    cohort_effect FLOAT NOT NULL,
    model_version VARCHAR(32) NOT NULL,
    weight_version VARCHAR(32) NOT NULL,
    created_at DATETIME2(6) NOT NULL
);
GO

CREATE OR ALTER VIEW dbo.v_opportunity_leg_diagnostics AS
SELECT * FROM dbo.opportunity_leg_diagnostics
WHERE model_version = 'opportunity_v1' AND weight_version = 'balanced_v1';
GO

CREATE OR ALTER VIEW dbo.v_opportunity_score_movement AS
SELECT * FROM dbo.opportunity_score_movement
WHERE model_version = 'opportunity_v1' AND weight_version = 'balanced_v1';
GO
