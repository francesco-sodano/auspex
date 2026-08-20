import {
  Check,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Plus,
  ShieldCheck,
  Trash2,
  UserCheck,
  UserCog,
  UserX,
} from 'lucide-react'
import { type FormEvent, useCallback, useEffect, useState } from 'react'
import { ErrorBlock, Loading, PageHeading } from '../components/common'
import { useApi } from '../lib/api'
import type {
  AdminUser,
  InitialPortfolioInput,
  OpeningPositionInput,
  RegistrationInput,
  RiskProfile,
  UserLifecycleStatus,
  UserSession,
} from '../lib/types'

const defaultRegistration = (displayName: string): RegistrationInput => ({
  display_name: displayName,
  risk_profile: 'MODERATE',
  cash_reserve_chf: '3000',
  investment_horizon: 'ONE_TO_THREE_YEARS',
  investment_objective: 'CAPITAL_GROWTH',
  directional_only_acknowledged: false,
  no_guarantee_acknowledged: false,
  not_financial_advice_acknowledged: false,
  market_loss_acknowledged: false,
  independent_decision_acknowledged: false,
})

const profileCopy: Record<RiskProfile, string> = {
  CONSERVATIVE: 'Higher evidence standards, smaller positions, and a larger liquidity reserve.',
  MODERATE: 'Balances evidence quality, concentration, costs, and portfolio liquidity.',
  AGGRESSIVE: 'Accepts more uncertainty, larger positions, and a smaller liquidity reserve.',
}

const acknowledgements: Array<{ key: keyof RegistrationInput; label: string }> = [
  { key: 'directional_only_acknowledged', label: 'Auspex provides directional decision support only.' },
  { key: 'no_guarantee_acknowledged', label: 'No score or suggestion guarantees an investment result.' },
  { key: 'not_financial_advice_acknowledged', label: 'Auspex is not regulated financial, legal, or tax advice and does not execute trades.' },
  { key: 'market_loss_acknowledged', label: 'Investing can result in partial or total loss of capital.' },
  { key: 'independent_decision_acknowledged', label: 'I remain responsible for verifying information and making every decision.' },
]

export function Registration({ displayName, onRegistered, onSignOut }: {
  displayName: string
  onRegistered: (session: UserSession) => void
  onSignOut: () => Promise<void>
}) {
  const api = useApi()
  const [step, setStep] = useState(0)
  const [draft, setDraft] = useState<RegistrationInput>(() => defaultRegistration(displayName))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const allAcknowledged = acknowledgements.every(({ key }) => draft[key] === true)

  const submit = async () => {
    setSaving(true)
    setError(null)
    try {
      onRegistered(await api.register(draft))
    } catch (cause) {
      setError(cause)
    } finally {
      setSaving(false)
    }
  }
  const submitForm = (event: FormEvent) => {
    event.preventDefault()
    if (step === 2 && allAcknowledged) void submit()
  }

  return (
    <main className="lifecycle-shell">
      <form className="lifecycle-panel" onSubmit={submitForm}>
        <span className="eyebrow">Create an Auspex account · Step {step + 1} of 3</span>
        <h1>{step === 0 ? 'Set your decision profile.' : step === 1 ? 'Define your time horizon.' : 'Confirm the regulated-use boundary.'}</h1>
        <div className="lifecycle-progress" role="progressbar" aria-valuemin={1} aria-valuemax={3} aria-valuenow={step + 1}><span style={{ width: `${((step + 1) / 3) * 100}%` }} /></div>
        {error && <ErrorBlock error={error} />}

        {step === 0 && (
          <div className="lifecycle-step">
            <label className="field">
              <span>Display name</span>
              <input required value={draft.display_name} onChange={(event) => setDraft({ ...draft, display_name: event.target.value })} />
            </label>
            <div className="risk-profile-grid">
              {(Object.keys(profileCopy) as RiskProfile[]).map((profile) => (
                <button
                  type="button"
                  className={`risk-profile-card ${draft.risk_profile === profile ? 'selected' : ''}`}
                  key={profile}
                  aria-pressed={draft.risk_profile === profile}
                  onClick={() => setDraft({ ...draft, risk_profile: profile })}
                >
                  <strong>{profile.charAt(0) + profile.slice(1).toLowerCase()}</strong>
                  <p>{profileCopy[profile]}</p>
                </button>
              ))}
            </div>
            <label className="field cash-reserve-field">
              <span>CHF cash reserve after BUY / ADD</span>
              <input type="number" min="0" max="50000" step="100" required value={draft.cash_reserve_chf} onChange={(event) => setDraft({ ...draft, cash_reserve_chf: event.target.value })} />
            </label>
          </div>
        )}

        {step === 1 && (
          <div className="lifecycle-step profile-select-grid">
            <label className="field">
              <span>Investment horizon</span>
              <select value={draft.investment_horizon} onChange={(event) => setDraft({ ...draft, investment_horizon: event.target.value as RegistrationInput['investment_horizon'] })}>
                <option value="SIX_MONTHS">6 months</option>
                <option value="ONE_YEAR">1 year</option>
                <option value="ONE_TO_THREE_YEARS">1–3 years</option>
                <option value="THREE_TO_SEVEN_YEARS">3–7 years</option>
                <option value="OVER_SEVEN_YEARS">More than 7 years</option>
              </select>
            </label>
            <label className="field">
              <span>Investment objective</span>
              <select value={draft.investment_objective} onChange={(event) => setDraft({ ...draft, investment_objective: event.target.value as RegistrationInput['investment_objective'] })}>
                <option value="CAPITAL_PRESERVATION">Capital preservation</option>
                <option value="INCOME">Income</option>
                <option value="BALANCED_GROWTH">Balanced growth</option>
                <option value="CAPITAL_GROWTH">Capital growth</option>
              </select>
            </label>
            <p className="lifecycle-note">Themes and cohorts are governed Auspex research configuration. They are visible after approval but are not personal account choices.</p>
          </div>
        )}

        {step === 2 && (
          <div className="lifecycle-step acknowledgement-list">
            {acknowledgements.map(({ key, label }) => (
              <label key={key}>
                <input
                  type="checkbox"
                  checked={draft[key] === true}
                  onChange={(event) => setDraft({ ...draft, [key]: event.target.checked })}
                />
                <span>{label}</span>
              </label>
            ))}
          </div>
        )}

        <footer className="lifecycle-actions">
          <button className="button" type="button" disabled={saving} onClick={() => void onSignOut()}>Sign out</button>
          <button className="button" type="button" disabled={step === 0 || saving} onClick={() => setStep((current) => current - 1)}>
            <ChevronLeft size={14} /> Back
          </button>
          {step < 2 ? (
            <button className="button primary" type="button" disabled={step === 0 && !draft.display_name.trim()} onClick={() => setStep((current) => current + 1)}>
              Continue <ChevronRight size={14} />
            </button>
          ) : (
            <button className="button primary" type="submit" disabled={!allAcknowledged || saving}>
              <ShieldCheck size={14} /> {saving ? 'Submitting…' : 'Request approval'}
            </button>
          )}
        </footer>
      </form>
    </main>
  )
}

