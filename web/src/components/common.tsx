/* oxlint-disable react/only-export-components -- Formatting helpers are colocated with the small display primitives that consume them. */
import type { PropsWithChildren, ReactNode } from 'react'

export function PageHeading({ eyebrow, title, description, aside }: {
  eyebrow: string
  title: string
  description: string
  aside?: ReactNode
}) {
  return (
    <header className="page-heading">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {aside}
    </header>
  )
}

export function Section({ title, description, count, children }: PropsWithChildren<{
  title: string
  description?: string
  count?: string | number
}>) {
  return (
    <section className="section">
      <header className="section-heading">
        <div><h2>{title}</h2>{description && <p>{description}</p>}</div>
        {count !== undefined && <span className="section-count">{count}</span>}
      </header>
      {children}
    </section>
  )
}

export function Loading({ label = 'Reading the signs' }: { label?: string }) {
  return (
    <div className="loading-block" role="status">
      <svg className="loading-brand-mark" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M12 2.4c2.3 2.7 3.1 4.6 3.1 6.1a3.1 3.1 0 1 1-6.2 0c0-1.1.5-2.1 1.4-3.1.1.9.6 1.5 1.2 1.9-.4-1.7.1-3.4.5-4.9Z" fill="currentColor" />
        <path d="M8.4 12.3h7.2M9.4 12.6h5.2l-1.2 8.8h-2.8l-1.2-8.8Z" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <strong>AUSPEX</strong>
      <span className="auspex-spinner" />
      <small>{label}</small>
    </div>
  )
}

export function ErrorBlock({ error }: { error: unknown }) {
  return <div className="error-block" role="alert"><strong>Auspex could not load this view.</strong><span>{error instanceof Error ? error.message : 'The requested data could not be loaded.'}</span></div>
}

export function MetricTile({ label, value, detail, tone }: {
  label: string
  value: string
  detail?: string
  tone?: 'positive' | 'negative' | 'gold'
}) {
  return <div className="metric-tile"><label>{label}</label><strong className={tone}>{value}</strong>{detail && <small>{detail}</small>}</div>
}

export const formatMoney = (amount: string | null | undefined, currency = 'CHF') => {
  if (amount === null || amount === undefined || amount === '') return '—'
  const parsed = Number(amount)
  if (!Number.isFinite(parsed)) return `${currency} ${amount}`
  return new Intl.NumberFormat('en-CH', {
    style: 'currency',
    currency,
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  }).format(parsed)
}

export const formatNumber = (
  value: string | number | null | undefined,
  maximumFractionDigits = 2,
) => {
  if (value === null || value === undefined || value === '') return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return String(value)
  return new Intl.NumberFormat('en-CH', { maximumFractionDigits }).format(parsed)
}

export const titleCase = (value: string) => value.toLowerCase().replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())

export function ActionPill({ action }: { action: string }) {
  const label = action === 'HOLD_NO_ACTION'
    ? 'No action'
    : action === 'HOLD_INSUFFICIENT_DATA'
      ? 'Insufficient data'
      : titleCase(action)
  return <span className={`action-pill ${action.toLowerCase().replaceAll('_', '-')}`}>{label}</span>
}
