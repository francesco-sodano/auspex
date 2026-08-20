/* oxlint-disable react/only-export-components -- Provider and its required hook intentionally share one module. */
import {
  PublicClientApplication,
  InteractionRequiredAuthError,
  type AccountInfo,
  type Configuration,
} from '@azure/msal-browser'
import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'

type AuthState = {
  ready: boolean
  account: AccountInfo | null
  accountName: string
  error: string | null
  signIn: () => Promise<void>
  signOut: () => Promise<void>
  getToken: () => Promise<string>
}

const AuthContext = createContext<AuthState | null>(null)
const devBypass = import.meta.env.DEV && import.meta.env.VITE_DEV_BYPASS_AUTH === 'true'
let redirectInProgress = false

type RuntimeAuthConfig = {
  client_id: string
  authority: string
  known_authorities?: string[]
  api_scope?: string
}

type LoadedAuthConfiguration = {
  configuration: Configuration
  tokenScopes: string[]
  preferAccessToken: boolean
}

async function loadConfiguration(): Promise<LoadedAuthConfiguration> {
  const buildClientId = import.meta.env.VITE_ENTRA_CLIENT_ID
  const buildAuthority = import.meta.env.VITE_ENTRA_AUTHORITY
  let runtime: RuntimeAuthConfig
  if (buildClientId && buildAuthority) {
    runtime = { client_id: buildClientId, authority: buildAuthority }
  } else {
    const response = await fetch('/auth-config.json', { cache: 'no-store' })
    if (!response.ok) throw new Error(`Authentication configuration failed (${response.status}).`)
    runtime = await response.json() as RuntimeAuthConfig
  }
  if (!runtime.client_id || !runtime.authority) {
    throw new Error('Authentication configuration is incomplete.')
  }
  return {
    configuration: {
      auth: {
        clientId: runtime.client_id,
        authority: runtime.authority,
        knownAuthorities: runtime.known_authorities,
        redirectUri: window.location.origin,
        postLogoutRedirectUri: window.location.origin,
        navigateToLoginRequestUrl: false,
      },
      cache: {
        cacheLocation: 'localStorage',
      },
    },
    tokenScopes: runtime.api_scope
      ? [runtime.api_scope]
      : ['openid', 'profile', 'email'],
    preferAccessToken: Boolean(runtime.api_scope),
  }
}

const developmentAccount: AccountInfo = {
  homeAccountId: 'local-owner',
  environment: 'local',
  tenantId: 'local',
  username: 'Local owner',
  localAccountId: 'local-owner',
  name: 'Local owner',
}

export function AuthProvider({ children }: PropsWithChildren) {
  const [ready, setReady] = useState(false)
  const [account, setAccount] = useState<AccountInfo | null>(devBypass ? developmentAccount : null)
  const [msal, setMsal] = useState<PublicClientApplication | null>(null)
  const [tokenScopes, setTokenScopes] = useState<string[]>(['openid', 'profile', 'email'])
  const [preferAccessToken, setPreferAccessToken] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (devBypass) {
      setReady(true)
      return
    }
    void (async () => {
      try {
        const loaded = await loadConfiguration()
        const application = new PublicClientApplication(loaded.configuration)
        await application.initialize()
        const result = await application.handleRedirectPromise()
        const selected = result?.account ?? application.getActiveAccount() ?? application.getAllAccounts()[0] ?? null
        if (selected) application.setActiveAccount(selected)
        setMsal(application)
        setTokenScopes(loaded.tokenScopes)
        setPreferAccessToken(loaded.preferAccessToken)
        setAccount(selected)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : 'Microsoft sign-in failed.')
      } finally {
        setReady(true)
      }
    })()
  }, [])

  const signIn = useCallback(async () => {
    setError(null)
    if (!msal) throw new Error('Authentication is still initializing.')
    await msal.loginRedirect({ scopes: ['openid', 'profile', 'email'] })
  }, [msal])

  const signOut = useCallback(async () => {
    if (devBypass) {
      setAccount(null)
      return
    }
    if (!msal) throw new Error('Authentication is still initializing.')
    await msal.logoutRedirect({ account: account ?? undefined })
  }, [account, msal])

  const getToken = useCallback(async () => {
    if (devBypass) return 'local-development-token'
    if (!msal) throw new Error('Authentication is still initializing.')
    const selected = account ?? msal.getActiveAccount()
    if (!selected) throw new Error('Authentication is required.')
    try {
      const result = await msal.acquireTokenSilent({
        account: selected,
        scopes: tokenScopes,
      })
      return preferAccessToken
        ? result.accessToken
        : result.idToken || result.accessToken
    } catch (cause) {
      if (!(cause instanceof InteractionRequiredAuthError)) throw cause
      if (!redirectInProgress) {
        redirectInProgress = true
        try {
          await msal.acquireTokenRedirect({
            account: selected,
            scopes: tokenScopes,
          })
        } catch (redirectError) {
          redirectInProgress = false
          throw redirectError
        }
      }
      throw new Error('Microsoft sign-in is being refreshed.')
    }
  }, [account, msal, preferAccessToken, tokenScopes])

  const value = useMemo<AuthState>(() => ({
    ready,
    account,
    accountName: account?.name || account?.username || '',
    error,
    signIn,
    signOut,
    getToken,
  }), [ready, account, error, signIn, signOut, getToken])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}
