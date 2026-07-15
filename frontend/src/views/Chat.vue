<script setup>
// Real /chat UI — WS /chat/stream, per HARNESS.md §10.3: tool-call events
// must render the moment they arrive (not batched until the turn ends), the
// pending->done transition must mutate the same card in place (no
// remove/reinsert -> no orphan DOM nodes), and a dead socket must show a
// clear state, never a silent hang.
//
// One session per page-load (sessionId regenerated on demand via "New
// chat") — see app/routers/chat.py: `session_id` is ownership-scoped per
// user, LangGraph's checkpointer is what actually carries conversation
// state across turns.
import { ref, computed, nextTick } from 'vue'
import { useChatSocket } from '../composables/useChatSocket'
import { renderMarkdown } from '../utils/markdown'

let seq = 0
function nextId() {
  seq += 1
  return `m${seq}`
}

function newSessionId() {
  return typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : `sess-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

const sessionId = ref(newSessionId())
const messages = ref([])
const sessionState = ref({ active_filters: {}, last_result_asins: [], resolved_entity: null })
const inputText = ref('')
const turnInFlight = ref(false)
const transcriptEl = ref(null)

const hasActiveFilters = computed(() => {
  const filters = sessionState.value.active_filters
  return Boolean(filters && Object.keys(filters).length)
})

function scrollToBottom() {
  nextTick(() => {
    const el = transcriptEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

// --- compact tool arg/result rendering -------------------------------
// The 6 tools (build_filter_sql / lookup_asin / plan_combo /
// run_readonly_sql / update_preferences / reset_topic — HARNESS.md §7.1)
// each return a different shaped payload; rather than special-case all six,
// summarize generically: primitives verbatim, arrays as a count, nested
// objects flattened one level. Good enough for "compact rendering", not a
// full result viewer.
function summarizeValue(v) {
  if (v === null || v === undefined) return '—'
  if (Array.isArray(v)) return `[${v.length}]`
  if (typeof v === 'object') return '{…}'
  if (typeof v === 'number') return String(Math.round(v * 100) / 100)
  return String(v)
}

function compactArgs(args) {
  const entries = Object.entries(args || {})
  if (!entries.length) return null
  return entries.map(([k, v]) => `${k}=${summarizeValue(v)}`).join('   ')
}

function compactResult(result) {
  if (result === null || result === undefined) return null
  if (typeof result === 'object' && !Array.isArray(result) && 'error' in result) {
    return `error: ${result.error}`
  }
  if (Array.isArray(result)) return `${result.length} item(s)`
  if (typeof result === 'object') {
    const entries = Object.entries(result)
    if (!entries.length) return null
    return entries.map(([k, v]) => `${k}: ${summarizeValue(v)}`).join('   ')
  }
  return String(result)
}

// --- WS event -> DOM update mapping -----------------------------------
// tool_call_start APPENDS a new card object (status: 'pending').
// tool_call_result MUTATES that same object in place (status/result
// fields on the existing array element) instead of pushing a new one —
// Vue's array/object reactivity then patches the existing DOM node's
// bindings rather than removing and reinserting it, which is what keeps
// this from leaving the empty-placeholder-div artifact HARNESS.md §10.3
// warns about. Tool calls are dispatched strictly sequentially by
// tools_node (app/agent/graph.py) — never more than one pending card at a
// time — so "the most recent pending card" is an unambiguous match.
//
// answer_delta/answer_done follow the identical in-place-mutation
// pattern: the first delta of a turn APPENDS one bubble object
// (`streaming: true`, `content: ''`) and every delta after that MUTATES
// its `.content` in place (`+=`) — never pushes a new bubble — so the
// answer visibly grows token-by-token instead of popping in all at once.
// `currentAnswerMessage` tracks which bubble is still accepting deltas;
// answer_done clears it and flips `streaming` off (stops the blinking
// cursor). Only one answer stream is ever in flight at a time (the server
// only starts streaming the final answer after every tool call for the
// turn has already resolved), so there's no ambiguity about which bubble
// a delta belongs to.
let currentAnswerMessage = null

const socket = useChatSocket({
  onToolStart(data) {
    messages.value.push({
      id: nextId(),
      type: 'tool',
      tool: data.tool,
      args: data.args,
      status: 'pending',
      result: null,
    })
    scrollToBottom()
  },
  onToolResult(data) {
    for (let i = messages.value.length - 1; i >= 0; i -= 1) {
      const m = messages.value[i]
      if (m.type === 'tool' && m.status === 'pending') {
        m.status = 'done'
        m.result = data.result
        scrollToBottom()
        return
      }
    }
    // Defensive fallback: a result with no matching pending card would mean
    // the start/result invariant above broke server-side. Surface it as its
    // own (already-done) card instead of silently dropping the event.
    messages.value.push({
      id: nextId(),
      type: 'tool',
      tool: data.tool,
      args: {},
      status: 'done',
      result: data.result,
    })
    scrollToBottom()
  },
  onAnswerDelta(data) {
    if (!currentAnswerMessage) {
      currentAnswerMessage = { id: nextId(), type: 'answer', content: '', streaming: true }
      messages.value.push(currentAnswerMessage)
    }
    currentAnswerMessage.content += data.content
    scrollToBottom()
  },
  onAnswerDone() {
    if (currentAnswerMessage) {
      currentAnswerMessage.streaming = false
    }
    currentAnswerMessage = null
  },
  onAnswerRetract() {
    // The backend streamed some deltas optimistically, then discovered the
    // LLM was actually narrating ahead of a tool call rather than giving
    // its final answer — remove the bubble entirely instead of leaving
    // stray "Let me look that up..." text in the transcript. A real
    // answer_delta run for this turn follows once the tool call resolves.
    if (currentAnswerMessage) {
      const idx = messages.value.findIndex((m) => m.id === currentAnswerMessage.id)
      if (idx !== -1) messages.value.splice(idx, 1)
      currentAnswerMessage = null
    }
  },
  onSessionState(data) {
    sessionState.value = data.state || {}
    turnInFlight.value = false
  },
  onTurnError(data) {
    // A turn-level error can arrive mid-stream (e.g. the graph blew up
    // after already having streamed a few answer_delta events) — don't
    // leave a bubble stuck showing a blinking "still streaming" cursor.
    if (currentAnswerMessage) {
      currentAnswerMessage.streaming = false
      currentAnswerMessage = null
    }
    messages.value.push({ id: nextId(), type: 'error', content: data.detail })
    turnInFlight.value = false
    scrollToBottom()
  },
})

// 'idle' counts as sendable -- that's the whole point of lazy connect:
// the first send() is what triggers the WS handshake, not a prerequisite
// for it.
const canSend = computed(
  () => (socket.status.value === 'open' || socket.status.value === 'idle') && !turnInFlight.value
)

// Only rendered while status !== 'open' (see template), so this only ever
// distinguishes "still trying" (default banner) from "needs the user to do
// something" (error styling).
const bannerVariant = computed(() => {
  const terminal = socket.status.value === 'failed' || socket.status.value === 'unauthorized'
  return terminal ? 'banner-error' : ''
})

function sendMessage() {
  const text = inputText.value.trim()
  if (!text || !canSend.value) return

  messages.value.push({ id: nextId(), type: 'user', content: text })
  const sent = socket.send(sessionId.value, text)
  if (!sent) {
    messages.value.push({
      id: nextId(),
      type: 'error',
      content: 'Could not send — the connection is not open.',
    })
    scrollToBottom()
    return
  }
  inputText.value = ''
  turnInFlight.value = true
  scrollToBottom()
}

function startNewChat() {
  if (turnInFlight.value) return
  sessionId.value = newSessionId()
  messages.value = []
  sessionState.value = { active_filters: {}, last_result_asins: [], resolved_entity: null }
  currentAnswerMessage = null
}

// Deliberately no onMounted(() => socket.connect()) here -- visiting /chat
// must not itself open a live WS connection (see useChatSocket.js's
// 'idle' status doc comment). The connection is lazily triggered by the
// first sendMessage() call instead.
</script>

<template>
  <div>
    <div class="chat-head">
      <div>
        <p class="eyebrow">04 — Multi-turn assistant</p>
        <h1>Chat</h1>
      </div>
      <div class="chat-head-right">
        <span class="session-tag mono">session {{ sessionId.slice(0, 8) }}</span>
        <button type="button" class="btn-text" :disabled="turnInFlight" @click="startNewChat">
          New chat
        </button>
      </div>
    </div>

    <div
      v-if="socket.status.value !== 'open' && socket.status.value !== 'idle'"
      class="banner"
      :class="bannerVariant"
    >
      <template v-if="socket.status.value === 'connecting'">Connecting to chat…</template>
      <template v-else-if="socket.status.value === 'reconnecting'">
        Connection lost — reconnecting… (attempt {{ socket.attempt.value }}/{{ socket.maxAutoRetries }})
      </template>
      <template v-else-if="socket.status.value === 'failed'">
        Couldn't reconnect after {{ socket.maxAutoRetries }} attempts — the chat connection is down.
        <button type="button" class="btn-text" @click="socket.retry()">Retry connection</button>
      </template>
      <template v-else-if="socket.status.value === 'unauthorized'">
        Your session expired or is invalid.
        <router-link to="/login">Log in again</router-link>.
      </template>
      <template v-else-if="socket.status.value === 'closed'">Chat connection closed.</template>
    </div>

    <details v-if="hasActiveFilters" class="panel filters-disclosure">
      <summary>Active filters &amp; state</summary>
      <div class="tag-row" style="margin-top: var(--space-3)">
        <span v-for="(value, key) in sessionState.active_filters" :key="key" class="chip" style="cursor: default">
          {{ key }}: {{ value }}
        </span>
        <span v-if="sessionState.resolved_entity" class="chip" style="cursor: default">
          resolved: {{ sessionState.resolved_entity }}
        </span>
      </div>
    </details>

    <section class="panel transcript-panel">
      <div ref="transcriptEl" class="transcript">
        <p v-if="!messages.length" class="empty-hint">
          Ask something like "show me eligible ASINs sorted by ROI" to start.
        </p>

        <template v-for="msg in messages" :key="msg.id">
          <div v-if="msg.type === 'user'" class="row row-user">
            <div class="bubble bubble-user">
              <span class="bubble-label mono">You</span>
              <p class="bubble-text">{{ msg.content }}</p>
            </div>
          </div>

          <div v-else-if="msg.type === 'tool'" class="row row-tool">
            <div class="tool-card" :class="msg.status">
              <div class="tool-card-head">
                <span class="tool-card-name mono">{{ msg.tool }}</span>
                <span class="tool-card-status mono" :class="{ 'tool-card-status-pending': msg.status === 'pending' }">
                  {{ msg.status === 'done' ? 'done' : 'running…' }}
                </span>
              </div>
              <p v-if="compactArgs(msg.args)" class="tool-card-line mono">args: {{ compactArgs(msg.args) }}</p>
              <p v-if="msg.status === 'done' && compactResult(msg.result)" class="tool-card-line mono">
                {{ compactResult(msg.result) }}
              </p>
            </div>
          </div>

          <div v-else-if="msg.type === 'answer'" class="row row-answer">
            <div class="bubble bubble-answer">
              <span class="bubble-label mono">Scout</span>
              <div class="bubble-text markdown-body" v-html="renderMarkdown(msg.content)"></div>
            </div>
          </div>

          <div v-else-if="msg.type === 'error'" class="row row-error">
            <p class="banner banner-error turn-error">{{ msg.content }}</p>
          </div>
        </template>
      </div>

      <form class="composer" @submit.prevent="sendMessage">
        <textarea
          id="chat-message"
          v-model="inputText"
          name="message"
          class="composer-input"
          rows="2"
          :disabled="!canSend"
          :placeholder="canSend ? 'Ask about eligibility, ROI, filters, combos…' : 'Waiting for connection…'"
          aria-label="Message"
          @keydown.enter.exact.prevent="sendMessage"
        ></textarea>
        <button type="submit" class="btn btn-primary" :disabled="!canSend || !inputText.trim()">
          {{ turnInFlight ? 'Thinking…' : 'Send' }}
        </button>
      </form>
    </section>
  </div>
</template>

<style scoped>
.chat-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.chat-head-right {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding-bottom: var(--space-3);
}

.session-tag {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--steel);
}

.filters-disclosure {
  margin-top: var(--space-5);
  padding: var(--space-4) var(--space-5);
}

.filters-disclosure summary {
  cursor: pointer;
  font-family: var(--font-body);
  font-size: var(--text-sm);
  font-weight: 600;
  letter-spacing: -0.005em;
  color: var(--steel);
}

.transcript-panel {
  margin-top: var(--space-5);
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
}

.transcript {
  flex: 1;
  min-height: 24rem;
  max-height: 34rem;
  overflow-y: auto;
  padding: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.empty-hint {
  color: var(--steel);
  font-size: var(--text-sm);
  margin: auto;
}

.row {
  display: flex;
}

.row-user {
  justify-content: flex-end;
}

.row-answer,
.row-tool,
.row-error {
  justify-content: flex-start;
}

.bubble {
  max-width: 40rem;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-md);
}

.bubble-user {
  background: var(--paper);
}

.bubble-answer {
  background: var(--raised);
  border-color: var(--line);
}

.bubble-label {
  display: block;
  font-size: var(--text-xs);
  font-weight: 700;
  letter-spacing: 0.01em;
  color: var(--steel);
  margin-bottom: var(--space-1);
}

.bubble-text {
  margin: 0;
  white-space: pre-wrap;
}

.tool-card {
  max-width: 34rem;
  padding: var(--space-3) var(--space-4);
  background: var(--paper);
  border: 1px dashed var(--line);
  border-radius: var(--radius-md);
}

.tool-card.done {
  border-style: solid;
  border-color: var(--line);
}

.tool-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.tool-card-name {
  font-size: var(--text-sm);
  font-weight: 600;
}

.tool-card-status {
  font-size: var(--text-xs);
  font-weight: 700;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  color: var(--teal);
}

.tool-card-status-pending {
  color: var(--blue);
}

/* CSS-only spinner glyph on the status text itself — no extra DOM node,
   so there is nothing left behind once the card flips to "done". */
.tool-card-status-pending::before {
  content: '';
  display: inline-block;
  width: 0.5rem;
  height: 0.5rem;
  margin-right: var(--space-2);
  border: 1.5px solid var(--blue);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 700ms linear infinite;
  vertical-align: middle;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.tool-card-line {
  margin: var(--space-2) 0 0;
  font-size: var(--text-xs);
  color: var(--ink-soft);
  word-break: break-word;
}

.turn-error {
  margin: 0;
  max-width: 34rem;
}

.composer {
  display: flex;
  gap: var(--space-3);
  align-items: flex-end;
  padding: var(--space-4) var(--space-5);
  border-top: 1px solid var(--line-strong);
  background: var(--raised);
}

.composer-input {
  flex: 1;
  resize: vertical;
}

/* markdown rendering inside the answer bubble — v-html content isn't
   scoped by Vue's normal attribute selectors, hence :deep(). */
.markdown-body :deep(p) {
  margin: 0 0 var(--space-2);
}

.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}

.markdown-body :deep(strong) {
  font-weight: 700;
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 0 0 var(--space-2);
  padding-left: var(--space-5);
}

.markdown-body :deep(table) {
  width: 100%;
  margin: var(--space-2) 0;
  font-size: var(--text-xs);
}

.markdown-body :deep(code) {
  font-family: var(--font-mono);
  background: var(--paper);
  padding: 0 0.2em;
}
</style>
