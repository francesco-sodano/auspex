-- Auspex E12 owner-scoped portfolio serving views (Fabric Warehouse T-SQL)

CREATE OR ALTER VIEW dbo.v_effective_portfolio_transactions AS
SELECT t.*
FROM dbo.fact_portfolio_transaction t
LEFT JOIN dbo.fact_portfolio_transaction correction
    ON correction.owner_user_sk = t.owner_user_sk
 AND correction.corrects_transaction_id = t.transaction_id
LEFT JOIN dbo.fact_portfolio_transaction parent_correction
        ON parent_correction.owner_user_sk = t.owner_user_sk
 AND parent_correction.corrects_transaction_id = t.linked_transaction_id
WHERE correction.transaction_id IS NULL
    AND parent_correction.transaction_id IS NULL;
GO

CREATE OR ALTER VIEW dbo.v_cash_balance AS
SELECT
    owner_user_sk,
    currency,
    SUM(cash_amount) AS cash_balance,
    MAX(knowledge_date) AS knowledge_date
FROM dbo.v_effective_portfolio_transactions
GROUP BY owner_user_sk, currency;
GO

CREATE OR ALTER VIEW dbo.v_portfolio_positions AS
SELECT
    p.owner_user_sk,
    p.account_id,
    p.security_sk,
    p.ticker,
    p.security_currency,
    p.gics_sector,
    p.country,
    p.quantity,
    p.market_value_base,
    p.position_weight,
    p.event_date,
    p.knowledge_date
FROM dbo.fact_portfolio_position p
WHERE p.quantity <> 0;
GO

CREATE OR ALTER VIEW dbo.v_portfolio_summary AS
WITH latest AS (
    SELECT
        owner_user_sk,
        valuation_date,
        base_currency,
        total_cash_base,
        total_stocks_base,
        total_value_base,
        cash_weight,
        missing_prices,
        missing_fx,
        coverage_complete,
        knowledge_date,
        ROW_NUMBER() OVER (
            PARTITION BY owner_user_sk, base_currency
            ORDER BY valuation_date DESC, knowledge_date DESC
        ) AS row_number
    FROM dbo.fact_portfolio_valuation
)
SELECT
    owner_user_sk,
    valuation_date,
    base_currency,
    total_cash_base,
    total_stocks_base,
    total_value_base,
    cash_weight,
    missing_prices,
    missing_fx,
    coverage_complete,
    knowledge_date
FROM latest
WHERE row_number = 1;
GO

CREATE OR ALTER VIEW dbo.v_portfolio_exposures AS
SELECT owner_user_sk, 'sector' AS exposure_type,
             COALESCE(gics_sector, 'Unknown') AS exposure_name,
             SUM(market_value_base) AS market_value_base,
             SUM(position_weight) AS exposure_weight
FROM dbo.fact_portfolio_position
WHERE quantity <> 0 AND market_value_base IS NOT NULL
GROUP BY owner_user_sk, COALESCE(gics_sector, 'Unknown')
UNION ALL
SELECT owner_user_sk, 'country', COALESCE(country, 'Unknown'),
             SUM(market_value_base), SUM(position_weight)
FROM dbo.fact_portfolio_position
WHERE quantity <> 0 AND market_value_base IS NOT NULL
GROUP BY owner_user_sk, COALESCE(country, 'Unknown')
UNION ALL
SELECT owner_user_sk, 'currency', COALESCE(security_currency, 'Unknown'),
             SUM(market_value_base), SUM(position_weight)
FROM dbo.fact_portfolio_position
WHERE quantity <> 0 AND market_value_base IS NOT NULL
GROUP BY owner_user_sk, COALESCE(security_currency, 'Unknown');
GO

CREATE OR ALTER VIEW dbo.v_portfolio_positions_with_features AS
WITH ranked_scores AS (
        SELECT s.*,
                     ROW_NUMBER() OVER (
                             PARTITION BY s.security_sk
                 ORDER BY s.as_of DESC
                     ) AS row_number
        FROM dbo.fact_theme_opportunity_score s
        WHERE s.coverage_status IN ('READY', 'PARTIAL')
)
SELECT p.owner_user_sk, p.account_id, p.security_sk, p.ticker,
             p.quantity, p.market_value_base, p.position_weight,
             p.gics_sector, p.country, p.security_currency,
             s.theme_id, s.as_of, s.opportunity_score,
             s.coverage_status, s.coverage_reasons_json,
             s.max_knowledge_date
FROM dbo.fact_portfolio_position p
LEFT JOIN ranked_scores s
    ON s.security_sk = p.security_sk AND s.row_number = 1
WHERE p.quantity <> 0;
GO

CREATE OR ALTER VIEW dbo.v_rebalance_inputs AS
WITH latest_valuation AS (
        SELECT v.*,
                     ROW_NUMBER() OVER (
                             PARTITION BY v.owner_user_sk
                             ORDER BY v.valuation_date DESC, v.knowledge_date DESC
                     ) AS row_number
        FROM dbo.fact_portfolio_valuation v
),
ranked_scores AS (
        SELECT s.*,
                     ROW_NUMBER() OVER (
                             PARTITION BY s.security_sk
                             ORDER BY s.as_of DESC
                     ) AS row_number
        FROM dbo.fact_theme_opportunity_score s
        WHERE s.coverage_status IN ('READY', 'PARTIAL')
),
owner_security AS (
        SELECT v.owner_user_sk, s.security_sk
        FROM latest_valuation v
        CROSS JOIN ranked_scores s
        WHERE v.row_number = 1 AND s.row_number = 1
        UNION
        SELECT owner_user_sk, security_sk
        FROM dbo.fact_portfolio_position
        WHERE quantity <> 0
)
SELECT os.owner_user_sk, os.security_sk, d.ticker,
             v.valuation_date, v.base_currency, v.total_cash_base,
             v.total_value_base, v.cash_weight, v.coverage_complete,
             COALESCE(p.quantity, 0) AS current_quantity,
             COALESCE(p.market_value_base, 0) AS current_value_base,
             COALESCE(p.position_weight, 0) AS current_weight,
             p.gics_sector, p.country, p.security_currency,
             s.theme_id, s.as_of AS score_as_of, s.opportunity_score,
             s.coverage_status, s.coverage_reasons_json,
             s.max_knowledge_date
FROM owner_security os
JOIN latest_valuation v
    ON v.owner_user_sk = os.owner_user_sk AND v.row_number = 1
JOIN dbo.dim_security d
    ON d.security_sk = os.security_sk AND d.is_current = 1
LEFT JOIN dbo.fact_portfolio_position p
    ON p.owner_user_sk = os.owner_user_sk AND p.security_sk = os.security_sk
LEFT JOIN ranked_scores s
    ON s.security_sk = os.security_sk AND s.row_number = 1;
GO
