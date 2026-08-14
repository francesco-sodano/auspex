import { Pencil, Plus, Trash2, X } from 'lucide-react'
import { Fragment, useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { ErrorBlock, Loading, MetricTile, PageHeading, Section, formatMoney, formatNumber, titleCase } from '../components/common'
import { useApi } from '../lib/api'
import type {
  Portfolio,
  PortfolioTransaction,
  PortfolioTransactionInput,
  Recommendation,
  TransactionType,
} from '../lib/types'

const SECURITY_TYPES = new Set<TransactionType>(['OPENING_POSITION', 'BUY', 'SELL'])
const AMOUNT_TYPES = new Set<TransactionType>(['OPENING_CASH', 'DEPOSIT', 'WITHDRAWAL', 'DIVIDEND', 'INTEREST', 'FEE', 'TAX'])
const TRANSACTION_TYPES: TransactionType[] = ['BUY', 'SELL', 'DIVIDEND', 'DEPOSIT', 'WITHDRAWAL', 'FEE', 'TAX', 'INTEREST', 'OPENING_POSITION', 'OPENING_CASH']
const COST_COMPONENT_TYPES = new Set<TransactionType>(['OPENING_POSITION', 'BUY', 'SELL', 'DIVIDEND'])
type CostCategory = PortfolioTransactionInput['cost_components'][number]['category']
type CostComponentDraft = PortfolioTransactionInput['cost_components'][number] & { id: string }
const COST_CATEGORIES: Array<[CostCategory, string]> = [
  ['BROKER_COMMISSION', 'Broker commission'],
  ['TRANSACTION_TAX', 'Transaction / stamp duty'],
  ['WITHHOLDING_TAX', 'Withholding tax'],
  ['VAT', 'VAT'],
  ['CUSTODY_FEE', 'Custody fee'],
  ['ACCOUNT_FEE', 'Account fee'],
  ['OTHER_FEE', 'Other fee'],
]

const optionalMoney = (value: string | null, currency = 'CHF', field: string) => (
  value === null
    ? <span title={`${field} is not available in the current transactions`}>—</span>
    : formatMoney(value, currency)
)

function PriceBars({ points }: { points: Array<{ date: string; open: string; high: string; low: string; close: string }> }) {
  if (points.length < 2) return <span className="sparkline-empty">—</span>
  const min = Math.min(...points.map((point) => Number(point.low)))
  const max = Math.max(...points.map((point) => Number(point.high)))
  const span = max - min || 1
  const chartWidth = points.length * 100
  const y = (value: number) => 76 - ((value - min) / span) * 68
  const candleWidth = 34
  return (
    <div className="position-price-history">
      <svg className="price-bars" viewBox={`0 0 ${chartWidth} 84`} preserveAspectRatio="none" role="img" aria-label="Seven-session daily price bars">
        {points.map((point, index) => {
          const open = Number(point.open)
          const high = Number(point.high)
          const low = Number(point.low)
          const close = Number(point.close)
          const x = index * 100 + 50
          const bodyTop = Math.min(y(open), y(close))
          const bodyHeight = Math.max(1.5, Math.abs(y(open) - y(close)))
          const direction = close >= open ? 'up' : 'down'
          return (
            <g className={`price-bar ${direction}`} key={point.date}>
              <line x1={x} x2={x} y1={y(high)} y2={y(low)} />
              <rect x={x - candleWidth / 2} y={bodyTop} width={candleWidth} height={bodyHeight} />
            </g>
          )
        })}
      </svg>
      <div className="spark-values">{points.map((point) => <span key={point.date}><strong>{Number(point.close).toFixed(2)}</strong><small>{point.date.slice(5)}</small></span>)}</div>
    </div>
  )
}

const emptyInput = (): PortfolioTransactionInput => ({
  client_request_id: crypto.randomUUID(),
  transaction_type: 'BUY',
  event_date: new Date().toISOString().slice(0, 10),
  currency: 'USD',
  security_code: '',
  quantity: '',
  price: '',
  amount: '',
  fees: '0',
  cost_components: [],
  fx_rate_to_base: '',
  followed_auspex: false,
  recommendation_id: null,
  notes: '',
})

const normalizeCostCategory = (category: string, transactionType: TransactionType): CostCategory => {
  if (category === 'STAMP_DUTY') return 'TRANSACTION_TAX'
  if (category === 'TAX') return transactionType === 'DIVIDEND' ? 'WITHHOLDING_TAX' : 'TRANSACTION_TAX'
  if (COST_CATEGORIES.some(([value]) => value === category)) return category as CostCategory
  return 'OTHER_FEE'
}

function TransactionEditor({ transaction, portfolio, recommendations, onClose, onSaved }: {
  transaction: PortfolioTransaction | null
  portfolio: Portfolio
  recommendations: Recommendation[]
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const api = useApi()
  const [input, setInput] = useState<PortfolioTransactionInput>(() => transaction ? {
    client_request_id: crypto.randomUUID(),
    transaction_type: transaction.transaction_type === 'VOID' ? 'FEE' : transaction.transaction_type,
    event_date: transaction.event_date,
    currency: transaction.currency,
    security_code: transaction.security_code ?? '',
    quantity: transaction.quantity ?? '',
    price: transaction.price ?? '',
    amount: AMOUNT_TYPES.has(transaction.transaction_type as TransactionType) ? transaction.gross_amount : '',
    fees: '0',
    cost_components: [],
    fx_rate_to_base: transaction.fx_rate_to_base ?? '',
    followed_auspex: transaction.followed_auspex,
    recommendation_id: transaction.recommendation_id,
    notes: transaction.notes ?? '',
  } : emptyInput())
  const [costComponents, setCostComponents] = useState<CostComponentDraft[]>(() => (
    transaction?.cost_components.map((component) => ({
      id: crypto.randomUUID(),
      category: normalizeCostCategory(
        component.category,
        transaction.transaction_type === 'VOID' ? 'FEE' : transaction.transaction_type,
      ),
      amount: component.source_amount ?? component.amount,
      currency: (component.source_currency ?? component.currency) as 'CHF' | 'USD',
    })) ?? []
  ))
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const securityFields = SECURITY_TYPES.has(input.transaction_type)
  const amountField = AMOUNT_TYPES.has(input.transaction_type)
  const needsTicker = securityFields || input.transaction_type === 'DIVIDEND'
  const acceptsCosts = COST_COMPONENT_TYPES.has(input.transaction_type)
  const needsFx = input.currency === 'USD' || (acceptsCosts && costComponents.some((component) => component.currency === 'USD'))
  const matchingRecommendation = recommendations.find((recommendation) => {
    if (recommendation.ticker !== input.security_code) return false
    if (input.transaction_type === 'BUY') return recommendation.action === 'BUY' || recommendation.action === 'ADD'
    if (input.transaction_type === 'SELL') return recommendation.action === 'SELL' || recommendation.action === 'TRIM'
    return false
  })
  const attributedRecommendation = recommendations.find((item) => item.id === input.recommendation_id) ?? matchingRecommendation
  const position = portfolio.positions.find((item) => item.ticker === input.security_code)
  const fx = Number(input.fx_rate_to_base || (input.currency === 'CHF' ? 1 : 0))
  const quantity = Number(input.quantity || 0)
  const price = Number(input.price || 0)
  const sourceGross = securityFields
    ? quantity * price
    : Number(input.amount || 0)
  const grossChf = sourceGross * (input.currency === 'CHF' ? 1 : fx)
  const costChf = costComponents.reduce((total, component) => {
    const amount = Number(component.amount || 0)
    return total + amount * (component.currency === 'CHF' ? 1 : fx)
  }, 0)
  const currentQuantity = Number(position?.quantity ?? 0)
  const resultingQuantity = input.transaction_type === 'BUY'
    ? currentQuantity + quantity
    : input.transaction_type === 'SELL'
      ? currentQuantity - quantity
      : currentQuantity
  const cashChange = input.transaction_type === 'BUY'
    ? -(grossChf + costChf)
    : input.transaction_type === 'SELL'
      ? grossChf - costChf
      : input.transaction_type === 'WITHDRAWAL'
        || input.transaction_type === 'FEE'
        || input.transaction_type === 'TAX'
        ? -grossChf
        : input.transaction_type === 'OPENING_POSITION'
          ? 0
          : grossChf - costChf
  const resultingCash = Number(portfolio.cash_chf) + cashChange
  const currentUnitValueChf = position && Number(position.quantity) > 0
    ? Number(position.market_value_chf ?? 0) / Number(position.quantity)
    : price * (input.currency === 'CHF' ? 1 : fx)
  const resultingPositionValue = Math.max(0, resultingQuantity) * currentUnitValueChf
  const resultingWeight = Number(portfolio.total_value_chf) > 0
    ? resultingPositionValue / Number(portfolio.total_value_chf) * 100
    : 0

  const set = (field: keyof PortfolioTransactionInput, value: string) => {
    setInput((current) => ({ ...current, [field]: value }))
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setSaving(true)
    setError(null)
    try {
      const payload = {
        ...input,
        security_code: needsTicker ? input.security_code || null : null,
        quantity: securityFields ? input.quantity || null : null,
        price: securityFields ? input.price || null : null,
        amount: amountField ? input.amount || null : null,
        cost_components: acceptsCosts
          ? costComponents.map(({ category, amount, currency }) => ({ category, amount, currency }))
          : [],
        fx_rate_to_base: input.fx_rate_to_base || null,
        followed_auspex: input.followed_auspex,
        recommendation_id: input.followed_auspex
          ? input.recommendation_id ?? attributedRecommendation?.id ?? null
          : null,
        notes: input.notes || null,
      }
      if (transaction) await api.updatePortfolioTransaction(transaction.transaction_id, payload)
      else await api.createPortfolioTransaction(payload)
      await onSaved()
      onClose()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The transaction could not be saved.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="transaction-modal" role="dialog" aria-modal="true" aria-label={transaction ? 'Edit transaction' : 'Add transaction'}>
        <header>
          <div><span className="eyebrow">{transaction ? 'Append-only correction' : 'New ledger event'}</span><h2>{transaction ? 'Edit transaction' : 'Add transaction'}</h2></div>
          <button className="icon-button" onClick={onClose} aria-label="Close"><X size={17} /></button>
        </header>
        <form onSubmit={submit}>
          <div className="form-grid">
            <label className="field"><span>Type</span><select value={input.transaction_type} onChange={(event) => set('transaction_type', event.target.value)}>{TRANSACTION_TYPES.map((type) => <option key={type} value={type}>{titleCase(type)}</option>)}</select></label>
            <label className="field"><span>Date</span><input type="date" required value={input.event_date} onChange={(event) => set('event_date', event.target.value)} /></label>
            <label className="field"><span>Currency</span><select value={input.currency} onChange={(event) => set('currency', event.target.value)}><option>CHF</option><option>USD</option></select></label>
            {needsTicker && <label className="field"><span>Ticker</span><input required value={input.security_code ?? ''} onChange={(event) => set('security_code', event.target.value.toUpperCase())} placeholder="NVDA" /></label>}
            {securityFields && <label className="field"><span>Quantity</span><input inputMode="decimal" required value={input.quantity ?? ''} onChange={(event) => set('quantity', event.target.value)} /></label>}
            {securityFields && <label className="field"><span>Price per share</span><input inputMode="decimal" required value={input.price ?? ''} onChange={(event) => set('price', event.target.value)} /></label>}
            {amountField && <label className="field"><span>Amount</span><input inputMode="decimal" required value={input.amount ?? ''} onChange={(event) => set('amount', event.target.value)} /></label>}
            {acceptsCosts && (
              <fieldset className="transaction-costs full">
                <legend>Fees and taxes</legend>
                {costComponents.map((component) => (
                  <div className="transaction-cost-row" key={component.id}>
                    <label className="field"><span>Type</span><select value={component.category} onChange={(event) => setCostComponents((current) => current.map((item) => item.id === component.id ? { ...item, category: event.target.value as CostCategory } : item))}>{COST_CATEGORIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                    <label className="field"><span>Amount</span><input type="number" min="0.01" step="0.01" required value={component.amount} onChange={(event) => setCostComponents((current) => current.map((item) => item.id === component.id ? { ...item, amount: event.target.value } : item))} /></label>
                    <label className="field"><span>Currency</span><select value={component.currency} onChange={(event) => setCostComponents((current) => current.map((item) => item.id === component.id ? { ...item, currency: event.target.value as 'CHF' | 'USD' } : item))}><option>CHF</option><option>USD</option></select></label>
                    <button className="icon-button danger" type="button" onClick={() => setCostComponents((current) => current.filter((item) => item.id !== component.id))} aria-label="Remove fee"><Trash2 size={14} /></button>
                  </div>
                ))}
                <button className="button compact" type="button" onClick={() => setCostComponents((current) => [...current, { id: crypto.randomUUID(), category: input.transaction_type === 'DIVIDEND' ? 'WITHHOLDING_TAX' : 'BROKER_COMMISSION', amount: '', currency: input.currency as 'CHF' | 'USD' }])} disabled={costComponents.length >= 20}><Plus size={14} /> Add fee or tax</button>
                {input.transaction_type === 'OPENING_POSITION' && costComponents.length > 0 && <small>Historical acquisition costs are recorded without debiting today&apos;s cash.</small>}
              </fieldset>
            )}
            <label className="field"><span>USD → CHF FX rate</span><input inputMode="decimal" required={needsFx} value={input.fx_rate_to_base ?? ''} onChange={(event) => set('fx_rate_to_base', event.target.value)} placeholder={needsFx ? 'Required once for this transaction' : 'Not required for CHF-only records'} /><small>Used for the transaction and every USD fee.</small></label>
            {(securityFields || input.followed_auspex) && (
              <label className="transaction-attribution full">
                <input
                  type="checkbox"
                  checked={input.followed_auspex}
                  disabled={!attributedRecommendation && !input.recommendation_id}
                  onChange={(event) => setInput((current) => ({
                    ...current,
                    followed_auspex: event.target.checked,
                    recommendation_id: event.target.checked
                      ? current.recommendation_id ?? attributedRecommendation?.id ?? null
                      : null,
                  }))}
                />
                <span>
                  <strong>I followed an Auspex suggestion</strong>
                  <small>
                    {attributedRecommendation
                      ? `Links this ${titleCase(input.transaction_type)} to the ${titleCase(attributedRecommendation.action)} suggestion for later outcome measurement.`
                      : input.recommendation_id
                        ? 'This transaction remains linked to its recorded Auspex suggestion.'
                        : 'No matching current BUY/ADD/TRIM/SELL suggestion is available for this ticker.'}
                  </small>
                </span>
              </label>
            )}
            <label className="field full"><span>Notes</span><input value={input.notes ?? ''} onChange={(event) => set('notes', event.target.value)} placeholder="Optional note" /></label>
          </div>
          {((securityFields && quantity > 0 && price > 0) || (amountField && sourceGross > 0)) && (
            <div className="transaction-preview">
              <span className="eyebrow">Before you save</span>
              <div>
                <span><small>Resulting quantity</small><strong>{securityFields ? formatNumber(resultingQuantity, 4) : '—'}</strong></span>
                <span><small>Estimated weight</small><strong>{securityFields ? `${formatNumber(resultingWeight)}%` : '—'}</strong></span>
                <span><small>Cash after transaction</small><strong className={resultingCash >= 0 ? '' : 'negative'}>{formatMoney(String(resultingCash))}</strong></span>
                <span><small>Fees and taxes</small><strong>{formatMoney(String(costChf))}</strong></span>
              </div>
              {input.followed_auspex && attributedRecommendation && <p>Attributed to {titleCase(attributedRecommendation.action)} suggestion dated {attributedRecommendation.id.split(':').at(-1)}.</p>}
            </div>
          )}
          {error && <div className="notice danger">{error}</div>}
          <footer><button type="button" className="button" onClick={onClose}>Cancel</button><button type="submit" className="button primary" disabled={saving}>{saving ? 'Saving…' : transaction ? 'Save correction' : 'Add transaction'}</button></footer>
        </form>
      </section>
    </div>
  )
}

export function PortfolioPage() {
  const api = useApi()
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null)
  const [transactions, setTransactions] = useState<PortfolioTransaction[]>([])
  const [recommendations, setRecommendations] = useState<Recommendation[]>([])
  const [transactionPage, setTransactionPage] = useState(1)
  const [editing, setEditing] = useState<PortfolioTransaction | null | undefined>(undefined)
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    const [projection, ledger, briefing] = await Promise.all([
      api.getPortfolio(),
      api.getPortfolioTransactions(),
      api.getBriefing(),
    ])
    setPortfolio(projection)
    setTransactions(ledger)
    setRecommendations(briefing.recommendations)
  }, [api])

  useEffect(() => { void load().catch(setError) }, [load])

  const voidTransaction = async (transaction: PortfolioTransaction) => {
    if (!window.confirm(`Remove ${titleCase(transaction.transaction_type)} from ${transaction.event_date}? The audit trail will be preserved.`)) return
    try {
      await api.deletePortfolioTransaction(transaction.transaction_id, crypto.randomUUID())
      await load()
    } catch (caught) {
      setError(caught)
    }
  }

  const effectiveTransactions = useMemo(() => transactions.filter((transaction) => transaction.status === 'EFFECTIVE'), [transactions])
  const transactionPageCount = Math.max(1, Math.ceil(effectiveTransactions.length / 5))
  const visibleTransactions = useMemo(
    () => effectiveTransactions.slice((transactionPage - 1) * 5, transactionPage * 5),
    [effectiveTransactions, transactionPage],
  )
  const growthPct = Number(portfolio?.invested_chf ?? 0) === 0
    ? null
    : Number(portfolio?.total_gain_chf ?? 0) / Number(portfolio?.invested_chf ?? 1) * 100

  useEffect(() => {
    setTransactionPage((current) => Math.min(current, transactionPageCount))
  }, [transactionPageCount])

  if (error) return <ErrorBlock error={error} />
  if (!portfolio) return <Loading label="Reading current transactions" />
  return (
    <>
      <PageHeading
        eyebrow="Owner portfolio"
        title="Portfolio"
        description="Current positions, seven-day market context, and the audited transaction ledger."
        aside={<button className="button primary" onClick={() => setEditing(null)}><Plus size={14} /> Add transaction</button>}
      />

      <Section title="Portfolio at a Glance" description={`Projected ${portfolio.as_of_date}`}>
        <div className="tile-grid eight">
          <MetricTile label="Total portfolio value" value={formatMoney(portfolio.total_value_chf)} />
          <MetricTile label="Invested" value={formatMoney(portfolio.invested_chf)} />
          <MetricTile label="Cash" value={formatMoney(portfolio.cash_chf)} tone="gold" />
          <MetricTile label="Total gain" value={formatMoney(portfolio.total_gain_chf)} tone={Number(portfolio.total_gain_chf) >= 0 ? 'positive' : 'negative'} />
          <MetricTile label="Variation from yesterday" value={formatMoney(portfolio.day_change_chf)} tone={Number(portfolio.day_change_chf) >= 0 ? 'positive' : 'negative'} />
          <MetricTile label="Commissions & taxes" value={formatMoney(portfolio.expenses_chf)} />
          <MetricTile label="Dividends" value={formatMoney(portfolio.dividends_chf)} tone="positive" />
          <MetricTile label="Growth on invested" value={growthPct === null ? '—' : `${growthPct.toFixed(2)}%`} tone={growthPct !== null && growthPct >= 0 ? 'positive' : 'negative'} />
        </div>
      </Section>

      <Section title="Current portfolio" description="Last seven sessions, current value, gain and Auspex Score" count={portfolio.positions.length}>
        <div className="position-card-grid">
          {portfolio.positions.map((position) => (
            <article className={`position-card ${position.unrealised_chf === null ? '' : Number(position.unrealised_chf) >= 0 ? 'gain-positive' : 'gain-negative'}`} key={position.ticker}>
              <div className="position-identity">
                <a className="position-security-link" href={`#/analysis?security=${encodeURIComponent(position.ticker)}`} aria-label={`Analyze ${position.ticker}`}>
                  <span className="ticker">{position.ticker}</span>
                  <div>
                    <strong>{position.company_name}</strong>
                    <span>{position.ticker}</span>
                    <small>{position.holding_period_days === null ? 'Held period unavailable' : `Held ${position.holding_period_days} days`}</small>
                    <small className={`position-readiness ${position.buy_ready ? 'positive' : position.action === 'HOLD_INSUFFICIENT_DATA' ? 'warning' : ''}`}>
                      {position.buy_ready
                        ? 'Buy ready'
                        : position.action === 'HOLD_NO_ACTION'
                          ? 'Held · no action'
                          : position.action === 'HOLD_INSUFFICIENT_DATA'
                            ? 'Insufficient data'
                            : position.action
                              ? titleCase(position.action)
                              : 'No action data'}
                    </small>
                    {position.readiness_reason && <small className="position-readiness-reason">{position.readiness_reason}</small>}
                  </div>
                </a>
                <div className="position-score">
                  <strong>{position.auspex_score ?? '—'}</strong>
                  <span>Auspex Score</span>
                </div>
              </div>
              <PriceBars points={position.price_history} />
              <div className="position-metrics">
                <div><span>Quantity</span><strong>{position.quantity}</strong></div>
                <div><span>Weight</span><strong>{position.weight === null ? '—' : `${(Number(position.weight) * 100).toFixed(1)}%`}</strong></div>
                <div><span>Value USD</span><strong>{optionalMoney(position.market_value_usd, 'USD', 'latest_price')}</strong></div>
                <div><span>Value CHF</span><strong>{optionalMoney(position.market_value_chf, 'CHF', 'latest_price')}</strong></div>
                <div><span>Unrealised USD</span><strong className={position.unrealised_usd !== null && Number(position.unrealised_usd) >= 0 ? 'positive' : 'negative'}>{optionalMoney(position.unrealised_usd, 'USD', 'cost_basis_usd')}</strong></div>
                <div><span>Unrealised CHF</span><strong className={position.unrealised_chf !== null && Number(position.unrealised_chf) >= 0 ? 'positive' : 'negative'}>{optionalMoney(position.unrealised_chf, 'CHF', 'cost_basis_chf')}</strong></div>
              </div>
            </article>
          ))}
        </div>
      </Section>

      <Section title="Current Transactions" description="Live owner records; edits and removals preserve the audit history" count={effectiveTransactions.length}>
        <div className="table-wrap transactions-table">
          <table>
            <thead><tr><th>Date</th><th>Type & details</th><th>Security</th><th className="align-right">Quantity</th><th className="align-right">Price</th><th className="align-right">Cash effect</th><th>Costs & FX</th><th>Notes</th><th /></tr></thead>
            <tbody>{visibleTransactions.map((transaction) => (
              <Fragment key={transaction.transaction_id}>
                <tr className="transaction-parent-row">
                  <td>{transaction.event_date}</td>
                  <td><span className="status-pill">{titleCase(transaction.transaction_type)}</span><small>{transaction.currency}{transaction.followed_auspex ? ' · Followed Auspex' : ''}</small></td>
                  <td>{transaction.security_code ?? '—'}</td>
                  <td className="align-right">{transaction.quantity ?? '—'}</td>
                  <td className="align-right">{transaction.price ? formatMoney(transaction.price, transaction.currency) : '—'}</td>
                  <td className={`align-right ${Number(transaction.cash_amount) >= 0 ? 'positive' : 'negative'}`}>{formatMoney(transaction.cash_amount, transaction.cash_currency)}</td>
                  <td><span>USD → CHF: {transaction.fx_rate_to_base ? Number(transaction.fx_rate_to_base).toFixed(4) : '—'}</span></td>
                  <td>{transaction.notes ?? '—'}</td>
                  <td><div className="table-actions"><button className="icon-button" onClick={() => setEditing(transaction)} aria-label="Edit transaction"><Pencil size={14} /></button><button className="icon-button danger" onClick={() => void voidTransaction(transaction)} aria-label="Remove transaction"><Trash2 size={14} /></button></div></td>
                </tr>
                {transaction.cost_components.map((component, index) => (
                  <tr className="transaction-child-row" key={`${transaction.transaction_id}-${component.category}-${component.amount}-${index}`}>
                    <td />
                    <td colSpan={2}><span className="child-connector">↳</span> {titleCase(component.category)}</td>
                    <td colSpan={2}>{component.currency}</td>
                    <td className="align-right negative">{formatMoney(component.amount, component.currency)}</td>
                    <td>{component.currency === 'USD' ? 'Uses transaction FX' : 'CHF'}</td>
                    <td colSpan={2}>Child cost of {transaction.security_code ?? titleCase(transaction.transaction_type)}</td>
                  </tr>
                ))}
              </Fragment>
            ))}</tbody>
          </table>
        </div>
        <div className="pagination" aria-label="Transaction pages">
          <button className="button compact" type="button" disabled={transactionPage === 1} onClick={() => setTransactionPage((page) => page - 1)}>Previous</button>
          <span>Page {transactionPage} of {transactionPageCount}</span>
          <button className="button compact" type="button" disabled={transactionPage === transactionPageCount} onClick={() => setTransactionPage((page) => page + 1)}>Next</button>
        </div>
      </Section>

      {editing !== undefined && <TransactionEditor transaction={editing} portfolio={portfolio} recommendations={recommendations} onClose={() => setEditing(undefined)} onSaved={load} />}
    </>
  )
}
