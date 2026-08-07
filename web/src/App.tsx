import { useEffect, useRef, useState } from 'react'
import {
  ArrowRight,
  BookOpenText,
  Check,
  CircleHelp,
  ChevronDown,
  Clock3,
  LogOut,
  Mail,
  MessageCircle,
  Search,
  Pencil,
  Plus,
  RotateCcw,
  Send,
  ShieldCheck,
  SlidersHorizontal,
  Sunrise,
  ThumbsDown,
  ThumbsUp,
  UserCheck,
  UserX,
  WalletCards,
  X,
} from 'lucide-react'
import './App.css'

type ClientPrincipal = {
  identityProvider: string
  userId: string
  userDetails?: string
  userRoles: string[]
}

type AppUser = {
  user_sk: string
  contact_email: string | null
  status: 'pending' | 'active' | 'rejected' | 'suspended'
  role: 'user' | 'admin' | null
  onboarded: boolean
  base_currency: string
  risk_profile: string | null
  investment_horizon: string | null
  suitability_acknowledged_at: string | null
  created_at?: string
  reviewed_at?: string | null
  review_note?: string | null
  capabilities: Array<'product' | 'admin'>
}

type PortfolioTransaction = {
  transaction_id: string
  transaction_type: string
  event_date: string
  currency: string
  security_code: string | null
  quantity: string | null
  price: string | null
  fees: string
  cash_amount: string
  base_currency: string | null
  fx_rate_to_base: string | null
  security_sk: number | null
  security_isin: string | null
  security_name: string | null
  security_currency: string | null
  security_exchange: string | null
  corrects_transaction_id: string | null
  gross_amount: string | null
  source_currency: string | null
  source_amount: string | null
  fx_rate_to_settlement: string | null
  linked_transaction_id: string | null
  cost_category: string | null
  affects_cash: boolean
}

type CostComponentDraft = {
  id: string
  category: string
  amount: string
  currency: string
  fxRateToSettlement: string
}

type SecurityOption = {
  security_sk: number
  ticker: string
  isin: string | null
  company_name: string
  currency: string
  exchange: string | null
}

type PortfolioSummary = {
  cash_by_currency: Record<string, string>
  positions: Array<{ security_code: string; quantity: string }>
  transaction_count: number
  updated_on: string | null
  net_contributed_capital_by_currency: Record<string, string>
  total_fees_by_currency: Record<string, string>
  dividends_by_currency: Record<string, string>
  interest_by_currency: Record<string, string>
  reporting_currency: 'USD'
  cash_total: string | null
  net_contributed_capital_total: string | null
  capital_breakdown_base: Record<'external_cash' | 'opening_positions' | 'historical_acquisition_costs' | 'withdrawals', string> | null
  current_position_cost_basis_base: string | null
  unrealized_gain_base: string | null
  other_earnings_base: string | null
  total_fees_total: string | null
  dividends_total: string | null
  interest_total: string | null
  currency_exposure: Array<{
    name: string
    market_value_base: string
    weight: string
  }>
  coverage: {
    missing_prices: string[]
    missing_fx: string[]
    missing_capital_fx: string[]
    oldest_price_date: string | null
  }
  assets: Array<{
    asset_type: 'cash' | 'stock'
    ticker: string
    name: string
    quantity: string | null
    price_currency: string | null
    latest_price: string | null
    current_value: string | null
    weight: string | null
    valuation_status: 'valued' | 'missing_price' | 'missing_fx'
  }>
  allocation: {
    cash_value: string | null
    stocks_value: string | null
    cash_weight: string | null
    stocks_weight: string | null
    complete: boolean
    reason: 'negative_cash' | 'incomplete_coverage' | null
  }
  total_value: {
    status: 'pending_market_valuation' | 'ready' | 'stale'
    value_by_currency: Record<string, string> | null
    reason: string
  }
  earnings: {
    status: 'pending_market_valuation' | 'ready' | 'stale'
    value_by_currency: Record<string, string> | null
    reason: string
  }
}

type PortfolioHomeSummary = {
  status: 'empty' | 'pending_ingestion' | 'ready' | 'stale' | 'unavailable'
  base_currency: string
  valuation_as_of: string | null
  total_cash_base: string | null
  total_stocks_base: string | null
  total_value_base: string | null
  net_contributed_capital_base: string | null
  total_earnings_base: string | null
  capital_breakdown_base: Record<'external_cash' | 'opening_positions' | 'historical_acquisition_costs' | 'withdrawals', string> | null
  current_position_cost_basis_base: string | null
  unrealized_gain_base: string | null
  other_earnings_base: string | null
  cash_weight: string | null
  holdings: Array<{
    security_sk: number | null
    ticker: string
    isin: string | null
    company_name: string
    currency: string
    price_currency: string | null
    exchange: string | null
    quantity: string
    latest_price: string | null
    price_as_of: string | null
    market_value_base: string | null
    weight: string | null
    average_acquisition_price: string | null
    gain_loss_pct: string | null
    price_history: Array<{ date: string; price: string }>
    opportunity_score: string | null
    score_as_of: string | null
    score_coverage_status: 'READY' | 'PARTIAL' | null
    score_coverage_reasons: string[]
    score_candidate_count: number | null
    score_classification_provenance: 'manual' | 'llm' | 'trs' | null
    theme_id: string | null
    theme_provenance: 'manual' | 'llm' | 'trs' | null
    theme_confidence: string | null
  }>
  exposures: Record<'sector' | 'country' | 'currency' | 'theme' | 'exchange', Array<{
    name: string
    market_value_base: string
    weight: string
  }>>
  coverage: {
    missing_prices: string[]
    missing_fx: string[]
    missing_capital_fx: string[]
    oldest_price_date: string | null
  }
}

