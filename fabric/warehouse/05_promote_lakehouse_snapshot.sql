-- Promote a settled Lakehouse Gold snapshot into the physical Fabric Warehouse.
-- Call only after all producing notebooks have succeeded. The procedure records
-- the exact source row counts observed inside the promotion transaction.

IF OBJECT_ID('dbo.gold_promotion_audit', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.gold_promotion_audit (
        promotion_run_id        VARCHAR(64)   NOT NULL,
        source_snapshot_manifest VARCHAR(8000) NOT NULL,
        source_row_count        BIGINT        NOT NULL,
        target_row_count        BIGINT        NOT NULL,
        started_at              DATETIME2(6)  NOT NULL,
        completed_at            DATETIME2(6)  NOT NULL,
        [status]                VARCHAR(16)   NOT NULL
    );
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_promote_lakehouse_gold
    @promotion_run_id VARCHAR(64)
AS
BEGIN
    SET NOCOUNT ON;

    IF EXISTS (
        SELECT 1 FROM dbo.gold_promotion_audit
        WHERE promotion_run_id = @promotion_run_id
    )
        THROW 50200, 'Promotion run ID already exists.', 1;

    DECLARE @started_at DATETIME2(6) = SYSUTCDATETIME();
    DECLARE @source_snapshot_manifest VARCHAR(8000);
    DECLARE @source_row_count BIGINT;
    DECLARE @target_row_count BIGINT;

    BEGIN TRY
        BEGIN TRANSACTION;

    DELETE FROM dbo.opportunity_score_snapshot_manifest;
    DELETE FROM dbo.fact_theme_opportunity_score;
    DELETE FROM dbo.security_daily_features;
    DELETE FROM dbo.fact_fundamental_anchor;
    DELETE FROM dbo.metric_weights;
    DELETE FROM dbo.fact_sec_filing_event;
    DELETE FROM dbo.fact_material_event;
    DELETE FROM dbo.fact_theme_membership;
    DELETE FROM dbo.fact_company_news;
    DELETE FROM dbo.fact_fundamentals;
    DELETE FROM dbo.fact_fx_rate;
    DELETE FROM dbo.fact_macro;
    DELETE FROM dbo.fact_contract_award;
    DELETE FROM dbo.fact_news_sentiment;
    DELETE FROM dbo.fact_ownership_event;
    DELETE FROM dbo.fact_institutional_holding;
    DELETE FROM dbo.fact_insider_txn;
    DELETE FROM dbo.fact_market_daily;
    DELETE FROM dbo.security_theme_classification;
    DELETE FROM dbo.bridge_theme_etf;
    DELETE FROM dbo.dim_theme;
    DELETE FROM dbo.dim_source;
    DELETE FROM dbo.dim_entity;
    DELETE FROM dbo.dim_date;
    DELETE FROM dbo.dim_security;

    INSERT INTO dbo.dim_security (
        security_sk, cik, ticker, isin, figi, company_name, gics_sector,
        gics_industry, country, exchange, currency, mcap_band, is_active,
        valid_from, valid_to, is_current, resolution_method, source_id, updated_at
    )
    SELECT security_sk, cik, ticker, isin, figi, company_name, gics_sector,
           gics_industry, country, exchange, currency, mcap_band, is_active,
           valid_from, valid_to, is_current, resolution_method, source_id, updated_at
    FROM auspex_bronze.dbo.dim_security;

    INSERT INTO dbo.dim_theme (
        theme_id, theme_name, benchmark_symbol, is_active, catalog_version, updated_at
    )
    SELECT theme_id, theme_name, benchmark_symbol, is_active, catalog_version, updated_at
    FROM auspex_bronze.dbo.dim_theme;

    INSERT INTO dbo.bridge_theme_etf (
        theme_id, etf_symbol, blend_weight, is_active, catalog_version, updated_at
    )
    SELECT theme_id, etf_symbol, blend_weight, is_active, catalog_version, updated_at
    FROM auspex_bronze.dbo.bridge_theme_etf;

    INSERT INTO dbo.security_theme_classification (
        classification_id, security_sk, ticker, theme_id, provenance,
        confidence, rationale, effective_from, effective_to,
        classification_version, updated_at
    )
    SELECT classification_id, security_sk, ticker, theme_id, provenance,
           confidence, rationale, effective_from, effective_to,
           classification_version, updated_at
    FROM auspex_bronze.dbo.security_theme_classification;

    INSERT INTO dbo.dim_date (
        date_sk, cal_date, [year], [quarter], [month], [day], is_trading_day,
        fiscal_quarter
    )
    SELECT date_sk, cal_date, [year], [quarter], [month], [day], is_trading_day,
           fiscal_quarter
    FROM auspex_bronze.dbo.dim_date;

    INSERT INTO dbo.dim_entity (
        entity_sk, entity_natural_id, entity_type, [name], [role], cik
    )
    SELECT entity_sk, entity_natural_id, entity_type, [name], [role], cik
    FROM auspex_bronze.dbo.dim_entity;

    INSERT INTO dbo.dim_source (
        source_sk, source_id, source_type, latency_class, reliability_weight,
        source_class
    )
    SELECT source_sk, source_id, source_type, latency_class, reliability_weight,
           source_class
    FROM auspex_bronze.dbo.dim_source;

    INSERT INTO dbo.fact_market_daily (
        security_sk, date_sk, price_revision_hash, [open], high, low, [close],
        adj_close, volume, ret_1d, source_sk, event_date, knowledge_date,
        ingest_ts, revision_loaded_at
    )
    SELECT security_sk, date_sk, price_revision_hash, [open], high, low, [close],
           adj_close, volume, ret_1d, source_sk, event_date, knowledge_date,
           ingest_ts, revision_loaded_at
    FROM auspex_bronze.dbo.fact_market_daily;

    INSERT INTO dbo.fact_insider_txn (
        insider_txn_sk, security_sk, entity_sk, date_sk, line_no, txn_code,
        is_buy, shares, price, value_usd, shares_after, accession_no, source_sk,
        event_date, knowledge_date
    )
    SELECT insider_txn_sk, security_sk, entity_sk, date_sk, line_no, txn_code,
           is_buy, shares, price, value_usd, shares_after, accession_no, source_sk,
           event_date, knowledge_date
    FROM auspex_bronze.dbo.fact_insider_txn;

    INSERT INTO dbo.fact_institutional_holding (
        security_sk, entity_sk, date_sk, shares, value_usd, shares_delta_qoq,
        pct_of_portfolio, accession_no, holding_revision_hash,
        silver_natural_key, silver_batch_id, silver_ingest_ts,
        silver_source_record_hash, silver_loaded_at, source_sk, event_date,
        knowledge_date
    )
    SELECT security_sk, entity_sk, date_sk, shares, value_usd, shares_delta_qoq,
           pct_of_portfolio, accession_no, holding_revision_hash,
           silver_natural_key, silver_batch_id, silver_ingest_ts,
           silver_source_record_hash, silver_loaded_at, source_sk, event_date,
           knowledge_date
    FROM auspex_bronze.dbo.fact_institutional_holding;

    INSERT INTO dbo.fact_ownership_event (
        security_sk, entity_sk, date_sk, pct_owned, filing_type, is_activist,
        accession_no, ownership_revision_hash, source_sk, event_date,
        knowledge_date
    )
    SELECT security_sk, entity_sk, date_sk, pct_owned, filing_type, is_activist,
           accession_no, ownership_revision_hash, source_sk, event_date,
           knowledge_date
    FROM auspex_bronze.dbo.fact_ownership_event;

    INSERT INTO dbo.fact_news_sentiment (
        news_sk, security_sk, date_sk, published_at, sentiment, relevance,
        title_hash, url, news_revision_hash, silver_natural_key, silver_batch_id,
        silver_ingest_ts, silver_source_record_hash, silver_loaded_at, source_sk,
        event_date, knowledge_date
    )
    SELECT news_sk, security_sk, date_sk, published_at, sentiment, relevance,
           title_hash, url, news_revision_hash, silver_natural_key, silver_batch_id,
           silver_ingest_ts, silver_source_record_hash, silver_loaded_at, source_sk,
           event_date, knowledge_date
    FROM auspex_bronze.dbo.fact_news_sentiment;

    INSERT INTO dbo.fact_contract_award (
        award_sk, transaction_id, award_id, contract_revision_hash, security_sk,
        entity_sk, date_sk, agency, amount_usd, description_hash, source_sk,
        event_date, knowledge_date
    )
    SELECT award_sk, transaction_id, award_id, contract_revision_hash, security_sk,
           entity_sk, date_sk, agency, amount_usd, description_hash, source_sk,
           event_date, knowledge_date
    FROM auspex_bronze.dbo.fact_contract_award;

    INSERT INTO dbo.fact_macro (
        indicator_code, date_sk, [value], macro_revision_hash, source_sk,
        event_date, knowledge_date
    )
    SELECT indicator_code, date_sk, [value], macro_revision_hash, source_sk,
           event_date, knowledge_date
    FROM auspex_bronze.dbo.fact_macro;

    INSERT INTO dbo.fact_fx_rate (
        ccy_pair, date_sk, rate, fx_revision_hash, source_sk, event_date,
        knowledge_date
    )
    SELECT ccy_pair, date_sk, rate, fx_revision_hash, source_sk, event_date,
           knowledge_date
    FROM auspex_bronze.dbo.fact_fx_rate;

    INSERT INTO dbo.fact_fundamentals (
        security_sk, date_sk, fundamentals_kind, currency, sector, industry,
        market_cap, shares_outstanding, ebitda, pe_ratio, peg_ratio, ps_ratio, ev_ebitda,
        gross_profit_ttm, profit_margin, rev_growth_yoy, cash_and_equivalents,
        total_debt, operating_cashflow, capital_expenditures, fcf_yield,
        net_debt_to_ebitda, fundamentals_revision_hash, silver_natural_key,
        silver_batch_id, silver_ingest_ts, silver_source_record_hash,
        silver_loaded_at, source_sk, event_date, knowledge_date
    )
    SELECT security_sk, date_sk, fundamentals_kind, currency, sector, industry,
            market_cap, shares_outstanding, ebitda, pe_ratio, peg_ratio, ps_ratio, ev_ebitda,
           gross_profit_ttm, profit_margin, rev_growth_yoy, cash_and_equivalents,
           total_debt, operating_cashflow, capital_expenditures, fcf_yield,
           net_debt_to_ebitda, fundamentals_revision_hash, silver_natural_key,
           silver_batch_id, silver_ingest_ts, silver_source_record_hash,
           silver_loaded_at, source_sk, event_date, knowledge_date
    FROM auspex_bronze.dbo.fact_fundamentals;

    INSERT INTO dbo.fact_company_news (
        news_sk, security_sk, date_sk, published_at, title, summary, url, source,
        news_revision_hash, silver_natural_key, silver_batch_id, silver_ingest_ts,
        silver_source_record_hash, silver_loaded_at, source_sk, event_date,
        knowledge_date
    )
    SELECT news_sk, security_sk, date_sk, published_at, title, summary, url, source,
           news_revision_hash, silver_natural_key, silver_batch_id, silver_ingest_ts,
           silver_source_record_hash, silver_loaded_at, source_sk, event_date,
           knowledge_date
    FROM auspex_bronze.dbo.fact_company_news;

    INSERT INTO dbo.fact_theme_membership (
        theme_id, etf_symbol, security_sk, [weight], is_ground_truth,
        theme_revision_hash, snapshot_batch_id, snapshot_ingest_ts,
        source_sk, event_date, knowledge_date
    )
    SELECT theme_id, etf_symbol, security_sk, [weight], is_ground_truth,
           theme_revision_hash, snapshot_batch_id, snapshot_ingest_ts,
           source_sk, event_date, knowledge_date
    FROM auspex_bronze.dbo.fact_theme_membership;

    INSERT INTO dbo.fact_material_event (
        event_sk, security_sk, date_sk, accession_no, filing_type, description,
        material_event_revision_hash, source_sk, event_date, knowledge_date
    )
    SELECT event_sk, security_sk, date_sk, accession_no, filing_type, description,
           material_event_revision_hash, source_sk, event_date, knowledge_date
    FROM auspex_bronze.dbo.fact_material_event;

    INSERT INTO dbo.fact_sec_filing_event (
        filing_event_sk, accession_no, filing_type, filer_name,
        filing_revision_hash, source_sk, event_date, knowledge_date
    )
    SELECT filing_event_sk, accession_no, filing_type, filer_name,
           filing_revision_hash, source_sk, event_date, knowledge_date
    FROM auspex_bronze.dbo.fact_sec_filing_event;

    INSERT INTO dbo.fact_fundamental_anchor (
        security_sk, date_sk, ev_sales, ev_ebitda, p_fcf, expected_ev_sales,
        residual_evs, residual_evebitda, residual_pfcf, anchor_residual,
        fundamental_anchor_z, anchor_method, n_peers, r2_sector, uses_forward,
        imputed_flags, model_version, source_sk, event_date, knowledge_date
    )
    SELECT
        security_sk, date_sk, ev_sales, ev_ebitda, p_fcf, expected_ev_sales,
        residual_evs, residual_evebitda, residual_pfcf, anchor_residual,
        fundamental_anchor_z, anchor_method, n_peers, r2_sector, uses_forward,
        imputed_flags, model_version, source_sk, event_date, knowledge_date
    FROM auspex_bronze.dbo.fact_fundamental_anchor;

    INSERT INTO dbo.metric_weights (
        metric_name, metric_group, [weight], direction, is_active, required_epic,
        [version], effective_from, effective_to, updated_at
    )
    SELECT metric_name, metric_group, [weight], direction, is_active, required_epic,
           [version], effective_from, effective_to, updated_at
    FROM auspex_bronze.dbo.metric_weights;

    INSERT INTO dbo.security_daily_features (
        security_sk, date_sk, ticker, company_name, gics_sector, country, as_of,
        [close], ret_1d, momentum_3m, momentum_6m, momentum_12m,
        rel_strength_sector, realized_vol_30d, realized_vol_90d,
        realized_vol_252d, downside_deviation_252d, max_drawdown_252d,
        beta_252d, illiquidity, ann_return_252d, sharpe_252d, sortino_252d,
        calmar_252d, info_ratio_252d, insider_net_buy_ratio_90d,
        insider_cluster_buy_30d, inst_net_flow_qoq, inst_new_initiations,
        institutional_holder_count_120d, activist_13d_flag,
        news_sentiment_ewma_14d, news_count_30d, news_volume_z_30d,
        contract_award_usd_trailing_90d, pe_ratio, peg_ratio, ps_ratio,
        ev_ebitda, profit_margin, rev_growth_yoy, fcf_yield,
        net_debt_to_ebitda, fundamental_anchor_z, fundamental_anchor_method,
        fundamental_anchor_imputed_flags, narrative_intensity,
        narrative_coverage_status, narrative_coverage_reasons_json,
        narrative_premium, narrative_premium_coverage_status,
        narrative_premium_coverage_reasons_json, narrative_decision_id,
        anchor_support_z, divergence_state, narrative_is_converging, composite_growth_score,
        opportunity_score, score_status, max_knowledge_date, stale_sources_json,
        feature_built_at
    )
    SELECT security_sk, date_sk, ticker, company_name, gics_sector, country, as_of,
           [close], ret_1d, momentum_3m, momentum_6m, momentum_12m,
           rel_strength_sector, realized_vol_30d, realized_vol_90d,
           realized_vol_252d, downside_deviation_252d, max_drawdown_252d,
           beta_252d, illiquidity, ann_return_252d, sharpe_252d, sortino_252d,
           calmar_252d, info_ratio_252d, insider_net_buy_ratio_90d,
           insider_cluster_buy_30d, inst_net_flow_qoq, inst_new_initiations,
           institutional_holder_count_120d, activist_13d_flag,
           news_sentiment_ewma_14d, news_count_30d, news_volume_z_30d,
           contract_award_usd_trailing_90d, pe_ratio, peg_ratio, ps_ratio,
           ev_ebitda, profit_margin, rev_growth_yoy, fcf_yield,
           net_debt_to_ebitda, fundamental_anchor_z, fundamental_anchor_method,
           fundamental_anchor_imputed_flags, narrative_intensity,
           narrative_coverage_status, narrative_coverage_reasons_json,
              narrative_premium, narrative_premium_coverage_status,
              narrative_premium_coverage_reasons_json, narrative_decision_id,
              anchor_support_z, divergence_state, narrative_is_converging, composite_growth_score,
           opportunity_score, score_status, max_knowledge_date, stale_sources_json,
           feature_built_at
    FROM auspex_bronze.dbo.security_daily_features;

    INSERT INTO dbo.fact_theme_opportunity_score (
        score_id, generation, cohort_snapshot_hash, theme_id, security_sk,
        date_sk, as_of, candidate_source, candidate_snapshot_id,
        candidate_snapshot_ingest_ts, candidate_count,
        thesis_linkage_z, attention_acceleration_z, smart_money_z,
        fundamental_health_z, valuation_brake_z, crowding_positioning_z,
        thesis_linkage_contribution, attention_acceleration_contribution,
        smart_money_contribution, fundamental_health_contribution,
        valuation_brake_contribution, crowding_positioning_contribution,
        opportunity_score_raw, opportunity_score, coverage_status,
        coverage_reasons_json, max_knowledge_date, model_version,
        weight_version, created_at
    )
    SELECT
        score_id, generation, cohort_snapshot_hash, theme_id, security_sk,
        date_sk, as_of, candidate_source, candidate_snapshot_id,
        candidate_snapshot_ingest_ts, candidate_count,
        thesis_linkage_z, attention_acceleration_z, smart_money_z,
        fundamental_health_z, valuation_brake_z, crowding_positioning_z,
        thesis_linkage_contribution, attention_acceleration_contribution,
        smart_money_contribution, fundamental_health_contribution,
        valuation_brake_contribution, crowding_positioning_contribution,
        opportunity_score_raw, opportunity_score, coverage_status,
        coverage_reasons_json, max_knowledge_date, model_version,
        weight_version, created_at
    FROM auspex_bronze.dbo.fact_theme_opportunity_score;

    INSERT INTO dbo.opportunity_score_snapshot_manifest (
        generation, as_of_date, model_version, weight_version, status,
        row_count, ready_count, partial_count, withheld_count, fingerprint,
        created_at, completed_at
    )
    SELECT
        generation, as_of_date, model_version, weight_version, status,
        row_count, ready_count, partial_count, withheld_count, fingerprint,
        created_at, completed_at
    FROM auspex_bronze.dbo.opportunity_score_snapshot_manifest;

    SELECT @source_row_count = SUM(row_count)
    FROM (
        SELECT COUNT_BIG(*) AS row_count FROM auspex_bronze.dbo.dim_security
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_theme
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.bridge_theme_etf
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.security_theme_classification
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_date
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_entity
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_source
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_market_daily
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_insider_txn
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_institutional_holding
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_ownership_event
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_news_sentiment
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_contract_award
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_macro
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_fx_rate
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_fundamentals
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_company_news
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_theme_membership
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_material_event
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_sec_filing_event
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_fundamental_anchor
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.metric_weights
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.security_daily_features
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_theme_opportunity_score
        UNION ALL SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.opportunity_score_snapshot_manifest
    ) source_counts;

    SELECT @source_snapshot_manifest = CONCAT(
        '{',
        STRING_AGG(
            CONCAT('"', table_name, '":{"rows":', source_count, '}'),
            ','
        ),
        '}'
    )
    FROM (VALUES
        ('dim_security', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_security)),
        ('dim_theme', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_theme)),
        ('bridge_theme_etf', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.bridge_theme_etf)),
        ('security_theme_classification', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.security_theme_classification)),
        ('dim_date', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_date)),
        ('dim_entity', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_entity)),
        ('dim_source', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_source)),
        ('fact_market_daily', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_market_daily)),
        ('fact_insider_txn', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_insider_txn)),
        ('fact_institutional_holding', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_institutional_holding)),
        ('fact_ownership_event', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_ownership_event)),
        ('fact_news_sentiment', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_news_sentiment)),
        ('fact_contract_award', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_contract_award)),
        ('fact_macro', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_macro)),
        ('fact_fx_rate', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_fx_rate)),
        ('fact_fundamentals', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_fundamentals)),
        ('fact_company_news', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_company_news)),
        ('fact_theme_membership', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_theme_membership)),
        ('fact_material_event', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_material_event)),
        ('fact_sec_filing_event', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_sec_filing_event)),
        ('fact_fundamental_anchor', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_fundamental_anchor)),
        ('metric_weights', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.metric_weights)),
        ('security_daily_features', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.security_daily_features)),
        ('fact_theme_opportunity_score', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_theme_opportunity_score)),
        ('opportunity_score_snapshot_manifest', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.opportunity_score_snapshot_manifest))
    ) source_manifest(table_name, source_count);

    SELECT @target_row_count = SUM(row_count)
    FROM (
        SELECT COUNT_BIG(*) AS row_count FROM dbo.dim_security
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.dim_theme
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.bridge_theme_etf
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.security_theme_classification
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.dim_date
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.dim_entity
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.dim_source
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_market_daily
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_insider_txn
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_institutional_holding
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_ownership_event
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_news_sentiment
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_contract_award
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_macro
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_fx_rate
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_fundamentals
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_company_news
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_theme_membership
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_material_event
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_sec_filing_event
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_fundamental_anchor
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.metric_weights
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.security_daily_features
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.fact_theme_opportunity_score
        UNION ALL SELECT COUNT_BIG(*) FROM dbo.opportunity_score_snapshot_manifest
    ) target_counts;

    IF @source_row_count <> @target_row_count
        THROW 50201, 'Warehouse promotion row-count reconciliation failed.', 1;

    IF EXISTS (
        SELECT 1
        FROM (VALUES
            ('dim_security', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_security), (SELECT COUNT_BIG(*) FROM dbo.dim_security)),
            ('dim_theme', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_theme), (SELECT COUNT_BIG(*) FROM dbo.dim_theme)),
            ('bridge_theme_etf', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.bridge_theme_etf), (SELECT COUNT_BIG(*) FROM dbo.bridge_theme_etf)),
            ('security_theme_classification', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.security_theme_classification), (SELECT COUNT_BIG(*) FROM dbo.security_theme_classification)),
            ('dim_date', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_date), (SELECT COUNT_BIG(*) FROM dbo.dim_date)),
            ('dim_entity', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_entity), (SELECT COUNT_BIG(*) FROM dbo.dim_entity)),
            ('dim_source', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.dim_source), (SELECT COUNT_BIG(*) FROM dbo.dim_source)),
            ('fact_market_daily', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_market_daily), (SELECT COUNT_BIG(*) FROM dbo.fact_market_daily)),
            ('fact_insider_txn', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_insider_txn), (SELECT COUNT_BIG(*) FROM dbo.fact_insider_txn)),
            ('fact_institutional_holding', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_institutional_holding), (SELECT COUNT_BIG(*) FROM dbo.fact_institutional_holding)),
            ('fact_ownership_event', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_ownership_event), (SELECT COUNT_BIG(*) FROM dbo.fact_ownership_event)),
            ('fact_news_sentiment', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_news_sentiment), (SELECT COUNT_BIG(*) FROM dbo.fact_news_sentiment)),
            ('fact_contract_award', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_contract_award), (SELECT COUNT_BIG(*) FROM dbo.fact_contract_award)),
            ('fact_macro', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_macro), (SELECT COUNT_BIG(*) FROM dbo.fact_macro)),
            ('fact_fx_rate', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_fx_rate), (SELECT COUNT_BIG(*) FROM dbo.fact_fx_rate)),
            ('fact_fundamentals', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_fundamentals), (SELECT COUNT_BIG(*) FROM dbo.fact_fundamentals)),
            ('fact_company_news', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_company_news), (SELECT COUNT_BIG(*) FROM dbo.fact_company_news)),
            ('fact_theme_membership', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_theme_membership), (SELECT COUNT_BIG(*) FROM dbo.fact_theme_membership)),
            ('fact_material_event', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_material_event), (SELECT COUNT_BIG(*) FROM dbo.fact_material_event)),
            ('fact_sec_filing_event', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_sec_filing_event), (SELECT COUNT_BIG(*) FROM dbo.fact_sec_filing_event)),
            ('fact_fundamental_anchor', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_fundamental_anchor), (SELECT COUNT_BIG(*) FROM dbo.fact_fundamental_anchor)),
            ('metric_weights', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.metric_weights), (SELECT COUNT_BIG(*) FROM dbo.metric_weights)),
            ('security_daily_features', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.security_daily_features), (SELECT COUNT_BIG(*) FROM dbo.security_daily_features)),
            ('fact_theme_opportunity_score', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.fact_theme_opportunity_score), (SELECT COUNT_BIG(*) FROM dbo.fact_theme_opportunity_score)),
            ('opportunity_score_snapshot_manifest', (SELECT COUNT_BIG(*) FROM auspex_bronze.dbo.opportunity_score_snapshot_manifest), (SELECT COUNT_BIG(*) FROM dbo.opportunity_score_snapshot_manifest))
        ) counts(table_name, source_count, target_count)
        WHERE source_count <> target_count
    )
        THROW 50208, 'Warehouse per-table row-count reconciliation failed.', 1;

    IF EXISTS (
        SELECT 1 FROM dbo.security_theme_classification
        WHERE provenance NOT IN ('manual', 'llm')
           OR confidence < 0 OR confidence > CASE WHEN provenance = 'llm' THEN 0.85 ELSE 1 END
           OR effective_from IS NULL
           OR (effective_to IS NOT NULL AND effective_to <= effective_from)
    ) OR EXISTS (
        SELECT classification_id FROM dbo.security_theme_classification
        GROUP BY classification_id HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_market_daily
        GROUP BY security_sk, date_sk, price_revision_hash HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_insider_txn
        GROUP BY accession_no, line_no HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_institutional_holding
        GROUP BY accession_no, security_sk, entity_sk, date_sk, holding_revision_hash
        HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_ownership_event
        GROUP BY accession_no, security_sk, entity_sk, event_date, ownership_revision_hash
        HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_news_sentiment
        GROUP BY news_sk, security_sk, news_revision_hash HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_contract_award
        GROUP BY transaction_id, contract_revision_hash HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_macro
        GROUP BY indicator_code, event_date, macro_revision_hash HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_fx_rate
        GROUP BY ccy_pair, event_date, fx_revision_hash HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_fundamentals
        GROUP BY security_sk, date_sk, fundamentals_revision_hash HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_company_news
        GROUP BY news_sk, security_sk, news_revision_hash HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_theme_membership
        GROUP BY snapshot_batch_id, theme_id, security_sk, event_date, theme_revision_hash
        HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_material_event
        GROUP BY event_sk, material_event_revision_hash HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_sec_filing_event
        GROUP BY filing_event_sk, filing_revision_hash HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_fundamental_anchor
        GROUP BY security_sk, date_sk, model_version HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.fact_theme_opportunity_score
        GROUP BY theme_id, security_sk, date_sk, model_version, weight_version
        HAVING COUNT_BIG(*) > 1
    ) OR EXISTS (
        SELECT 1 FROM dbo.opportunity_score_snapshot_manifest
        GROUP BY as_of_date, model_version, weight_version
        HAVING COUNT_BIG(*) > 1
    )
        THROW 50202, 'Warehouse promotion produced duplicate revisions.', 1;

    IF EXISTS (
        SELECT 1 FROM dbo.security_daily_features
        GROUP BY security_sk, date_sk HAVING COUNT_BIG(*) > 1
    )
        THROW 50203, 'Warehouse promotion produced duplicate feature rows.', 1;

    IF EXISTS (
        SELECT 1 FROM dbo.security_daily_features
        WHERE max_knowledge_date IS NULL OR max_knowledge_date > as_of
           OR feature_built_at IS NULL
           OR opportunity_score IS NOT NULL
           OR score_status <> 'THEME_CONTEXT_REQUIRED'
    )
        THROW 50204, 'Warehouse feature PIT validation failed.', 1;

    IF EXISTS (
        SELECT 1 FROM dbo.fact_theme_opportunity_score
        WHERE model_version <> 'e6b_v2'
           OR weight_version <> 'e6b_balanced_v1'
           OR coverage_status NOT IN ('READY', 'PARTIAL', 'WITHHELD')
           OR coverage_reasons_json IS NULL
           OR LEN(score_id) <> 64
           OR LEN(cohort_snapshot_hash) <> 64
           OR candidate_snapshot_id IS NULL
           OR candidate_snapshot_ingest_ts IS NULL
           OR max_knowledge_date > as_of
           OR candidate_count < 1
           OR (
               coverage_status IN ('READY', 'PARTIAL')
               AND (
                   candidate_count < 8
                   OR opportunity_score IS NULL
                   OR opportunity_score NOT BETWEEN 0 AND 100
                   OR opportunity_score_raw IS NULL
                   OR thesis_linkage_z IS NULL
                   OR attention_acceleration_z IS NULL
                   OR smart_money_z IS NULL
                   OR fundamental_health_z IS NULL
                   OR valuation_brake_z IS NULL
                   OR crowding_positioning_z IS NULL
                   OR thesis_linkage_contribution IS NULL
                   OR attention_acceleration_contribution IS NULL
                   OR smart_money_contribution IS NULL
                   OR fundamental_health_contribution IS NULL
                   OR valuation_brake_contribution IS NULL
                   OR crowding_positioning_contribution IS NULL
               )
           )
           OR (
               coverage_status = 'WITHHELD'
               AND (candidate_count >= 8 OR opportunity_score IS NOT NULL OR opportunity_score_raw IS NOT NULL)
           )
           OR (
               opportunity_score_raw IS NOT NULL
               AND ABS(
                   opportunity_score_raw
                   - thesis_linkage_contribution
                   - attention_acceleration_contribution
                   - smart_money_contribution
                   - fundamental_health_contribution
                   - valuation_brake_contribution
                   - crowding_positioning_contribution
               ) > 1e-10
           )
    )
        THROW 50213, 'Warehouse Opportunity Score contract validation failed.', 1;

    IF EXISTS (
        SELECT 1
        FROM dbo.fact_theme_opportunity_score f
        LEFT JOIN dbo.opportunity_score_snapshot_manifest m
          ON m.generation = f.generation
         AND m.as_of_date = f.as_of
         AND m.model_version = f.model_version
         AND m.weight_version = f.weight_version
         AND m.status = 'completed'
        WHERE m.generation IS NULL
    )
        THROW 50216, 'Warehouse Opportunity Score fact has no completed manifest.', 1;

    IF EXISTS (
        SELECT 1
        FROM dbo.opportunity_score_snapshot_manifest m
        LEFT JOIN (
            SELECT generation, as_of, model_version, weight_version,
                   COUNT_BIG(*) AS row_count,
                   SUM(CASE WHEN coverage_status = 'READY' THEN 1 ELSE 0 END) AS ready_count,
                   SUM(CASE WHEN coverage_status = 'PARTIAL' THEN 1 ELSE 0 END) AS partial_count,
                   SUM(CASE WHEN coverage_status = 'WITHHELD' THEN 1 ELSE 0 END) AS withheld_count,
                   LOWER(CONVERT(VARCHAR(64), HASHBYTES(
                       'SHA2_256',
                       STRING_AGG(CAST(score_id AS VARCHAR(MAX)), '|')
                           WITHIN GROUP (ORDER BY score_id)
                   ), 2)) AS computed_fingerprint
            FROM dbo.fact_theme_opportunity_score
            GROUP BY generation, as_of, model_version, weight_version
        ) f
          ON f.generation = m.generation
         AND f.as_of = m.as_of_date
         AND f.model_version = m.model_version
         AND f.weight_version = m.weight_version
        WHERE m.status <> 'completed'
           OR m.model_version <> 'e6b_v2'
           OR m.weight_version <> 'e6b_balanced_v1'
           OR LEN(m.fingerprint) <> 64
           OR COALESCE(f.row_count, 0) <> m.row_count
           OR COALESCE(f.ready_count, 0) <> m.ready_count
           OR COALESCE(f.partial_count, 0) <> m.partial_count
           OR COALESCE(f.withheld_count, 0) <> m.withheld_count
           OR COALESCE(
               f.computed_fingerprint,
               'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
           ) <> m.fingerprint
    )
        THROW 50214, 'Warehouse Opportunity Score manifest reconciliation failed.', 1;

    DECLARE @active_opportunity_score_count BIGINT = (
        SELECT COUNT_BIG(*)
        FROM dbo.fact_theme_opportunity_score f
        JOIN dbo.opportunity_score_snapshot_manifest m
          ON m.generation = f.generation
         AND m.as_of_date = f.as_of
         AND m.model_version = f.model_version
         AND m.weight_version = f.weight_version
         AND m.status = 'completed'
        WHERE f.max_knowledge_date <= f.as_of
          AND f.model_version = 'e6b_v2'
          AND f.weight_version = 'e6b_balanced_v1'
    );

    IF (SELECT COUNT_BIG(*) FROM dbo.v_opportunity_score) <> @active_opportunity_score_count
        THROW 50217, 'Warehouse Opportunity Score serving projection lost score facts.', 1;

    IF (SELECT COUNT_BIG(*) FROM dbo.v_security_score_attribution) <> @active_opportunity_score_count * 6
        THROW 50218, 'Warehouse Opportunity Score attribution projection lost score facts.', 1;

    IF EXISTS (
        SELECT 1 FROM dbo.fact_fundamental_anchor
        WHERE knowledge_date > DATEFROMPARTS(
            date_sk / 10000, (date_sk / 100) % 100, date_sk % 100
        )
           OR model_version <> 'e20_v2'
           OR uses_forward <> 0
    )
        THROW 50212, 'Warehouse fundamental-anchor PIT/model validation failed.', 1;

    IF EXISTS (
        SELECT 1 FROM (
            SELECT event_date, knowledge_date FROM dbo.fact_market_daily
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_insider_txn
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_institutional_holding
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_ownership_event
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_news_sentiment
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_contract_award
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_macro
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_fx_rate
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_fundamentals
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_company_news
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_theme_membership
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_material_event
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_sec_filing_event
            UNION ALL SELECT event_date, knowledge_date FROM dbo.fact_fundamental_anchor
        ) fact_dates
        WHERE event_date IS NULL OR knowledge_date IS NULL OR event_date > knowledge_date
    )
        THROW 50205, 'Warehouse fact PIT validation failed.', 1;

    IF EXISTS (
        SELECT 1 FROM dbo.fact_institutional_holding
        WHERE holding_revision_hash IS NULL OR silver_natural_key IS NULL
           OR silver_batch_id IS NULL OR silver_ingest_ts IS NULL
           OR silver_source_record_hash IS NULL OR silver_loaded_at IS NULL
    )
        THROW 50206, 'Warehouse institutional-holding provenance validation failed.', 1;

    IF EXISTS (
        SELECT 1 FROM dbo.fact_theme_membership
        WHERE theme_revision_hash IS NULL
           OR snapshot_batch_id IS NULL
           OR snapshot_ingest_ts IS NULL
    )
        THROW 50215, 'Warehouse theme snapshot provenance validation failed.', 1;

    IF EXISTS (
        SELECT 1
        FROM dbo.fact_market_daily f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1
        FROM dbo.fact_insider_txn f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1
        FROM dbo.fact_institutional_holding f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.fact_ownership_event f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.fact_news_sentiment f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.fact_contract_award f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.fact_fundamentals f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1
        FROM dbo.fact_company_news f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.fact_theme_membership f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.fact_material_event f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.fact_fundamental_anchor f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.security_daily_features f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.fact_theme_opportunity_score f
        LEFT JOIN dbo.dim_security d ON d.security_sk = f.security_sk
        WHERE f.security_sk IS NULL OR d.security_sk IS NULL
    )
        THROW 50207, 'Warehouse security dimension orphan validation failed.', 1;

    IF EXISTS (
        SELECT 1 FROM dbo.fact_insider_txn f
        LEFT JOIN dbo.dim_entity d ON d.entity_sk = f.entity_sk
        WHERE f.entity_sk IS NOT NULL AND d.entity_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.fact_institutional_holding f
        LEFT JOIN dbo.dim_entity d ON d.entity_sk = f.entity_sk
        WHERE f.entity_sk IS NULL OR d.entity_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.fact_ownership_event f
        LEFT JOIN dbo.dim_entity d ON d.entity_sk = f.entity_sk
        WHERE f.entity_sk IS NULL OR d.entity_sk IS NULL
        UNION ALL
        SELECT 1 FROM dbo.fact_contract_award f
        LEFT JOIN dbo.dim_entity d ON d.entity_sk = f.entity_sk
        WHERE f.entity_sk IS NULL OR d.entity_sk IS NULL
    )
        THROW 50209, 'Warehouse entity dimension orphan validation failed.', 1;

    IF EXISTS (
        SELECT 1 FROM (
            SELECT source_sk FROM dbo.fact_market_daily
            UNION ALL SELECT source_sk FROM dbo.fact_insider_txn
            UNION ALL SELECT source_sk FROM dbo.fact_institutional_holding
            UNION ALL SELECT source_sk FROM dbo.fact_ownership_event
            UNION ALL SELECT source_sk FROM dbo.fact_news_sentiment
            UNION ALL SELECT source_sk FROM dbo.fact_contract_award
            UNION ALL SELECT source_sk FROM dbo.fact_macro
            UNION ALL SELECT source_sk FROM dbo.fact_fx_rate
            UNION ALL SELECT source_sk FROM dbo.fact_fundamentals
            UNION ALL SELECT source_sk FROM dbo.fact_company_news
            UNION ALL SELECT source_sk FROM dbo.fact_theme_membership
            UNION ALL SELECT source_sk FROM dbo.fact_material_event
            UNION ALL SELECT source_sk FROM dbo.fact_sec_filing_event
            UNION ALL SELECT source_sk FROM dbo.fact_fundamental_anchor
        ) f
        LEFT JOIN dbo.dim_source d ON d.source_sk = f.source_sk
        WHERE f.source_sk IS NULL OR d.source_sk IS NULL
    )
        THROW 50210, 'Warehouse source dimension orphan validation failed.', 1;

    IF EXISTS (
        SELECT 1 FROM (
            SELECT date_sk FROM dbo.fact_market_daily
            UNION ALL SELECT date_sk FROM dbo.fact_insider_txn
            UNION ALL SELECT date_sk FROM dbo.fact_institutional_holding
            UNION ALL SELECT date_sk FROM dbo.fact_ownership_event
            UNION ALL SELECT date_sk FROM dbo.fact_news_sentiment
            UNION ALL SELECT date_sk FROM dbo.fact_contract_award
            UNION ALL SELECT date_sk FROM dbo.fact_macro
            UNION ALL SELECT date_sk FROM dbo.fact_fx_rate
            UNION ALL SELECT date_sk FROM dbo.fact_fundamentals
            UNION ALL SELECT date_sk FROM dbo.fact_company_news
            UNION ALL SELECT date_sk FROM dbo.fact_material_event
            UNION ALL SELECT date_sk FROM dbo.fact_fundamental_anchor
            UNION ALL SELECT date_sk FROM dbo.security_daily_features
            UNION ALL SELECT date_sk FROM dbo.fact_theme_opportunity_score
        ) f
        LEFT JOIN dbo.dim_date d ON d.date_sk = f.date_sk
        WHERE f.date_sk IS NULL OR d.date_sk IS NULL
    )
        THROW 50211, 'Warehouse date dimension orphan validation failed.', 1;

    INSERT INTO dbo.gold_promotion_audit (
        promotion_run_id, source_snapshot_manifest, source_row_count,
        target_row_count, started_at, completed_at, [status]
    ) VALUES (
        @promotion_run_id, @source_snapshot_manifest, @source_row_count,
        @target_row_count, @started_at, SYSUTCDATETIME(), 'SUCCEEDED'
    );

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO
