import { Search } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { ActionPill, ErrorBlock, Loading, MetricTile, PageHeading, Section, formatMoney, formatNumber, titleCase } from '../components/common'
import { useApi } from '../lib/api'
import type { GateTrace, Recommendation, SecurityPackage, SecuritySummary } from '../lib/types'

const LEG_EXPLANATIONS = {
  thesis_linkage: {
    title: 'Thesis Linkage',
    description: 'Measures how strongly recent filings and news support the company-specific investment themes Auspex is tracking.',
    interpretation: 'Higher means current evidence reinforces the thesis; lower means support is weak or deteriorating.',
    window: 'Trailing 180 days, with evidence losing half its weight every 90 days.',
  },
  attention_acceleration: {
    title: 'Attention Acceleration',
    description: 'Compares weighted material events in the latest 30 days with the previous 30 days.',
    interpretation: 'Higher means relevant activity is accelerating; lower means attention is fading.',
    window: 'Latest 30 days versus days 30–60.',
  },
  narrative_premium: {
    title: 'Narrative Premium',
    description: 'Compares the strength of the current company story with the expectations already implied by peer-relative revenue growth.',
    interpretation: 'Higher means the narrative is stronger than fundamentals currently justify; this can be opportunity or expectation risk.',
    window: 'Narrative evidence decays with a 90-day half-life.',
  },
  smart_money: {
    title: 'Smart Money',
    description: 'Measures net open-market buying minus selling by officers and directors at full weight and 10%+ owners at half weight, scaled by company size.',
    interpretation: 'Higher means insiders are net buyers; lower means insiders are net sellers.',
    window: 'Trailing 90 days; not computable for foreign private issuers without Form 4 data.',
  },
  fundamental_health: {
    title: 'Fundamental Health',
    description: 'Combines revenue growth, gross-margin trend, free-cash-flow margin, net cash, and return on invested capital.',
    interpretation: 'Higher means operating quality and balance-sheet health are stronger relative to peers.',
    window: 'Latest point-in-time XBRL data; at least three of five inputs are required.',
  },
  valuation_brake: {
    title: 'Valuation Brake',
    description: 'Compares EV/Sales, EV/EBITDA, and free-cash-flow yield with comparable companies, oriented so cheaper is better.',
    interpretation: 'Higher means relatively inexpensive; lower means relatively expensive.',
    window: 'Latest price and point-in-time fundamentals.',
  },
} as const

const selectedFromHash = () => {
  const query = window.location.hash.split('?')[1] ?? ''
  return new URLSearchParams(query).get('security')
}
const evidenceFromHash = () => {
  const query = window.location.hash.split('?')[1] ?? ''
  return new URLSearchParams(query).get('evidence')
}

const PERCENT_RATIO_GATES = new Set(['coverage_min', 'cost_pct_max'])
const PERCENT_POINT_GATES = new Set(['resulting_weight_max', 'weight_gap_min', 'weight_max'])
const MONEY_GATES = new Set(['cash_after_trade_min', 'trade_min'])

const formatGateValue = (gate: GateTrace, value: GateTrace['actual']) => {
  if (value === null) return 'Not available'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (MONEY_GATES.has(gate.gate)) return formatMoney(String(value))
  if (PERCENT_RATIO_GATES.has(gate.gate)) return `${formatNumber(Number(value) * 100)}%`
  if (PERCENT_POINT_GATES.has(gate.gate)) return `${formatNumber(value)}%`
  return formatNumber(value)
}

const suggestedAction = (recommendation: Recommendation) => {
  if (
    recommendation.action.startsWith('HOLD')
    || !recommendation.suggested_trade_chf
    || Number(recommendation.suggested_trade_chf) <= 0
  ) return null
  const verb = recommendation.action === 'TRIM' || recommendation.action === 'SELL' ? 'SELL' : 'BUY'
  const quantity = recommendation.suggested_quantity
    ? `${formatNumber(recommendation.suggested_quantity)} ${Number(recommendation.suggested_quantity) === 1 ? 'stock' : 'stocks'}`
    : 'stocks'
  return `Suggested: ${verb} ${quantity} (${formatMoney(recommendation.suggested_trade_chf)})`
}

function PriceChart({ points }: { points: Array<{ date: string; close: string }> }) {
  if (points.length < 2) return <div className="empty">Fifteen-session price history is not available.</div>
  const values = points.map((point) => Number(point.close))
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const coordinates = values.map((value, index) => ({
    x: 18 + (index / (values.length - 1)) * 624,
    y: 20 + (1 - (value - min) / span) * 150,
  }))
  const path = coordinates.map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ')
  const rising = values.at(-1)! >= values[0]
  return (
    <div className="price-chart-wrap">
      <svg className={`price-chart ${rising ? 'up' : 'down'}`} viewBox="0 0 660 200" role="img" aria-label="Latest fifteen adjusted closing prices">
        <line x1="18" x2="642" y1="170" y2="170" className="axis" />
        <path d={path} fill="none" stroke="currentColor" strokeWidth="3" vectorEffect="non-scaling-stroke" />
        {coordinates.map((point, index) => <circle key={points[index].date} cx={point.x} cy={point.y} r="3" fill="currentColor" />)}
      </svg>
      <div className="price-observations">
        {points.map((point) => <div key={point.date}><strong>{formatMoney(point.close, 'USD')}</strong><span>{point.date.slice(5)}</span></div>)}
      </div>
    </div>
  )
}

export function Analysis() {
  const api = useApi()
  const requestedSecurity = useRef(selectedFromHash())
  const [securities, setSecurities] = useState<SecuritySummary[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [security, setSecurity] = useState<SecurityPackage | null>(null)
  const [recommendationHistory, setRecommendationHistory] = useState<Recommendation[]>([])
  const [filter, setFilter] = useState('')
  const [filingPage, setFilingPage] = useState(1)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    void api.getSecurities().then((items) => {
      setSecurities(items)
      const requested = requestedSecurity.current
      const matched = items.find((item) => item.security_id === requested || item.ticker === requested)
      setSelected(matched?.security_id ?? items[0]?.security_id ?? null)
    }).catch(setError)
  }, [api])

  useEffect(() => {
    if (!selected) return
    setSecurity(null)
    setFilingPage(1)
    void Promise.all([
      api.getSecurity(selected),
      api.getRecommendationHistory(selected),
    ]).then(([securityPackage, history]) => {
      setSecurity(securityPackage)
      setRecommendationHistory(history)
    }).catch(setError)
  }, [api, selected])

  useEffect(() => {
    if (!security) return
    const evidence = evidenceFromHash()
    if (!evidence) return
    const index = security.documents.findIndex((item) => item.document_id === evidence)
    if (index >= 0) setFilingPage(Math.floor(index / 3) + 1)
    window.setTimeout(() => document.getElementById(`evidence-${evidence}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 50)
  }, [security])

  const shown = useMemo(() => {
    const query = filter.trim().toLowerCase()
    if (!query) return securities
    return securities.filter((item) => item.ticker.toLowerCase().includes(query) || item.name.toLowerCase().includes(query))
  }, [filter, securities])

  const selectSecurity = (securityId: string) => {
    setSelected(securityId)
    window.history.replaceState(null, '', `#/analysis?security=${encodeURIComponent(securityId)}`)
  }

  if (error) return <ErrorBlock error={error} />
  const isUnheld = security?.recommendation
    && Number(security.recommendation.current_weight ?? 0) === 0
  const filingDocuments = security?.documents.filter((item) => item.form !== 'NEWS') ?? []
  const filingPageCount = Math.max(1, Math.ceil(filingDocuments.length / 3))
  const visibleFilings = filingDocuments.slice((filingPage - 1) * 3, filingPage * 3)

  return (
    <>
      <PageHeading eyebrow="Grounded research" title="Analysis" description="Select a company to understand its price, fundamentals, six Auspex legs, score, evidence, and portfolio action." />
      <div className="discussion-grid">
        <aside>
          <div className="search-box"><Search size={15} /><input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Ticker or company" aria-label="Find security" /></div>
          <div className="security-list">
            {shown.map((item) => (
              <button className={`security-option ${selected === item.security_id ? 'active' : ''}`} key={item.security_id} onClick={() => selectSecurity(item.security_id)}>
                <span>{item.ticker}</span><small>{item.percentile === null ? 'No score' : `P${item.percentile}`}</small>
              </button>
            ))}
          </div>
        </aside>
        <section>
          {!security ? <Loading label="Loading standard security analysis" /> : (
            <>
              <div className="security-overview">
                <div className="analysis-score-stack">
                  <div className="score-orb gold"><div><strong>{security.security.percentile ?? '—'}</strong><small>Auspex Score</small></div></div>
                  <span className={`readiness-badge ${security.recommendation?.buy_ready ? 'ready' : 'blocked'}`}>
                    {security.recommendation?.buy_ready ? 'Buy ready' : security.recommendation ? 'Not buy ready' : 'No action data'}
                  </span>
                </div>
                <div className="score-title">
                  <span className="eyebrow">{security.security.ticker} · {security.market}</span>
                  <h2>{security.security.name}</h2>
                  <p>{security.business_summary || 'A grounded company and research recap is not yet available.'}</p>
                </div>
                <div className="security-market-price">
                  <strong>{formatMoney(security.current_price_usd, 'USD')}</strong>
                  <span className={Number(security.price_change_pct ?? 0) >= 0 ? 'positive' : 'negative'}>
                    {Number(security.price_change_pct ?? 0) >= 0 ? '+' : ''}{Number(security.price_change_pct ?? 0).toFixed(2)}% vs prior session
                  </span>
                </div>
              </div>

              <Section title="Latest 15 sessions" description="Adjusted closing prices; each observation is labeled">
                <div className="chart-panel"><PriceChart points={security.price_history} /></div>
              </Section>

              <Section title="Main fundamentals" description="Latest point-in-time XBRL values">
                <div className="fundamentals-grid">
                  {security.fundamentals.map((metric) => <MetricTile key={metric.label} label={metric.label} value={metric.value ?? '—'} detail={metric.period_end ?? undefined} />)}
                </div>
              </Section>

              <Section title="How to read the six legs" description="Each leg is ranked against comparable companies; together they produce the 0–100 Auspex Score.">
                <div className="leg-explanation-grid">
                  {Object.entries(LEG_EXPLANATIONS).map(([name, explanation]) => (
                    <article className="leg-explanation-card" key={name}>
                      <span className="eyebrow">{explanation.title}</span>
                      <p>{explanation.description}</p>
                      <small>{explanation.interpretation}</small>
                      <small>{explanation.window}</small>
                    </article>
                  ))}
                </div>
              </Section>

              <Section title="Auspex" description="Composite 0–100 score and six independently ranked legs">
                <div className="auspex-reasoning">
                  <div><span className="eyebrow">Why this score</span><p>{security.score_reasoning}</p></div>
                  <div className="score-change-callout"><strong className="gold">{security.security.percentile ?? '—'}</strong><span className={(security.score_change ?? 0) >= 0 ? 'positive' : 'negative'}>{security.score_change === null ? 'No prior score' : `${security.score_change > 0 ? '+' : ''}${security.score_change} vs prior`}</span></div>
                </div>
                <div className="leg-score-grid">
                  {Object.entries(security.legs).map(([name, leg]) => (
                    <article className="leg-score-card" key={name}>
                      <label>{titleCase(name)}</label>
                      <strong className="gold">{leg.neutral ? 'Neutral' : leg.computable ? (leg.score ?? '—') : 'N/C'}</strong>
                      <small>z {formatNumber(leg.z)} · weight {formatNumber(Number(leg.weight) * 100, 0)}%</small>
                      {leg.status_explanation && <p className="leg-status">{leg.status_explanation}</p>}
                    </article>
                  ))}
                </div>
              </Section>

              {security.recommendation && !security.recommendation.action.startsWith('HOLD') && (
                <Section
                  title="Suggestion"
                  description={
                    security.recommendation.action.startsWith('HOLD')
                      ? 'No portfolio action is justified now. The details below explain why.'
                      : isUnheld
                        ? 'The score ranks the security; Buy Readiness and portfolio gates decide whether a purchase can be executed now.'
                        : 'What Auspex suggests, why, and how much.'
                  }
                >
                  <div className="suggestion-detail panel">
                    <ActionPill action={security.recommendation.action} />
                    <div>
                      <strong>{formatNumber(security.recommendation.current_weight ?? 0)}% → {formatNumber(security.recommendation.target_weight ?? 0)}%</strong>
                      <p>{security.recommendation.rationale}</p>
                      {security.recommendation.blocking_reasons.length > 0 && <div className="buy-blockers">{security.recommendation.blocking_reasons.map((reason) => <span key={reason}>{reason}</span>)}</div>}
                      {security.recommendation.blocking_reasons.length === 0 && <p className="action-summary positive">This action passed the policy branch that produced the recorded suggestion.</p>}
                      {suggestedAction(security.recommendation) && <small>{suggestedAction(security.recommendation)}{security.recommendation.estimated_cost_chf ? ` · estimated cost ${formatMoney(security.recommendation.estimated_cost_chf)}` : ''}</small>}
                    </div>
                  </div>
                  <details className="technical-details">
                    <summary>Technical policy gates</summary>
                    <div className="gate-list">
                      {security.recommendation.gate_trace.map((gate) => (
                        <div className={`gate ${gate.passed ? 'pass' : 'fail'}`} key={gate.gate}>
                          <strong>{titleCase(gate.gate)} · {gate.passed ? 'Passed' : 'Failed'}</strong>
                          <span>{formatGateValue(gate, gate.actual)} / {formatGateValue(gate, gate.threshold)}</span>
                        </div>
                      ))}
                    </div>
                  </details>
                </Section>
              )}

              <Section title="Recommendation history" description="Actionable BUY, ADD, SELL, and TRIM advice from the latest three calendar days" count={recommendationHistory.length}>
                <div className="recommendation-history">
                  {recommendationHistory.length === 0 && <div className="empty">No recommendation history is available for this ticker yet.</div>}
                  {recommendationHistory.slice(0, 10).map((item) => (
                    <article key={item.id}>
                      <time>{item.as_of_date}</time>
                      <ActionPill action={item.action} />
                      <span>{item.followed ? 'Followed' : 'Not followed'}</span>
                      <span>{item.outcome_mature ? 'Outcome mature' : `Pending · estimated after ${item.outcome_matures_on}`}</span>
                    </article>
                  ))}
                </div>
              </Section>

              <Section title="Latest news" description="Most recent retrieved company news" count={security.news.length}>
                <div className="panel">
                  {security.news.length === 0 && <div className="empty">No recent news is stored for this security.</div>}
                  {security.news.map((document) => (
                    <article className={`evidence-card ${evidenceFromHash() === document.document_id ? 'highlighted' : ''}`} id={`evidence-${document.document_id}`} key={document.document_id}>
                      <header><h3>{document.headline || 'Company news'}</h3><time>{document.filed_at}</time></header>
                      <p>{document.digest}</p>
                      <div className="evidence-meta"><span>{document.publisher}</span><span>{document.relevance_reason}</span><span>Retrieved {new Date(document.retrieved_at).toLocaleDateString()}</span>{document.stale && <span className="warning">Stale</span>}</div>
                      {document.source_url && <a href={document.source_url} target="_blank" rel="noreferrer">Open source</a>}
                    </article>
                  ))}
                </div>
              </Section>

              <Section title="Latest filings and evidence" description="Three consistent evidence records per page, newest first" count={filingDocuments.length}>
                <div className="panel">
                  {visibleFilings.map((document) => (
                    <article className={`evidence-card ${evidenceFromHash() === document.document_id ? 'highlighted' : ''}`} id={`evidence-${document.document_id}`} key={document.document_id}>
                      <header><h3>{document.headline || document.form}</h3><time>{document.form} · {document.filed_at}</time></header>
                      <p>{document.digest || 'No digest is available for this evidence record.'}</p>
                      <div className="evidence-meta"><span>{document.publisher}</span><span>{document.relevance_reason}</span><span>Retrieved {new Date(document.retrieved_at).toLocaleDateString()}</span>{document.stale && <span className="warning">Stale</span>}</div>
                      {document.source_url
                        ? <a href={document.source_url} target="_blank" rel="noreferrer">Open source</a>
                        : <span className="source-unavailable">Source unavailable</span>}
                    </article>
                  ))}
                </div>
                {filingPageCount > 1 && (
                  <div className="pagination" aria-label="Evidence pages">
                    <button className="button compact" type="button" disabled={filingPage === 1} onClick={() => setFilingPage((page) => page - 1)}>Previous</button>
                    <span>Page {filingPage} of {filingPageCount}</span>
                    <button className="button compact" type="button" disabled={filingPage === filingPageCount} onClick={() => setFilingPage((page) => page + 1)}>Next</button>
                  </div>
                )}
              </Section>
            </>
          )}
        </section>
      </div>
    </>
  )
}
