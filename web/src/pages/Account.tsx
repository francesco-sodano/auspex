import { CheckCircle2, ShieldCheck } from 'lucide-react'
import { type FormEvent, useEffect, useState } from 'react'
import { ErrorBlock, Loading, PageHeading, formatMoney } from '../components/common'
import { useApi } from '../lib/api'
import type {
  AccountConfiguration,
  RiskProfile,
  UserSettings,
  UserSettingsInput,
} from '../lib/types'

const PROFILE_DEFAULTS: Record<RiskProfile, string> = {
  CONSERVATIVE: '5000',
  MODERATE: '3000',
  AGGRESSIVE: '1000',
}

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
  const [error, setError] = useState<unknown>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

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
        investment_horizon: value.investment_horizon,
        investment_objective: value.investment_objective,
        directional_only_acknowledged: value.directional_only_acknowledged,
        no_guarantee_acknowledged: value.no_guarantee_acknowledged,
        not_financial_advice_acknowledged: value.not_financial_advice_acknowledged,
        market_loss_acknowledged: value.market_loss_acknowledged,
        independent_decision_acknowledged: value.independent_decision_acknowledged,
      })
    }).catch(setError)
  }, [api])

  if (error) return <ErrorBlock error={error} />
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
    setError(null)
    try {
      const updated = await api.updateUserSettings(draft)
      setSettings(updated)
      setSaved(true)
    } catch (cause) {
      setError(cause)
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <PageHeading eyebrow="Owner controls" title="Account" description="Choose how cautiously Auspex converts evidence into portfolio actions and record the required decision-support acknowledgements." />
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
                <option value="SHORT_TERM">Short term · up to 3 years</option>
                <option value="MEDIUM_TERM">Medium term · 3–7 years</option>
                <option value="LONG_TERM">Long term · more than 7 years</option>
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
    </>
  )
}
