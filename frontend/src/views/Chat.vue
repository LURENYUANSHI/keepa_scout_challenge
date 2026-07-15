<script setup>
// Real /chat UI — WS /chat/stream, per HARNESS.md §10.3: tool-call events
// must render the moment they arrive (not batched until the turn ends), the
// pending->done transition must mutate the same card in place (no
// remove/reinsert -> no orphan DOM nodes), and a dead socket must show a
// clear state, never a silent hang.
//
// The session lives in the URL (`/chat/:sessionId`, router/index.js), not
// just component state -- a reload, a shared link, or browser back/forward
// must all land on the same conversation rather than silently minting a
// fresh one. `sessionId` below mirrors the `sessionId` route prop; switching
// sessions (New chat / clicking a past one) navigates via the router, and a
// watcher on the prop is the single place that reacts to "the active
// session changed" regardless of what triggered it.
import { ref, computed, nextTick, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useChatSocket } from '../composables/useChatSocket'
import { renderMarkdown } from '../utils/markdown'
import { api, ApiError } from '../api/client'
import { newChatSessionId } from '../utils/session'

const props = defineProps({ sessionId: { type: String, required: true } })
const router = useRouter()

let seq = 0
function nextId() {
  seq += 1
  return `m${seq}`
}

const sessionId = ref(props.sessionId)
const messages = ref([])
const sessionState = ref({ active_filters: {}, last_result_asins: [], resolved_entity: null })
const inputText = ref('')
const turnInFlight = ref(false)
const transcriptEl = ref(null)

// Tracks which (if any) message in `messages` is still accepting
// answer_delta chunks -- declared up here, not down near useChatSocket(),
// because loadSessionHistory() (below) already assigns to it, and that
// function is invoked synchronously on mount via the `immediate: true`
// sessionId watcher, before `<script setup>` reaches any statement below
// this point.
let currentAnswerMessage = null

// --- session list (history/resume) ------------------------------------
// Plain REST via api/client.js -- deliberately does NOT touch
// useChatSocket.js at all. Fetching the list and loading a past session's
// history must not themselves open a WS connection; only an actual
// sendMessage() does that, and only for the duration of that one turn (see
// useChatSocket.js).
const sessions = ref([])
const sessionsLoading = ref(false)
const sessionsError = ref('')
const sidebarOpen = ref(true)
const historyLoading = ref(false)
const historyError = ref('')

async function fetchSessions() {
  sessionsLoading.value = true
  sessionsError.value = ''
  try {
    sessions.value = await api.get('/chat/sessions')
  } catch (err) {
    sessionsError.value = err instanceof ApiError ? err.message : 'Could not load past conversations.'
  } finally {
    sessionsLoading.value = false
  }
}

onMounted(fetchSessions)

// Slow tick that the sidebar timestamps read from, so "just now" / "2m ago"
// keep aging in place instead of freezing at whatever they were when the
// list last (re)fetched.
const nowTick = ref(Date.now())
let tickTimer = null
onMounted(() => {
  tickTimer = setInterval(() => {
    nowTick.value = Date.now()
  }, 30_000)
})
onBeforeUnmount(() => clearInterval(tickTimer))

// Tiny local relative-time formatter -- no date library dependency, this is
// the only place in the app that needs one.
function relativeTime(iso) {
  if (!iso) return ''
  const diffMs = nowTick.value - new Date(iso).getTime()
  const diffSec = Math.round(diffMs / 1000)
  if (diffSec < 5) return 'just now'
  if (diffSec < 60) return `${diffSec}s ago`
  const diffMin = Math.round(diffSec / 60)
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHour = Math.round(diffMin / 60)
  if (diffHour < 24) return `${diffHour}h ago`
  const diffDay = Math.round(diffHour / 24)
  if (diffDay < 30) return `${diffDay}d ago`
  const diffMonth = Math.round(diffDay / 30)
  if (diffMonth < 12) return `${diffMonth}mo ago`
  const diffYear = Math.round(diffMonth / 12)
  return `${diffYear}y ago`
}

