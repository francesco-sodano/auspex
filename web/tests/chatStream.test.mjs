import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ChatStreamError, consumeChatStream } from '../src/lib/chatStream.ts'

function response(text, fragmentSize = 7) {
  const bytes = new TextEncoder().encode(text)
  return new Response(new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += fragmentSize) controller.enqueue(bytes.slice(i, i + fragmentSize))
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } })
}

test('parses fragmented UTF-8, CRLF frames, statuses, and a terminal event', async () => {
  const chunks = []
  const statuses = []
  const wire = [
    'event: status\r\ndata: {"message":"Reading evidence..."}',
    ': keep-alive',
    'data: {"chunk":"Café "}',
    'data: {"chunk":"[cite:score:NVDA:2026-09-18]"}',
    'event: conversation\r\ndata: {"conversation_id":"c1"}',
    'event: done\r\ndata: {}',
  ].join('\r\n\r\n') + '\r\n\r\n'

  await consumeChatStream(response(wire, 1), (chunk) => chunks.push(chunk), {
    onStatus: (message) => statuses.push(message),
  })

  assert.deepEqual(statuses, ['Reading evidence...'])
  assert.equal(chunks.join(''), 'Café [cite:score:NVDA:2026-09-18]')
})

test('server errors are visible rather than silently treated as empty chunks', async () => {
  const chunks = []
  await assert.rejects(
    consumeChatStream(response(
      'event: error\ndata: {"message":"Please try again.","request_id":"safe-reference"}\n\n'
      + 'event: done\ndata: {}\n\n',
    ), (chunk) => chunks.push(chunk)),
    { name: 'ChatStreamError', message: 'Please try again. Reference: safe-reference' },
  )
  assert.deepEqual(chunks, [])
})

test('EOF without done reports interruption even after a partial answer', async () => {
  const chunks = []
  await assert.rejects(
    consumeChatStream(response('data: {"chunk":"Partial answer"}\n\n'), (chunk) => chunks.push(chunk)),
    /interrupted/,
  )
  assert.deepEqual(chunks, ['Partial answer'])
})

test('malformed JSON is not appended to the answer as plain text', async () => {
  await assert.rejects(
    consumeChatStream(response('data: {"chunk":broken}\n\n'), () => assert.fail('invalid chunk rendered')),
    /invalid answer stream/,
  )
})

test('multi-line event data is decoded once', async () => {
  const chunks = []
  await consumeChatStream(
    response('data: {"chunk":\ndata: "Answer"}\n\nevent: done\ndata: {}'),
    (chunk) => chunks.push(chunk),
  )
  assert.deepEqual(chunks, ['Answer'])
})

test('HTTP errors use the API detail, not raw JSON', async () => {
  const errorResponse = new Response(JSON.stringify({ detail: 'Too many chat requests. Try again shortly.' }), {
    status: 429,
    headers: { 'Content-Type': 'application/json' },
  })
  await assert.rejects(consumeChatStream(errorResponse, () => {}), {
    name: 'ChatStreamError', message: 'Too many chat requests. Try again shortly.',
  })
})

test('a successful HTML response is not treated as a chat stream', async () => {
  await assert.rejects(
    consumeChatStream(new Response('<html/>', { headers: { 'Content-Type': 'text/html' } }), () => {}),
    /did not return an answer stream/,
  )
})

test('network reader failures produce actionable text', async () => {
  const stream = new ReadableStream({ start(controller) { controller.error(new TypeError('network error')) } })
  await assert.rejects(
    consumeChatStream(new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }), () => {}),
    { name: 'ChatStreamError', message: 'The connection to Auspex was interrupted. Please try again.' },
  )
})

test('intentional cancellation is not reported as a service error', async () => {
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(
    consumeChatStream(response(''), () => {}, { signal: controller.signal }),
    (error) => error.name === 'AbortError' && !(error instanceof ChatStreamError),
  )
})
