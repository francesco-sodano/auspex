-- E17 metadata for every portfolio and recommendation metric displayed by the MVP SPA.

IF OBJECT_ID('dbo.metric_metadata', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.metric_metadata (
        metric_key          VARCHAR(64)   NOT NULL,
        display_name        VARCHAR(128)  NOT NULL,
        plain_description   VARCHAR(512)  NOT NULL,
        unit                VARCHAR(32)   NOT NULL,
        direction           VARCHAR(32)   NOT NULL,
        tier                VARCHAR(16)   NOT NULL
    );
END;

DELETE FROM dbo.metric_metadata;

INSERT INTO dbo.metric_metadata (
    metric_key, display_name, plain_description, unit, direction, tier
)
VALUES
('portfolio_value', 'Portfolio value', 'Current cash plus the market value of all covered holdings.', 'currency', 'contextual', 'simple'),
('net_contributed_capital', 'Net contributed capital', 'Opening capital and deposits minus withdrawals.', 'currency', 'contextual', 'simple'),
('total_gain_loss', 'Total gain or loss', 'Portfolio value minus net contributed capital.', 'currency', 'higher_is_better', 'simple'),
('cash_available', 'Cash available', 'Ledger cash not currently invested in holdings.', 'currency', 'contextual', 'simple'),
('stocks_value', 'Stocks value', 'Current market value of covered stock positions.', 'currency', 'contextual', 'simple'),
('position_weight', 'Position weight', 'Share of total portfolio value held in this security.', 'percentage', 'contextual', 'simple'),
('latest_price', 'Latest price', 'Most recent covered closing price known by the as-of date.', 'currency', 'contextual', 'simple'),
('opportunity_score', 'Opportunity Score', 'Transparent 0-100 heuristic combining six thesis legs; not a return forecast.', 'score_0_100', 'higher_is_better', 'simple'),
('target_weight', 'Target weight', 'Maximum portfolio share selected by deterministic risk policy.', 'percentage', 'contextual', 'simple'),
('suggested_amount', 'Suggested amount', 'Policy-sized amount to deploy or raise; Auspex never executes it.', 'currency', 'contextual', 'simple'),
('estimated_cost', 'Estimated cost', 'Estimated brokerage, spread, and applicable Swiss stamp duty.', 'currency', 'lower_is_better', 'simple'),
('confidence', 'Confidence', 'Data-coverage confidence, not certainty about future performance.', 'category', 'higher_is_better', 'simple'),
('position_quantity', 'Position quantity', 'Current number of shares derived from the effective ledger.', 'shares', 'contextual', 'simple'),
('transaction_count', 'Ledger entries', 'Immutable audit rows, including corrections and superseded originals.', 'count', 'contextual', 'advanced'),
('total_fees', 'Fees and commissions', 'All recorded ledger costs to date.', 'currency', 'lower_is_better', 'simple'),
('dividends', 'Dividends', 'Gross dividends recorded before separate fees.', 'currency', 'higher_is_better', 'simple'),
('interest', 'Interest', 'Gross interest income recorded in the ledger.', 'currency', 'higher_is_better', 'simple'),
('cash_impact', 'Cash impact', 'Server-derived signed cash movement for one ledger event.', 'currency', 'contextual', 'advanced'),
('fx_rate', 'FX rate', 'Recorded conversion rate: one transaction-currency unit in base currency.', 'rate', 'contextual', 'advanced'),
('monthly_outlook_range', 'Monthly outlook range', 'Uncertain one-month value range; published only when measured portfolio volatility is available.', 'currency_range', 'contextual', 'advanced'),
('thesis_linkage', 'Thesis fit', 'How strongly the security is linked to the selected investment theme.', 'score_contribution', 'higher_is_better', 'advanced'),
('attention_acceleration', 'Rising attention', 'Whether relevant market and information activity is accelerating.', 'score_contribution', 'higher_is_better', 'advanced'),
('smart_money', 'Smart-money activity', 'Insider and institutional activity contributing to the score.', 'score_contribution', 'higher_is_better', 'advanced'),
('fundamental_health', 'Fundamental health', 'Quality and valuation-supported business fundamentals.', 'score_contribution', 'higher_is_better', 'advanced'),
('valuation_brake', 'Valuation discipline', 'Contribution that reduces enthusiasm when valuation looks stretched.', 'score_contribution', 'higher_is_better', 'advanced'),
('crowding_positioning', 'Crowding position', 'Whether the opportunity remains under-recognized or has become crowded.', 'score_contribution', 'higher_is_better', 'advanced');
GO