// Single place that reacts to "the active session is now X", regardless of
// whether that came from the initial page load, clicking a past session,
// "New chat", a browser back/forward, or a pasted link -- all of those just
// change the `sessionId` route param, and this watcher is what actually
// replays history for it via GET /chat/sessions/{id}/messages, reusing the
// exact same {id, type: 'user'|'tool'|'answer', ...} shape/templates live
// turns already use (backend's app/routers/chat.py `_replay_messages`
// builds that shape directly) -- ids are re-minted through nextId() so they
// don't collide with ids any subsequent live turn generates.
//
// A brand-new "New chat" id (no ChatSession row yet -- rows are only
// minted once a first turn actually lands) comes back as a plain 200 + []
// from the backend, exactly like an existing-but-empty session, so the
// common new-chat path never even logs a console 404. The 404 tolerance in
// the catch below is kept as defensive back-compat (e.g. an older API
// container still running the previous semantics), not something the
// current backend emits for this route.
//
// Never opens a WS connection itself -- history replay is plain REST;
// sending a NEW message in the (possibly-resumed) session is what opens
// this turn's own socket (see useChatSocket.js).
async function loadSessionHistory(id) {
  historyError.value = ''
  messages.value = []
  sessionState.value = { active_filters: {}, last_result_asins: [], resolved_entity: null }
  currentAnswerMessage = null

  historyLoading.value = true
  try {
    const history = await api.get(`/chat/sessions/${encodeURIComponent(id)}/messages`)
    messages.value = history.map((m) => ({ ...m, id: nextId() }))
    scrollToBottom(true)
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 404)) {
      historyError.value = err instanceof ApiError ? err.message : 'Could not load that conversation.'
    }
  } finally {
    historyLoading.value = false
  }
}

watch(
  () => props.sessionId,
  (id) => {
    sessionId.value = id
    loadSessionHistory(id)
  },
  { immediate: true }
)

function selectSession(session) {
  if (turnInFlight.value || historyLoading.value) return
  if (session.session_id === sessionId.value) return
  router.push({ name: 'chat', params: { sessionId: session.session_id } })
}

// `active_filters` always carries backend defaults (sort/limit) even when
// the user never asked to filter — hide those so the panel only appears
// for filters the user actually set.
const DEFAULT_FILTER_KEYS = new Set(['sort', 'limit'])

const displayFilters = computed(() => {
  const filters = sessionState.value.active_filters || {}
  return Object.fromEntries(
    Object.entries(filters).filter(([key]) => !DEFAULT_FILTER_KEYS.has(key)),
  )
})

const hasActiveFilters = computed(() =>
  Boolean(Object.keys(displayFilters.value).length || sessionState.value.resolved_entity),
)

// Auto-follow: stay pinned to the bottom while deltas stream in, but the
// moment the user scrolls up to re-read something, stop yanking them back
// down on every token. Deliberate actions (sending a message, switching
// sessions, a turn error) re-engage following via `force`.
const followBottom = ref(true)
let lastScrollTop = 0

function onTranscriptScroll() {
  const el = transcriptEl.value
  if (!el) return
  // Disengage following only on an upward scroll that actually LEAVES the
  // bottom. Both halves of that condition matter:
  // - "distance from bottom" alone misfires when a large delta (a whole
  //   markdown table in one chunk) grows scrollHeight before the pin runs —
  //   the scroll event then observes a big gap the user never created;
  // - "went up" alone misfires because each delta re-renders the answer
  //   bubble's v-html, and the momentary DOM swap can shrink scrollHeight,
  //   making the browser clamp scrollTop DOWN — an upward move with no user
  //   involved. A clamp always lands exactly at the (new) bottom, though,
  //   so requiring "up AND away from the bottom" filters it out.
  const wentUp = el.scrollTop < lastScrollTop - 1
  lastScrollTop = el.scrollTop
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120
  if (nearBottom) {
    followBottom.value = true
  } else if (wentUp) {
    followBottom.value = false
  }
}

function scrollToBottom(force = false) {
  if (force) followBottom.value = true
  if (!followBottom.value) return
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
// `currentAnswerMessage` (declared near the top of this file, see comment
// there) tracks which bubble is still accepting deltas; answer_done clears
// it and flips `streaming` off (stops the blinking cursor). Only one answer
// stream is ever in flight at a time (the server only starts streaming the
// final answer after every tool call for the turn has already resolved),
// so there's no ambiguity about which bubble a delta belongs to.

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
    // End of every turn -- refetch the sidebar list so a brand-new
    // session (created by this very turn) shows up with its title, and an
    // existing one re-sorts to the top from its bumped `updated_at`,
    // without waiting for a manual page reload.
    fetchSessions()
  },
  onTurnError(data) {
    // A turn-level error can arrive mid-stream (e.g. the graph blew up
    // after already having streamed a few answer_delta events) — don't
    // leave a bubble stuck showing a blinking "still streaming" cursor.
    finalizeTurnWithError(data.detail)
  },
})

