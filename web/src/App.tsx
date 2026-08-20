import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  BookOpenText,
  ChartNoAxesCombined,
  LogOut,
  Menu,
  MessageSquareText,
  Search,
  Shield,
  Settings,
  WalletCards,
  X,
} from 'lucide-react'
import { AuthProvider, useAuth } from './auth'
import { ErrorBlock, Loading } from './components/common'
import { ApiProvider, useApi } from './lib/api'
import { AdminPanel, ApprovalStatus, InitialPortfolio, Registration } from './pages/Lifecycle'
import type { UserSession } from './lib/types'
import './App.css'

const Account = lazy(() => import('./pages/Account').then((module) => ({ default: module.Account })))
const Analysis = lazy(() => import('./pages/Analysis').then((module) => ({ default: module.Analysis })))
const Discussion = lazy(() => import('./pages/Discussion').then((module) => ({ default: module.Discussion })))
const Home = lazy(() => import('./pages/Home').then((module) => ({ default: module.Home })))
const Performance = lazy(() => import('./pages/Performance').then((module) => ({ default: module.Performance })))
const PortfolioPage = lazy(() => import('./pages/Portfolio').then((module) => ({ default: module.PortfolioPage })))

export type Page = 'home' | 'analysis' | 'discussion' | 'portfolio' | 'performance' | 'account' | 'admin'

const pageFromHash = (): Page => {
  const page = window.location.hash.replace('#/', '').split('?')[0]
  return ['home', 'analysis', 'discussion', 'portfolio', 'performance', 'account', 'admin'].includes(page)
    ? page as Page
    : 'home'
}

function Brand() {
  return (
    <a className="brand" href="#/home" aria-label="Auspex home">
      <svg className="brand-mark" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M12 2.4c2.3 2.7 3.1 4.6 3.1 6.1a3.1 3.1 0 1 1-6.2 0c0-1.1.5-2.1 1.4-3.1.1.9.6 1.5 1.2 1.9-.4-1.7.1-3.4.5-4.9Z" fill="currentColor" />
        <path d="M8.4 12.3h7.2M9.4 12.6h5.2l-1.2 8.8h-2.8l-1.2-8.8Z" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className="brand-word">AUS<span>P</span>EX</span>
    </a>
  )
}

const navigation = [
  { page: 'home' as const, label: 'Home', icon: BookOpenText },
  { page: 'analysis' as const, label: 'Analysis', icon: Search },
  { page: 'discussion' as const, label: 'Discussion', icon: MessageSquareText },
  { page: 'portfolio' as const, label: 'Portfolio', icon: WalletCards },
  { page: 'performance' as const, label: 'Performance', icon: ChartNoAxesCombined },
  { page: 'account' as const, label: 'Account', icon: Settings },
]

function Login() {
  const { signIn, error } = useAuth()
  return (
    <main className="login-shell">
      <div className="constellation" aria-hidden="true" />
      <section className="login-panel">
        <Brand />
        <p className="brand-tag">Read the signs.</p>
        <h1>Evidence before conviction.</h1>
        <p>Auspex reads the market overnight, scores every company deterministically, and presents the evidence for you to decide.</p>
        {error && <div className="notice danger" role="alert">{error}</div>}
        <button className="button primary" onClick={signIn}>Continue with Microsoft</button>
        <small>Admin-approved access · Decision support only · No trade execution</small>
      </section>
    </main>
  )
}

function Workspace({ session }: { session: UserSession }) {
  const auth = useAuth()
  const [page, setPage] = useState<Page>(pageFromHash)
  const [mobileOpen, setMobileOpen] = useState(false)

  useEffect(() => {
    const onHashChange = () => {
      setPage(pageFromHash())
      setMobileOpen(false)
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const content = useMemo(() => {
    switch (page) {
      case 'analysis': return <Analysis />
      case 'discussion': return <Discussion />
      case 'portfolio': return <PortfolioPage />
      case 'performance': return <Performance />
      case 'account': return <Account />
      case 'admin': return session.role === 'ADMIN'
        ? <AdminPanel session={session} />
        : <ErrorBlock error={new Error('Administrator access is required.')} />
      default: return <Home />
    }
  }, [page, session])
  const visibleNavigation = session.role === 'ADMIN'
    ? [...navigation, { page: 'admin' as const, label: 'Admin', icon: Shield }]
    : navigation

  return (
    <div className="app-shell">
        <header className="topbar">
          <Brand />
          <button
            className="menu-button"
            aria-label={mobileOpen ? 'Close navigation' : 'Open navigation'}
            onClick={() => setMobileOpen((open) => !open)}
          >
            {mobileOpen ? <X size={19} /> : <Menu size={19} />}
          </button>
          <nav className={mobileOpen ? 'open' : ''} aria-label="Primary navigation">
            {visibleNavigation.map(({ page: target, label, icon: Icon }) => (
              <a key={target} href={`#/${target}`} aria-current={page === target ? 'page' : undefined}>
                <Icon size={15} aria-hidden="true" />
                {label}
              </a>
            ))}
          </nav>
          <div className="session">
            <span className="live-dot"><Activity size={12} /> Live</span>
            <span className="account">{auth.accountName}</span>
            <button className="icon-button" onClick={auth.signOut} aria-label="Sign out">
              <LogOut size={16} />
            </button>
          </div>
        </header>
        <main className="workspace">
          <Suspense fallback={<Loading label="Loading workspace" />}>{content}</Suspense>
        </main>
        <footer>
          <span>Auspex is research support, not financial advice.</span>
          <span>AI reads · Code decides · AI explains · You act</span>
        </footer>
    </div>
  )
}

function LifecycleRouter() {
  const auth = useAuth()
  const api = useApi()
  const [session, setSession] = useState<UserSession | null>(null)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    let active = true
    const refresh = () => {
      void api.getSession().then((value) => {
        if (active) {
          setSession(value)
          setError(null)
        }
      }).catch((cause) => {
        if (active) setError(cause)
      })
    }
    refresh()
    window.addEventListener('focus', refresh)
    return () => {
      active = false
      window.removeEventListener('focus', refresh)
    }
  }, [api])

  if (error) return <main className="lifecycle-shell"><section className="lifecycle-panel"><ErrorBlock error={error} /><button className="button" onClick={() => void auth.signOut()}>Sign out</button></section></main>
  if (!session) return <main className="loading-shell"><Brand /><span className="auspex-spinner" aria-label="Loading account" /></main>
  if (session.status === 'UNREGISTERED') return <Registration displayName={auth.accountName} onRegistered={setSession} onSignOut={auth.signOut} />
  if (session.status === 'APPROVED_NEEDS_ONBOARDING') return <InitialPortfolio onComplete={setSession} onSignOut={auth.signOut} />
  if (session.status !== 'ACTIVE') return <ApprovalStatus session={session} onRefresh={setSession} onSignOut={auth.signOut} />
  if (!session.onboarding_completed) return <ErrorBlock error={new Error('This account is active but onboarding is incomplete. Contact an administrator.')} />
  return <Workspace session={session} />
}

function AuthenticatedApp() {
  const auth = useAuth()
  const { ready, account } = auth
  if (!ready) {
    return <main className="loading-shell"><Brand /><span className="auspex-spinner" aria-label="Loading Auspex" /></main>
  }
  return account
    ? <ApiProvider key={account.homeAccountId} getToken={auth.getToken}><LifecycleRouter /></ApiProvider>
    : <Login />
}

export default function App() {
  return <AuthProvider><AuthenticatedApp /></AuthProvider>
}
