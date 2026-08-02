-- Auspex E12 portfolio dimensions (Fabric Warehouse T-SQL)

IF OBJECT_ID('dbo.dim_account', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.dim_account (
        owner_user_sk VARCHAR(64) NOT NULL,
        account_id    VARCHAR(64) NOT NULL,
        currency      VARCHAR(3)  NOT NULL,
        is_active     BIT         NOT NULL,
        created_at    DATETIME2(6) NOT NULL
    );
END;