// The turn's socket can also disappear with no `error` frame at all --
// a network drop or the server crashing mid-turn. useChatSocket.js surfaces
// that as `status === 'error'`; this is the one place both failure shapes
// (an explicit `error` frame, and no frame at all) funnel through the same
// cleanup, so a dropped connection can never leave `turnInFlight` stuck
// `true` (which would otherwise permanently disable the composer).
function finalizeTurnWithError(detail) {
  if (currentAnswerMessage) {
    currentAnswerMessage.streaming = false
    currentAnswerMessage = null
  }
  messages.value.push({ id: nextId(), type: 'error', content: detail })
  turnInFlight.value = false
  scrollToBottom(true)
}

watch(
  () => socket.status.value,
  (value) => {
    if (value === 'error' && turnInFlight.value) {
      finalizeTurnWithError('Connection lost before the turn finished — please try again.')
    }
  }
)

// Sendable = no turn currently in flight. 'connecting'/'streaming' mean a
// turn is running on its own socket; 'error' and 'unauthorized' are settled
// failure states and MUST stay sendable — the error banner literally tells
// the user to try sending again (each send() opens a fresh socket, so
// retrying costs nothing), and a permanently disabled composer after one
// network blip would otherwise force a full page reload.
const canSend = computed(
  () => socket.status.value !== 'connecting' && socket.status.value !== 'streaming' && !turnInFlight.value
)

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
  scrollToBottom(true)
}

function startNewChat() {
  if (turnInFlight.value) return
  router.push({ name: 'chat', params: { sessionId: newChatSessionId() } })
}
</script>

<template>
  <div class="chat-layout">
    <aside class="chat-sidebar panel" :class="{ 'chat-sidebar-collapsed': !sidebarOpen }">
      <div class="sidebar-head">
        <span class="sidebar-title">History</span>
        <button
          type="button"
          class="btn-text"
          :aria-expanded="sidebarOpen"
          @click="sidebarOpen = !sidebarOpen"
        >
          {{ sidebarOpen ? 'Hide' : 'Show' }}
        </button>
      </div>

      <div v-if="sidebarOpen" class="sidebar-body">
        <button
          type="button"
          class="btn btn-ghost new-chat-btn"
          :disabled="turnInFlight"
          @click="startNewChat"
        >
          + New chat
        </button>
        <p v-if="sessionsLoading && !sessions.length" class="sidebar-hint">Loading…</p>
        <p v-else-if="sessionsError" class="sidebar-hint error-text">{{ sessionsError }}</p>
        <p v-else-if="!sessions.length" class="sidebar-hint">No past conversations yet.</p>
        <ul v-else class="session-list">
          <li v-for="s in sessions" :key="s.session_id">
            <button
              type="button"
              class="session-item"
              :class="{ 'session-item-active': s.session_id === sessionId }"
              :disabled="historyLoading"
              @click="selectSession(s)"
            >
              <span class="session-item-title" :title="s.title || ''">{{ s.title || 'New conversation' }}</span>
              <span class="session-item-time mono">{{ relativeTime(s.updated_at || s.created_at) }}</span>
            </button>
          </li>
        </ul>
      </div>
    </aside>

    <div class="chat-main">
      <div
        v-if="socket.status.value === 'unauthorized' || socket.status.value === 'error'"
        class="banner banner-error"
      >
        <template v-if="socket.status.value === 'unauthorized'">
          Your session expired or is invalid.
          <router-link to="/login">Log in again</router-link>.
        </template>
        <template v-else>Couldn't reach the chat service — please try sending your message again.</template>
      </div>

      <p v-if="historyError" class="banner banner-error">
        {{ historyError }}
      </p>

      <details v-if="hasActiveFilters" class="panel filters-disclosure">
        <summary>Active filters &amp; state</summary>
        <div class="tag-row" style="margin-top: var(--space-3)">
          <span v-for="(value, key) in displayFilters" :key="key" class="chip" style="cursor: default">
            {{ key }}: {{ value }}
          </span>
          <span v-if="sessionState.resolved_entity" class="chip" style="cursor: default">
            resolved: {{ sessionState.resolved_entity }}
          </span>
        </div>
      </details>

      <section class="panel transcript-panel">
        <div ref="transcriptEl" class="transcript" @scroll.passive="onTranscriptScroll">
          <p v-if="historyLoading" class="empty-hint">Loading conversation…</p>
          <p v-else-if="!messages.length" class="empty-hint">
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
            :placeholder="canSend ? 'Ask about eligibility, ROI, filters, combos…' : 'Scout is thinking…'"
            aria-label="Message"
            @keydown.enter.exact.prevent="sendMessage"
          ></textarea>
          <button type="submit" class="btn btn-primary" :disabled="!canSend || !inputText.trim()">
            {{ turnInFlight ? 'Thinking…' : 'Send' }}
          </button>
        </form>
      </section>
    </div>
  </div>
</template>

<style scoped>
.chat-layout {
  display: flex;
  align-items: flex-start;
  gap: var(--space-5);
}

.chat-main {
  flex: 1;
  min-width: 0;
}

.chat-sidebar {
  flex: 0 0 16rem;
  padding: var(--space-4);
  position: sticky;
  top: var(--space-5);
}

.chat-sidebar-collapsed {
  flex-basis: auto;
  padding: var(--space-3) var(--space-4);
}

.sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.sidebar-title {
  font-family: var(--font-body);
  font-size: var(--text-sm);
  font-weight: 700;
  letter-spacing: -0.005em;
  color: var(--ink);
}

.sidebar-body {
  margin-top: var(--space-3);
}

.new-chat-btn {
  width: 100%;
  padding: var(--space-2) var(--space-4);
  margin-bottom: var(--space-3);
  font-size: var(--text-sm);
}

.sidebar-hint {
  font-size: var(--text-xs);
  color: var(--steel);
  margin: 0;
}

.session-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: 30rem;
  overflow-y: auto;
}

.session-item {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
  text-align: left;
  color: var(--ink);
  font-family: var(--font-body);
}

.session-item:hover:not(:disabled) {
  background: var(--paper);
  border-color: var(--line);
}

.session-item:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.session-item-active {
  background: var(--blue-soft);
  border-color: var(--blue);
}

.session-item-title {
  font-size: var(--text-sm);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

.session-item-time {
  font-size: var(--text-xs);
  color: var(--steel);
}

.session-item-active .session-item-time {
  color: var(--blue-dark);
}

@media (max-width: 56rem) {
  .chat-layout {
    flex-direction: column;
  }

  .chat-sidebar {
    flex-basis: auto;
    width: 100%;
    position: static;
  }

  .session-list {
    max-height: 14rem;
  }
}

/* The chat screen is an app view, not a document page — trim the .page
   shell's generous editorial padding down to app chrome so the transcript
   gets the vertical space instead. Scoped styles can't reach the parent
   AppShell element, hence :global + :has. */
:global(.page:has(.chat-layout)) {
  padding: var(--space-4) 0;
}

.chat-main > .banner {
  margin: 0 0 var(--space-5);
}

.filters-disclosure {
  margin: 0 0 var(--space-5);
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
  /* display:block + overflow-x so a wide LLM-generated table scrolls inside
     the bubble instead of blowing the transcript open horizontally. */
  display: block;
  overflow-x: auto;
  width: 100%;
  margin: var(--space-2) 0;
  font-size: var(--text-xs);
}

.markdown-body :deep(code) {
  font-family: var(--font-mono);
  background: var(--paper);
  padding: 0 0.2em;
}

/* Desktop: the whole chat view fits the viewport — exactly one scrollbar
   (inside the transcript), composer always on screen, no page-level
   scrolling to find the input and no scroll-inside-scroll. 6.5rem accounts
   for the 4.5rem header plus the trimmed --space-4 top/bottom .page padding
   (see the :global(.page:has(.chat-layout)) override above). Mobile (the
   max-width query above) keeps the stacked, naturally-flowing layout. */
@media (min-width: 56.0625rem) {
  .chat-layout {
    height: max(28rem, calc(100vh - 6.5rem));
    align-items: stretch;
  }

  .chat-sidebar {
    position: static;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  /* A collapsed sidebar is just its head row — don't stretch it into a
     tall empty column. */
  .chat-sidebar-collapsed {
    align-self: flex-start;
  }

  .sidebar-body {
    min-height: 0;
    overflow-y: auto;
  }

  .session-list {
    max-height: none;
  }

  .chat-main {
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  .transcript-panel {
    flex: 1;
    min-height: 0;
  }

  .transcript {
    /* Keep a usable floor even on short viewports (the panel then overflows
       the viewport-fit height and the page scrolls a little — better than a
       letterbox-thin transcript). */
    min-height: 14rem;
    max-height: none;
  }
}
</style>
