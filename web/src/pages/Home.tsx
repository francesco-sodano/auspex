import { ArrowDownRight, ArrowUpRight, MessageSquareText, ShieldAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { ActionPill, ErrorBlock, Loading, MetricTile, Section, formatMoney, formatNumber } from '../components/common'
import { useApi } from '../lib/api'
import type { Briefing, Recommendation, ScoreMover } from '../lib/types'

const suggestedAction = (recommendation: Recommendation) => {
  if (!recommendation.suggested_trade_chf) return 'Amount unavailable'
  const verb = recommendation.action === 'TRIM' || recommendation.action === 'SELL' ? 'SELL' : 'BUY'
  const quantity = recommendation.suggested_quantity
    ? `${formatNumber(recommendation.suggested_quantity)} ${Number(recommendation.suggested_quantity) === 1 ? 'stock' : 'stocks'}`
    : 'stocks'
  return `Suggested: ${verb} ${quantity} (${formatMoney(recommendation.suggested_trade_chf)})`
}

function Movers({ movers, direction }: { movers: ScoreMover[]; direction: 'up' | 'down' }) {
  const Icon = direction === 'up' ? ArrowUpRight : ArrowDownRight
  return (
    <div className="mover-column">
      <header>
        <div>
          <span className={`mover-direction ${direction}`}><Icon size={14} /> Movers {direction}</span>
          <p>Largest one-day Auspex Score changes</p>
        </div>
      </header>
      <div className="mover-list">
        {movers.length === 0 && <div className="empty">No score moved in this direction.</div>}
        {movers.map((mover) => (
          <article className="mover-card" key={mover.security_id}>
            <a className="identity identity-link" href={`#/analysis?security=${encodeURIComponent(mover.security_id)}`}>
              <span className="ticker">{mover.ticker}</span>
              <div><strong>{mover.company_name}</strong><small>Prior score {mover.prior_score}</small></div>
            </a>
            <div className="score-shift">
              <strong className="gold">{mover.score}</strong>
              <span className={direction === 'up' ? 'positive' : 'negative'}>
                {mover.score_change > 0 ? '+' : ''}{mover.score_change}
              </span>
            </div>
            {mover.narrative && <p>{mover.narrative}</p>}
            <small className={`buy-readiness ${mover.buy_ready ? 'ready' : 'blocked'}`}>
              {mover.buy_ready ? 'Buy ready' : mover.buy_blockers[0] ? `Buy candidate · ${mover.buy_blockers[0]}` : 'Research candidate'}
            </small>
          </article>
        ))}
      </div>
    </div>
  )
}

export function Home() {
  const api = useApi()
  const [briefing, setBriefing] = useState<Briefing | null>(null)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    void api.getBriefing().then(setBriefing).catch(setError)
  }, [api])

  if (error) return <ErrorBlock error={error} />
  if (!briefing) return <Loading label="Preparing the daily briefing" />

  const portfolio = briefing.portfolio
  const suggestions = briefing.recommendations
    .filter((recommendation) => !recommendation.action.startsWith('HOLD'))
    .slice(0, 5)
  const additionCount = suggestions.filter((item) => item.action === 'BUY' || item.action === 'ADD').length

  return (
    <>
      {briefing.run_status === 'DEGRADED' && (
        <div className="notice warning">
          <ShieldAlert size={14} /> Today&apos;s run is degraded. {briefing.assertion_failures.join(' · ')}
        </div>
      )}
      <section className="hero">
        <div>
          <span className="eyebrow">Daily briefing · {briefing.date}</span>
          <h1>What changed overnight.</h1>
          <p>Evidence-ranked movements, deterministic suggestions, and newly escalated risks across the full research universe.</p>
          <div className="hero-actions">
            <a className="button primary" href={`#/discussion?prompt=${encodeURIComponent('Explain today’s top movers and portfolio suggestions. Include the evidence, suggested quantities, and any blockers.')}`}><MessageSquareText size={14} /> Ask Auspex</a>
          </div>
        </div>
        <div className="question">
          <span className="eyebrow">Knowledge boundary</span>
          <p className="as-of">Sources retrieved through {briefing.max_knowledge_date}</p>
        </div>
      </section>

      <Section title="Portfolio at a Glance" description="Current market value, capital at work, and one-day movement">
        <div className="tile-grid five">
          <MetricTile label="Total portfolio value" value={portfolio ? formatMoney(portfolio.value_chf) : 'Ledger unavailable'} detail="Stocks at current price + cash (dividends included)" />
          <MetricTile label="Invested" value={portfolio ? formatMoney(portfolio.invested_chf) : '—'} detail="Open-position cost + cash − withdrawals − expenses" />
          <MetricTile label="Cash" value={portfolio ? formatMoney(portfolio.cash_chf) : '—'} detail="Includes received dividends" tone="gold" />
          <MetricTile label="Total gain" value={portfolio ? formatMoney(portfolio.total_gain_chf) : '—'} tone={portfolio && Number(portfolio.total_gain_chf) >= 0 ? 'positive' : 'negative'} detail="Portfolio value − invested" />
          <MetricTile label="Variation from yesterday" value={portfolio ? formatMoney(portfolio.day_change_chf) : '—'} tone={portfolio && Number(portfolio.day_change_chf) >= 0 ? 'positive' : 'negative'} />
        </div>
      </Section>

      <Section title="What changed today" description="Auspex Score is a 0–100 cross-sectional rank">
        <div className="movers-grid">
          <Movers movers={briefing.movers_up} direction="up" />
          <Movers movers={briefing.movers_down} direction="down" />
        </div>
      </Section>

      {briefing.escalated_risks.length > 0 && (
        <Section title="New high-severity risks" description="Surfaced independently of score movement" count={briefing.escalated_risks.length}>
          <div className="panel">
            {briefing.escalated_risks.map((risk) => (
              <article className="row" key={`${risk.security_id}-${risk.category}`}>
                <div className="identity"><span className="ticker">{risk.ticker}</span><div><strong>{risk.category}</strong><small>{risk.severity}</small></div></div>
                <div className="row-copy"><p>{risk.summary}</p></div>
                <ShieldAlert className="negative" size={19} />
              </article>
            ))}
          </div>
        </Section>
      )}

      <Section title="Daily Suggestions" description="Up to five portfolio actions with the strongest estimated benefit" count={suggestions.length}>
        <div className="suggestion-grid">
          {suggestions.length === 0 && <div className="empty">No portfolio action is justified today. Existing positions remain unchanged.</div>}
          {suggestions.length > 0 && additionCount === 0 && <div className="suggestion-note">No new BUY or ADD candidate cleared every active policy gate today.</div>}
          {suggestions.map((recommendation) => (
            <article className="suggestion-card" key={recommendation.id}>
              <a className="identity identity-link" href={`#/analysis?security=${encodeURIComponent(recommendation.security_id)}`}>
                <span className="ticker">{recommendation.ticker}</span>
                <div><strong>{recommendation.company_name}</strong><small>{formatNumber(recommendation.current_weight ?? 0)}% → target {formatNumber(recommendation.target_weight ?? 0)}%</small></div>
              </a>
              <p>{recommendation.rationale}</p>
              <div className="score-readiness">
                <span><strong>{recommendation.auspex_score ?? '—'}</strong><small>Auspex Score</small></span>
                <span className={recommendation.buy_ready ? 'ready' : 'blocked'}><strong>{recommendation.buy_ready ? 'Ready' : 'Blocked'}</strong><small>Buy Readiness</small></span>
              </div>
              <footer>
                <span>{suggestedAction(recommendation)}</span>
                <a className="action-link" href={`#/analysis?security=${encodeURIComponent(recommendation.security_id)}`}><ActionPill action={recommendation.action} /></a>
              </footer>
            </article>
          ))}
        </div>
      </Section>
    </>
  )
}
