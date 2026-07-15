<script setup>
import { ref } from 'vue'
import { api, ApiError } from '../api/client'

const upcInput = ref('')
const result = ref(null)
const error = ref('')
const loading = ref(false)

async function lookup() {
  error.value = ''
  result.value = null
  const value = upcInput.value.trim()
  if (!value) {
    error.value = 'Enter a UPC or EAN first.'
    return
  }
  loading.value = true
  try {
    result.value = await api.get(`/upc?upc=${encodeURIComponent(value)}`)
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : 'Lookup failed.'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div>
    <h1>UPC → ASIN</h1>

    <section class="panel">
      <form class="lookup-row" @submit.prevent="lookup">
        <input
          id="upc-input"
          v-model="upcInput"
          type="text"
          name="upc"
          class="mono"
          placeholder="e.g. 070537500052"
          aria-label="UPC or EAN"
        />
        <button type="submit" class="btn btn-primary" :disabled="loading">
          {{ loading ? 'Looking up…' : 'Look up' }}
        </button>
      </form>
      <p class="field-hint">
        Accepts 11/12/13/14-digit codes and dirty input (dashes, spaces) — the backend tries
        length-based variants (zero-padding, EAN-13/GTIN-14 stripping) until one resolves.
      </p>

      <p v-if="error" class="error-text">{{ error }}</p>

      <div v-if="result" class="result">
        <hr class="divider" />

        <div class="field">
          <span class="metric-label">Input</span>
          <p class="mono" style="margin: var(--space-1) 0 0">{{ result.input }}</p>
        </div>

        <div class="field">
          <span class="metric-label">Variants tried, in order</span>
          <div class="tag-row" style="margin-top: var(--space-2)">
            <span v-for="(variant, idx) in result.normalized" :key="variant" class="chip" style="cursor: default">
              {{ idx + 1 }}. {{ variant }}
            </span>
          </div>
        </div>

        <div class="field">
          <span class="metric-label">Resolved ASINs</span>
          <div v-if="result.asins.length" class="tag-row" style="margin-top: var(--space-2)">
            <span v-for="asin in result.asins" :key="asin" class="stamp stamp-pass" style="transform: none">
              {{ asin }}
            </span>
          </div>
          <p v-else class="banner banner-error" style="margin-top: var(--space-2)">
            No ASIN resolved for this code — none of the variants tried matched a Keepa product.
          </p>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.lookup-row {
  display: flex;
  gap: var(--space-3);
}

.lookup-row input {
  flex: 1;
}

.metric-label {
  font-size: var(--text-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--steel);
}
</style>
