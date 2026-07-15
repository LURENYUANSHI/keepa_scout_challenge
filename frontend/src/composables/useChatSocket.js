// WS /chat/stream connection management, kept separate from Chat.vue so the
// reconnect/backoff state machine can be reasoned about (and tested) on its
// own — see app/routers/chat.py's module docstring for the wire protocol
// this talks to.
//
// Protocol recap (verified against app/routers/chat.py, not assumed):
// - Connect: `ws(s)://<api>/chat/stream?token=<access_token>` — token is a
//   query param because a plain `new WebSocket(url)` can't set an
//   Authorization header. A bad/missing token means the server closes the
//   handshake with code 4401 without ever accepting it.
// - Client sends one `{"session_id", "message"}` JSON text frame per turn;
//   the socket stays open across many turns.
// - Server sends, per turn, in order: zero or more
//   `tool_call_start`/`tool_call_result` pairs (one pair per tool call, sent
//   the moment each happens — never batched), then either
//   zero-or-more `{"type":"answer_delta", "content": "..."}` (one per
//   token/token-chunk of the final answer, sent as the LLM generates them —
//   never batched) followed by `{"type":"answer_done"}` +
//   `{"type":"session_state", ...}` on success, or a single
//   `{"type":"error", ...}` on turn failure (which does NOT close the
//   connection — the client can send the next turn).
// - Rare: a run of `answer_delta`s can be followed by
//   `{"type":"answer_retract"}` instead of `answer_done` — the backend
//   optimistically streams as soon as it sees answer-shaped text, but the
//   underlying LLM occasionally narrates ("Let me look that up...") right
//   before deciding to call a tool after all; the client must then discard
//   that in-progress bubble entirely rather than finalize it.
//
// HARNESS.md §10.3 point 3: a broken WS must never leave the UI silently
// stuck. This composable surfaces a `status` ref the component renders as a
// banner, auto-reconnects a few times with backoff, and exposes `retry()`
// for a manual retry once auto-reconnect gives up.

import { ref, onBeforeUnmount } from 'vue'
import { getToken, WS_BASE_URL } from '../api/client'

const MAX_AUTO_RETRIES = 5
const BASE_DELAY_MS = 1000
const MAX_DELAY_MS = 10000

// status values:
//   'idle'          — no connection attempted yet (page just opened, user
//                     hasn't sent anything) — deliberately NOT the same as
//                     'connecting': visiting /chat must not itself open a
//                     live server connection, only sending a message does.
//   'connecting'    — first-ever connection attempt in flight
//   'open'          — connected, ready to send
//   'reconnecting'  — connection dropped, auto-retry in flight/scheduled
//   'failed'        — auto-retry attempts exhausted, needs a manual retry()
//   'unauthorized'  — server rejected the handshake (bad/expired token)
//   'closed'        — deliberately closed by the client (unmount)
export function useChatSocket(handlers = {}) {
  const status = ref('idle')
  const attempt = ref(0)

  let socket = null
  let retryTimer = null
  let deliberatelyClosed = false
  // Set by send() when called before a connection exists yet -- flushed the
  // moment the (lazily-triggered) connection actually opens, so the
  // caller's first send() doesn't need to know/await the handshake itself.
  let pendingSend = null

  function clearRetryTimer() {
    if (retryTimer) {
      clearTimeout(retryTimer)
      retryTimer = null
    }
  }

  function handleMessage(event) {
    let data
    try {
      data = JSON.parse(event.data)
    } catch {
      return // not JSON — ignore rather than crash the socket handling
    }
    switch (data.type) {
      case 'tool_call_start':
        handlers.onToolStart?.(data)
        break
      case 'tool_call_result':
        handlers.onToolResult?.(data)
        break
      case 'answer_delta':
        handlers.onAnswerDelta?.(data)
        break
      case 'answer_done':
        handlers.onAnswerDone?.(data)
        break
      case 'answer_retract':
        handlers.onAnswerRetract?.(data)
        break
      case 'session_state':
        handlers.onSessionState?.(data)
        break
      case 'error':
        handlers.onTurnError?.(data)
        break
      default:
        break
    }
  }

  function scheduleReconnect() {
    if (attempt.value >= MAX_AUTO_RETRIES) {
      status.value = 'failed'
      return
    }
    attempt.value += 1
    status.value = 'reconnecting'
    const delay = Math.min(BASE_DELAY_MS * 2 ** (attempt.value - 1), MAX_DELAY_MS)
    retryTimer = setTimeout(connect, delay)
  }

  function connect() {
    deliberatelyClosed = false
    clearRetryTimer()
    status.value = attempt.value > 0 ? 'reconnecting' : 'connecting'

    const token = getToken()
    const url = `${WS_BASE_URL}/chat/stream?token=${encodeURIComponent(token || '')}`

    try {
      socket = new WebSocket(url)
    } catch {
      scheduleReconnect()
      return
    }

    socket.onopen = () => {
      attempt.value = 0
      status.value = 'open'
      if (pendingSend) {
        const { sessionId, message } = pendingSend
        pendingSend = null
        socket.send(JSON.stringify({ session_id: sessionId, message }))
      }
    }

    socket.onmessage = handleMessage

    // onerror carries no useful detail in browsers (spec-hidden); onclose
    // always follows it and is where the actual reconnect decision happens.
    socket.onerror = () => {}

    socket.onclose = (event) => {
      socket = null
      if (deliberatelyClosed) {
        status.value = 'closed'
        return
      }
      // Server rejects the handshake itself with 4401 on a bad/expired
      // token (see chat_stream()) — retrying with the same stale token
      // would just loop, so surface a distinct state instead.
      if (event.code === 4401) {
        status.value = 'unauthorized'
        return
      }
      scheduleReconnect()
    }
  }

  function retry() {
    attempt.value = 0
    connect()
  }

  function send(sessionId, message) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ session_id: sessionId, message }))
      return true
    }
    // Lazy connect: nothing has opened a socket yet (status is 'idle', or a
    // prior one was deliberately closed) -- this first send() is what
    // triggers connect(), and the message is queued to flush from
    // socket.onopen the moment the handshake completes. Refuse to queue a
    // second message on top of one already pending (only 'idle'/'closed'
    // reach here for a *fresh* queue; mid-connect/reconnect states already
    // have a real attempt in flight that onopen will flush).
    if (status.value === 'idle' || status.value === 'closed' || status.value === 'failed') {
      pendingSend = { sessionId, message }
      attempt.value = 0 // fresh attempt, not a continuation of a prior exhausted retry run
      connect()
      return true
    }
    return false
  }

  function disconnect() {
    deliberatelyClosed = true
    clearRetryTimer()
    if (socket) {
      socket.close()
      socket = null
    }
  }

  onBeforeUnmount(disconnect)

  return { status, attempt, maxAutoRetries: MAX_AUTO_RETRIES, connect, disconnect, retry, send }
}
