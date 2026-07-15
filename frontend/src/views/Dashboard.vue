<script setup>
/*
  Design note on why this page is "ASIN lookup", not "ASIN list":

  There is no `GET /asins` / "list everything" endpoint in the backend —
  only `GET /eligibility/{asin}` (one ASIN) and `POST /eligibility/batch`
  (`{"asins": [...]}`, an explicit list you provide — see
  app/routers/eligibility.py). Building a page around a list endpoint that
  doesn't exist would mean faking pagination over nothing.

  So this page treats single-ASIN lookup as the primary flow (the thing
  someone reaches for first — "is THIS ASIN worth buying") and batch paste
  as a secondary mode for "I have a list of candidates, check them all
  at once". The quick-pick chips below are a handful of ASINs from
  data/sample_asins.csv (the demo catalog the ETL actually loads) so the
  page isn't a dead end on first load — a fresh visitor has something to
  click without needing to already know an ASIN.
*/
import { ref, computed } from 'vue'
import { api, ApiError } from '../api/client'

const DEMO_ASINS = [
  'B010MU00UM',
  'B0CPRLHYRB',
  'B0DJDMVQJG',
  'B0BZ5DMMS4',
  'B006JVZXJM',
  'B001FB5MBK',
]

const mode = ref('single') // 'single' | 'batch'

// --- single lookup ------------------------------------------------------
const asinInput = ref('')
const singleResult = ref(null)
const singleError = ref('')
const singleLoading = ref(false)

async function lookupSingle(asin) {
  const target = (asin || asinInput.value).trim().toUpperCase()
  if (!target) {
    singleError.value = 'Enter an ASIN first.'
    return
  }
  asinInput.value = target
  singleError.value = ''
  singleResult.value = null
  singleLoading.value = true
  try {
    singleResult.value = await api.get(`/eligibility/${encodeURIComponent(target)}`)
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      singleError.value = `${target} isn't in the catalog yet — it hasn't been ETL'd / refreshed.`
    } else if (err instanceof ApiError) {
      singleError.value = err.message
    } else {
      singleError.value = 'Lookup failed.'
    }
  } finally {
    singleLoading.value = false
  }
}

// --- batch lookup ---------------------------------------------------------
const batchInput = ref(DEMO_ASINS.join('\n'))
const batchResults = ref(null)
const batchError = ref('')
const batchLoading = ref(false)

const parsedBatchAsins = computed(() =>
  batchInput.value
    .split(/[\s,]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean),
)

async function lookupBatch() {
  batchError.value = ''
  batchResults.value = null
  const asins = parsedBatchAsins.value
  if (asins.length === 0) {
    batchError.value = 'Paste at least one ASIN.'
    return
  }
  batchLoading.value = true
  try {
    batchResults.value = await api.post('/eligibility/batch', { asins })
  } catch (err) {
    batchError.value = err instanceof ApiError ? err.message : 'Batch lookup failed.'
  } finally {
    batchLoading.value = false
  }
}

function fmtMoney(v) {
  return v === null || v === undefined ? '—' : `$${Number(v).toFixed(2)}`
}
function fmtPct(v) {
  return v === null || v === undefined ? '—' : `${Number(v).toFixed(1)}%`
}
function fmtDate(v) {
  if (!v) return '—'
  return new Date(v).toLocaleString()
}
function fmtCheckValue(v) {
  if (v === null || v === undefined) return '—'
  return typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(1)) : v
}
</script>

