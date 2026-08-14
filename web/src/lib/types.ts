export type GateTrace = {
  gate: string
  passed: boolean
  actual: string | number | boolean | null
  threshold: string | number | boolean | null
  reason?: string
}

export type Recommendation = {
  id: string
  security_id: string
  ticker: string
  company_name: string
  action: 'BUY' | 'ADD' | 'HOLD_NO_ACTION' | 'HOLD_INSUFFICIENT_DATA' | 'TRIM' | 'SELL'
  rationale: string
  target_weight?: string
  current_weight?: string
  suggested_trade_chf?: string
  suggested_quantity?: string
  estimated_cost_chf?: string
  auspex_score?: number | null
  buy_ready: boolean
  blocking_reasons: string[]
  gate_trace: GateTrace[]
  as_of_date?: string | null
  disposition?: 'ACCEPTED' | 'REJECTED' | 'DEFERRED' | null
  followed?: boolean
  outcome_matures_on?: string | null
  outcome_mature?: boolean
}

export type Briefing = {
  date: string
  run_status: 'SUCCESS' | 'DEGRADED' | 'FAILED' | 'RUNNING'
  max_knowledge_date: string
  portfolio: {
    value_chf: string
    invested_chf: string
    cash_chf: string
    total_gain_chf: string
    day_change_chf: string
    expenses_chf: string
    dividends_chf: string
    unrealised_chf: string
  } | null
  changes: Array<{
    security_id: string
    ticker: string
    company_name: string
    leg: string
    contribution_delta: string
    narrative: string
    evidence_excerpt: string
  }>
  movers_up: ScoreMover[]
  movers_down: ScoreMover[]
  escalated_risks: Array<{
    security_id: string
    ticker: string
    category: string
    summary: string
    severity: string
  }>
  recommendations: Recommendation[]
  assertion_failures: string[]
}

export type ScoreMover = {
  security_id: string
  ticker: string
  company_name: string
  score: number
  prior_score: number
  score_change: number
  narrative: string
  buy_ready: boolean
  buy_blockers: string[]
}

export type SecuritySummary = {
  security_id: string
  ticker: string
  name: string
  market: string
  cohort: string
  score: string | null
  percentile: number | null
  direction: 'STRENGTHENING' | 'STABLE' | 'WEAKENING' | null
  coverage: string | null
  action: Recommendation['action'] | null
}

export type SecurityPackage = {
  security: SecuritySummary & { filer_profile: 'DOMESTIC' | 'FPI' }
  as_of_date: string
  narrative: string
  legs: Record<string, {
    raw: string | null
    z: string | null
    weight: string
    contribution: string | null
    computable: boolean
    score: number | null
    neutral: boolean
    status_explanation: string | null
  }>
  recommendation: Recommendation | null
  market: string
  business_summary: string
  current_price_usd: string | null
  price_change_pct: string | null
  price_history: Array<{ date: string; close: string }>
  fundamentals: Array<{ label: string; value: string | null; period_end: string | null }>
  score_change: number | null
  score_reasoning: string
  news: Array<{
    document_id: string
    form: string
    filed_at: string
    headline: string
    digest: string
    source_url: string
    publisher: string
    retrieved_at: string
    relevance_reason: string
    stale: boolean
  }>
  history: Array<{ as_of_date: string; composite: string; percentile: number }>
  documents: Array<{
    document_id: string
    form: string
    filed_at: string
    headline: string
    digest: string
    source_url: string
    publisher: string
    retrieved_at: string
    relevance_reason: string
    stale: boolean
  }>
}

export type Portfolio = {
  as_of_date: string
  lot_level: boolean
  total_value_chf: string
  invested_chf: string
  cash_chf: string
  total_gain_chf: string
  day_change_chf: string
  expenses_chf: string
  dividends_chf: string
  source_ledger_read_at: string
  degraded_fields: string[]
  positions: Array<{
    ticker: string
    company_name: string
    quantity: string
    weight: string | null
    market_value_usd: string | null
    market_value_chf: string | null
    cost_basis_usd: string | null
    cost_basis_chf: string | null
    unrealised_usd: string | null
    unrealised_chf: string | null
    fx_effect_chf: string | null
    holding_period_days: number | null
    source_ledger_read_at: string
    degraded_fields: string[]
    auspex_score: number | null
    action: Recommendation['action'] | null
    buy_ready: boolean | null
    readiness_reason: string | null
    price_history: Array<{ date: string; open: string; high: string; low: string; close: string }>
  }>
}

