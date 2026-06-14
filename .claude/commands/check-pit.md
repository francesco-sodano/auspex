Before finishing any change that touches a fact table, metric view, silver transform, or AI Search query, verify point-in-time correctness:

1. Every new fact table or silver table has both `event_date` (when the event occurred) and `knowledge_date` (when Auspex first knew it) columns.
2. Every metric view or gold query filters `WHERE knowledge_date <= @asof` (or equivalent). No filter that uses only `event_date` for PIT control.
3. For 13F data specifically: `knowledge_date` must be the SEC filing date, not the quarter-end date.
4. For AI Search queries: the filter must include `knowledge_date le {asof}` — not just a date range on `event_date`.
5. Any backtest or historical query must parameterize `@asof` and pass it through all layers; no hardcoded `GETDATE()` or `CURRENT_DATE` in reusable views.

Report any violations found. If all checks pass, confirm "PIT checks passed."