const statusCopy: Record<Exclude<UserLifecycleStatus, 'UNREGISTERED' | 'APPROVED_NEEDS_ONBOARDING' | 'ACTIVE'>, { title: string; body: string }> = {
  PENDING_APPROVAL: {
    title: 'Your account is awaiting approval.',
    body: 'An Auspex administrator must approve this registration before portfolio data or research suggestions become available.',
  },
  REJECTED: {
    title: 'This registration was not approved.',
    body: 'Contact an Auspex administrator if you believe this decision should be reviewed.',
  },
  SUSPENDED: {
    title: 'This account is suspended.',
    body: 'Your private data remains isolated, but application access is paused until an administrator restores it.',
  },
  DELETION_PENDING: {
    title: 'Your data is being deleted.',
    body: 'Access is blocked while Auspex removes every private portfolio, conversation, setting, projection, recommendation, and attribution record.',
  },
  DELETED: {
    title: 'Your Auspex account has been deleted.',
    body: 'The application account and private data have been removed. Your Microsoft identity was not deleted.',
  },
}

export function ApprovalStatus({ session, onRefresh, onSignOut }: {
  session: UserSession
  onRefresh: (session: UserSession) => void
  onSignOut: () => Promise<void>
}) {
  const api = useApi()
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const copy = statusCopy[session.status as keyof typeof statusCopy]
  const refresh = async () => {
    setChecking(true)
    setError(null)
    try {
      if (session.status === 'DELETION_PENDING') {
        const deletion = await api.getDeletionStatus()
        if (deletion.status === 'COMPLETED') window.location.reload()
      } else {
        onRefresh(await api.getRegistrationStatus())
      }
    } catch (cause) {
      setError(cause)
    } finally {
      setChecking(false)
    }
  }
  return (
    <main className="lifecycle-shell">
      <section className="lifecycle-panel status-panel">
        <Clock3 className="status-icon" size={36} />
        <span className="eyebrow">{session.status.replaceAll('_', ' ')}</span>
        <h1>{copy?.title ?? 'Account access is not available.'}</h1>
        <p>{copy?.body}</p>
        {error && <ErrorBlock error={error} />}
        <div className="lifecycle-actions">
          <button className="button primary" type="button" disabled={checking} onClick={() => void refresh()}>
            {checking ? 'Checking…' : 'Check status'}
          </button>
          <button className="button" type="button" onClick={() => void onSignOut()}>Sign out</button>
        </div>
      </section>
    </main>
  )
}

