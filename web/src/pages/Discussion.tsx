import { Clock3, Plus, Send } from 'lucide-react'
import { type FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { ErrorBlock, PageHeading } from '../components/common'
import { useApi } from '../lib/api'
import type { ConversationTurn } from '../lib/types'

type ChatMessage = { role: 'user' | 'assistant'; content: string; error?: string }

const promptFromHash = () => {
  const query = window.location.hash.split('?')[1] ?? ''
  return new URLSearchParams(query).get('prompt')
}

const QUICK_QUESTIONS = [
  'What changed in the top movers today, and why?',
  'What are today’s portfolio suggestions, including quantities and blockers?',
  'Which stocks are the strongest buy candidates right now?',
]

function CitationText({ content }: { content: string }) {
  const parts = content.split(/(\[cite:[^\]]+\])/g)
  return parts.map((part, index) => {
    const match = part.match(/^\[cite:([^\]]+)\]$/)
    if (!match) return part
    const citation = match[1]
    const segments = citation.split(':')
    const ticker = segments[1]
    if (
      !ticker
      || segments[0] === 'performance'
      || segments[0] === 'portfolio'
      || ticker === 'current'
      || ticker.match(/^\d{4}-/)
    ) {
      return <span className="citation" key={`${citation}-${index}`}>[source]</span>
    }
    const evidence = segments[0] === 'document' ? segments.at(-1) : null
    const href = `#/analysis?security=${encodeURIComponent(ticker)}${evidence ? `&evidence=${encodeURIComponent(evidence)}` : ''}`
    return <a className="citation" href={href} key={`${citation}-${index}`} title={citation}>[{ticker} source]</a>
  })
}

export function Discussion() {
  const api = useApi()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [question, setQuestion] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [statusMessage, setStatusMessage] = useState('')
  const [historyError, setHistoryError] = useState<unknown>(null)
  const [loadingConversation, setLoadingConversation] = useState(false)
  const [history, setHistory] = useState<ConversationTurn[]>([])
  const logRef = useRef<HTMLDivElement>(null)
  const autoPromptSent = useRef(false)
  const conversationId = useRef<string>(crypto.randomUUID())
  const activeRequest = useRef<AbortController | null>(null)
  const refreshHistory = useCallback(() => {
    void api.getChatHistory().then((turns) => {
      setHistory(turns)
      setHistoryError(null)
    }).catch(setHistoryError)
  }, [api])

  useEffect(refreshHistory, [refreshHistory])
  useEffect(() => () => activeRequest.current?.abort(), [])

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, statusMessage])

  const ask = useCallback(async (text: string) => {
    const normalized = text.trim()
    if (!normalized || activeRequest.current || loadingConversation) return
    const controller = new AbortController()
    activeRequest.current = controller
    setQuestion('')
    setStatusMessage('Connecting to Auspex...')
    setMessages((current) => [...current, { role: 'user', content: normalized }, { role: 'assistant', content: '' }])
    setStreaming(true)
    try {
      await api.streamChat(normalized, conversationId.current, (chunk) => {
        setMessages((current) => current.map((message, index) => index === current.length - 1 ? { ...message, content: message.content + chunk } : message))
      }, { signal: controller.signal, onStatus: setStatusMessage })
    } catch (cause) {
      if (controller.signal.aborted) return
      const error = cause instanceof Error ? cause.message : 'The answer stream failed. Please try again.'
      setMessages((current) => current.map((message, index) => index === current.length - 1 ? { ...message, error } : message))
      setQuestion((current) => current || normalized)
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null
      if (!controller.signal.aborted) {
        setStreaming(false)
        setStatusMessage('')
        refreshHistory()
      }
    }
  }, [api, refreshHistory, loadingConversation])

  useEffect(() => {
    const prompt = promptFromHash()
    if (!prompt || autoPromptSent.current) return
    let cancelled = false
    void Promise.resolve().then(() => {
      if (cancelled) return
      autoPromptSent.current = true
      window.history.replaceState(null, '', '#/discussion')
      void ask(prompt)
    })
    return () => { cancelled = true }
  }, [ask])

  const send = (event: FormEvent) => {
    event.preventDefault()
    void ask(question)
  }
  const newChat = () => {
    if (activeRequest.current || loadingConversation) return
    conversationId.current = crypto.randomUUID()
    setMessages([])
    setQuestion('')
  }
  const openConversation = async (id: string) => {
    if (activeRequest.current || loadingConversation) return
    setLoadingConversation(true)
    try {
      const turns = await api.getChatHistory(id)
      conversationId.current = id
      setMessages(turns.flatMap((turn) => [
        { role: 'user' as const, content: turn.question },
        { role: 'assistant' as const, content: turn.answer ?? '' },
      ]))
      setQuestion('')
      setHistoryError(null)
    } catch (cause) {
      setHistoryError(cause)
    } finally {
      setLoadingConversation(false)
    }
  }
  const conversationSummaries = Array.from(
    history.reduce((map, turn) => {
      if (!map.has(turn.conversation_id)) map.set(turn.conversation_id, turn)
      return map
    }, new Map<string, ConversationTurn>()).values(),
  ).slice(0, 15)

  return (
    <>
      <PageHeading eyebrow="Grounded conversation" title="Discussion" description="Ask Auspex about today’s movers, portfolio suggestions, scores, filings, fundamentals, or evidence. Answers use only retrieved Auspex facts." />
      <div className="discussion-chat-layout">
        <aside className="chat-history-panel">
          <button className="button primary" type="button" disabled={streaming || loadingConversation} onClick={newChat}><Plus size={14} /> New chat</button>
          <header><Clock3 size={13} /><span>Last 15 days</span></header>
          <div>
            {historyError !== null && (
              <div role="alert">
                <p>Saved conversations could not be loaded.</p>
                <ErrorBlock error={historyError} />
                <button className="button compact" type="button" onClick={refreshHistory}>Retry history</button>
              </div>
            )}
            {historyError === null && conversationSummaries.length === 0 && <p>No saved conversations yet.</p>}
            {conversationSummaries.map((turn) => (
              <button type="button" key={turn.conversation_id} disabled={streaming || loadingConversation} onClick={() => void openConversation(turn.conversation_id)}>
                <strong>{turn.question}</strong>
                <small>{new Date(turn.created_at).toLocaleDateString()}</small>
              </button>
            ))}
          </div>
        </aside>
        <section className="chat-panel chat-panel-standalone">
        <div className="chat-log" ref={logRef}>
          {messages.length === 0 && (
            <div className="chat-welcome">
              <p>What would you like to understand?</p>
              <div>{QUICK_QUESTIONS.map((item) => <button type="button" className="button" key={item} disabled={streaming || loadingConversation} onClick={() => void ask(item)}>{item}</button>)}</div>
            </div>
          )}
          {messages.map((message, index) => (
            <div className={`chat-message ${message.role}`} key={index}>
              <CitationText content={message.content || (streaming && index === messages.length - 1 ? statusMessage : '')} />
              {message.error && <p className="chat-message-error" role="alert">{message.error}</p>}
            </div>
          ))}
        </div>
        <form className="chat-composer" onSubmit={send}>
          <textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Ask a grounded question about Auspex data…" aria-label="Question" />
          <button className="button primary" disabled={streaming || loadingConversation || !question.trim()}><Send size={14} /> Send</button>
        </form>
        </section>
      </div>
    </>
  )
}
