import { useEffect, useState } from 'react'
import { ErrorBlock, Loading, MetricTile, PageHeading, formatNumber, titleCase } from '../components/common'
import { useApi } from '../lib/api'
import type { PerformanceReport } from '../lib/types'

const percentage = (value: string | null | undefined) => value === null || value === undefined ? '—' : `${(Number(value) * 100).toFixed(1)}%`
const correlationTone = (value: string | null) => {
  const parsed = Math.abs(Number(value ?? 0))
  return { opacity: Math.max(.16, Math.min(1, parsed)) }
}
const coefficient = (value: string | null | undefined) => value === null || value === undefined ? '—' : formatNumber(value, 3)

export function Performance() {
  const api = useApi()
  const [report, setReport] = useState<PerformanceReport | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [mode, setMode] = useState<'simple' | 'technical'>('simple')
  useEffect(() => { void api.getPerformance().then(setReport).catch(setError) }, [api])

  if (error) return <ErrorBlock error={error} />
  if (!report) return <Loading label="Measuring the model" />
  return (
    <>
      <PageHeading eyebrow="Self-measurement" title="Performance" description="This page measures whether Auspex scores and actions predicted later peer-relative USD returns. It does not measure your portfolio return." aside={<span className="as-of">Through {report.as_of_date}</span>} />
      <div className="view-toggle" role="group" aria-label="Performance detail level">
        <button type="button" className={mode === 'simple' ? 'active' : ''} onClick={() => setMode('simple')}>Simple</button>
        <button type="button" className={mode === 'technical' ? 'active' : ''} onClick={() => setMode('technical')}>Technical</button>
      </div>
      <section className="performance-explainer">
        <div><strong>Information coefficient</strong><p>Correlation from −1 to +1 between today&apos;s ranking and future returns. Positive is useful; zero has no relationship; negative means the ranking was backwards.</p></div>
        <div><strong>Suggestion outcome</strong><p>An actionable suggestion is evaluated after 126 sessions. A BUY/ADD succeeds by beating peers; a TRIM/SELL succeeds when the stock subsequently underperforms peers.</p></div>
        <div><strong>Your feedback</strong><p>Mark a transaction as following Auspex. Once 126 sessions pass, it enters the accepted outcome sample so Auspex can compare followed and overridden suggestions.</p></div>
      </section>
      <div className="tile-grid">
        <MetricTile label="Suggestion hit rate" value={percentage(report.suggestion_hit_rate)} detail="Action beat its peer-relative objective after 126 sessions" tone="gold" />
        <MetricTile label="Followed outcome" value={percentage(report.dispositions.accepted)} detail={`${report.dispositions.accepted_sample_size} mature attributed suggestions · 126 sessions required`} />
        <MetricTile label="Not followed outcome" value={percentage(report.dispositions.rejected)} detail={`${report.dispositions.rejected_sample_size} mature rejected suggestions · 126 sessions required`} />
        <MetricTile label="Evaluation sample" value={String(report.sample_size)} detail={`${report.backfilled_sample_size} backfilled rows`} />
      </div>
      <section className="attribution-status">
        <div><strong>{report.attribution.followed_pending}</strong><span>Followed · pending 126 sessions</span></div>
        <div><strong>{report.attribution.followed_mature}</strong><span>Followed · mature</span></div>
        <div><strong>{report.attribution.not_followed_pending}</strong><span>Not followed · pending</span></div>
        <div><strong>{report.attribution.not_followed_mature}</strong><span>Not followed · mature</span></div>
      </section>
      <div className="performance-grid">
        <section className="chart-panel">
          <h2>Composite information coefficient</h2>
          <p>Cross-sectional Spearman correlation with forward USD returns.</p>
          <div className="ic-grid">
            {(['21', '63', '126'] as const).map((horizon) => <div className="ic-cell" key={horizon}><strong>{coefficient(report.composite_ic[horizon])}</strong><small>{horizon} sessions</small></div>)}
          </div>
        </section>
        <section className="chart-panel">
          <h2>Per-leg IC</h2>
          <p>Contribution is earned, not assumed.</p>
          {Object.entries(report.leg_ic).map(([leg, value]) => {
            return <div className="performance-value-row" key={leg}><span>{titleCase(leg)}</span><strong>{coefficient(value)}</strong></div>
          })}
        </section>
        {mode === 'technical' && <section className="chart-panel">
          <h2>Leg correlation</h2>
          <p>High correlation reveals duplicated signals.</p>
          <div className="heatmap">
            {report.leg_correlation.values.map((row, rowIndex) => (
              <div className="heat-row" key={report.leg_correlation.labels[rowIndex]}>
                <span className="heat-label">{titleCase(report.leg_correlation.labels[rowIndex])}</span>
                {row.map((value, columnIndex) => <span style={correlationTone(value)} title={`${report.leg_correlation.labels[rowIndex]} / ${report.leg_correlation.labels[columnIndex]}`} key={columnIndex}>{coefficient(value)}</span>)}
              </div>
            ))}
          </div>
        </section>}
        {mode === 'technical' && <section className="chart-panel">
          <h2>Cohort dispersion</h2>
          <p>Ranking quality depends on differentiated peer returns.</p>
          {Object.entries(report.cohort_dispersion).map(([cohort, value]) => <div className="performance-value-row" key={cohort}><span>{titleCase(cohort)}</span><strong>{percentage(value)}</strong></div>)}
          {Object.keys(report.cohort_dispersion).length === 0 && <div className="empty">No mature cohort-dispersion sample is available yet.</div>}
        </section>}
      </div>
    </>
  )
}
