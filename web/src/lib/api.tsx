/* oxlint-disable react/only-export-components -- Provider and its required hook intentionally share one module. */
import { createContext, type PropsWithChildren, useCallback, useContext, useEffect, useMemo, useRef } from 'react'
import type {
  Briefing,
  ConversationTurn,
  PerformanceReport,
  Portfolio,
  PortfolioTransaction,
  PortfolioTransactionInput,
  SecurityPackage,
  SecuritySummary,
  UserSettings,
  UserSettingsInput,
  AccountConfiguration,
  AccountDeletionStatus,
  AdminUser,
  InitialPortfolioInput,
  RegistrationInput,
  UserRole,
  UserSession,
  UserLifecycleStatus,
} from './types'

type Api = {
  getBriefing: (date?: string) => Promise<Briefing>
  getSecurities: () => Promise<SecuritySummary[]>
  getSecurity: (id: string) => Promise<SecurityPackage>
  getRecommendationHistory: (id: string) => Promise<import('./types').Recommendation[]>
  getPortfolio: () => Promise<Portfolio>
  getPortfolioTransactions: () => Promise<PortfolioTransaction[]>
  createPortfolioTransaction: (input: PortfolioTransactionInput) => Promise<PortfolioTransaction>
  updatePortfolioTransaction: (id: string, input: PortfolioTransactionInput) => Promise<PortfolioTransaction>
  deletePortfolioTransaction: (id: string, clientRequestId: string) => Promise<void>
  getPerformance: () => Promise<PerformanceReport>
  disposition: (id: string, value: 'ACCEPTED' | 'REJECTED' | 'DEFERRED') => Promise<void>
  streamChat: (question: string, conversationId: string | null, onChunk: (chunk: string) => void) => Promise<void>
  getUserSettings: () => Promise<UserSettings>
  updateUserSettings: (input: UserSettingsInput) => Promise<UserSettings>
  getAccountConfiguration: () => Promise<AccountConfiguration>
  getChatHistory: (conversationId?: string) => Promise<ConversationTurn[]>
  getSession: () => Promise<UserSession>
  register: (input: RegistrationInput) => Promise<UserSession>
  getRegistrationStatus: () => Promise<UserSession>
  initializePortfolio: (input: InitialPortfolioInput) => Promise<UserSession>
  completeOnboarding: () => Promise<UserSession>
  listAdminUsers: (status?: UserLifecycleStatus) => Promise<AdminUser[]>
  approveUser: (id: string) => Promise<AdminUser>
  rejectUser: (id: string) => Promise<AdminUser>
  suspendUser: (id: string) => Promise<AdminUser>
  reinstateUser: (id: string) => Promise<AdminUser>
  updateUserRole: (id: string, role: UserRole) => Promise<AdminUser>
  deleteAccount: (confirmation: string) => Promise<AccountDeletionStatus>
  getDeletionStatus: () => Promise<AccountDeletionStatus>
}

const ApiContext = createContext<Api | null>(null)
const baseUrl = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')
const CACHE_TTL_MS = 60_000

type CacheEntry = {
  promise: Promise<unknown>
  expiresAt: number
}

