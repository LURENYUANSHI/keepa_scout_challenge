// WS /chat/stream connection management, kept separate from Chat.vue so the
// per-turn socket lifecycle can be reasoned about (and tested) on its own —
// see app/routers/chat.py's module docstring for the wire protocol this
// talks to.
//
// One fresh WebSocket per turn, closed the moment the turn ends -- not one
// long-lived connection reused across many turns. `send()` opens a new
// socket, sends the single `{"session_id", "message"}` frame once it's
// open, and the socket is closed as soon as `session_state` (success) or
// `error` (turn failure) arrives. This means there is no persistent
// connection to babysit between turns -- no idle-timeout/heartbeat concern,
// and no reconnect/backoff state machine, since a dropped connection just
// means *this* turn failed; the next send() opens an entirely new one.
// (The backend's WS loop still supports many turns per connection --
// nothing here relies on the server closing after one turn -- this is a
// client-side choice.)
//
// Protocol recap (verified against app/routers/chat.py, not assumed):
// - Connect: `ws(s)://<api>/chat/stream?token=<access_token>` — token is a
//   query param because a plain `new WebSocket(url)` can't set an
//   Authorization header. A bad/missing token means the server closes the
//   handshake with code 4401 without ever accepting it.
// - Client sends one `{"session_id", "message"}` JSON text frame right
//   after the socket opens.
// - Server sends, per turn: interleaved `tool_call_start`/`tool_call_result`
//   pairs (one pair per tool call, sent the moment each happens — never
//   batched) and answer segments — each segment is a run of
//   `{"type":"answer_delta", "content": "..."}` (one per token/token-chunk,
//   sent as the LLM generates them — never batched) closed by one
//   `{"type":"answer_done"}`. A turn usually has one segment (after all
//   tool calls resolve), but the model sometimes writes the FIRST part of
//   its answer before its tool calls (e.g. the explanation half of a mixed
//   question) — that arrives as its own delta-run + answer_done BEFORE the
//   tool events, and the post-tool segment continues without repeating it.
//   The turn ends with `{"type":"session_state", ...}` on success, or a
//   single `{"type":"error", ...}` on turn failure.
//
// HARNESS.md §10.3 point 3: a broken WS must never leave the UI silently
// stuck. This composable surfaces a `status` ref the component renders as a
// banner for the two states that actually need one: `unauthorized` and
// `error`.

import { ref, onBeforeUnmount } from 'vue'
import { getToken, WS_BASE_URL } from '../api/client'

// status values:
//   'idle'         — no turn in flight; ready to send. Also what a turn
//                    settles back to once its socket closes cleanly — there
//                    is no persistent 'open, waiting for the next message'
//                    state to distinguish it from.
//   'connecting'   — this turn's socket handshake is in flight
//   'streaming'    — this turn's socket is open; tool calls/answer deltas
//                    may be arriving
//   'unauthorized' — the server rejected the handshake (bad/expired token)
//   'error'        — this turn's socket closed before the turn finished
//                    (network drop, server crash mid-turn, etc.) — no
//                    auto-retry; the user resends
export function useChatSocket(handlers = {}) {
  const status = ref('idle')

  let socket = null
  // Flips true the moment session_state/error arrives for the in-flight
  // turn, so the onclose that immediately follows (this same turn's socket
  // being closed on purpose, right below) isn't mistaken for a mid-turn
  // connection drop.
  let turnSettled = false

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
      case 'session_state':
        turnSettled = true
        handlers.onSessionState?.(data)
        socket?.close()
        break
      case 'error':
        turnSettled = true
        handlers.onTurnError?.(data)
        socket?.close()
        break
      default:
        break
    }
  }

  function send(sessionId, message) {
    // Only an actually-in-flight turn blocks sending. 'error'/'unauthorized'
    // are settled states from a PREVIOUS turn — a new send() is exactly how
    // the user retries (fresh socket per turn), so they must not wedge the
    // composer shut.
    if (status.value === 'connecting' || status.value === 'streaming') return false

    turnSettled = false
    status.value = 'connecting'

    const token = getToken()
    const url = `${WS_BASE_URL}/chat/stream?token=${encodeURIComponent(token || '')}`

    let s
    try {
      s = new WebSocket(url)
    } catch {
      status.value = 'error'
      return false
    }
    socket = s

    s.onopen = () => {
      status.value = 'streaming'
      s.send(JSON.stringify({ session_id: sessionId, message }))
    }

    s.onmessage = handleMessage

    // onerror carries no useful detail in browsers (spec-hidden); onclose
    // always follows it and is where the actual status decision happens.
    s.onerror = () => {}

    s.onclose = (event) => {
      socket = null
      if (turnSettled) {
        status.value = 'idle'
        return
      }
      // Server rejects the handshake itself with 4401 on a bad/expired
      // token (see chat_stream()).
      status.value = event.code === 4401 ? 'unauthorized' : 'error'
    }

    return true
  }

  function disconnect() {
    if (socket) {
      const s = socket
      socket = null
      s.close()
    }
  }

  onBeforeUnmount(disconnect)

  return { status, disconnect, send }
}
