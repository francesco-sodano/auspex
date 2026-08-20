import { CheckCircle2, ShieldCheck, Trash2 } from 'lucide-react'
import { type FormEvent, useEffect, useState } from 'react'
import { ErrorBlock, Loading, PageHeading, formatMoney } from '../components/common'
import { useApi } from '../lib/api'
import type {
  AccountConfiguration,
  InvestmentHorizon,
  AccountDeletionStatus,
  RiskProfile,
  UserSettings,
  UserSettingsInput,
} from '../lib/types'

const PROFILE_DEFAULTS: Record<RiskProfile, string> = {
  CONSERVATIVE: '5000',
  MODERATE: '3000',
  AGGRESSIVE: '1000',
}

const normalizeHorizon = (value: string): InvestmentHorizon => ({
  SHORT_TERM: 'ONE_TO_THREE_YEARS',
  MEDIUM_TERM: 'THREE_TO_SEVEN_YEARS',
  LONG_TERM: 'OVER_SEVEN_YEARS',
}[value] as InvestmentHorizon | undefined) ?? value as InvestmentHorizon

const PROFILES: Array<{ id: RiskProfile; title: string; description: string; thresholds: string }> = [
  {
    id: 'CONSERVATIVE',
    title: 'Conservative',
    description: 'Higher evidence standards, smaller positions, more cash retained, and earlier risk reduction.',
    thresholds: 'BUY ≥85 · ADD ≥80 · max position 10% · default reserve CHF 5,000',
  },
  {
    id: 'MODERATE',
    title: 'Moderate',
    description: 'Balanced evidence, concentration, liquidity, and transaction-size requirements.',
    thresholds: 'BUY ≥75 · ADD ≥70 · max position 15% · default reserve CHF 3,000',
  },
  {
    id: 'AGGRESSIVE',
    title: 'Aggressive',
    description: 'Accepts more valuation and evidence uncertainty, larger positions, and a smaller liquidity buffer.',
    thresholds: 'BUY ≥65 · ADD ≥60 · max position 20% · default reserve CHF 1,000',
  },
]

const ACKNOWLEDGEMENTS: Array<{ field: keyof UserSettingsInput; label: string }> = [
  { field: 'directional_only_acknowledged', label: 'I understand Auspex provides directional decision support only.' },
  { field: 'no_guarantee_acknowledged', label: 'I understand no score, suggestion, or model output guarantees any result.' },
  { field: 'not_financial_advice_acknowledged', label: 'I understand Auspex is not financial, legal, tax, or regulated investment advice and does not execute trades.' },
  { field: 'market_loss_acknowledged', label: 'I understand investing can result in partial or total loss of capital.' },
  { field: 'independent_decision_acknowledged', label: 'I remain responsible for independently verifying information and making every investment decision.' },
]