function PriceSparkline({ points, ticker }: {
  points: Array<{ date: string; price: string }>
  ticker: string
}) {
  const values = points.map((point) => Number(point.price)).filter(Number.isFinite)
  if (values.length < 2) return <span className="sparkline-empty">Not enough history</span>
  const width = 128
  const height = 38
  const padding = 3
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const range = maximum - minimum || 1
  const coordinates = values.map((value, index) => {
    const x = padding + (index / (values.length - 1)) * (width - padding * 2)
    const y = height - padding - ((value - minimum) / range) * (height - padding * 2)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  const rising = values.at(-1)! >= values[0]
  const change = ((values.at(-1)! / values[0]) - 1) * 100
  return <div className={`price-sparkline ${rising ? 'rising' : 'falling'}`} title={`${ticker}: ${change >= 0 ? '+' : ''}${change.toFixed(1)}% over ${values.length} sessions`}>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${ticker} ${values.length}-session price trend, ${change >= 0 ? 'up' : 'down'} ${Math.abs(change).toFixed(1)} percent`}>
      <polyline points={coordinates} fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke" />
    </svg>
    <small>{change >= 0 ? '+' : ''}{change.toFixed(1)}%</small>
  </div>
}

type RecommendationResponse = {
  status: 'ready' | 'stale' | 'withheld'
  as_of: string | null
  risk_profile: string
  base_currency: string
  reasons: string[]
  policy_gates: {
    cash_buffer_pct: string
    required_cash_buffer_base: string
    available_cash_base: string
    financing_policy_configured: boolean
    ready_signal_count: number
    total_signal_count: number
  }
  disclaimer?: string
  recommendations: Array<{
    recommendation_id: string
    security_sk: number
    ticker: string
    action: 'BUY' | 'ADD' | 'HOLD' | 'TRIM' | 'SELL'
    current_weight: string
    target_weight: string
    suggested_amount_base: string
    estimated_cost_base: string
    expected_edge_base: string
    confidence: 'LOW' | 'MEDIUM' | 'HIGH'
    rationale: string
    suppression_reasons: string[]
    tax_flags: string[]
    opportunity_score: string | null
    opportunity_score_raw: string | null
    candidate_count: number | null
    classification_provenance: 'manual' | 'llm' | 'trs' | null
    theme_id: string | null
    coverage_status: 'READY' | 'PARTIAL' | 'WITHHELD'
    coverage_reasons: string[]
    attribution: Array<{
      key: string
      contribution: string | null
      direction: 'RAISED' | 'LOWERED' | 'NEUTRAL'
    }>
  }>
}

type CompanyOpportunity = {
  package_version: string
  package_fingerprint: string
  security_sk: number
  ticker: string
  company_name: string
  as_of: string
  outlook_horizon_days: number
  outlook_direction: 'ACCELERATING' | 'STABLE' | 'DETERIORATING' | 'UNCERTAIN'
  theme_id: string
  candidate_count: number
  coverage_status: 'READY' | 'PARTIAL' | 'WITHHELD'
  coverage_reasons: string[]
  opportunity_score_raw: number | null
  opportunity_score: number | null
  max_knowledge_date: string
  legs: Array<{
    leg_name: string
    normalized_value: number | null
    contribution: number | null
    direction: 'RAISED' | 'LOWERED' | 'NEUTRAL' | 'UNAVAILABLE'
    coverage_reasons: string[]
    evidence_ids: string[]
  }>
  evidence: Array<{
    evidence_id: string
    source_type: string
    event_date: string
    knowledge_date: string
    excerpt: string | null
  }>
  narrative?: {
    summary: string
    uncertainty: string
    citation_ids: string[]
    citations: Array<{
      evidence_id: string
      source_type: string
      event_date: string
      knowledge_date: string
      excerpt: string | null
    }>
  }
  research_only: boolean
}

type CompanyOpportunityResponse = {
  generated_at: string
  count: number
  opportunities: CompanyOpportunity[]
  disclaimer: string
}

type MetricMetadata = {
  key: string
  display_name: string
  plain_description: string
  unit: string
  direction: 'higher_is_better' | 'lower_is_better' | 'contextual'
  tier: 'simple' | 'advanced'
}

type GroundedExplanation = {
  decision_id: string
  recommendation_id: string
  ticker: string
  action: string
  status: 'published' | 'withheld'
  as_of: string
  output: {
    explanation?: string
    uncertainty?: string
    evidence_ids?: string[]
  }
  citations: Array<{
    id: string
    title: string | null
    url: string | null
    source_name: string | null
    source_type: string | null
    excerpt: string | null
    event_date: string | null
    knowledge_date: string | null
    content_status: string | null
  }>
  reasons: string[]
  created_at: string
  disclaimer: string
}

type RecommendationEvent = {
  event_id: string
  recommendation_id: string
  ticker: string
  action: string
  disposition: 'ACCEPTED' | 'DISMISSED'
  recommendation_as_of: string
  created_at: string
}

type RecommendationHistory = {
  decisions: GroundedExplanation[]
  events: RecommendationEvent[]
  current_dispositions: Record<string, 'ACCEPTED' | 'DISMISSED'>
}

type DiscussionCitation = {
  id: string
  title: string | null
  url: string | null
  source_name: string | null
  source_type: string | null
  excerpt: string | null
  event_date: string | null
  knowledge_date: string | null
}

type DiscussionExchange = {
  exchange_id: string
  conversation_id: string
  query: string
  status: 'published' | 'withheld'
  answer: string
  confidence: 'LOW' | 'MEDIUM' | 'HIGH'
  limitations: string
  citations: DiscussionCitation[]
  metric_keys: string[]
  what_if: null | {
    kind: string
    ticker: string
    amount_base: string
    base_currency: string
    portfolio_value_before: string
    portfolio_value_after: string
    current_weight: string
    projected_weight: string
    target_weight: string | null
    distance_to_target_before: string | null
    distance_to_target_after: string | null
    assumption: string
  }
  reasons: string[]
  created_at: string
  disclaimer: string
}

type AdvisorProfile = {
  instructions: string
  is_default: boolean
  prompt_version: string
  risk_profile: string | null
}

type MorningSummary = {
  status: 'ready' | 'withheld'
  summary_date: string
  valuation_as_of: string | null
  base_currency: string
  portfolio_value_base: string | null
  cash_base: string | null
  holding_count: number
  top_suggestion: RecommendationResponse['recommendations'][number] | null
  delivery_channel: 'IN_APP'
  limitations: string
}

function useMetricMetadata(enabled = true) {
  const [metadata, setMetadata] = useState<Record<string, MetricMetadata>>({})

  useEffect(() => {
    if (!enabled) return
    fetch('/api/metric_metadata')
      .then(async (response) => {
        const payload = await response.json()
        if (!response.ok) throw new Error(payload.message || 'Metric metadata unavailable.')
        setMetadata(Object.fromEntries(
          (payload.metrics as MetricMetadata[]).map((metric) => [metric.key, metric]),
        ))
      })
      .catch(() => setMetadata({}))
  }, [enabled])

  return metadata
}

function MetricLabel({ metricKey, metadata, fallback }: {
  metricKey: string
  metadata: Record<string, MetricMetadata>
  fallback: string
}) {
  const metric = metadata[metricKey]
  const description = metric?.plain_description || 'Metric definition is temporarily unavailable.'
  return <span className="metric-label" title={description}>{metric?.display_name || fallback}<CircleHelp size={12} aria-hidden="true" /></span>
}

const acknowledgments = [
  ['adult_confirmed', 'I confirm that I am 18 years of age or older.'],
  ['risk_disclosure_accepted', 'I have read and accept the risk disclosure.'],
  ['advisory_disclaimer_accepted', 'I understand Auspex is research, not financial advice.'],
  ['terms_accepted', 'I accept the terms and conditions.'],
  ['privacy_acknowledged', 'I acknowledge the privacy notice.'],
] as const

function Brand() {
  return (
    <div className="brand-lockup" aria-label="Auspex">
      <svg className="brand-mark" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M12 2.4c2.3 2.7 3.1 4.6 3.1 6.1a3.1 3.1 0 1 1-6.2 0c0-1.1.5-2.1 1.4-3.1.1.9.6 1.5 1.2 1.9-.4-1.7.1-3.4.5-4.9Z" fill="currentColor" />
        <path d="M8.4 12.3h7.2M9.4 12.6h5.2l-1.2 8.8h-2.8l-1.2-8.8Z" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <div>
        <div className="brand-name">AUS<span>P</span>EX</div>
        <div className="brand-tag">Read the signs.</div>
      </div>
    </div>
  )
}

function LoadingScreen({ message }: { message: string }) {
  return (
    <section className="loading-screen" role="status" aria-live="polite" aria-busy="true">
      <Brand />
      <div className="loading-ring" aria-hidden="true"><span /></div>
      <span className="loading-message">{message}</span>
    </section>
  )
}

function Login() {
  return (
    <main className="auth-shell">
      <div className="constellation" aria-hidden="true" />
      <section className="login-panel">
        <Brand />
        <p className="intro">Sign in with an account you already trust.<br />Auspex never stores a password.</p>
        <div className="provider-list">
          <a className="provider enabled" href="/.auth/login/aad?post_login_redirect_uri=/auth/return">
            <img className="provider-logo" src="/providers/microsoft.svg" alt="" />
            <span>Continue with Microsoft</span>
            <ArrowRight size={17} />
          </a>
          <button className="provider disabled" type="button" disabled>
            <img className="provider-logo" src="/providers/google.png" alt="" />
            <span>Continue with Google</span>
            <small>Coming soon</small>
          </button>
          <button className="provider disabled" type="button" disabled>
            <img className="provider-logo" src="/providers/github.svg" alt="" />
            <span>Continue with GitHub</span>
            <small>Coming soon</small>
          </button>
        </div>
        <a className="create-link" href="/.auth/login/aad?post_login_redirect_uri=/register">
          Create an Auspex account <ArrowRight size={14} />
        </a>
        <p className="fine-print">Research, not advice. Auspex shows you a view and suggestions. You decide and act at your bank.</p>
      </section>
    </main>
  )
}

function Registration({ principal, onSubmitted }: { principal: ClientPrincipal; onSubmitted: (user: AppUser) => void }) {
  const [accepted, setAccepted] = useState<Record<string, boolean>>({})
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const allAccepted = acknowledgments.every(([key]) => accepted[key])

  async function submit() {
    setSubmitting(true)
    setError('')
    try {
      const response = await fetch('/api/registration', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.fromEntries(acknowledgments.map(([key]) => [key, accepted[key] === true]))),
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.message || 'Registration could not be submitted.')
      onSubmitted(payload)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Registration could not be submitted.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="page-shell">
      <header><Brand /><a className="quiet-action" href="/.auth/logout?post_logout_redirect_uri=/"><LogOut size={16} /> Sign out</a></header>
      <section className="registration-layout">
        <div className="registration-copy">
          <span className="eyebrow">Create your account</span>
          <h1>One clear agreement<br />before the signals.</h1>
          <p>Your Microsoft account is authenticated. Auspex keeps a separate application profile so your portfolio, approvals, and decisions remain isolated.</p>
          <div className="identity-row"><Mail size={17} /><span>{principal.userDetails || 'Microsoft personal account'}</span></div>
        </div>
        <div className="registration-form">
          <h2>Review and acknowledge</h2>
          <p className="form-note">Each item is required. The accepted document version and UTC timestamp are retained with your account.</p>
          <div className="check-list">
            {acknowledgments.map(([key, label]) => (
              <label key={key} className={accepted[key] ? 'checked' : ''}>
                <input type="checkbox" checked={accepted[key] === true} onChange={(event) => setAccepted((current) => ({ ...current, [key]: event.target.checked }))} />
                <span className="check-box"><Check size={14} /></span>
                <span>{label}</span>
              </label>
            ))}
          </div>
          {error && <p className="error" role="alert">{error}</p>}
          <button className="primary-action" type="button" disabled={!allAccepted || submitting} onClick={submit}>
            {submitting ? 'Submitting…' : 'Request access'} <ArrowRight size={17} />
          </button>
          <p className="approval-note"><Clock3 size={15} /> Access begins only after an Auspex administrator approves the request.</p>
        </div>
      </section>
    </main>
  )
}

function RegistrationRequired() {
  return (
    <main className="state-shell">
      <div className="state-signal"><UserX size={28} /></div>
      <span className="eyebrow">No Auspex account</span>
      <h1>This Microsoft account is not registered.</h1>
      <p>Sign in is reserved for existing Auspex accounts. Start a separate account request to review the required acknowledgments and enter the approval queue.</p>
      <a className="primary-action link-button" href="/register">Create an Auspex account <ArrowRight size={17} /></a>
      <a className="quiet-action" href="/.auth/logout?post_logout_redirect_uri=/"><LogOut size={16} /> Use another Microsoft account</a>
    </main>
  )
}

type AppPage = 'home' | 'opportunities' | 'discussion' | 'account' | 'onboarding' | 'ledger' | 'admin'

function AccountHeader({ user, currentPage }: { user: AppUser; currentPage: AppPage }) {
  const identity = user.contact_email || 'Microsoft personal account'
  return (
    <header>
      <a className="brand-home" href="/" aria-label="Auspex home"><Brand /></a>
      <nav className="product-nav" aria-label="Primary navigation">
        <a href="/" aria-current={currentPage === 'home' ? 'page' : undefined}>Home</a>
        {user.onboarded && <a href="/opportunities" aria-current={currentPage === 'opportunities' ? 'page' : undefined}>Discover</a>}
        {user.onboarded && <a href="/discussion" aria-current={currentPage === 'discussion' ? 'page' : undefined}>Discussion</a>}
        <a href={user.onboarded ? '/ledger' : '/onboarding'} aria-current={currentPage === 'onboarding' || currentPage === 'ledger' ? 'page' : undefined}>{user.onboarded ? 'Ledger' : 'Set up'}</a>
      </nav>
      <div className="account-actions">
        <details className="identity-menu">
          <summary className="account-name" title="Open account menu">{identity}<ChevronDown size={14} /></summary>
          <nav className="identity-dropdown" aria-label="Account menu">
            <a href="/account" aria-current={currentPage === 'account' ? 'page' : undefined}>Account</a>
            {user.role === 'admin' && <a href="/admin" aria-current={currentPage === 'admin' ? 'page' : undefined}>Administration</a>}
            <a href="/.auth/logout?post_logout_redirect_uri=/">Sign out</a>
          </nav>
        </details>
      </div>
    </header>
  )
}

function AccessState({ user }: { user: AppUser }) {
  const views = {
    pending: { icon: Clock3, title: 'Your request is under review.', copy: 'You will be able to enter Auspex after an administrator approves your account.' },
    rejected: { icon: UserX, title: 'Your request was not approved.', copy: user.review_note || 'Contact the Auspex administrator if you believe this requires another look.' },
    suspended: { icon: ShieldCheck, title: 'Your access is suspended.', copy: user.review_note || 'Your data remains isolated, but product access is currently disabled.' },
    active: { icon: UserCheck, title: 'Your access is approved.', copy: user.onboarded ? 'Continue to Auspex.' : 'The next step is your portfolio and risk-profile onboarding.' },
  } as const
  const view = views[user.status]
  const Icon = view.icon
  const stateContent = <>
    <div className={`state-signal ${user.status}`}><Icon size={28} /></div>
    <span className="eyebrow">Account status · {user.status}</span>
    <h1>{view.title}</h1>
    <p>{view.copy}</p>
  </>

  if (user.status !== 'active') {
    return <main className="state-shell">
      {stateContent}
      <a className="quiet-action" href="/.auth/logout?post_logout_redirect_uri=/"><LogOut size={16} /> Sign out</a>
    </main>
  }

  return (
    <main className="page-shell access-page">
      <AccountHeader user={user} currentPage="account" />
      <section className="account-main">
        <div className="account-heading">
          <div><span className="eyebrow">Profile & access</span><h1>Account</h1><p>Your identity, access, and investor guardrails.</p></div>
          <a className="secondary-action" href="/">Use Auspex <ArrowRight size={16} /></a>
        </div>
        <div className="account-grid">
          <article className="account-panel">
            <span className="eyebrow">Identity</span>
            <h2>{user.contact_email || 'Microsoft personal account'}</h2>
            <dl><div><dt>Status</dt><dd>Active</dd></div><div><dt>Access</dt><dd>{user.role === 'admin' ? 'User + administrator' : 'User'}</dd></div><div><dt>Provider</dt><dd>Microsoft</dd></div></dl>
          </article>
          <article className="account-panel">
            <span className="eyebrow">Investor profile</span>
            <h2>{user.risk_profile || 'Not configured'}</h2>
            <dl><div><dt>Base currency</dt><dd>{user.base_currency}</dd></div><div><dt>Horizon</dt><dd>{user.investment_horizon === 'short' ? 'Under 1 year' : user.investment_horizon === 'long' ? '3+ years' : user.investment_horizon === '12m' ? 'About 12 months' : 'Not set'}</dd></div><div><dt>Suitability</dt><dd>{user.suitability_acknowledged_at ? 'Acknowledged' : 'Not acknowledged'}</dd></div></dl>
            <a className="text-action" href="/onboarding">{user.onboarded ? 'Update investor profile' : 'Complete investor profile'} <ArrowRight size={14} /></a>
          </article>
          <article className="account-panel">
            <span className="eyebrow">Portfolio setup</span>
            <h2>{user.onboarded ? 'Ready for entries' : 'Not started'}</h2>
            <p>{user.onboarded ? 'Add opening cash, one stock, or both. You can also leave the ledger empty and return later.' : 'Complete your investor profile before adding cash or holdings.'}</p>
            <a className="text-action" href={user.onboarded ? '/ledger' : '/onboarding'}>{user.onboarded ? 'Open ledger' : 'Start setup'} <ArrowRight size={14} /></a>
          </article>
        </div>
      </section>
    </main>
  )
}

function ProductHome({ user }: { user: AppUser }) {
  const [summary, setSummary] = useState<PortfolioHomeSummary | null>(null)
  const [recommendations, setRecommendations] = useState<RecommendationResponse | null>(null)
  const [error, setError] = useState('')
  const metadata = useMetricMetadata(user.onboarded)
  const [explanations, setExplanations] = useState<Record<string, GroundedExplanation>>({})
  const [explanationErrors, setExplanationErrors] = useState<Record<string, string>>({})
  const [explainingId, setExplainingId] = useState<string | null>(null)
  const [history, setHistory] = useState<RecommendationHistory>({
    decisions: [], events: [], current_dispositions: {},
  })
  const [dispositionErrors, setDispositionErrors] = useState<Record<string, string>>({})
  const [savingDisposition, setSavingDisposition] = useState<string | null>(null)
  const dispositionRequestIds = useRef<Record<string, string>>({})

  useEffect(() => {
    if (!user.onboarded) return
    Promise.all([
      fetch('/api/portfolio_summary'),
      fetch('/api/recommendations'),
      fetch('/api/recommendation_history'),
    ])
      .then(async ([summaryResponse, recommendationResponse, historyResponse]) => {
        const [summaryPayload, recommendationPayload, historyPayload] = await Promise.all([
          summaryResponse.json(), recommendationResponse.json(), historyResponse.json(),
        ])
        if (!summaryResponse.ok) throw new Error(summaryPayload.message || 'Portfolio could not be loaded.')
        setSummary(summaryPayload)
        setRecommendations(recommendationResponse.ok ? recommendationPayload : {
          status: 'withheld',
          as_of: summaryPayload.valuation_as_of,
          risk_profile: user.risk_profile || 'Not configured',
          base_currency: summaryPayload.base_currency,
          reasons: ['recommendation_service_unavailable'],
          policy_gates: {
            cash_buffer_pct: '0', required_cash_buffer_base: '0', available_cash_base: '0',
            financing_policy_configured: false, ready_signal_count: 0, total_signal_count: 0,
          },
          recommendations: [],
        })
        if (historyResponse.ok) setHistory(historyPayload)
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : 'Portfolio could not be loaded.'))
  }, [user.onboarded, user.risk_profile])

  const formatMoney = (value: string | null | undefined) => value === null || value === undefined
    ? 'Pending valuation'
    : new Intl.NumberFormat(undefined, { style: 'currency', currency: summary?.base_currency || user.base_currency }).format(Number(value))
  const formatScore = (holding: PortfolioHomeSummary['holdings'][number]) => {
    if (holding.opportunity_score === null) return '—'
    const score = Number(holding.opportunity_score)
    if (holding.score_coverage_status !== 'PARTIAL') {
      const step = holding.score_candidate_count ? 100 / (holding.score_candidate_count + 0.25) : 0
      return score.toFixed(step >= 1 ? 0 : 1)
    }
    const lower = Math.min(90, Math.floor(score / 10) * 10)
    return `${lower}–${lower + 10}`
  }
  const formatRecommendationScore = (recommendation: RecommendationResponse['recommendations'][number]) => {
    if (recommendation.opportunity_score === null) return '—'
    if (recommendation.coverage_status === 'PARTIAL') {
      const score = Number(recommendation.opportunity_score)
      const lower = Math.min(90, Math.floor(score / 10) * 10)
      return `${lower}–${lower + 10}`
    }
    const step = recommendation.candidate_count ? 100 / (recommendation.candidate_count + 0.25) : 0
    return Number(recommendation.opportunity_score).toFixed(step >= 1 ? 0 : 1)
  }
  const coverageNote = (reasons: string[]) => reasons
    .filter((reason) => reason.startsWith('missing:'))
    .map((reason) => reason.replace('missing:', '').replaceAll('_', ' '))
    .join(', ')
  const earnings = summary?.total_earnings_base === null || summary?.total_earnings_base === undefined
    ? null
    : Number(summary.total_earnings_base)
  const pending = summary?.status === 'pending_ingestion'
  const stale = summary?.status === 'stale'
  const cashOnly = Boolean(summary && summary.holdings.length === 0)
  const largestHolding = summary?.holdings.reduce<(PortfolioHomeSummary['holdings'][number] | null)>(
    (largest, holding) => !largest || Number(holding.weight || 0) > Number(largest.weight || 0)
      ? holding
      : largest,
    null,
  )
  const scoredHoldings = summary?.holdings.filter((holding) => holding.opportunity_score !== null) || []
  const strongestHolding = scoredHoldings.reduce<(PortfolioHomeSummary['holdings'][number] | null)>(
    (strongest, holding) => !strongest || Number(holding.opportunity_score) > Number(strongest.opportunity_score)
      ? holding
      : strongest,
    null,
  )
  const actionableRecommendations = recommendations?.recommendations.filter((row) => row.action !== 'HOLD') || []
  const holdRecommendations = recommendations?.recommendations.filter((row) => row.action === 'HOLD') || []
  const analysisText = !summary
    ? 'Portfolio analysis is loading.'
    : recommendations?.status === 'withheld'
      ? `Auspex is withholding actions until ${recommendations.reasons.map((reason) => reason.replaceAll('_', ' ')).join(' and ')}.`
      : actionableRecommendations.length
        ? `${actionableRecommendations.length} policy action${actionableRecommendations.length === 1 ? '' : 's'} currently clear coverage, portfolio limits, cash buffers, and estimated costs. The highest-priority action is ${actionableRecommendations[0].action} ${actionableRecommendations[0].ticker}.`
        : `No trade currently clears all policy, cash-buffer, and cost gates. ${holdRecommendations.length} covered holding${holdRecommendations.length === 1 ? '' : 's'} remain at HOLD.`

  async function explainRecommendation(recommendationId: string) {
    setExplainingId(recommendationId)
    setExplanationErrors((current) => ({ ...current, [recommendationId]: '' }))
    try {
      const response = await fetch(`/api/recommendations/${encodeURIComponent(recommendationId)}/explain`, {
        method: 'POST',
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.message || 'Grounded explanation could not be generated.')
      setExplanations((current) => ({ ...current, [recommendationId]: payload }))
      setHistory((current) => ({
        ...current,
        decisions: current.decisions.some((decision) => decision.decision_id === payload.decision_id)
          ? current.decisions
          : [payload, ...current.decisions],
      }))
    } catch (reason) {
      setExplanationErrors((current) => ({
        ...current,
        [recommendationId]: reason instanceof Error ? reason.message : 'Grounded explanation could not be generated.',
      }))
    } finally {
      setExplainingId(null)
    }
  }

  async function recordDisposition(
    recommendationId: string,
    disposition: 'ACCEPTED' | 'DISMISSED',
  ) {
    const requestKey = `${recommendationId}:${disposition}`
    const clientRequestId = dispositionRequestIds.current[requestKey] || crypto.randomUUID()
    dispositionRequestIds.current[requestKey] = clientRequestId
    setSavingDisposition(requestKey)
    setDispositionErrors((current) => ({ ...current, [recommendationId]: '' }))
    try {
      const response = await fetch(`/api/recommendations/${encodeURIComponent(recommendationId)}/disposition`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_request_id: clientRequestId, disposition }),
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.message || 'Suggestion response could not be saved.')
      setHistory((current) => ({
        ...current,
        events: current.events.some((event) => event.event_id === payload.event.event_id)
          ? current.events
          : [payload.event, ...current.events],
        current_dispositions: {
          ...current.current_dispositions,
          [recommendationId]: disposition,
        },
      }))
    } catch (reason) {
      setDispositionErrors((current) => ({
        ...current,
        [recommendationId]: reason instanceof Error ? reason.message : 'Suggestion response could not be saved.',
      }))
    } finally {
      setSavingDisposition(null)
    }
  }

  return (
    <main className="product-page">
      <AccountHeader user={user} currentPage="home" />
      <section className="product-main portfolio-home">
        {!user.onboarded ? <section className="home-empty">
          <span className="eyebrow">Portfolio setup</span>
          <h1>Your portfolio starts with its guardrails.</h1>
          <p>Choose a base currency, horizon, and risk profile before entering cash or holdings.</p>
          <a className="primary-action link-button" href="/onboarding">Start setup <ArrowRight size={16} /></a>
        </section> : !summary && !error ? <LoadingScreen message="Valuing your portfolio…" /> : error ? <section className="home-empty"><span className="eyebrow">Portfolio unavailable</span><h1>We could not load your portfolio.</h1><p>{error}</p><a className="secondary-action" href="/ledger">Open ledger</a></section> : summary?.status === 'empty' ? <section className="home-empty">
          <WalletCards size={27} />
          <span className="eyebrow">No portfolio entries</span>
          <h1>Add your first ledger entry.</h1>
          <p>Start with cash, one stock, both, or nothing more than you know today.</p>
          <a className="primary-action link-button" href="/ledger">Open ledger <ArrowRight size={16} /></a>
        </section> : <>
          <section className="portfolio-value-hero">
            <span className="eyebrow" aria-label="Portfolio value · cash + stocks"><MetricLabel metricKey="portfolio_value" metadata={metadata} fallback="Portfolio value" /> · cash + stocks</span>
            <h1>{formatMoney(summary?.total_value_base)}</h1>
            <p>{pending ? 'Auspex will show a total only when every holding has a current price and required FX rate.' : `${summary?.holdings.length || 0} positions plus cash in ${summary?.base_currency}.`}</p>
          </section>
          <section className={`coverage-strip ${pending || stale ? 'attention' : ''}`} aria-label="Coverage and freshness">
            <div className="coverage-status"><span className={`pulse ${pending || stale ? 'pending' : ''}`} /><div><span className="eyebrow">Coverage & freshness</span><strong>{pending ? 'Valuation waiting for market coverage' : stale ? 'Coverage complete · sources are stale' : 'Portfolio coverage complete'}</strong></div></div>
            <dl><div><dt>Valued through</dt><dd>{summary?.valuation_as_of || 'Unavailable'}</dd></div><div><dt>Oldest price</dt><dd>{summary?.coverage.oldest_price_date || 'Unavailable'}</dd></div><div><dt>Missing prices</dt><dd>{summary?.coverage.missing_prices.length ? summary.coverage.missing_prices.join(', ') : 'None'}</dd></div><div><dt>Missing current FX</dt><dd>{summary?.coverage.missing_fx.length ? summary.coverage.missing_fx.join(', ') : 'None'}</dd></div></dl>
          </section>
          <div className="portfolio-stat-grid">
            <article><MetricLabel metricKey="net_contributed_capital" metadata={metadata} fallback="Net contributed capital" /><strong>{formatMoney(summary?.net_contributed_capital_base)}</strong><small>{summary?.coverage.missing_capital_fx.length ? `Missing historical FX: ${summary.coverage.missing_capital_fx.join(', ')}` : 'Opening capital + deposits − withdrawals'}</small></article>
            <article><MetricLabel metricKey="total_gain_loss" metadata={metadata} fallback="Total gain / loss" /><strong className={earnings !== null && earnings < 0 ? 'negative' : earnings !== null && earnings > 0 ? 'positive' : ''}>{formatMoney(summary?.total_earnings_base)}</strong><small>{summary?.coverage.missing_capital_fx.length ? `Missing historical FX: ${summary.coverage.missing_capital_fx.join(', ')}` : 'Total value − net contributed capital'}</small></article>
            <article><MetricLabel metricKey="cash_available" metadata={metadata} fallback="Cash available" /><strong>{formatMoney(summary?.total_cash_base)}</strong><small>{summary?.cash_weight ? `${(Number(summary.cash_weight) * 100).toFixed(1)}% of portfolio` : 'Cash derived from the ledger'}</small></article>
            <article><MetricLabel metricKey="stocks_value" metadata={metadata} fallback="Stocks value" /><strong>{cashOnly ? 'No stock positions' : formatMoney(summary?.total_stocks_base)}</strong><small>{cashOnly ? 'Add a holding from the ledger when ready' : `Latest covered closes in ${summary?.base_currency}`}</small></article>
          </div>
          {summary?.capital_breakdown_base && <section className="home-panel capital-reconciliation"><div className="panel-title"><div><h2>Capital and earnings reconciliation</h2><p>Trade-date capital and current market value, shown separately.</p></div></div><dl><div><dt>Opening positions</dt><dd>{formatMoney(summary.capital_breakdown_base.opening_positions)}</dd></div><div><dt>External cash</dt><dd>{formatMoney(summary.capital_breakdown_base.external_cash)}</dd></div><div><dt>Historical acquisition costs</dt><dd>{formatMoney(summary.capital_breakdown_base.historical_acquisition_costs)}</dd></div><div><dt>Current position cost basis</dt><dd>{formatMoney(summary.current_position_cost_basis_base)}</dd></div><div><dt>Unrealized gain / loss</dt><dd>{formatMoney(summary.unrealized_gain_base)}</dd></div><div><dt>Other earnings, fees and cash FX</dt><dd>{formatMoney(summary.other_earnings_base)}</dd></div></dl></section>}
          <section className="home-panel holdings-panel">
            <div className="panel-title"><div><h2>Holdings</h2><p>{summary?.holdings.length || 0} current positions · seven latest sessions</p></div><a className="text-action" href="/ledger">Open ledger <ArrowRight size={14} /></a></div>
            <div className="table-scroll">
              <table className="holdings-table analytical-holdings">
                <thead><tr><th scope="col">Security</th><th scope="col">7 sessions</th><th scope="col"><MetricLabel metricKey="position_quantity" metadata={metadata} fallback="Quantity" /></th><th scope="col"><MetricLabel metricKey="latest_price" metadata={metadata} fallback="Last price" /></th><th scope="col"><MetricLabel metricKey="stocks_value" metadata={metadata} fallback="Stock value" /></th><th scope="col"><MetricLabel metricKey="opportunity_score" metadata={metadata} fallback="Auspex score" /></th></tr></thead>
                <tbody>{summary?.holdings.map((holding) => <tr key={`${holding.security_sk}-${holding.ticker}`}>
                  <th scope="row"><strong>{holding.ticker}</strong><small>{holding.company_name}</small><small>{holding.exchange || 'Exchange unavailable'} · {(holding.theme_id || 'Unclassified').replaceAll('_', ' ')}{holding.theme_provenance ? ` · ${holding.theme_provenance} classification` : ''}</small></th>
                  <td><PriceSparkline points={holding.price_history} ticker={holding.ticker} /></td>
                  <td>{holding.quantity}<small>Avg {holding.average_acquisition_price ? `${holding.currency} ${holding.average_acquisition_price}` : 'unavailable'}</small></td>
                  <td>{holding.latest_price ? `${holding.price_currency || holding.currency} ${holding.latest_price}` : 'Pending'}<small>{holding.price_as_of || 'Date unavailable'}</small></td>
                  <td>{formatMoney(holding.market_value_base)}<small className={Number(holding.gain_loss_pct) > 0 ? 'positive' : Number(holding.gain_loss_pct) < 0 ? 'negative' : ''}>{holding.gain_loss_pct === null ? 'Return unavailable' : `${Number(holding.gain_loss_pct) >= 0 ? '+' : ''}${Number(holding.gain_loss_pct).toFixed(1)}% vs acquisition`}</small></td>
                  <td><strong className="holding-score">{formatScore(holding)}</strong><small>{holding.score_coverage_status ? `${holding.score_coverage_status.toLowerCase()}${holding.score_coverage_status === 'PARTIAL' ? ' relative band' : ''} · ${holding.score_as_of}` : 'Score unavailable'}</small>{holding.score_coverage_status === 'PARTIAL' && <small>Ready when available: {coverageNote(holding.score_coverage_reasons) || 'named missing legs'}</small>}</td>
                </tr>)}</tbody>
              </table>
            </div>
          </section>
          <section className="home-panel exposure-panel">
            <div className="panel-title"><div><h2>Portfolio exposure</h2><p>Stocks and cash as a share of total value</p></div></div>
            <div className="exposure-groups four-up">
              {(['theme', 'exchange', 'country', 'currency'] as const).map((type) => <div className="exposure-group" key={type}><h3>{type}</h3>{summary?.exposures[type].map((exposure) => <div className="exposure-row" key={`${type}-${exposure.name}`}><div><span>{exposure.name.replaceAll('_', ' ')}</span><strong title={metadata.position_weight?.plain_description}>{(Number(exposure.weight) * 100).toFixed(1)}%</strong></div><span className="exposure-track"><i style={{ width: `${Math.min(100, Number(exposure.weight) * 100)}%` }} /></span></div>)}</div>)}
            </div>
          </section>
          <section className="home-panel monthly-outlook-panel"><div className="panel-title"><div><span className="eyebrow">Latest deterministic review</span><h2>Current portfolio analysis</h2><p>Auspex policy applied to the latest portfolio, scores, coverage, and estimated trading costs.</p></div><span className="research-label">As of {recommendations?.as_of || summary?.valuation_as_of || 'unavailable'}</span></div><dl><div><dt>Portfolio mix</dt><dd>{summary?.holdings.length || 0} stocks · {summary?.cash_weight ? `${(Number(summary.cash_weight) * 100).toFixed(1)}% cash` : 'cash weight unavailable'}</dd></div><div><dt>Largest concentration</dt><dd>{largestHolding ? `${largestHolding.ticker} · ${(Number(largestHolding.weight || 0) * 100).toFixed(1)}%` : 'No stock concentration'}</dd></div><div><dt>Strongest holding signal</dt><dd>{strongestHolding ? `${strongestHolding.ticker} · ${formatScore(strongestHolding)}` : 'No scored holding'}</dd></div><div><dt>Score coverage</dt><dd>{scoredHoldings.length} of {summary?.holdings.length || 0} holdings</dd></div></dl><p className="portfolio-analysis-copy">{analysisText}</p></section>
          <section className="home-panel recommendations-panel">
            <div className="panel-title"><div><span className="eyebrow">Deterministic policy · {user.risk_profile}</span><h2>Suggested actions</h2><p>{recommendations?.as_of ? `Signals through ${recommendations.as_of}` : 'Waiting for complete inputs'}</p></div><span className="research-label">Research only</span></div>
            {!recommendations ? <p className="recommendation-state">Loading recommendations…</p> : recommendations.status === 'withheld' ? <p className="recommendation-state">Recommendations are withheld until {recommendations.reasons.map((reason) => reason.replaceAll('_', ' ')).join(' and ')}.</p> : recommendations.recommendations.length === 0 ? <p className="recommendation-state">No trade currently clears portfolio limits, cash buffers, coverage, and estimated costs.</p> : <div className="recommendation-list">
              {recommendations.recommendations.slice(0, 12).map((recommendation) => <article className={`recommendation-card action-${recommendation.action.toLowerCase()}`} key={recommendation.recommendation_id} aria-labelledby={`recommendation-${recommendation.recommendation_id}`}>
                <dl className="recommendation-card-head">
                  <div className="recommendation-security"><dt>Security</dt><dd><strong id={`recommendation-${recommendation.recommendation_id}`}>{recommendation.ticker}</strong><small>{(recommendation.theme_id || 'Unclassified').replaceAll('_', ' ')} · {recommendation.confidence.toLowerCase()} confidence</small></dd></div>
                  <div><dt>Auspex score</dt><dd><strong>{formatRecommendationScore(recommendation)}</strong><small>{recommendation.coverage_status.toLowerCase()} coverage{recommendation.candidate_count ? ` · ${recommendation.candidate_count} peers` : ''}</small></dd></div>
                  <div><dt>Action</dt><dd><b className={`recommendation-action action-${recommendation.action.toLowerCase()}`}>{recommendation.action}</b><small>{recommendation.suppression_reasons.length ? recommendation.suppression_reasons.join(', ').replaceAll('_', ' ') : 'Policy eligible'}</small></dd></div>
                  <div><dt>Current → target</dt><dd><strong>{(Number(recommendation.current_weight) * 100).toFixed(1)}% → {(Number(recommendation.target_weight) * 100).toFixed(1)}%</strong><small>Portfolio weight</small></dd></div>
                  <div><dt>Suggested amount</dt><dd><strong>{new Intl.NumberFormat(undefined, { style: 'currency', currency: recommendations.base_currency }).format(Number(recommendation.suggested_amount_base))}</strong><small>Est. cost {new Intl.NumberFormat(undefined, { style: 'currency', currency: recommendations.base_currency }).format(Number(recommendation.estimated_cost_base))}</small></dd></div>
                </dl>
                <p className="recommendation-rationale">{recommendation.rationale}</p>
                <details className="score-attribution"><summary>Score details · six deterministic legs</summary><ol>{recommendation.attribution.map((leg) => <li key={leg.key}><span><MetricLabel metricKey={leg.key} metadata={metadata} fallback={leg.key.replaceAll('_', ' ')} /></span><strong className={leg.direction === 'RAISED' ? 'positive' : leg.direction === 'LOWERED' ? 'negative' : ''}>{leg.contribution === null ? 'Unavailable' : `${Number(leg.contribution) >= 0 ? '+' : ''}${Number(leg.contribution).toFixed(2)}`}</strong><small>{leg.direction.toLowerCase()}</small></li>)}</ol>{recommendation.coverage_reasons.length > 0 && <p>Coverage notes: {recommendation.coverage_reasons.join(', ').replaceAll('_', ' ')}</p>}</details>
                <footer className="recommendation-card-actions" aria-label={`${recommendation.ticker} suggestion response`}>
                  <button className="secondary-action compact" type="button" disabled={savingDisposition !== null || history.current_dispositions[recommendation.recommendation_id] === 'ACCEPTED'} onClick={() => recordDisposition(recommendation.recommendation_id, 'ACCEPTED')}><ThumbsUp size={14} />Accept</button>
                  <button className="secondary-action compact" type="button" disabled={savingDisposition !== null || history.current_dispositions[recommendation.recommendation_id] === 'DISMISSED'} onClick={() => recordDisposition(recommendation.recommendation_id, 'DISMISSED')}><ThumbsDown size={14} />Dismiss</button>
                  <button className="secondary-action compact" type="button" disabled={explainingId === recommendation.recommendation_id || Boolean(explanations[recommendation.recommendation_id])} onClick={() => explainRecommendation(recommendation.recommendation_id)}><BookOpenText size={14} />{explainingId === recommendation.recommendation_id ? 'Checking…' : explanations[recommendation.recommendation_id] ? 'Evidence checked' : 'Explain with evidence'}</button>
                  {history.current_dispositions[recommendation.recommendation_id] && <span className="disposition-status">{history.current_dispositions[recommendation.recommendation_id].toLowerCase()}</span>}
                  <small>Records your decision only; no trade is placed.</small>
                </footer>
                {dispositionErrors[recommendation.recommendation_id] && <p className="error" role="alert">{dispositionErrors[recommendation.recommendation_id]}</p>}
                {explanationErrors[recommendation.recommendation_id] && <p className="error" role="alert">{explanationErrors[recommendation.recommendation_id]}</p>}
                {explanations[recommendation.recommendation_id]?.status === 'withheld' && <p className="grounded-withheld">Explanation withheld: {explanations[recommendation.recommendation_id].reasons.map((reason) => reason.replaceAll('_', ' ')).join(', ')}.</p>}
                {explanations[recommendation.recommendation_id]?.status === 'published' && <div className="grounded-output" data-ai-generated="true"><span className="ai-output-label">AI-generated explanation</span><p>{explanations[recommendation.recommendation_id].output.explanation}</p><small>{explanations[recommendation.recommendation_id].output.uncertainty}</small><div className="evidence-list">{explanations[recommendation.recommendation_id].citations.map((citation) => <details className="evidence-item" key={citation.id}><summary>{citation.title || citation.source_name || 'Source evidence'}<small>{citation.knowledge_date ? `Known ${citation.knowledge_date}` : 'Knowledge date unavailable'}</small></summary>{citation.excerpt && <p>{citation.excerpt}</p>}<dl><div><dt>Source</dt><dd>{citation.source_name || citation.source_type || 'Unavailable'}</dd></div><div><dt>Event date</dt><dd>{citation.event_date || 'Unavailable'}</dd></div><div><dt>Knowledge date</dt><dd>{citation.knowledge_date || 'Unavailable'}</dd></div><div><dt>Content</dt><dd>{citation.content_status?.replaceAll('_', ' ') || 'Unavailable'}</dd></div></dl>{citation.url && <a href={citation.url} target="_blank" rel="noreferrer">Open original source <ArrowRight size={12} /></a>}</details>)}</div></div>}
              </article>)}
            </div>}
            {recommendations && recommendations.status !== 'withheld' && <dl className="policy-gates"><div><dt>Ready signals</dt><dd>{recommendations.policy_gates.ready_signal_count} / {recommendations.policy_gates.total_signal_count}</dd></div><div><dt>Cash above {(Number(recommendations.policy_gates.cash_buffer_pct) * 100).toFixed(0)}% buffer</dt><dd>{new Intl.NumberFormat(undefined, { style: 'currency', currency: recommendations.base_currency }).format(Number(recommendations.policy_gates.available_cash_base))}</dd></div><div><dt>Financing gate</dt><dd>{recommendations.policy_gates.financing_policy_configured ? 'Configured' : 'Fail-closed pending calibration'}</dd></div></dl>}
            <p className="recommendation-disclaimer">{recommendations?.disclaimer || 'Research only; not financial or tax advice. You decide and execute.'}</p>
          </section>
          {(history.events.length > 0 || history.decisions.length > 0) && <section className="home-panel history-panel"><div className="panel-title"><div><h2>Decision history</h2><p>Immutable explanations and your recorded suggestion responses.</p></div></div><ol className="decision-history">{history.events.map((event) => <li key={event.event_id}><span className={`history-state ${event.disposition.toLowerCase()}`}>{event.disposition.toLowerCase()}</span><div><strong>{event.ticker} · {event.action}</strong><p>Suggestion response recorded. No trade was placed.</p></div><time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString()}</time></li>)}{history.decisions.map((decision) => <li key={decision.decision_id}><span className={`history-state ${decision.status}`}>{decision.status}</span><div><strong>{decision.ticker} · grounded explanation</strong><p>{decision.status === 'published' ? 'Evidence-validated explanation recorded.' : `Withheld: ${decision.reasons.join(', ').replaceAll('_', ' ')}`}</p></div><time dateTime={decision.created_at}>{new Date(decision.created_at).toLocaleString()}</time></li>)}</ol></section>}
        </>}
      </section>
    </main>
  )
}

const riskBands = [
  ['Conservative', 'Protect capital first with smaller positions and more cash.'],
  ['Balanced', 'Pursue steady growth with measured risk and diversification.'],
  ['Growth', 'Lean into return with larger positions and a smaller cash buffer.'],
  ['Aggressive', 'Accept larger swings and losses in pursuit of maximum return.'],
] as const

function Onboarding({ user, onComplete }: { user: AppUser; onComplete: (user: AppUser) => void }) {
  const [riskProfile, setRiskProfile] = useState(user.risk_profile || 'Balanced')
  const [baseCurrency, setBaseCurrency] = useState(user.base_currency || 'USD')
  const [investmentHorizon, setInvestmentHorizon] = useState(user.investment_horizon || '12m')
  const [acknowledged, setAcknowledged] = useState(false)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    try {
      const response = await fetch('/api/onboarding', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          risk_profile: riskProfile,
          base_currency: baseCurrency,
          investment_horizon: investmentHorizon,
          suitability_acknowledged: acknowledged,
        }),
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.message || 'Onboarding could not be saved.')
      window.history.replaceState({}, '', '/ledger')
      onComplete(payload)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Onboarding could not be saved.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="product-page">
      <AccountHeader user={user} currentPage="onboarding" />
      <section className="onboarding-main">
        <div className="onboarding-intro">
          <span className="eyebrow">Investor profile</span>
          <h1>Set the guardrails first.</h1>
          <p>Your profile controls position caps, cash buffer, and how strongly Auspex weighs downside. You can update it later from Account.</p>
        </div>
        <form className="onboarding-form" onSubmit={submit}>
          <fieldset>
            <legend>Risk profile</legend>
            <div className="risk-bands">
              {riskBands.map(([band, description]) => <label className={riskProfile === band ? 'selected' : ''} key={band}>
                <input type="radio" name="risk-profile" value={band} checked={riskProfile === band} onChange={() => setRiskProfile(band)} />
                <strong>{band}</strong><span>{description}</span>
              </label>)}
            </div>
          </fieldset>
          <div className="onboarding-fields">
            <label><span>Base currency</span><select value={baseCurrency} onChange={(event) => setBaseCurrency(event.target.value)}><option>USD</option><option>CHF</option><option>EUR</option></select></label>
            <label><span>Investment horizon</span><select value={investmentHorizon} onChange={(event) => setInvestmentHorizon(event.target.value)}><option value="short">Short · under 1 year</option><option value="12m">About 12 months</option><option value="long">Long · 3+ years</option></select></label>
          </div>
          <label className="suitability-check"><input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} /><span>I understand this profile shapes advisory suggestions and does not guarantee returns or prevent losses.</span></label>
          {error && <p className="error" role="alert">{error}</p>}
          <button className="primary-action" disabled={!acknowledged || submitting}>{submitting ? 'Saving…' : 'Save and add portfolio'} <ArrowRight size={17} /></button>
        </form>
      </section>
    </main>
  )
}

const securityTransactionTypes = new Set(['OPENING_POSITION', 'BUY', 'SELL'])
const securityReferenceTypes = new Set([...securityTransactionTypes, 'DIVIDEND'])
const heldSecurityTypes = new Set(['SELL', 'DIVIDEND'])
const fxCapitalTypes = new Set(['OPENING_CASH', 'OPENING_POSITION', 'DEPOSIT', 'WITHDRAWAL'])
const linkedCostTypes = new Set(['OPENING_POSITION', 'BUY', 'SELL', 'DIVIDEND', 'FEE'])
const costCategories = [
  ['BROKER_COMMISSION', 'Broker commission'],
  ['TRANSACTION_TAX', 'Transaction / stamp tax'],
  ['WITHHOLDING_TAX', 'Withholding tax'],
  ['VAT', 'VAT'],
  ['CUSTODY_FEE', 'Custody fee'],
  ['ACCOUNT_FEE', 'Account fee'],
  ['OTHER_FEE', 'Other fee'],
]

function TransactionsPage({ user }: { user: AppUser }) {
  const metadata = useMetricMetadata(true)
  const [transactions, setTransactions] = useState<PortfolioTransaction[]>([])
  const [summary, setSummary] = useState<PortfolioSummary>({
    cash_by_currency: {},
    positions: [],
    transaction_count: 0,
    updated_on: null,
    net_contributed_capital_by_currency: {},
    total_fees_by_currency: {},
    dividends_by_currency: {},
    interest_by_currency: {},
    reporting_currency: 'USD',
    cash_total: null,
    net_contributed_capital_total: null,
    capital_breakdown_base: null,
    current_position_cost_basis_base: null,
    unrealized_gain_base: null,
    other_earnings_base: null,
    total_fees_total: null,
    dividends_total: null,
    interest_total: null,
    currency_exposure: [],
    coverage: { missing_prices: [], missing_fx: [], missing_capital_fx: [], oldest_price_date: null },
    assets: [],
    allocation: {
      cash_value: null,
      stocks_value: null,
      cash_weight: null,
      stocks_weight: null,
      complete: false,
      reason: 'incomplete_coverage',
    },
    total_value: {
      status: 'pending_market_valuation',
      value_by_currency: null,
      reason: 'Total value requires market prices and FX valuation.',
    },
    earnings: {
      status: 'pending_market_valuation',
      value_by_currency: null,
      reason: 'Current earnings requires market prices and FX valuation.',
    },
  })
  const [transactionType, setTransactionType] = useState('DEPOSIT')
  const [loadError, setLoadError] = useState('')
  const [ledgerLoaded, setLedgerLoaded] = useState(false)
  const [formError, setFormError] = useState('')
  const [saving, setSaving] = useState(false)
  const [transactionOpen, setTransactionOpen] = useState(false)
  const [correctionTarget, setCorrectionTarget] = useState<PortfolioTransaction | null>(null)
  const [securityCode, setSecurityCode] = useState('')
  const [resolvedSecurity, setResolvedSecurity] = useState<(SecurityOption & { query: string }) | null>(null)
  const [securityOptions, setSecurityOptions] = useState<SecurityOption[]>([])
  const [securityLookupError, setSecurityLookupError] = useState('')
  const [transactionCurrency, setTransactionCurrency] = useState(user.base_currency)
  const [feeCategory, setFeeCategory] = useState('OTHER_FEE')
  const [costComponents, setCostComponents] = useState<CostComponentDraft[]>([])
  const [draftRequestId, setDraftRequestId] = useState(() => crypto.randomUUID())
  const today = new Date().toISOString().slice(0, 10)

  async function load() {
    const [transactionsResponse, summaryResponse] = await Promise.all([
      fetch('/api/transactions'),
      fetch('/api/transaction_summary'),
    ])
    const transactionsPayload = await transactionsResponse.json()
    const summaryPayload = await summaryResponse.json()
    if (!transactionsResponse.ok) throw new Error(transactionsPayload.message || 'Transactions could not be loaded.')
    if (!summaryResponse.ok) throw new Error(summaryPayload.message || 'Portfolio summary could not be loaded.')
    setTransactions(transactionsPayload)
    setSummary(summaryPayload)
    setLoadError('')
    setLedgerLoaded(true)
  }

  useEffect(() => { load().catch((reason) => setLoadError(reason.message)) }, [])

  useEffect(() => {
    if (!transactionOpen) return
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') setTransactionOpen(false)
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [transactionOpen])

  useEffect(() => {
    const normalizedSecurityCode = securityCode.trim().toUpperCase()
    if (!securityReferenceTypes.has(transactionType) || normalizedSecurityCode.length < 1) {
      setResolvedSecurity(null)
      setSecurityOptions([])
      setSecurityLookupError('')
      return
    }
    if (resolvedSecurity?.query === normalizedSecurityCode) {
      setSecurityOptions([])
      setSecurityLookupError('')
      return
    }
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      const query = securityCode.trim().toUpperCase()
      const endpoint = heldSecurityTypes.has(transactionType) || /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(query)
        ? `/api/stock/${encodeURIComponent(query)}/lookup`
        : `/api/stock/search?q=${encodeURIComponent(query)}`
      fetch(endpoint, { signal: controller.signal })
        .then(async (response) => {
          const payload = await response.json()
          if (!response.ok) throw new Error(payload.message || 'Security was not found.')
          if (Array.isArray(payload)) {
            const exact = payload.find((security: SecurityOption) => security.ticker === query)
            setSecurityOptions(exact ? [] : payload)
            setResolvedSecurity(exact ? { ...exact, query } : null)
            if (exact) setTransactionCurrency(correctionTarget?.currency || exact.currency)
          } else {
            setSecurityOptions([])
            setResolvedSecurity({ ...payload, query })
            setTransactionCurrency(correctionTarget?.currency || payload.currency)
          }
          setSecurityLookupError('')
        })
        .catch((reason) => {
          if (reason instanceof DOMException && reason.name === 'AbortError') return
          setResolvedSecurity(null)
          setSecurityOptions([])
          setSecurityLookupError(reason instanceof Error ? reason.message : 'Security was not found.')
        })
    }, 300)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [securityCode, transactionType, correctionTarget, resolvedSecurity])

  function openTransaction(type = 'DEPOSIT') {
    setCorrectionTarget(null)
    setTransactionType(type)
    setSecurityCode('')
    setResolvedSecurity(null)
    setSecurityOptions([])
    setSecurityLookupError('')
    setTransactionCurrency(user.base_currency)
    setFeeCategory('OTHER_FEE')
    setCostComponents([])
    setDraftRequestId(crypto.randomUUID())
    setFormError('')
    setTransactionOpen(true)
  }

  function openCorrection(transaction: PortfolioTransaction) {
    setCorrectionTarget(transaction)
    setTransactionType(transaction.transaction_type)
    setSecurityCode(transaction.security_code || '')
    setResolvedSecurity(transaction.security_sk === null || !transaction.security_code ? null : {
      security_sk: transaction.security_sk,
      ticker: transaction.security_code,
      isin: transaction.security_isin,
      company_name: transaction.security_name || transaction.security_code,
      currency: transaction.security_currency || transaction.currency,
      exchange: transaction.security_exchange,
      query: transaction.security_code,
    })
    setSecurityOptions([])
    setSecurityLookupError('')
    setTransactionCurrency(transaction.currency)
    setFeeCategory(transaction.cost_category || 'OTHER_FEE')
    const linkedCosts = transactions.filter((row) => row.linked_transaction_id === transaction.transaction_id)
    setCostComponents(linkedCosts.map((row) => ({
      id: crypto.randomUUID(),
      category: row.cost_category || 'OTHER_FEE',
      amount: row.source_amount || row.gross_amount || Math.abs(Number(row.cash_amount)).toFixed(2),
      currency: row.source_currency || row.currency,
      fxRateToSettlement: row.fx_rate_to_settlement || '',
    })))
    if (linkedCosts.length === 0 && Number(transaction.fees) > 0) {
      setCostComponents([{
        id: crypto.randomUUID(),
        category: 'OTHER_FEE',
        amount: transaction.fees,
        currency: transaction.currency,
        fxRateToSettlement: '',
      }])
    }
    setDraftRequestId(crypto.randomUUID())
    setFormError('')
    setTransactionOpen(true)
  }

  function correctedAmount(transaction: PortfolioTransaction) {
    if (transaction.transaction_type === 'DIVIDEND') {
      return transaction.source_amount || transaction.gross_amount || transaction.cash_amount
    }
    if (transaction.transaction_type === 'FEE') {
      return transaction.gross_amount || Math.abs(Number(transaction.cash_amount)).toFixed(2)
    }
    const cash = Math.abs(Number(transaction.cash_amount))
    const fees = Number(transaction.fees)
    return ['OPENING_CASH', 'DEPOSIT', 'DIVIDEND', 'INTEREST'].includes(transaction.transaction_type)
      ? (cash + fees).toFixed(2)
      : Math.max(0, cash - fees).toFixed(2)
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget
    setSaving(true)
    setFormError('')
    const form = new FormData(event.currentTarget)
    const payload: Record<string, unknown> = {
      client_request_id: draftRequestId,
      transaction_type: transactionType,
      event_date: String(form.get('event_date')),
      account_id: 'primary',
      currency: securityReferenceTypes.has(transactionType) && resolvedSecurity ? resolvedSecurity.currency : String(form.get('currency')),
      fees: '0',
    }
    if (securityReferenceTypes.has(transactionType)) {
      payload.security_code = resolvedSecurity?.ticker || String(form.get('security_code'))
      payload.settlement_currency = String(form.get('currency'))
    }
    if (securityTransactionTypes.has(transactionType)) {
      payload.quantity = String(form.get('quantity'))
      payload.price = String(form.get('price'))
    } else {
      payload.amount = String(form.get('amount'))
    }
    if (form.get('fx_rate_to_base')) payload.fx_rate_to_base = String(form.get('fx_rate_to_base'))
    if (form.get('fx_rate_to_settlement')) payload.fx_rate_to_settlement = String(form.get('fx_rate_to_settlement'))
    if (transactionType === 'FEE') payload.cost_category = feeCategory
    if (costComponents.length > 0) {
      payload.cost_components = costComponents.map((component) => ({
        category: component.category,
        amount: component.amount,
        currency: component.currency,
        ...(component.fxRateToSettlement ? { fx_rate_to_settlement: component.fxRateToSettlement } : {}),
      }))
    }
    try {
      const endpoint = correctionTarget
        ? `/api/transactions/${encodeURIComponent(correctionTarget.transaction_id)}/correct`
        : '/api/transactions'
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const responsePayload = await response.json()
      if (!response.ok) throw new Error(responsePayload.message || 'Transaction could not be saved.')
      formElement.reset()
      setTransactionType('DEPOSIT')
      setSecurityCode('')
      setResolvedSecurity(null)
      setSecurityOptions([])
      setTransactionOpen(false)
      setCorrectionTarget(null)
      setDraftRequestId(crypto.randomUUID())
      try {
        await load()
      } catch (reason) {
        setLoadError(reason instanceof Error ? `Transaction saved, but refresh failed: ${reason.message}` : 'Transaction saved, but refresh failed.')
      }
    } catch (reason) {
      setFormError(reason instanceof Error ? reason.message : 'Transaction could not be saved.')
    } finally {
      setSaving(false)
    }
  }

  const securityTransaction = securityTransactionTypes.has(transactionType)
  const securityReference = securityReferenceTypes.has(transactionType)
  const heldSecurity = heldSecurityTypes.has(transactionType)
  const selectedHolding = summary.positions.find((position) => position.security_code === securityCode)
  const availableCash = summary.cash_by_currency[transactionCurrency] || '0.00'
  const showFxRate = fxCapitalTypes.has(transactionType)
    && transactionCurrency !== user.base_currency
    && !(securityReference && resolvedSecurity?.currency === user.base_currency)
  const showSettlementFx = securityReference && resolvedSecurity && transactionCurrency !== resolvedSecurity.currency
  function addCostComponent() {
    if (costComponents.length >= 20) return
    setCostComponents((current) => [...current, {
      id: crypto.randomUUID(),
      category: transactionType === 'DIVIDEND' ? 'WITHHOLDING_TAX' : transactionType === 'FEE' ? 'VAT' : 'BROKER_COMMISSION',
      amount: '',
      currency: resolvedSecurity?.currency || transactionCurrency,
      fxRateToSettlement: '',
    }])
  }
  function updateCostComponent(id: string, values: Partial<CostComponentDraft>) {
    setCostComponents((current) => current.map((component) => component.id === id ? { ...component, ...values } : component))
  }
  function chooseSecurity(security: SecurityOption) {
    setSecurityCode(security.ticker)
    setResolvedSecurity({ ...security, query: security.ticker })
    setSecurityOptions([])
    setSecurityLookupError('')
    setTransactionCurrency(security.currency)
  }
  const formatUsd = (value: string | null, empty: string) => value === null
    ? empty
    : new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(Number(value))
  const cashPercent = summary.allocation.cash_weight === null ? null : Number(summary.allocation.cash_weight) * 100
  const stocksPercent = summary.allocation.stocks_weight === null ? null : Number(summary.allocation.stocks_weight) * 100
  const formatUpdatedOn = (value: string | null) => value
    ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(`${value}T00:00:00Z`))
    : 'not available'
  return (
    <main className="product-page">
      <AccountHeader user={user} currentPage="ledger" />
      <section className="transactions-main">
        <div className="transactions-heading"><h1>Ledger</h1><div className="ledger-heading-meta"><span>Updated {formatUpdatedOn(summary.updated_on)}</span><span className="transaction-count" title={metadata.transaction_count?.plain_description}>{summary.transaction_count} entries</span></div></div>
        <div className="ledger-metrics">
          <article className={`ledger-total ${summary.total_value.status === 'pending_market_valuation' ? 'valuation-pending' : ''}`}><MetricLabel metricKey="portfolio_value" metadata={metadata} fallback={summary.total_value.status === 'pending_market_valuation' ? 'Valued subtotal' : 'Total ledger value'} /><strong>{ledgerLoaded ? summary.total_value.value_by_currency ? formatUsd(summary.total_value.value_by_currency.USD, 'Pending valuation') : 'Pending valuation' : 'Loading…'}</strong><small>{summary.total_value.status === 'pending_market_valuation' ? summary.total_value.reason : summary.total_value.status === 'stale' ? 'Last complete valuation; market prices are stale' : 'Cash + current stock values; income and fees already flow through cash'}</small></article>
          <article><MetricLabel metricKey="cash_available" metadata={metadata} fallback="Current cash" /><strong>{formatUsd(summary.cash_total, 'Pending valuation')}</strong><small>All cash balances converted to USD</small></article>
          <article><MetricLabel metricKey="net_contributed_capital" metadata={metadata} fallback="Net contributed capital" /><strong>{formatUsd(summary.net_contributed_capital_total, 'Pending historical FX')}</strong><small>{summary.coverage.missing_capital_fx.length ? `Missing historical FX: ${summary.coverage.missing_capital_fx.join(', ')}` : 'Opening capital + deposits − withdrawals, in USD'}</small></article>
          <article className={summary.earnings.status === 'pending_market_valuation' ? 'valuation-pending' : ''}><MetricLabel metricKey="total_gain_loss" metadata={metadata} fallback="Current earnings" /><strong>{summary.earnings.value_by_currency ? formatUsd(summary.earnings.value_by_currency.USD, 'Pending historical FX') : 'Pending historical FX'}</strong><small>{summary.coverage.missing_capital_fx.length ? `Missing historical FX: ${summary.coverage.missing_capital_fx.join(', ')}` : summary.earnings.status === 'stale' ? 'Last complete earnings; market prices are stale' : 'Total value − net contributed capital, in USD'}</small></article>
          <article><MetricLabel metricKey="total_fees" metadata={metadata} fallback="Fees & commissions" /><strong>{formatUsd(summary.total_fees_total, 'Pending FX')}</strong><small>All ledger costs converted to USD</small></article>
          <article><MetricLabel metricKey="dividends" metadata={metadata} fallback="Dividends" /><strong>{formatUsd(summary.dividends_total, 'Pending FX')}</strong><small>Gross dividends converted to USD</small></article>
          <article><MetricLabel metricKey="interest" metadata={metadata} fallback="Interest" /><strong>{formatUsd(summary.interest_total, 'Pending FX')}</strong><small>Gross interest converted to USD</small></article>
          <article><span>Portfolio value by currency</span><strong className="currency-value-list">{summary.currency_exposure.length ? summary.currency_exposure.map((exposure) => <span key={exposure.name}><b>{exposure.name}</b><i>{formatUsd(exposure.market_value_base, 'Pending')} · {(Number(exposure.weight) * 100).toFixed(1)}%</i></span>) : 'Pending valuation'}</strong><small>Underlying currency; values converted to USD</small></article>
        </div>
        {summary.capital_breakdown_base && <section className="ledger-allocation capital-reconciliation" aria-labelledby="ledger-capital-title"><div className="ledger-allocation-head"><div><span className="eyebrow">Trust the arithmetic</span><h2 id="ledger-capital-title">Capital and earnings reconciliation</h2></div></div><dl><div><dt>Opening positions</dt><dd>{formatUsd(summary.capital_breakdown_base.opening_positions, 'Pending')}</dd></div><div><dt>External cash</dt><dd>{formatUsd(summary.capital_breakdown_base.external_cash, 'Pending')}</dd></div><div><dt>Historical acquisition costs</dt><dd>{formatUsd(summary.capital_breakdown_base.historical_acquisition_costs, 'Pending')}</dd></div><div><dt>Current position cost basis</dt><dd>{formatUsd(summary.current_position_cost_basis_base, 'Pending')}</dd></div><div><dt>Unrealized gain / loss</dt><dd>{formatUsd(summary.unrealized_gain_base, 'Pending')}</dd></div><div><dt>Other earnings, fees and cash FX</dt><dd>{formatUsd(summary.other_earnings_base, 'Pending')}</dd></div></dl></section>}
        <section className="ledger-allocation" aria-labelledby="ledger-allocation-title">
          <div className="ledger-allocation-head"><div><span className="eyebrow">Portfolio allocation</span><h2 id="ledger-allocation-title">Stocks and cash</h2></div><strong>{formatUsd(summary.total_value.value_by_currency?.USD || null, 'Pending valuation')}</strong></div>
          {cashPercent === null || stocksPercent === null ? <p>{summary.allocation.reason === 'negative_cash' ? 'Allocation is unavailable while cash is negative.' : `Allocation is available when coverage is complete${summary.coverage.missing_prices.length ? `; missing prices: ${summary.coverage.missing_prices.join(', ')}` : ''}${summary.coverage.missing_fx.length ? `; missing FX: ${summary.coverage.missing_fx.join(', ')}` : ''}.`}</p> : <>
            <div className="allocation-bar" role="img" aria-label={`Portfolio allocation: ${stocksPercent.toFixed(1)}% stocks and ${cashPercent.toFixed(1)}% cash`}><span className="allocation-stocks" style={{ width: `${stocksPercent}%` }} /><span className="allocation-cash" style={{ width: `${cashPercent}%` }} /></div>
            <div className="allocation-legend"><div><i className="stocks-key" /><span>Stocks</span><strong>{stocksPercent.toFixed(1)}%</strong><small>{formatUsd(summary.allocation.stocks_value, 'Pending')}</small></div><div><i className="cash-key" /><span>Cash</span><strong>{cashPercent.toFixed(1)}%</strong><small>{formatUsd(summary.allocation.cash_value, 'Pending')}</small></div></div>
          </>}
        </section>
        <section className="ledger-assets" aria-labelledby="ledger-assets-title">
          <div className="panel-title ledger-assets-head"><div><h2 id="ledger-assets-title">Current assets</h2><p>Consolidated cash and current holdings. Repeated transactions for one ticker are combined.</p></div><strong>{summary.assets.filter((asset) => asset.asset_type === 'stock').length} stocks</strong></div>
          <div className="table-scroll"><table className="asset-table"><thead><tr><th scope="col">Asset</th><th scope="col">Quantity</th><th scope="col">Current price</th><th scope="col">Current value (USD)</th><th scope="col">Share / status</th></tr></thead><tbody>{summary.assets.map((asset) => <tr key={`${asset.asset_type}-${asset.ticker}`}><th scope="row"><strong>{asset.ticker}</strong><small>{asset.name}</small></th><td>{asset.quantity || '—'}</td><td>{asset.latest_price && asset.price_currency ? `${asset.price_currency} ${asset.latest_price}` : '—'}</td><td>{asset.current_value === null ? 'Pending' : formatUsd(asset.current_value, 'Pending')}</td><td>{asset.valuation_status === 'valued' ? asset.weight !== null ? `${(Number(asset.weight) * 100).toFixed(1)}%` : 'Valued' : asset.valuation_status === 'missing_price' ? 'Missing current price' : 'Missing FX rate'}</td></tr>)}</tbody></table></div>
        </section>
        {loadError && <p className="error" role="alert">{loadError}</p>}
        <section className="ledger-list ledger-full">
          <div className="panel-title ledger-head"><div><h2>Transactions</h2><p>Chronological record of the actions entered in the Ledger.</p></div><button className="primary-action compact ledger-add-action" type="button" onClick={() => openTransaction()}><Plus size={16} aria-hidden="true" /><span>Add transaction</span></button></div>
          {!ledgerLoaded ? <div className="empty-ledger"><span>Loading ledger…</span></div> : transactions.length === 0 ? <div className="empty-ledger"><WalletCards size={24} /><h3>Start with what you have</h3><p>Cash only and one-stock-only portfolios are both valid. Add the other side later if you want.</p><div className="empty-ledger-actions"><button className="secondary-action" onClick={() => openTransaction('OPENING_CASH')}>Start with cash</button><button className="secondary-action" onClick={() => openTransaction('OPENING_POSITION')}>Start with one stock</button></div></div> : <div className="table-scroll"><table className="transaction-table"><thead><tr><th scope="col">Date</th><th scope="col">Type</th><th scope="col">Asset</th><th scope="col"><MetricLabel metricKey="cash_impact" metadata={metadata} fallback="Cash impact" /></th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead><tbody>{transactions.map((transaction) => {
            const typeLabel = transaction.cost_category || transaction.transaction_type
            return <tr className={`transaction-row ${transaction.linked_transaction_id ? 'linked-cost-row' : ''}`} key={transaction.transaction_id}><td>{transaction.event_date}</td><td>{typeLabel.replaceAll('_',' ')}{transaction.linked_transaction_id && <small>Linked cost</small>}</td><td>{transaction.security_code || transaction.source_currency || transaction.currency}</td><td><strong className={transaction.cash_amount.startsWith('-') ? 'negative' : 'positive'}>{transaction.currency} {transaction.cash_amount === '0.00' ? 'No cash movement' : transaction.cash_amount}</strong>{transaction.source_currency && transaction.source_currency !== transaction.currency && <small>{transaction.source_currency} {transaction.source_amount} at {transaction.fx_rate_to_settlement}</small>}</td><td><button className="icon-action correction-action" type="button" disabled={Boolean(transaction.linked_transaction_id)} onClick={() => openCorrection(transaction)} aria-label={`Edit ${transaction.transaction_type.toLowerCase().replaceAll('_', ' ')} from ${transaction.event_date}`} title={transaction.linked_transaction_id ? 'Edit this cost with its parent transaction' : 'Edit transaction'}><Pencil size={15} /></button></td></tr>
          })}</tbody></table></div>}
        </section>
      </section>
      {transactionOpen && <div className="transaction-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setTransactionOpen(false) }}>
        <section className="transaction-sheet" role="dialog" aria-modal="true" aria-labelledby="transaction-title">
          <div className="transaction-sheet-head"><div><span className="eyebrow">Portfolio ledger</span><h2 id="transaction-title">{correctionTarget ? 'Edit transaction' : 'Add transaction'}</h2></div><button className="icon-action" type="button" onClick={() => setTransactionOpen(false)} aria-label="Close transaction form" title="Close"><X size={17} /></button></div>
          <form className="transaction-form overlay-form" onSubmit={submit}>
            <label><span>Type</span><select name="transaction_type" value={transactionType} onChange={(event) => { setTransactionType(event.target.value); setSecurityCode(''); setResolvedSecurity(null); setSecurityOptions([]); setSecurityLookupError(''); setTransactionCurrency(user.base_currency); setFeeCategory('OTHER_FEE'); setCostComponents([]) }}><option value="DEPOSIT">Deposit</option><option value="WITHDRAWAL">Withdrawal</option><option value="OPENING_CASH">Opening cash (starting balance)</option><option value="OPENING_POSITION">Opening position (already owned)</option><option value="BUY">Buy</option><option value="SELL">Sell</option><option value="DIVIDEND">Dividend</option><option value="INTEREST">Interest</option><option value="FEE">Fee</option></select>{transactionType === 'OPENING_CASH' && <small className="field-note">Cash already held when you began tracking in Auspex.</small>}{transactionType === 'OPENING_POSITION' && <small className="field-note">Shares already owned when you began tracking, at their original average cost. Linked acquisition costs do not reduce today's cash.</small>}</label>
            <div className="form-row"><label><span>Date</span><input name="event_date" type="date" defaultValue={correctionTarget?.event_date || today} max={today} required /></label><label><span>{securityReference ? 'Settlement currency' : 'Currency'}</span><select name="currency" value={transactionCurrency} onChange={(event) => { const currency = event.target.value; setTransactionCurrency(currency); setCostComponents((current) => current.map((component) => component.currency === currency ? { ...component, fxRateToSettlement: '' } : component)) }}><option>USD</option><option>CHF</option><option>EUR</option><option>GBP</option></select>{securityReference && <small className="field-note">Cash currency on the broker statement; the listing currency is preserved separately.</small>}</label></div>
            {securityReference && <>
              {heldSecurity ? <label><span>{transactionType === 'SELL' ? 'Position to sell' : 'Dividend security'}</span><select name="security_code" value={securityCode} onChange={(event) => { setSecurityCode(event.target.value); setResolvedSecurity(null); setSecurityLookupError('') }} required><option value="">Select a current holding</option>{correctionTarget?.security_code && !summary.positions.some((position) => position.security_code === correctionTarget.security_code) && <option value={correctionTarget.security_code}>{correctionTarget.security_code} · corrected entry</option>}{summary.positions.map((position) => <option key={position.security_code} value={position.security_code}>{position.security_code} · {position.quantity} held</option>)}</select></label> : <div className="security-combobox"><label htmlFor="security-code"><span>Ticker / ISIN</span><input id="security-code" name="security_code" role="combobox" aria-autocomplete="list" aria-expanded={securityOptions.length > 0} aria-controls="security-options" autoComplete="off" placeholder="Start typing a ticker" value={securityCode} onChange={(event) => { setSecurityCode(event.target.value.toUpperCase()); setResolvedSecurity(null); setSecurityLookupError('') }} required /></label>{securityOptions.length > 0 && <div className="security-options" id="security-options" role="listbox">{securityOptions.map((security) => <button key={security.security_sk} type="button" role="option" aria-selected={resolvedSecurity?.security_sk === security.security_sk} onClick={() => chooseSecurity(security)}><strong>{security.ticker}</strong><span>{security.company_name}</span><small>{security.exchange || 'Exchange unavailable'} · {security.currency}</small></button>)}</div>}</div>}
              {resolvedSecurity && resolvedSecurity.query === securityCode.trim().toUpperCase() && <div className="security-resolution"><Check size={15} /><span><strong>{resolvedSecurity.ticker}</strong> · {resolvedSecurity.company_name}{resolvedSecurity.exchange ? ` · ${resolvedSecurity.exchange}` : ''} · {resolvedSecurity.currency}</span></div>}
              {securityLookupError && <p className="error security-lookup-error" role="alert">{securityLookupError}</p>}
              {securityTransaction && <div className="form-row"><label><span>Quantity</span><input name="quantity" type="number" min="0.00000001" max={transactionType === 'SELL' && !correctionTarget ? selectedHolding?.quantity || '0' : '1000000000'} step="0.00000001" defaultValue={correctionTarget?.quantity || undefined} required />{transactionType === 'SELL' && !correctionTarget && <small className="field-note">Available to sell: {selectedHolding?.quantity || '0'} shares</small>}</label><label><span>{transactionType === 'OPENING_POSITION' ? 'Average acquisition cost' : 'Price'}</span><input name="price" type="number" min="0.01" max="999999999999.99" step="0.01" defaultValue={correctionTarget?.price || undefined} required /></label></div>}
            </>}
            {!securityTransaction && <label><span>{transactionType === 'FEE' ? 'Fee amount' : 'Amount'}</span><input name="amount" type="number" min="0.01" max="999999999999.99" step="0.01" defaultValue={correctionTarget ? correctedAmount(correctionTarget) : undefined} required />{['WITHDRAWAL', 'FEE'].includes(transactionType) && !correctionTarget && <small className="field-note">Available: {transactionCurrency} {availableCash}. Cannot exceed available cash.</small>}</label>}
            {transactionType === 'FEE' && <label><span>Fee category</span><select value={feeCategory} onChange={(event) => setFeeCategory(event.target.value)}>{costCategories.filter(([value]) => value !== 'VAT' && value !== 'WITHHOLDING_TAX').map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>}
            {transactionType === 'BUY' && <small className="field-note cash-rule">Available: {transactionCurrency} {availableCash}. Cannot exceed available cash including fees.</small>}
            {showFxRate && <label><span>FX rate to {user.base_currency}</span><input name="fx_rate_to_base" type="number" min="0.00000001" max="1000000000" step="0.00000001" defaultValue={correctionTarget?.fx_rate_to_base || undefined} placeholder={`1 ${transactionCurrency} in ${user.base_currency}`} /><small className="field-note">Optional. Enter the rate you actually received: 1 {transactionCurrency} = X {user.base_currency}.</small></label>}
            {showSettlementFx && <label><span>Gross FX rate to {transactionCurrency}</span><input name="fx_rate_to_settlement" type="number" min="0.00000001" max="1000000000" step="0.00000001" defaultValue={correctionTarget?.fx_rate_to_settlement || undefined} placeholder={`1 ${resolvedSecurity.currency} in ${transactionCurrency}`} required /><small className="field-note">1 {resolvedSecurity.currency} = X {transactionCurrency}, from the broker statement.</small></label>}
            {linkedCostTypes.has(transactionType) && <fieldset className="cost-components"><legend>Costs and deductions</legend>{costComponents.map((component) => <div className="cost-component" key={component.id}><div className="cost-component-grid"><label><span>Category</span><select value={component.category} onChange={(event) => updateCostComponent(component.id, { category: event.target.value })}>{costCategories.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label><span>Amount</span><input type="number" min="0.01" max="999999999999.99" step="0.01" value={component.amount} onChange={(event) => updateCostComponent(component.id, { amount: event.target.value })} required /></label><label><span>Currency</span><select value={component.currency} onChange={(event) => updateCostComponent(component.id, { currency: event.target.value, fxRateToSettlement: '' })}><option>USD</option><option>CHF</option><option>EUR</option><option>GBP</option></select></label>{component.currency !== transactionCurrency && <label><span>FX to {transactionCurrency}</span><input type="number" min="0.00000001" max="1000000000" step="0.00000001" value={component.fxRateToSettlement} onChange={(event) => updateCostComponent(component.id, { fxRateToSettlement: event.target.value })} placeholder={`1 ${component.currency} in ${transactionCurrency}`} required /></label>}</div><button className="icon-action remove-cost" type="button" onClick={() => setCostComponents((current) => current.filter((row) => row.id !== component.id))} aria-label="Remove cost" title="Remove cost"><X size={15} /></button></div>)}<button className="secondary-action compact add-cost" type="button" onClick={addCostComponent} disabled={costComponents.length >= 20}><Plus size={15} /> Add cost or deduction</button>{transactionType === 'OPENING_POSITION' && costComponents.length > 0 && <small className="field-note">These historical acquisition costs increase contributed capital but do not debit current cash.</small>}</fieldset>}
            {formError && <p className="error" role="alert">{formError}</p>}
            <div className="transaction-sheet-actions"><button className="secondary-action" type="button" onClick={() => setTransactionOpen(false)}>Cancel</button><button className="primary-action" disabled={saving || (securityReference && (!resolvedSecurity || resolvedSecurity.query !== securityCode.trim().toUpperCase()))}>{saving ? 'Saving…' : correctionTarget ? 'Save changes' : 'Save transaction'}</button></div>
          </form>
        </section>
      </div>}
    </main>
  )
}

function OpportunitiesPage({ user }: { user: AppUser }) {
  const [response, setResponse] = useState<CompanyOpportunityResponse | null>(null)
  const [error, setError] = useState('')
  const [theme, setTheme] = useState('all')
  const [direction, setDirection] = useState('all')

  useEffect(() => {
    fetch('/api/opportunities?limit=200')
      .then(async (result) => {
        const payload = await result.json()
        if (!result.ok) throw new Error(payload.message || 'Opportunities could not be loaded.')
        setResponse(payload)
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : 'Opportunities could not be loaded.'))
  }, [])

  const themes = Array.from(new Set(
    response?.opportunities.map((opportunity) => opportunity.theme_id) || [],
  )).sort()
  const opportunities = response?.opportunities.filter((opportunity) => (
    (theme === 'all' || opportunity.theme_id === theme)
    && (direction === 'all' || opportunity.outlook_direction === direction)
  )) || []

  return <main className="product-page opportunities-page">
    <AccountHeader user={user} currentPage="opportunities" />
    <section className="opportunities-main">
      <header className="opportunities-heading">
        <div><span className="eyebrow">Fresh company research</span><h1>Discover opportunities</h1><p>Every company is evaluated independently from your portfolio using compact current data and six evidence-backed legs.</p></div>
        <span className="research-label">90-day direction · research only</span>
      </header>
      <section className="opportunity-toolbar" aria-label="Opportunity filters">
        <label><span>Theme</span><select value={theme} onChange={(event) => setTheme(event.target.value)}><option value="all">All themes</option>{themes.map((value) => <option key={value} value={value}>{value.replaceAll('_', ' ')}</option>)}</select></label>
        <label><span>Direction</span><select value={direction} onChange={(event) => setDirection(event.target.value)}><option value="all">All directions</option>{['ACCELERATING', 'STABLE', 'DETERIORATING', 'UNCERTAIN'].map((value) => <option key={value} value={value}>{value.toLowerCase()}</option>)}</select></label>
        <div><span>Current set</span><strong>{opportunities.length} companies</strong></div>
        <div><span>Generated</span><strong>{response?.generated_at ? new Date(response.generated_at).toLocaleString() : 'Loading'}</strong></div>
      </section>
      {error && <p className="error" role="alert">{error}</p>}
      {!response && !error && <p className="opportunity-state">Reading fresh company packages…</p>}
      {response && <div className="opportunity-grid">
        {opportunities.map((opportunity) => {
          const evidence = new Map(opportunity.evidence.map((item) => [item.evidence_id, item]))
          return <article className={`opportunity-card direction-${opportunity.outlook_direction.toLowerCase()}`} key={opportunity.security_sk}>
            <header>
              <div><span className="opportunity-ticker">{opportunity.ticker}</span><h2>{opportunity.company_name}</h2><p>{opportunity.theme_id.replaceAll('_', ' ')} · {opportunity.candidate_count} peers</p></div>
              <div className="opportunity-score"><strong>{opportunity.opportunity_score === null ? '—' : Number(opportunity.opportunity_score).toFixed(0)}</strong><span>{opportunity.outlook_direction.toLowerCase()}</span></div>
            </header>
            <div className="opportunity-meta"><span>{opportunity.coverage_status.toLowerCase()} coverage</span><span>Known through {opportunity.max_knowledge_date}</span><span>As of {opportunity.as_of}</span></div>
            {opportunity.narrative && <section className="company-narrative"><span className="eyebrow">Company outlook</span><p>{opportunity.narrative.summary}</p><small>{opportunity.narrative.uncertainty}</small></section>}
            <section className="six-leg-panel"><h3>Six-leg validation</h3><ol>{opportunity.legs.map((leg) => {
              const contribution = Number(leg.contribution || 0)
              return <li key={leg.leg_name}>
                <div><strong>{leg.leg_name.replaceAll('_', ' ')}</strong><span className={leg.direction === 'RAISED' ? 'positive' : leg.direction === 'LOWERED' ? 'negative' : ''}>{leg.contribution === null ? 'Unavailable' : `${contribution >= 0 ? '+' : ''}${contribution.toFixed(2)}`}</span></div>
                <span className="leg-axis"><i className={contribution >= 0 ? 'positive-bar' : 'negative-bar'} style={{ width: `${Math.min(50, Math.abs(contribution) * 60)}%`, left: contribution >= 0 ? '50%' : `${50 - Math.min(50, Math.abs(contribution) * 60)}%` }} /></span>
                <small>{leg.direction.toLowerCase()}{leg.coverage_reasons.length ? ` · ${leg.coverage_reasons.join(', ').replaceAll('_', ' ')}` : ''}</small>
                {leg.evidence_ids.length > 0 && <details><summary>{leg.evidence_ids.length} source reference{leg.evidence_ids.length === 1 ? '' : 's'}</summary>{leg.evidence_ids.map((id) => { const item = evidence.get(id); return item ? <p key={id}><b>{item.source_type.replaceAll('_', ' ')}</b> · known {item.knowledge_date}<br />{item.excerpt}</p> : null })}</details>}
              </li>
            })}</ol></section>
            {opportunity.coverage_reasons.length > 0 && <p className="opportunity-coverage">Coverage notes: {opportunity.coverage_reasons.join(', ').replaceAll('_', ' ')}</p>}
            <footer><span>Model {opportunity.package_version}</span><span>{opportunity.package_fingerprint.slice(0, 12)}</span></footer>
          </article>
        })}
      </div>}
      {response && opportunities.length === 0 && <section className="home-empty"><Search size={26} /><h1>No companies match these filters.</h1><p>Change the theme or direction filter to inspect the current research universe.</p></section>}
      {response && <p className="opportunity-disclaimer">{response.disclaimer}</p>}
    </section>
  </main>
}

const suggestedDiscussionQuestions = [
  'Why trim my largest position?',
  'What changed in my portfolio?',
  'Where is my concentration risk?',
]

function DiscussionPage({ user }: { user: AppUser }) {
  const [conversationId, setConversationId] = useState(() => {
    const existing = window.sessionStorage.getItem('auspex:discussion-id')
    return existing || crypto.randomUUID()
  })
  const [exchanges, setExchanges] = useState<DiscussionExchange[]>([])
  const [question, setQuestion] = useState('')
  const [profile, setProfile] = useState<AdvisorProfile | null>(null)
  const [profileDraft, setProfileDraft] = useState('')
  const [summary, setSummary] = useState<MorningSummary | null>(null)
  const [inAppEnabled, setInAppEnabled] = useState(true)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [sending, setSending] = useState(false)
  const [savingProfile, setSavingProfile] = useState(false)
  const [error, setError] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    window.sessionStorage.setItem('auspex:discussion-id', conversationId)
    setError('')
    Promise.all([
      fetch(`/api/discussion/turns?conversation_id=${encodeURIComponent(conversationId)}`),
      fetch('/api/advisor_profile'),
      fetch('/api/morning_summary'),
      fetch('/api/notification_preferences'),
    ])
      .then(async ([historyResponse, profileResponse, summaryResponse, preferencesResponse]) => {
        const [historyPayload, profilePayload, summaryPayload, preferencesPayload] = await Promise.all([
          historyResponse.json(), profileResponse.json(), summaryResponse.json(), preferencesResponse.json(),
        ])
        if (!historyResponse.ok) throw new Error(historyPayload.message || 'Discussion could not be loaded.')
        setExchanges(historyPayload.exchanges || [])
        if (profileResponse.ok) {
          setProfile(profilePayload)
          setProfileDraft(profilePayload.instructions)
        }
        if (summaryResponse.ok) setSummary(summaryPayload)
        if (preferencesResponse.ok) setInAppEnabled(preferencesPayload.in_app_enabled)
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : 'Discussion could not be loaded.'))
  }, [conversationId])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [exchanges])

  async function ask(suggestedQuestion?: string) {
    const query = (suggestedQuestion ?? question).trim()
    if (!query || sending) return
    setSending(true)
    setError('')
    if (!suggestedQuestion) setQuestion('')
    try {
      const response = await fetch('/api/discussion/turns', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: conversationId,
          client_request_id: crypto.randomUUID(),
          query,
        }),
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.message || 'Auspex could not answer that question.')
      setExchanges((current) => [...current, payload.exchange])
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Auspex could not answer that question.')
      if (!suggestedQuestion) setQuestion(query)
    } finally {
      setSending(false)
    }
  }

  function newDiscussion() {
    const nextId = crypto.randomUUID()
    window.sessionStorage.setItem('auspex:discussion-id', nextId)
    setConversationId(nextId)
    setExchanges([])
    setQuestion('')
  }

  async function saveProfile() {
    setSavingProfile(true)
    setError('')
    try {
      const response = await fetch('/api/advisor_profile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instructions: profileDraft }),
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.message || 'Advisor settings could not be saved.')
      setProfile(payload)
      setProfileDraft(payload.instructions)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Advisor settings could not be saved.')
    } finally {
      setSavingProfile(false)
    }
  }

  async function resetProfile() {
    const response = await fetch('/api/advisor_profile/reset', { method: 'POST' })
    const payload = await response.json()
    if (!response.ok) {
      setError(payload.message || 'Advisor settings could not be reset.')
      return
    }
    setProfile(payload)
    setProfileDraft(payload.instructions)
  }

  async function updateInApp(enabled: boolean) {
    const response = await fetch('/api/notification_preferences', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ in_app_enabled: enabled, email_opt_in: false }),
    })
    const payload = await response.json()
    if (!response.ok) {
      setError(payload.message || 'Notification preferences could not be saved.')
      return
    }
    setInAppEnabled(payload.in_app_enabled)
  }

  const money = (value: string | null | undefined) => value == null
    ? 'Unavailable'
    : new Intl.NumberFormat(undefined, { style: 'currency', currency: summary?.base_currency || user.base_currency, maximumFractionDigits: 0 }).format(Number(value))

  return (
    <main className="product-page discussion-page">
      <AccountHeader user={user} currentPage="discussion" />
      <section className="discussion-main">
        <header className="discussion-heading">
          <div><span className="eyebrow">AI-assisted portfolio research</span><h1>Discussion</h1><p>You are interacting with an AI system. Its answers use your portfolio and point-in-time evidence; decisions and arithmetic stay deterministic.</p></div>
          <div className="discussion-heading-actions">
            <button className="secondary-action" type="button" onClick={() => setSettingsOpen((open) => !open)}><SlidersHorizontal size={15} /> Advisor settings</button>
            <button className="secondary-action" type="button" onClick={newDiscussion}><Plus size={15} /> New discussion</button>
          </div>
        </header>

        {summary && inAppEnabled && <section className="morning-summary" aria-labelledby="morning-title">
          <Sunrise size={21} />
          <div><span className="eyebrow">{summary.summary_date}</span><h2 id="morning-title">Morning summary</h2><p>{summary.holding_count} holdings · {money(summary.portfolio_value_base)} portfolio · {money(summary.cash_base)} cash</p></div>
          <div className="morning-signal">{summary.top_suggestion ? <><strong>{summary.top_suggestion.action} {summary.top_suggestion.ticker}</strong><small>{money(summary.top_suggestion.suggested_amount_base)} suggested</small></> : <><strong>No active change</strong><small>Current deterministic view</small></>}</div>
        </section>}

        {settingsOpen && <section className="advisor-settings" aria-labelledby="advisor-title">
          <div className="advisor-settings-copy"><span className="eyebrow">Tone, not decisions</span><h2 id="advisor-title">Advisor settings</h2><p>Your note can shape emphasis and language. It cannot change recommendations, arithmetic, evidence, or safety rules.</p></div>
          <div className="advisor-settings-form">
            <label><span>Advisor note</span><textarea maxLength={600} value={profileDraft} onChange={(event) => setProfileDraft(event.target.value)} /></label>
            <div className="advisor-actions"><button className="secondary-action" type="button" onClick={resetProfile}><RotateCcw size={14} /> Reset</button><button className="primary-action compact" type="button" disabled={savingProfile || !profileDraft.trim()} onClick={saveProfile}>{savingProfile ? 'Saving…' : 'Save note'}</button></div>
            {profile && <small>{profile.is_default ? `Default ${profile.risk_profile || ''} posture` : 'Your bounded advisor note is active'}</small>}
            <label className="notification-toggle"><input type="checkbox" checked={inAppEnabled} onChange={(event) => updateInApp(event.target.checked)} /><span>Show the in-app morning summary</span></label>
            <label className="notification-toggle unavailable"><input type="checkbox" disabled /><span><strong>Email unavailable</strong> under the Switzerland North data-region policy</span></label>
          </div>
        </section>}

        <div className="discussion-layout">
          <section className="conversation" aria-label="Portfolio discussion">
            {exchanges.length === 0 && <div className="discussion-welcome">
              <MessageCircle size={27} />
              <h2>Start with the portfolio you have.</h2>
              <p>Auspex answers only from your current ledger, deterministic recommendations, and point-in-time evidence.</p>
              <span>Suggested questions</span>
              <div className="suggested-questions">{suggestedDiscussionQuestions.map((suggestion) => <button key={suggestion} type="button" onClick={() => ask(suggestion)}>{suggestion}<ArrowRight size={14} /></button>)}</div>
            </div>}
            {exchanges.map((exchange) => <article className="discussion-exchange" key={exchange.exchange_id}>
              <div className="user-turn"><span>You</span><p>{exchange.query}</p></div>
              <div className={`advisor-turn ${exchange.status}`}>
                <div className="advisor-turn-meta"><span>Auspex</span><small>{exchange.confidence} confidence · {new Date(exchange.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</small></div>
                <p data-ai-generated="true">{exchange.answer}</p>
                {exchange.what_if && <dl className="what-if-result">
                  <div><dt>Amount</dt><dd>{exchange.what_if.base_currency} {exchange.what_if.amount_base}</dd></div>
                  <div><dt>Projected weight</dt><dd>{(Number(exchange.what_if.projected_weight) * 100).toFixed(1)}%</dd></div>
                  <div><dt>Portfolio after</dt><dd>{exchange.what_if.base_currency} {exchange.what_if.portfolio_value_after}</dd></div>
                  <p>{exchange.what_if.assumption}</p>
                </dl>}
                {exchange.metric_keys.length > 0 && <div className="discussion-metrics">{exchange.metric_keys.map((metricKey) => <button type="button" key={metricKey} onClick={() => ask(`Explain the ${metricKey} number in your last answer.`)}><CircleHelp size={13} /> Explain this number <small>{metricKey.replaceAll('_', ' ')}</small></button>)}</div>}
                {exchange.citations.length > 0 && <details className="discussion-evidence"><summary>{exchange.citations.length} grounded source{exchange.citations.length === 1 ? '' : 's'}</summary><ol>{exchange.citations.map((citation) => <li key={citation.id}>{citation.url ? <a href={citation.url} target="_blank" rel="noreferrer">{citation.title || citation.source_name || citation.id}</a> : <strong>{citation.title || citation.source_name || citation.id}</strong>}<small>{citation.source_type} · known {citation.knowledge_date || 'date unavailable'}</small></li>)}</ol></details>}
                {exchange.limitations && <small className="discussion-limitations" data-ai-generated="true">{exchange.limitations}</small>}
              </div>
            </article>)}
            <div ref={endRef} />
          </section>

          <aside className="discussion-context">
            <span className="eyebrow">Current context</span>
            <h2>{user.risk_profile || 'Investor'} posture</h2>
            <dl><div><dt>Base currency</dt><dd>{user.base_currency}</dd></div><div><dt>Evidence</dt><dd>Point in time</dd></div><div><dt>Trade execution</dt><dd>Never</dd></div></dl>
            <p>Answers may be withheld when portfolio valuation, recommendation coverage, or evidence is insufficient.</p>
          </aside>
        </div>

        <form className="discussion-composer" onSubmit={(event) => { event.preventDefault(); ask() }}>
          <label htmlFor="discussion-question">Ask Auspex</label>
          <div><textarea id="discussion-question" rows={2} maxLength={1000} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Ask about a recommendation, risk, or amount…" /><button type="submit" disabled={sending || !question.trim()} title="Send question"><Send size={18} /><span className="visually-hidden">Send question</span></button></div>
          <small>Research only. Auspex does not provide financial, tax, or legal advice and never executes trades.</small>
        </form>
        {error && <p className="error discussion-error" role="alert">{error}</p>}
      </section>
    </main>
  )
}

function AdminReview({ currentUser }: { currentUser: AppUser }) {
  const [users, setUsers] = useState<AppUser[]>([])
  const [error, setError] = useState('')

  async function load() {
    const response = await fetch('/api/registration_queue?status=pending')
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.message || 'Could not load registrations.')
    setUsers(payload)
  }
  useEffect(() => { load().catch((reason) => setError(reason.message)) }, [])

  async function review(user: AppUser, action: 'approve' | 'reject') {
    const response = await fetch(`/api/${action}_registration/${user.user_sk}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note: action === 'approve' ? 'Approved for MVP access' : 'Registration not approved' }),
    })
    if (!response.ok) {
      const payload = await response.json(); setError(payload.message || 'Review failed.'); return
    }
    setUsers((current) => current.filter((item) => item.user_sk !== user.user_sk))
  }

  return (
    <main className="admin-shell">
      <AccountHeader user={currentUser} currentPage="admin" />
      <section className="admin-heading"><span className="eyebrow">Access administration</span><h1>Registration review</h1><p>Approval grants the <strong>user</strong> role. Authentication alone never grants product access.</p></section>
      {error && <p className="error" role="alert">{error}</p>}
      <section className="review-list" aria-live="polite">
        {users.length === 0 && <div className="empty-state"><ShieldCheck size={25} /><h2>No pending registrations</h2><p>The review queue is clear.</p></div>}
        {users.map((user) => (
          <article className="review-row" key={user.user_sk}>
            <div><span className="review-email">{user.contact_email || 'Microsoft personal account'}</span><span className="review-date">Requested {user.created_at ? new Date(user.created_at).toLocaleString() : 'recently'}</span></div>
            <div className="review-actions"><button className="reject" onClick={() => review(user, 'reject')}>Reject</button><button className="approve" onClick={() => review(user, 'approve')}><Check size={15} /> Approve</button></div>
          </article>
        ))}
      </section>
    </main>
  )
}