export type PortfolioTransaction = {
  transaction_id: string
  transaction_type: TransactionType | 'VOID'
  event_date: string
  currency: string
  security_code: string | null
  quantity: string | null
  price: string | null
  gross_amount: string
  cash_amount: string
  cash_currency: string
  fees: string
  cost_components: Array<{
    category: string
    amount: string
    currency: string
    source_amount: string | null
    source_currency: string | null
    fx_rate_to_settlement: string | null
  }>
  fx_rate_to_base: string | null
  followed_auspex: boolean
  recommendation_id: string | null
  notes: string | null
  created_at: string
  corrects_transaction_id: string | null
  status: 'EFFECTIVE' | 'CORRECTED' | 'VOIDED'
}

export type TransactionType =
  | 'OPENING_POSITION'
  | 'OPENING_CASH'
  | 'BUY'
  | 'SELL'
  | 'DEPOSIT'
  | 'WITHDRAWAL'
  | 'DIVIDEND'
  | 'INTEREST'
  | 'FEE'
  | 'TAX'

export type PortfolioTransactionInput = {
  client_request_id: string
  transaction_type: TransactionType
  event_date: string
  currency: string
  security_code?: string | null
  quantity?: string | null
  price?: string | null
  amount?: string | null
  fees: string
  cost_components: Array<{
    category:
      | 'BROKER_COMMISSION'
      | 'TRANSACTION_TAX'
      | 'WITHHOLDING_TAX'
      | 'VAT'
      | 'CUSTODY_FEE'
      | 'ACCOUNT_FEE'
      | 'OTHER_FEE'
    amount: string
    currency: 'CHF' | 'USD'
  }>
  fx_rate_to_base?: string | null
  followed_auspex: boolean
  recommendation_id?: string | null
  notes?: string | null
}

export type PerformanceReport = {
  as_of_date: string
  composite_ic: Record<'21' | '63' | '126', string | null>
  leg_ic: Record<string, string | null>
  leg_correlation: { labels: string[]; values: Array<Array<string | null>> }
  suggestion_hit_rate: string | null
  dispositions: {
    accepted: string | null
    rejected: string | null
    accepted_sample_size: number
    rejected_sample_size: number
  }
  attribution: {
    followed_pending: number
    followed_mature: number
    not_followed_pending: number
    not_followed_mature: number
  }
  cohort_dispersion: Record<string, string | null>
  sample_size: number
  backfilled_sample_size: number
}

export type RiskProfile = 'CONSERVATIVE' | 'MODERATE' | 'AGGRESSIVE'
export type InvestmentHorizon = 'SHORT_TERM' | 'MEDIUM_TERM' | 'LONG_TERM'
export type InvestmentObjective =
  | 'CAPITAL_PRESERVATION'
  | 'INCOME'
  | 'BALANCED_GROWTH'
  | 'CAPITAL_GROWTH'

export type UserSettings = {
  id: string
  user_id: string
  risk_profile: RiskProfile
  cash_reserve_chf: string
  investment_horizon: InvestmentHorizon
  investment_objective: InvestmentObjective
  directional_only_acknowledged: boolean
  no_guarantee_acknowledged: boolean
  not_financial_advice_acknowledged: boolean
  market_loss_acknowledged: boolean
  independent_decision_acknowledged: boolean
  acknowledgement_version: string
  acknowledged_at: string | null
  updated_at: string
}

export type UserSettingsInput = Omit<
  UserSettings,
  'id' | 'user_id' | 'acknowledgement_version' | 'acknowledged_at' | 'updated_at'
>

export type AccountConfiguration = {
  themes: Array<{ id: string; label: string }>
  cohorts: Array<{ id: string; parent: string; tickers: string[] }>
}

export type ConversationTurn = {
  id: string
  user_id: string
  conversation_id: string
  turn_index: number
  question: string
  answer: string | null
  created_at: string
}