type OpeningPositionDraft = OpeningPositionInput & { id: string }

const localDate = () => {
  const now = new Date()
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000).toISOString().slice(0, 10)
}

const emptyPosition = (): OpeningPositionDraft => ({
  id: crypto.randomUUID(),
  ticker: '',
  quantity: '',
  price: '',
  currency: 'USD',
  fx_rate_to_base: null,
  acquisition_date: localDate(),
})

export function InitialPortfolio({ onComplete, onSignOut }: {
  onComplete: (session: UserSession) => void
  onSignOut: () => Promise<void>
}) {
  const api = useApi()
  const [requestId] = useState(() => crypto.randomUUID())
  const [cash, setCash] = useState('')
  const [positions, setPositions] = useState<OpeningPositionDraft[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const rowHasValue = (item: OpeningPositionDraft) => Boolean(item.ticker.trim() || item.quantity || item.price || item.fx_rate_to_base)
  const rowIsValid = (item: OpeningPositionDraft) => Boolean(
    item.ticker.trim()
    && Number(item.quantity) > 0
    && Number(item.price) > 0
    && (item.currency === 'CHF' || Number(item.fx_rate_to_base) > 0)
  )
  const enteredPositions = positions.filter(rowHasValue)
  const allRowsValid = enteredPositions.every(rowIsValid)
  const valid = allRowsValid && (Number(cash || 0) > 0 || enteredPositions.length > 0)

  const updatePosition = (index: number, update: Partial<OpeningPositionInput>) => {
    setPositions((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, ...update } : item))
  }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!valid) return
    setSaving(true)
    setError(null)
    try {
      const payload: InitialPortfolioInput = {
        client_request_id: requestId,
        opening_cash_chf: cash || '0',
        positions: enteredPositions.map(({ id: _id, ...position }) => ({
          ...position,
          ticker: position.ticker.trim().toUpperCase(),
          fx_rate_to_base: position.currency === 'USD' ? position.fx_rate_to_base : null,
        })),
      }
      await api.initializePortfolio(payload)
      onComplete(await api.completeOnboarding())
    } catch (cause) {
      setError(cause)
    } finally {
      setSaving(false)
    }
  }

  return (
    <main className="lifecycle-shell">
      <form className="lifecycle-panel onboarding-panel" onSubmit={submit}>
        <span className="eyebrow">Initial portfolio</span>
        <h1>Give Auspex a real starting point.</h1>
        <p>Add CHF cash, one or more stock positions, or both. Auspex cannot produce portfolio-aware policy without at least one opening item.</p>
        {error && <ErrorBlock error={error} />}
        <label className="field">
          <span>Opening CHF cash</span>
          <input type="number" min="0" step="0.01" value={cash} onChange={(event) => setCash(event.target.value)} placeholder="0.00" />
        </label>
        <div className="opening-positions">
          <header>
            <div><strong>Opening stock positions</strong><p>Use the original acquisition price and date when known.</p></div>
            <button className="button compact" type="button" onClick={() => setPositions((current) => [...current, emptyPosition()])}><Plus size={13} /> Add position</button>
          </header>
          {positions.map((position, index) => (
            <div className={`opening-position ${rowHasValue(position) && !rowIsValid(position) ? 'invalid' : ''}`} key={position.id}>
              <label className="field"><span>Ticker</span><input value={position.ticker} onChange={(event) => updatePosition(index, { ticker: event.target.value.toUpperCase() })} /></label>
              <label className="field"><span>Quantity</span><input type="number" min="0" step="0.000001" value={position.quantity} onChange={(event) => updatePosition(index, { quantity: event.target.value })} /></label>
              <label className="field"><span>Acquisition price</span><input type="number" min="0" step="0.000001" value={position.price} onChange={(event) => updatePosition(index, { price: event.target.value })} /></label>
              <label className="field"><span>Currency</span><select value={position.currency} onChange={(event) => updatePosition(index, { currency: event.target.value as 'CHF' | 'USD' })}><option value="USD">USD</option><option value="CHF">CHF</option></select></label>
              <label className="field"><span>USD→CHF FX</span><input disabled={position.currency === 'CHF'} required={position.currency === 'USD'} type="number" min="0.000001" step="0.000001" value={position.fx_rate_to_base ?? ''} onChange={(event) => updatePosition(index, { fx_rate_to_base: event.target.value || null })} /></label>
              <label className="field"><span>Acquisition date</span><input type="date" max={localDate()} value={position.acquisition_date} onChange={(event) => updatePosition(index, { acquisition_date: event.target.value })} /></label>
              <button className="icon-button danger" type="button" aria-label={`Remove ${position.ticker || 'position'}`} onClick={() => setPositions((current) => current.filter((_, itemIndex) => itemIndex !== index))}><Trash2 size={14} /></button>
            </div>
          ))}
          {positions.length === 0 && <div className="empty">No opening positions. Cash alone is a valid starting portfolio.</div>}
        </div>
        <footer className="lifecycle-actions">
          <button className="button" type="button" onClick={() => void onSignOut()}>Sign out</button>
          <span className={valid ? 'positive' : 'negative'}>{!allRowsValid ? 'Complete or remove every position row' : valid ? 'Portfolio requirement satisfied' : 'Add positive CHF cash or at least one position'}</span>
          <button className="button primary" disabled={!valid || saving} type="submit"><Check size={14} /> {saving ? 'Creating…' : 'Create portfolio'}</button>
        </footer>
      </form>
    </main>
  )
}