<template>
  <div>
    <h1>ASIN lookup</h1>

    <div class="mode-toggle" role="tablist">
      <button
        type="button"
        role="tab"
        class="mode-btn"
        :class="{ active: mode === 'single' }"
        @click="mode = 'single'"
      >
        Single ASIN
      </button>
      <button
        type="button"
        role="tab"
        class="mode-btn"
        :class="{ active: mode === 'batch' }"
        @click="mode = 'batch'"
      >
        Batch paste
      </button>
    </div>

    <!-- ============================= SINGLE ============================= -->
    <section v-if="mode === 'single'" class="panel">
      <form class="lookup-row" @submit.prevent="lookupSingle()">
        <input
          id="single-asin"
          v-model="asinInput"
          type="text"
          name="asin"
          class="mono"
          placeholder="e.g. B010MU00UM"
          aria-label="ASIN"
        />
        <button type="submit" class="btn btn-primary" :disabled="singleLoading">
          {{ singleLoading ? 'Checking…' : 'Check' }}
        </button>
      </form>

      <div class="tag-row" style="margin-top: var(--space-3)">
        <button
          v-for="demo in DEMO_ASINS"
          :key="demo"
          type="button"
          class="chip"
          @click="lookupSingle(demo)"
        >
          {{ demo }}
        </button>
      </div>

      <p v-if="singleError" class="error-text">{{ singleError }}</p>

      <div v-if="singleResult" class="result">
        <hr class="divider" />
        <div class="result-head">
          <div>
            <h2 class="asin-title mono">{{ singleResult.asin }}</h2>
            <p v-if="singleResult.title" class="result-subtitle">{{ singleResult.title }}</p>
          </div>
          <span class="stamp" :class="singleResult.eligible ? 'stamp-pass' : 'stamp-fail'">
            {{ singleResult.eligible ? 'Eligible' : `Rejected: ${singleResult.filter_failed}` }}
          </span>
        </div>

        <div class="metrics">
          <div class="metric">
            <span class="metric-label">ROI</span>
            <span class="metric-value">{{ fmtPct(singleResult.computed_roi_pct) }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">BuyBox</span>
            <span class="metric-value">{{ fmtMoney(singleResult.buybox) }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">Supplier cost</span>
            <span class="metric-value">{{ fmtMoney(singleResult.supplier_cost) }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">Amazon BuyBox share</span>
            <span class="metric-value">{{ fmtPct(singleResult.amazon_buybox_pct) }}</span>
          </div>
        </div>

        <table>
          <thead>
            <tr>
              <th>Check</th>
              <th>Result</th>
              <th>Value</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(check, name) in singleResult.checks" :key="name">
              <td class="mono">{{ name }}</td>
              <td :style="{ color: check.pass ? 'var(--teal)' : 'var(--brick)' }">
                {{ check.pass ? 'pass' : 'fail' }}
              </td>
              <td class="mono">{{ fmtCheckValue(check.value) }}<span v-if="check.threshold"> / threshold {{ check.threshold }}</span></td>
            </tr>
          </tbody>
        </table>

        <p class="field-hint">Snapshot taken {{ fmtDate(singleResult.snapshot_at) }}</p>
        <p v-if="singleResult.data_freshness_note" class="banner banner-error" style="margin-top: var(--space-3)">
          {{ singleResult.data_freshness_note }}
        </p>
        <p v-if="singleResult.price_anomaly_note" class="banner" style="margin-top: var(--space-3)">
          {{ singleResult.price_anomaly_note }}
        </p>
      </div>
    </section>

    <!-- ============================== BATCH ============================== -->
    <section v-else class="panel">
      <div class="field">
        <label for="batch">ASINs — one per line, or comma-separated</label>
        <textarea id="batch" v-model="batchInput"></textarea>
        <p class="field-hint">{{ parsedBatchAsins.length }} ASIN(s) parsed</p>
      </div>
      <button type="button" class="btn btn-primary" :disabled="batchLoading" @click="lookupBatch">
        {{ batchLoading ? 'Checking…' : 'Check batch' }}
      </button>

      <p v-if="batchError" class="error-text">{{ batchError }}</p>

      <table v-if="batchResults" style="margin-top: var(--space-5)">
        <thead>
          <tr>
            <th>ASIN</th>
            <th>Status</th>
            <th>ROI</th>
            <th>BuyBox</th>
            <th>Amazon share</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in batchResults" :key="row.asin">
            <td class="mono">{{ row.asin }}</td>
            <td v-if="row.error">
              <span class="stamp stamp-fail" style="transform: none">not found</span>
            </td>
            <template v-else>
              <td :style="{ color: row.eligible ? 'var(--teal)' : 'var(--brick)' }">
                {{ row.eligible ? 'eligible' : `fail: ${row.filter_failed}` }}
              </td>
              <td class="mono">{{ fmtPct(row.computed_roi_pct) }}</td>
              <td class="mono">{{ fmtMoney(row.buybox) }}</td>
              <td class="mono">{{ fmtPct(row.amazon_buybox_pct) }}</td>
            </template>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.mode-toggle {
  display: flex;
  gap: var(--space-1);
  margin-bottom: var(--space-5);
  border-bottom: 1px solid var(--line);
}

.mode-btn {
  font-family: var(--font-body);
  font-size: var(--text-sm);
  font-weight: 600;
  letter-spacing: -0.005em;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  padding: var(--space-3) var(--space-2);
  color: var(--steel);
  cursor: pointer;
  margin-bottom: -1px;
}

.mode-btn.active {
  color: var(--ink);
  border-bottom-color: var(--blue);
}

.lookup-row {
  display: flex;
  gap: var(--space-3);
}

.lookup-row input {
  flex: 1;
}

.result-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

.asin-title {
  margin: 0;
}

.result-subtitle {
  margin: var(--space-1) 0 0;
  color: var(--steel);
  font-size: var(--text-sm);
  max-width: 34rem;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: var(--space-4);
  margin-bottom: var(--space-5);
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