export function Account() {
  const api = useApi()
  const [settings, setSettings] = useState<UserSettings | null>(null)
  const [configuration, setConfiguration] = useState<AccountConfiguration | null>(null)
  const [draft, setDraft] = useState<UserSettingsInput | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [actionError, setActionError] = useState<unknown>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [deletionOpen, setDeletionOpen] = useState(false)
  const [deletionConfirmation, setDeletionConfirmation] = useState('')
  const [deletionStatus, setDeletionStatus] = useState<AccountDeletionStatus | null>(null)

  useEffect(() => {
    void Promise.all([
      api.getUserSettings(),
      api.getAccountConfiguration(),
    ]).then(([value, config]) => {
      setSettings(value)
      setConfiguration(config)
      setDraft({
        risk_profile: value.risk_profile,
        cash_reserve_chf: value.cash_reserve_chf,
        investment_horizon: normalizeHorizon(value.investment_horizon),
        investment_objective: value.investment_objective,
        directional_only_acknowledged: value.directional_only_acknowledged,
        no_guarantee_acknowledged: value.no_guarantee_acknowledged,
        not_financial_advice_acknowledged: value.not_financial_advice_acknowledged,
        market_loss_acknowledged: value.market_loss_acknowledged,
        independent_decision_acknowledged: value.independent_decision_acknowledged,
      })
    }).catch(setLoadError)
  }, [api])

  useEffect(() => {
    if (!deletionStatus || !['PENDING', 'RUNNING'].includes(deletionStatus.status)) return
    const timer = window.setInterval(() => {
      void api.getDeletionStatus().then((status) => {
        setDeletionStatus(status)
        if (status.status === 'COMPLETED') window.location.reload()
      }).catch(setActionError)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [api, deletionStatus])

  if (loadError) return <ErrorBlock error={loadError} />
  if (!settings || !draft || !configuration) return <Loading label="Loading account settings" />

  const setProfile = (profile: RiskProfile) => {
    setDraft((current) => current && ({
      ...current,
      risk_profile: profile,
      cash_reserve_chf: current.risk_profile === profile
        ? current.cash_reserve_chf
        : PROFILE_DEFAULTS[profile],
    }))
    setSaved(false)
  }
  const acknowledged = ACKNOWLEDGEMENTS.every(({ field }) => draft[field] === true)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setSaving(true)
    setActionError(null)
    try {
      const updated = await api.updateUserSettings(draft)
      setSettings(updated)
      setSaved(true)
    } catch (cause) {
      setActionError(cause)
    } finally {
      setSaving(false)
    }
  }
  const requestDeletion = async () => {
    if (deletionConfirmation.trim() !== 'DELETE MY ACCOUNT') return
    setSaving(true)
    setActionError(null)
    try {
      const status = await api.deleteAccount(deletionConfirmation)
      setDeletionStatus(status)
      if (status.status === 'COMPLETED') window.location.reload()
    } catch (cause) {
      setActionError(cause)
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <PageHeading eyebrow="Personal controls" title="Account" description="Choose how cautiously Auspex converts evidence into portfolio actions and record the required decision-support acknowledgements." />
      {actionError && <div className="notice danger"><ErrorBlock error={actionError} /></div>}
      <form className="account-settings" onSubmit={submit}>
        <section>
          <header><h2>Risk profile</h2><p>This changes policy gates used by the next nightly recommendation run; it never changes the six-leg score itself.</p></header>
          <div className="risk-profile-grid">
            {PROFILES.map((profile) => (
              <button className={`risk-profile-card ${draft.risk_profile === profile.id ? 'selected' : ''}`} type="button" key={profile.id} onClick={() => setProfile(profile.id)}>
                <strong>{profile.title}</strong><p>{profile.description}</p><small>{profile.thresholds}</small>
              </button>
            ))}
          </div>
          <label className="field cash-reserve-field">
            <span>Cash reserve after a BUY / ADD</span>
            <input type="number" min="0" max="50000" step="100" value={draft.cash_reserve_chf} onChange={(event) => setDraft({ ...draft, cash_reserve_chf: event.target.value })} />
            <small>Current requirement: {formatMoney(draft.cash_reserve_chf)}. This remains available for fees, withdrawals, or future opportunities.</small>
          </label>
          <div className="profile-select-grid">
            <label className="field">
              <span>Investment horizon</span>
              <select value={draft.investment_horizon} onChange={(event) => setDraft({ ...draft, investment_horizon: event.target.value as UserSettingsInput['investment_horizon'] })}>
                <option value="SIX_MONTHS">6 months</option>
                <option value="ONE_YEAR">1 year</option>
                <option value="ONE_TO_THREE_YEARS">1–3 years</option>
                <option value="THREE_TO_SEVEN_YEARS">3–7 years</option>
                <option value="OVER_SEVEN_YEARS">More than 7 years</option>
              </select>
            </label>
            <label className="field">
              <span>Investment objective</span>
              <select value={draft.investment_objective} onChange={(event) => setDraft({ ...draft, investment_objective: event.target.value as UserSettingsInput['investment_objective'] })}>
                <option value="CAPITAL_PRESERVATION">Capital preservation</option>
                <option value="INCOME">Income</option>
                <option value="BALANCED_GROWTH">Balanced growth</option>
                <option value="CAPITAL_GROWTH">Capital growth</option>
              </select>
            </label>
          </div>
        </section>
        <section>
          <header><h2>Auspex research scope</h2><p>The themes and cohorts below define the current research universe. They are versioned system configuration and cannot be changed from Account.</p></header>
          <div className="scope-columns">
            <div>
              <span className="eyebrow">Included themes · {configuration.themes.length}</span>
              <div className="read-only-tags">{configuration.themes.map((theme) => <span key={theme.id}>{theme.label}</span>)}</div>
            </div>
            <div>
              <span className="eyebrow">Current cohorts · {configuration.cohorts.length}</span>
              <div className="cohort-scope-list">{configuration.cohorts.map((cohort) => <details key={cohort.id}><summary>{cohort.id.replaceAll('-', ' ')} <small>{cohort.tickers.length} securities</small></summary><p>{cohort.tickers.join(' · ')}</p></details>)}</div>
            </div>
          </div>
        </section>
        <section>
          <header><h2>Decision-support acknowledgements</h2><p>These acknowledgements are recorded with version {settings.acknowledgement_version}. They do not waive statutory rights or replace professional advice.</p></header>
          <div className="acknowledgement-list">
            {ACKNOWLEDGEMENTS.map(({ field, label }) => (
              <label key={field}>
                <input type="checkbox" checked={draft[field] === true} onChange={(event) => setDraft({ ...draft, [field]: event.target.checked })} />
                <span>{label}</span>
              </label>
            ))}
          </div>
        </section>
        <footer>
          <span>{settings.acknowledged_at ? <><ShieldCheck size={14} /> Last acknowledged {new Date(settings.acknowledged_at).toLocaleString()}</> : 'Acknowledgements have not been saved.'}</span>
          {saved && <span className="positive"><CheckCircle2 size={14} /> Saved. The next nightly run will use this profile.</span>}
          <button className="button primary" disabled={!acknowledged || saving}>{saving ? 'Saving…' : 'Save account settings'}</button>
        </footer>
      </form>
      <section className="danger-zone">
        <header><h2>Delete Auspex account</h2><p>Permanently removes your portfolio events, settings, recommendations, dispositions, projections, private attribution, onboarding state, and conversations. Shared market research and your Microsoft identity are not deleted.</p></header>
        {!deletionOpen ? (
          <button className="button danger" type="button" onClick={() => setDeletionOpen(true)}><Trash2 size={14} /> Delete my account and data</button>
        ) : (
          <div className="deletion-confirmation">
            <label className="field">
              <span>Type DELETE MY ACCOUNT to confirm</span>
              <input value={deletionConfirmation} onChange={(event) => setDeletionConfirmation(event.target.value)} />
            </label>
            <div>
              <button className="button" type="button" onClick={() => { setDeletionOpen(false); setDeletionConfirmation('') }}>Cancel</button>
              <button className="button danger" type="button" disabled={saving || deletionConfirmation.trim() !== 'DELETE MY ACCOUNT'} onClick={() => void requestDeletion()}><Trash2 size={14} /> Permanently delete</button>
            </div>
          </div>
        )}
        {deletionStatus && <div className="notice warning">Deletion {deletionStatus.status.toLowerCase()} · {deletionStatus.deleted_items} items removed · {deletionStatus.remaining_items} remaining</div>}
      </section>
    </>
  )
}
