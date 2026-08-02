-- Auspex E12 transactional promotion from Lakehouse portfolio tables.

CREATE OR ALTER PROCEDURE dbo.usp_promote_portfolio_snapshot
AS
BEGIN
    SET NOCOUNT ON;

    IF NOT EXISTS (
        SELECT 1
        FROM auspex_bronze.dbo.portfolio_snapshot_manifest
        WHERE status = 'completed'
    )
        THROW 51200, 'Portfolio snapshot manifest is not completed.', 1;

    DECLARE @expected_transactions BIGINT = (
        SELECT TOP 1 transaction_count
        FROM auspex_bronze.dbo.portfolio_snapshot_manifest
        WHERE status = 'completed'
        ORDER BY completed_at DESC
    );
    DECLARE @expected_positions BIGINT = (
        SELECT TOP 1 position_count
        FROM auspex_bronze.dbo.portfolio_snapshot_manifest
        WHERE status = 'completed'
        ORDER BY completed_at DESC
    );
    DECLARE @expected_valuations BIGINT = (
        SELECT TOP 1 valuation_count
        FROM auspex_bronze.dbo.portfolio_snapshot_manifest
        WHERE status = 'completed'
        ORDER BY completed_at DESC
    );

    IF @expected_transactions <> (SELECT COUNT(*) FROM auspex_bronze.dbo.silver_portfolio_transaction)
       OR @expected_positions <> (SELECT COUNT(*) FROM auspex_bronze.dbo.fact_portfolio_position)
       OR @expected_valuations <> (SELECT COUNT(*) FROM auspex_bronze.dbo.fact_portfolio_valuation)
        THROW 51205, 'Portfolio snapshot manifest row counts do not reconcile.', 1;

    BEGIN TRY
        BEGIN TRANSACTION;

        DELETE FROM dbo.fact_portfolio_transaction;
        DELETE FROM dbo.fact_portfolio_position;
        DELETE FROM dbo.fact_portfolio_valuation;

        INSERT INTO dbo.fact_portfolio_transaction (
            transaction_id, owner_user_sk, client_request_id, account_id,
            transaction_type, security_sk, ticker, security_currency, quantity,
            price, currency, fees, cash_amount, base_currency, fx_rate_to_base,
            corrects_transaction_id, gross_amount, source_currency, source_amount,
            fx_rate_to_settlement, linked_transaction_id, cost_category, affects_cash,
            event_date, knowledge_date, created_at, payload_hash
        )
        SELECT
            transaction_id, owner_user_sk, client_request_id, account_id,
            transaction_type, security_sk, ticker, security_currency, quantity,
            price, currency, fees, cash_amount, base_currency, fx_rate_to_base,
            corrects_transaction_id, gross_amount, source_currency, source_amount,
            fx_rate_to_settlement, linked_transaction_id, cost_category, affects_cash,
            event_date, knowledge_date, created_at, payload_hash
        FROM auspex_bronze.dbo.silver_portfolio_transaction;

        INSERT INTO dbo.fact_portfolio_position (
            owner_user_sk, account_id, security_sk, ticker, quantity,
            security_currency, gics_sector, country, market_value_base,
            position_weight, event_date, knowledge_date
        )
        SELECT
            owner_user_sk, account_id, security_sk, ticker, quantity,
            security_currency, gics_sector, country, market_value_base,
            position_weight, event_date, knowledge_date
        FROM auspex_bronze.dbo.fact_portfolio_position;

        INSERT INTO dbo.fact_portfolio_valuation (
            owner_user_sk, valuation_date, base_currency, total_cash_base,
            total_stocks_base, total_value_base, cash_weight, missing_prices,
            missing_fx, coverage_complete, knowledge_date
        )
        SELECT
            owner_user_sk, valuation_date, base_currency, total_cash_base,
            total_stocks_base, total_value_base, cash_weight, missing_prices,
            missing_fx, coverage_complete, knowledge_date
        FROM auspex_bronze.dbo.fact_portfolio_valuation;

        IF EXISTS (
            SELECT 1
            FROM dbo.fact_portfolio_transaction
            WHERE owner_user_sk IS NULL
               OR event_date IS NULL
               OR knowledge_date IS NULL
               OR event_date > knowledge_date
        )
            THROW 51201, 'Portfolio transaction PIT/owner validation failed.', 1;

        IF EXISTS (
            SELECT owner_user_sk, transaction_id
            FROM dbo.fact_portfolio_transaction
            GROUP BY owner_user_sk, transaction_id
            HAVING COUNT(*) > 1
        )
            THROW 51202, 'Portfolio transaction duplicate validation failed.', 1;

                IF EXISTS (
                        SELECT 1
                        FROM dbo.fact_portfolio_transaction c
                        LEFT JOIN dbo.fact_portfolio_transaction t
                            ON t.owner_user_sk = c.owner_user_sk
                         AND t.transaction_id = c.corrects_transaction_id
                        WHERE c.corrects_transaction_id IS NOT NULL
                            AND (t.transaction_id IS NULL OR t.corrects_transaction_id IS NOT NULL)
                ) OR EXISTS (
                        SELECT owner_user_sk, corrects_transaction_id
                        FROM dbo.fact_portfolio_transaction
                        WHERE corrects_transaction_id IS NOT NULL
                        GROUP BY owner_user_sk, corrects_transaction_id
                        HAVING COUNT(*) > 1
                )
                        THROW 51206, 'Portfolio correction graph validation failed.', 1;

        IF EXISTS (
            SELECT owner_user_sk, account_id, security_sk
            FROM dbo.fact_portfolio_position
            GROUP BY owner_user_sk, account_id, security_sk
            HAVING COUNT(*) > 1
        )
            THROW 51203, 'Portfolio position duplicate validation failed.', 1;

        IF EXISTS (
            SELECT 1 FROM dbo.fact_portfolio_position
            WHERE position_weight < 0 OR position_weight > 1
               OR market_value_base < 0
        )
            THROW 51207, 'Portfolio position exposure validation failed.', 1;

        IF EXISTS (
            SELECT owner_user_sk, valuation_date, base_currency
            FROM dbo.fact_portfolio_valuation
            GROUP BY owner_user_sk, valuation_date, base_currency
            HAVING COUNT(*) > 1
        )
            THROW 51204, 'Portfolio valuation duplicate validation failed.', 1;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO