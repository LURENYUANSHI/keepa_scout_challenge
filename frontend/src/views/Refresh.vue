<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { api, ApiError } from '../api/client'

const POLL_INTERVAL_MS = 2000

const status = ref(null)
const error = ref('')
const starting = ref(false)
let pollTimer = null

const progressPct = computed(() => {
  if (!status.value || !status.value.total) return 0
  return Math.round((100 * status.value.done) / status.value.total)
})

function fmtDate(v) {
  if (!v) return 'never'
  return new Date(v).toLocaleString()
}

async function fetchStatus() {
  try {
    status.value = await api.get('/refresh/status')
    error.value = ''
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : 'Could not load refresh status.'
  }
}

function schedulePoll() {
  stopPoll()
  pollTimer = setInterval(async () => {
    await fetchStatus()
    if (status.value && status.value.state !== 'running') {
      stopPoll()
    }
  }, POLL_INTERVAL_MS)
}

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function startRefresh() {
  starting.value = true
  error.value = ''
  try {
    status.value = await api.post('/refresh', {})
    if (status.value.state === 'running') {
      schedulePoll()
    }
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : 'Could not start refresh.'
  } finally {
    starting.value = false
  }
}

onMounted(async () => {
  await fetchStatus()
  if (status.value && status.value.state === 'running') {
    schedulePoll()
  }
})

onUnmounted(() => {
  stopPoll()
})
</script>

<template>
  <div>
    <h1>Refresh</h1>

    <section class="panel">
      <div class="refresh-head">
        <div>
          <span class="stamp" :class="status && status.state === 'done' ? 'stamp-pass' : 'stamp-fail'" v-if="status && status.state">
            {{ status.state }}
          </span>
          <span v-else class="metric-label">no job has run yet</span>
        </div>
        <button type="button" class="btn btn-primary" :disabled="starting || (status && status.state === 'running')" @click="startRefresh">
          {{ status && status.state === 'running' ? 'Refresh running…' : starting ? 'Starting…' : 'Start refresh' }}
        </button>
      </div>

      <p v-if="error" class="error-text">{{ error }}</p>

      <template v-if="status && status.job_id">
        <div class="progress-track" role="progressbar" :aria-valuenow="progressPct" aria-valuemin="0" aria-valuemax="100">
          <div class="progress-fill" :style="{ width: progressPct + '%' }"></div>
        </div>

        <div class="metrics">
          <div class="metric">
            <span class="metric-label">Job</span>
            <span class="metric-value mono">{{ status.job_id }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">Done</span>
            <span class="metric-value">{{ status.done }} / {{ status.total }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">Failed</span>
            <span class="metric-value" :style="{ color: status.failed ? 'var(--brick)' : 'inherit' }">{{ status.failed }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">Last activity</span>
            <span class="metric-value">{{ fmtDate(status.last_refresh_at) }}</span>
          </div>
        </div>

        <p v-if="status.state === 'running'" class="field-hint">Polling every 2s while the job is running…</p>
      </template>

      <p v-else-if="status" class="field-hint">
        No refresh has ever run. Starting one will pull fresh Keepa data for every catalog ASIN and
        recompute eligibility/ROI.
      </p>
    </section>
  </div>
</template>

<style scoped>
.refresh-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-5);
  min-height: 2.5rem;
}

.progress-track {
  height: 0.5rem;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: var(--radius-pill);
  margin-bottom: var(--space-5);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--blue);
  border-radius: var(--radius-pill);
  transition: width 300ms ease;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: var(--space-4);
  padding: var(--space-4) var(--space-5);
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
}

.metric {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.metric-label {
  font-size: var(--text-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--steel);
}

.metric-value {
  font-family: var(--font-mono);
  font-size: var(--text-lg);
  font-weight: 600;
}
</style>
