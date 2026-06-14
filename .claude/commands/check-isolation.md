Review all web API endpoints or data-access methods added or changed in the current diff for per-user data isolation:

1. Every query or mutation that touches a per-user table (`dim_account`, `fact_portfolio_transaction`, `fact_portfolio_valuation`, `recommendation`, `app_config`, `user_watchlist`) must filter by `owner_user_sk = @current_user`.
2. No data-access method should have an un-scoped overload — every path must require a `user_sk` parameter.
3. Write operations (`UPDATE`, `DELETE`) must include `WHERE owner_user_sk = @current_user` so a mismatched ID affects zero rows rather than wrong data.
4. The web API must resolve `owner_user_sk` from the validated Entra token, never from a query parameter or request body.
5. Shared signal data (prices, filings, RAGS features, `dim_security`) is intentionally not per-user — no `owner_user_sk` filter needed there.

Report any isolation gaps found. If all checks pass, confirm "Isolation checks passed."