export function ApiProvider({ getToken, children }: PropsWithChildren<{ getToken: () => Promise<string> }>) {
  const cache = useRef(new Map<string, CacheEntry>())
  const request = useCallback(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const token = await getToken()
    const headers = new Headers(init?.headers)
    headers.set('Authorization', `Bearer ${token}`)
    if (!(init?.body instanceof FormData)) headers.set('Content-Type', 'application/json')
    const response = await fetch(`${baseUrl}${path}`, { ...init, headers })
    if (!response.ok) {
      const contentType = response.headers.get('content-type') ?? ''
      let detail = `${response.status} ${response.statusText}`
      if (contentType.includes('application/json')) {
        const payload = await response.json() as {
          detail?: string | Array<{ msg?: string }> | { message?: string; reason?: string }
        }
        if (typeof payload.detail === 'string') detail = payload.detail
        else if (Array.isArray(payload.detail)) detail = payload.detail.map((item) => item.msg).filter(Boolean).join(' · ') || detail
        else if (payload.detail?.message) detail = payload.detail.message
      }
      throw new Error(detail)
    }
    if (response.status === 204) return undefined as T
    return await response.json() as T
  }, [getToken])

  const cachedRequest = useCallback(<T,>(path: string): Promise<T> => {
    const existing = cache.current.get(path)
    if (existing && existing.expiresAt > Date.now()) return existing.promise as Promise<T>
    if (existing) cache.current.delete(path)
    const pending = request<T>(path).catch((error) => {
      cache.current.delete(path)
      throw error
    })
    cache.current.set(path, { promise: pending, expiresAt: Date.now() + CACHE_TTL_MS })
    return pending
  }, [request])

  const invalidatePortfolio = useCallback(() => {
    for (const key of cache.current.keys()) {
      if (key.startsWith('/api/portfolio') || key.startsWith('/api/briefing')) {
        cache.current.delete(key)
      }
    }
  }, [])

  useEffect(() => () => cache.current.clear(), [])

  const api = useMemo<Api>(() => ({
    getBriefing: (date) => cachedRequest(`/api/briefing${date ? `?date=${encodeURIComponent(date)}` : ''}`),
    getSecurities: () => cachedRequest('/api/securities'),
    getSecurity: (id) => cachedRequest(`/api/securities/${encodeURIComponent(id)}`),
    getRecommendationHistory: (id) => cachedRequest(`/api/recommendations/history/${encodeURIComponent(id)}`),
    getPortfolio: () => cachedRequest('/api/portfolio'),
    getPortfolioTransactions: () => cachedRequest('/api/portfolio/transactions'),
    createPortfolioTransaction: async (input) => {
      const result = await request('/api/portfolio/transactions', {
        method: 'POST',
        body: JSON.stringify(input),
      })
      invalidatePortfolio()
      return result as PortfolioTransaction
    },
    updatePortfolioTransaction: async (id, input) => {
      const result = await request(`/api/portfolio/transactions/${encodeURIComponent(id)}`, {
        method: 'PUT',
        body: JSON.stringify(input),
      })
      invalidatePortfolio()
      return result as PortfolioTransaction
    },
    deletePortfolioTransaction: async (id, clientRequestId) => {
      await request(
        `/api/portfolio/transactions/${encodeURIComponent(id)}?client_request_id=${encodeURIComponent(clientRequestId)}`,
        { method: 'DELETE' },
      )
      invalidatePortfolio()
    },
    getPerformance: () => cachedRequest('/api/performance'),
    getUserSettings: () => cachedRequest('/api/account/settings'),
    getAccountConfiguration: () => cachedRequest('/api/account/settings/configuration'),
    updateUserSettings: async (input) => {
      const result = await request('/api/account/settings', {
        method: 'PUT',
        body: JSON.stringify(input),
      })
      cache.current.delete('/api/account/settings')
      for (const key of cache.current.keys()) {
        if (key.startsWith('/api/briefing') || key.startsWith('/api/securities')) {
          cache.current.delete(key)
        }
      }
      return result as UserSettings
    },
    getChatHistory: (conversationId) => request(
      `/api/chat/history${conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ''}`,
    ) as Promise<ConversationTurn[]>,
    disposition: async (id, disposition) => {
      await request(`/api/recommendations/${encodeURIComponent(id)}/disposition`, {
        method: 'POST',
        body: JSON.stringify({ disposition }),
      })
      for (const key of cache.current.keys()) {
        if (key.startsWith('/api/briefing') || key.startsWith('/api/securities')) {
          cache.current.delete(key)
        }
      }
    },
    getSession: () => request('/api/session'),
    register: (input) => request('/api/session/register', {
      method: 'POST',
      body: JSON.stringify(input),
    }),
    getRegistrationStatus: () => request('/api/session/status'),
    initializePortfolio: (input) => request('/api/onboarding/initial-portfolio', {
      method: 'PUT',
      body: JSON.stringify(input),
    }),
    completeOnboarding: () => request('/api/onboarding/complete', { method: 'POST' }),
    listAdminUsers: (status) => request(`/api/admin/users${status ? `?status=${encodeURIComponent(status)}` : ''}`),
    approveUser: (id) => request(`/api/admin/users/${encodeURIComponent(id)}/approve`, { method: 'POST' }),
    rejectUser: (id) => request(`/api/admin/users/${encodeURIComponent(id)}/reject`, { method: 'POST' }),
    suspendUser: (id) => request(`/api/admin/users/${encodeURIComponent(id)}/suspend`, { method: 'POST' }),
    reinstateUser: (id) => request(`/api/admin/users/${encodeURIComponent(id)}/reinstate`, { method: 'POST' }),
    updateUserRole: (id, role) => request(`/api/admin/users/${encodeURIComponent(id)}/role`, {
      method: 'PUT',
      body: JSON.stringify({ role }),
    }),
    deleteAccount: (confirmation) => request('/api/account/deletion', {
      method: 'POST',
      body: JSON.stringify({ confirmation }),
    }),
    getDeletionStatus: () => request('/api/account/deletion'),
    streamChat: async (question, conversationId, onChunk) => {
      const token = await getToken()
      const response = await fetch(`${baseUrl}/api/chat`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify({ question, conversation_id: conversationId }),
      })
      if (!response.ok || !response.body) throw new Error(await response.text() || 'Chat stream unavailable.')
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value, { stream: !done })
        const events = buffer.split('\n\n')
        buffer = events.pop() ?? ''
        for (const event of events) {
          for (const line of event.split('\n')) {
            if (!line.startsWith('data:')) continue
            const payload = line.slice(5).trim()
            if (payload === '[DONE]') continue
            try {
              const decoded = JSON.parse(payload) as { chunk?: string; content?: string; text?: string }
              onChunk(decoded.chunk ?? decoded.content ?? decoded.text ?? '')
            } catch {
              onChunk(payload)
            }
          }
        }
        if (done) break
      }
    },
  }), [cachedRequest, getToken, invalidatePortfolio, request])

  return <ApiContext.Provider value={api}>{children}</ApiContext.Provider>
}

export function useApi() {
  const context = useContext(ApiContext)
  if (!context) throw new Error('useApi must be used within ApiProvider')
  return context
}