function App() {
  const [principal, setPrincipal] = useState<ClientPrincipal | null | undefined>(undefined)
  const [user, setUser] = useState<AppUser | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    async function hydrate() {
      const authResponse = await fetch('/.auth/me')
      const authPayload = await authResponse.json()
      const clientPrincipal = authPayload.clientPrincipal as ClientPrincipal | null
      setPrincipal(clientPrincipal)
      if (clientPrincipal) {
        const response = await fetch('/api/me')
        if (response.ok) setUser(await response.json())
      }
      setLoading(false)
    }
    hydrate().catch(() => { setPrincipal(null); setLoading(false) })
  }, [])

  useEffect(() => {
    if (loading || !user) return
    const path = window.location.pathname
    if (user.status !== 'active') {
      if (!path.startsWith('/account')) window.history.replaceState({}, '', '/account')
      return
    }
    const validPath = path === '/'
      || path.startsWith('/account')
      || path.startsWith('/onboarding')
      || (path.startsWith('/opportunities') && user.onboarded)
      || (path.startsWith('/discussion') && user.onboarded)
      || (path.startsWith('/ledger') && user.onboarded)
      || (path.startsWith('/admin') && user.role === 'admin')
    if (path.startsWith('/transactions') && user.onboarded) {
      window.history.replaceState({}, '', '/ledger')
      return
    }
    if (!validPath) {
      window.history.replaceState({}, '', path.startsWith('/ledger') || path.startsWith('/transactions') ? '/onboarding' : '/')
    }
  }, [loading, user])

  function completeRegistration(registeredUser: AppUser) {
    window.history.replaceState({}, '', '/account')
    setUser(registeredUser)
  }

  function completeOnboarding(onboardedUser: AppUser) {
    setUser(onboardedUser)
  }

  if (loading) return <LoadingScreen message="Reading the signs…" />
  if (!principal) return <Login />
  if (!user && window.location.pathname.startsWith('/register')) return <Registration principal={principal} onSubmitted={completeRegistration} />
  if (!user) return <RegistrationRequired />
  if (user.status !== 'active') return <AccessState user={user} />
  if (window.location.pathname.startsWith('/admin') && user.role === 'admin') return <AdminReview currentUser={user} />
  if (window.location.pathname.startsWith('/onboarding')) return <Onboarding user={user} onComplete={completeOnboarding} />
  if (window.location.pathname.startsWith('/opportunities') && user.onboarded) return <OpportunitiesPage user={user} />
  if (window.location.pathname.startsWith('/discussion') && user.onboarded) return <DiscussionPage user={user} />
  if (window.location.pathname.startsWith('/ledger') && user.onboarded) return <TransactionsPage user={user} />
  if (window.location.pathname.startsWith('/account')) return <AccessState user={user} />
  return <ProductHome user={user} />
}

export default App
