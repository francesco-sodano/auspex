import { responseError } from './httpError.ts'

export type ChatStreamOptions = {
  onStatus?: (message: string) => void
  signal?: AbortSignal
}

export class ChatStreamError extends Error {
  override name = 'ChatStreamError'
}

const interruptedMessage = 'The connection to Auspex was interrupted. Please try again.'

export async function consumeChatStream(
  response: Response,
  onChunk: (chunk: string) => void,
  options: ChatStreamOptions = {},
): Promise<void> {
  if (!response.ok) throw new ChatStreamError(await responseError(response))
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
    throw new ChatStreamError('Auspex did not return an answer stream. Please try again.')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const handleEvent = (frame: string): boolean => {
    let eventName = 'message'
    const data: string[] = []
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith('event:')) eventName = line.slice(6).trim()
      if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
    }
    const payload = data.join('\n')
    if (eventName === 'done' || payload === '[DONE]') return true
    if (!payload) return false

    let decoded: unknown
    try {
      decoded = JSON.parse(payload)
    } catch {
      throw new ChatStreamError('Auspex returned an invalid answer stream. Please try again.')
    }
    if (!decoded || typeof decoded !== 'object') {
      throw new ChatStreamError('Auspex returned an invalid answer stream. Please try again.')
    }
    if (eventName === 'error') {
      const message = 'message' in decoded && typeof decoded.message === 'string'
        ? decoded.message
        : 'Auspex could not complete the answer. Please try again.'
      const reference = 'request_id' in decoded && typeof decoded.request_id === 'string'
        ? ` Reference: ${decoded.request_id}`
        : ''
      throw new ChatStreamError(message + reference)
    }
    if (eventName === 'status') {
      if ('message' in decoded && typeof decoded.message === 'string') options.onStatus?.(decoded.message)
    } else if (eventName === 'message' || eventName === 'chunk') {
      const chunk = 'chunk' in decoded ? decoded.chunk : 'content' in decoded ? decoded.content
        : 'text' in decoded ? decoded.text : null
      if (typeof chunk !== 'string') {
        throw new ChatStreamError('Auspex returned an invalid answer stream. Please try again.')
      }
      onChunk(chunk)
    }
    return false
  }

  try {
    for (;;) {
      options.signal?.throwIfAborted()
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      let boundary = /\r?\n\r?\n/.exec(buffer)
      while (boundary) {
        const frame = buffer.slice(0, boundary.index)
        buffer = buffer.slice(boundary.index + boundary[0].length)
        if (handleEvent(frame)) return
        boundary = /\r?\n\r?\n/.exec(buffer)
      }
      if (done) {
        if (buffer.trim() && handleEvent(buffer)) return
        throw new ChatStreamError(interruptedMessage)
      }
    }
  } catch (cause) {
    if (cause instanceof ChatStreamError || (cause instanceof Error && cause.name === 'AbortError')) throw cause
    throw new ChatStreamError(interruptedMessage, { cause })
  } finally {
    try {
      await reader.cancel()
    } catch (cause) {
      console.warn('Could not close the Discussion stream reader.', cause)
    }
    reader.releaseLock()
  }
}