export function AdminPanel({ session }: { session: UserSession }) {
  const api = useApi()
  const [users, setUsers] = useState<AdminUser[] | null>(null)
  const [filter, setFilter] = useState<UserLifecycleStatus | ''>('PENDING_APPROVAL')
  const [busy, setBusy] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [actionError, setActionError] = useState<unknown>(null)
  const load = useCallback(async () => {
    setLoadError(null)
    try {
      setUsers(await api.listAdminUsers(filter || undefined))
    } catch (cause) {
      setLoadError(cause)
    }
  }, [api, filter])
  useEffect(() => { void load() }, [load])

  const act = async (id: string, operation: () => Promise<AdminUser>) => {
    setBusy(id)
    setActionError(null)
    try {
      await operation()
      await load()
    } catch (cause) {
      setActionError(cause)
    } finally {
      setBusy(null)
    }
  }
  if (loadError) return <ErrorBlock error={loadError} />
  if (!users) return <Loading label="Loading user administration" />
  return (
    <>
      <PageHeading eyebrow="Access governance" title="Admin" description="Approve registrations and manage application roles. Admin access never grants access to another user’s private portfolio or conversations." />
      <div className="admin-toolbar">
        <label className="field"><span>Status</span><select value={filter} onChange={(event) => setFilter(event.target.value as UserLifecycleStatus | '')}><option value="">All users</option><option value="PENDING_APPROVAL">Pending approval</option><option value="ACTIVE">Active</option><option value="SUSPENDED">Suspended</option><option value="REJECTED">Rejected</option></select></label>
        <span>{users.length} users</span>
      </div>
      <div className="admin-user-list">
        {actionError && <div className="admin-action-error"><ErrorBlock error={actionError} /></div>}
        {users.length === 0 && <div className="empty">No users match this status.</div>}
        {users.map((user) => (
          <article className="admin-user" key={user.user_id}>
            <div>
              <span className="eyebrow">{user.role} · {user.status.replaceAll('_', ' ')}{user.user_id === session.user_id ? ' · You' : ''}</span>
              <strong>{user.display_name || user.email}</strong>
              <small>{user.email} · Registered {user.created_at?.slice(0, 10) ?? 'date unavailable'}</small>
            </div>
            <div className="admin-user-actions">
              {user.status === 'PENDING_APPROVAL' && <>
                <button className="button compact primary" disabled={busy === user.user_id} onClick={() => void act(user.user_id, () => api.approveUser(user.user_id))}><UserCheck size={13} /> Approve</button>
                <button className="button compact danger" disabled={busy === user.user_id} onClick={() => { if (window.confirm(`Reject ${user.email}?`)) void act(user.user_id, () => api.rejectUser(user.user_id)) }}><UserX size={13} /> Reject</button>
              </>}
              {user.status === 'ACTIVE' && <button className="button compact" disabled={busy === user.user_id || user.user_id === session.user_id} onClick={() => { if (window.confirm(`Suspend ${user.email}?`)) void act(user.user_id, () => api.suspendUser(user.user_id)) }}><UserX size={13} /> Suspend</button>}
              {(user.status === 'SUSPENDED' || user.status === 'REJECTED') && <button className="button compact primary" disabled={busy === user.user_id} onClick={() => void act(user.user_id, () => api.reinstateUser(user.user_id))}><UserCheck size={13} /> {user.status === 'REJECTED' ? 'Return to pending' : 'Reinstate'}</button>}
              {(user.status === 'ACTIVE' || user.status === 'APPROVED_NEEDS_ONBOARDING') && (
                <button className="button compact" disabled={busy === user.user_id || user.user_id === session.user_id} onClick={() => void act(user.user_id, () => api.updateUserRole(user.user_id, user.role === 'ADMIN' ? 'USER' : 'ADMIN'))}>
                  <UserCog size={13} /> Make {user.role === 'ADMIN' ? 'user' : 'admin'}
                </button>
              )}
            </div>
          </article>
        ))}
      </div>
    </>
  )
}
