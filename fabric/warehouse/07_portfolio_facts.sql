-- Auspex E12 owner-scoped portfolio facts (Fabric Warehouse T-SQL)

IF OBJECT_ID('dbo.fact_portfolio_transaction', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_portfolio_transaction (
        transaction_id    VARCHAR(64)   NOT NULL,
        owner_user_sk     VARCHAR(64)   NOT NULL,
        client_request_id VARCHAR(128)  NOT NULL,
        account_id        VARCHAR(64)   NOT NULL,
        transaction_type  VARCHAR(32)   NOT NULL,
        security_sk       BIGINT        NULL,
        ticker            VARCHAR(16)   NULL,
        security_currency VARCHAR(3)    NULL,
        quantity          DECIMAL(20,8) NULL,
        price             DECIMAL(20,8) NULL,
        currency          VARCHAR(3)    NOT NULL,
        fees              DECIMAL(20,2) NOT NULL,
        cash_amount       DECIMAL(20,2) NOT NULL,
        base_currency     VARCHAR(3)    NULL,
        fx_rate_to_base   DECIMAL(20,8) NULL,
        corrects_transaction_id VARCHAR(64) NULL,
        gross_amount      DECIMAL(20,2) NULL,
        source_currency   VARCHAR(3)    NULL,
        source_amount     DECIMAL(20,2) NULL,
        fx_rate_to_settlement DECIMAL(20,8) NULL,
        linked_transaction_id VARCHAR(64) NULL,
        cost_category     VARCHAR(32)   NULL,
        affects_cash      BIT           NOT NULL,
        event_date        DATE          NOT NULL,
        knowledge_date    DATE          NOT NULL,
        created_at        DATETIME2(6)  NOT NULL,
        payload_hash      CHAR(64)      NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_transaction') AND name = 'base_currency')
    ALTER TABLE dbo.fact_portfolio_transaction ADD base_currency VARCHAR(3) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_transaction') AND name = 'fx_rate_to_base')
    ALTER TABLE dbo.fact_portfolio_transaction ADD fx_rate_to_base DECIMAL(20,8) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_transaction') AND name = 'corrects_transaction_id')
    ALTER TABLE dbo.fact_portfolio_transaction ADD corrects_transaction_id VARCHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_transaction') AND name = 'gross_amount')
    ALTER TABLE dbo.fact_portfolio_transaction ADD gross_amount DECIMAL(20,2) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_transaction') AND name = 'source_currency')
    ALTER TABLE dbo.fact_portfolio_transaction ADD source_currency VARCHAR(3) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_transaction') AND name = 'source_amount')
    ALTER TABLE dbo.fact_portfolio_transaction ADD source_amount DECIMAL(20,2) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_transaction') AND name = 'fx_rate_to_settlement')
    ALTER TABLE dbo.fact_portfolio_transaction ADD fx_rate_to_settlement DECIMAL(20,8) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_transaction') AND name = 'linked_transaction_id')
    ALTER TABLE dbo.fact_portfolio_transaction ADD linked_transaction_id VARCHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_transaction') AND name = 'cost_category')
    ALTER TABLE dbo.fact_portfolio_transaction ADD cost_category VARCHAR(32) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_transaction') AND name = 'affects_cash')
    ALTER TABLE dbo.fact_portfolio_transaction ADD affects_cash BIT NULL;
GO

UPDATE dbo.fact_portfolio_transaction SET affects_cash = 1 WHERE affects_cash IS NULL;

IF OBJECT_ID('dbo.fact_portfolio_position', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_portfolio_position (
        owner_user_sk  VARCHAR(64)   NOT NULL,
        account_id     VARCHAR(64)   NOT NULL,
        security_sk    BIGINT        NOT NULL,
        ticker         VARCHAR(16)   NOT NULL,
        security_currency VARCHAR(3) NULL,
        gics_sector    VARCHAR(64)   NULL,
        country        VARCHAR(2)    NULL,
        quantity       DECIMAL(20,8) NOT NULL,
        market_value_base DECIMAL(20,2) NULL,
        position_weight DECIMAL(12,8) NULL,
        event_date     DATE          NOT NULL,
        knowledge_date DATE          NOT NULL
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_position') AND name = 'security_currency')
    ALTER TABLE dbo.fact_portfolio_position ADD security_currency VARCHAR(3) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_position') AND name = 'gics_sector')
    ALTER TABLE dbo.fact_portfolio_position ADD gics_sector VARCHAR(64) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_position') AND name = 'country')
    ALTER TABLE dbo.fact_portfolio_position ADD country VARCHAR(2) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_position') AND name = 'market_value_base')
    ALTER TABLE dbo.fact_portfolio_position ADD market_value_base DECIMAL(20,2) NULL;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.fact_portfolio_position') AND name = 'position_weight')
    ALTER TABLE dbo.fact_portfolio_position ADD position_weight DECIMAL(12,8) NULL;

IF OBJECT_ID('dbo.fact_portfolio_valuation', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.fact_portfolio_valuation (
        owner_user_sk     VARCHAR(64)   NOT NULL,
        valuation_date    DATE          NOT NULL,
        base_currency     VARCHAR(3)    NOT NULL,
        total_cash_base   DECIMAL(20,2) NULL,
        total_stocks_base DECIMAL(20,2) NULL,
        total_value_base  DECIMAL(20,2) NULL,
        cash_weight       DECIMAL(12,8) NULL,
        missing_prices    INT           NOT NULL,
        missing_fx        INT           NOT NULL,
        coverage_complete BIT           NOT NULL,
        knowledge_date    DATE          NOT NULL
    );
END;
