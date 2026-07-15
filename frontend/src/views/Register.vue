<script setup>
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore, ApiError } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()

const email = ref('')
const password = ref('')
const confirmPassword = ref('')
const submitting = ref(false)
const errorMessage = ref('')

// Mirrors app/routers/auth.py's MIN/MAX_PASSWORD_BYTES so the person gets
// instant feedback instead of waiting on a round trip for the obvious case.
// The server remains the source of truth (utf-8 byte length, not char
// length) — this is just a fast local pre-check.
const passwordByteLength = computed(() => new TextEncoder().encode(password.value).length)
const passwordTooShort = computed(() => password.value.length > 0 && passwordByteLength.value < 8)
const passwordsMismatch = computed(
  () => confirmPassword.value.length > 0 && password.value !== confirmPassword.value,
)

async function handleSubmit() {
  errorMessage.value = ''

  if (!email.value || !password.value) {
    errorMessage.value = 'Enter an email and a password.'
    return
  }
  if (passwordByteLength.value < 8) {
    errorMessage.value = 'Password is too short — use at least 8 characters.'
    return
  }
  if (password.value !== confirmPassword.value) {
    errorMessage.value = 'Passwords do not match.'
    return
  }

  submitting.value = true
  try {
    await auth.register(email.value.trim(), password.value)
    router.push('/')
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      errorMessage.value = 'That email is already registered — log in instead.'
    } else if (err instanceof ApiError) {
      errorMessage.value = err.message
    } else {
      errorMessage.value = 'Something went wrong. Try again.'
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-panel panel">
      <p class="eyebrow">Scout access</p>
      <h1>Create an account</h1>

      <form @submit.prevent="handleSubmit" novalidate>
        <div class="field">
          <label for="email">Email</label>
          <input id="email" v-model="email" type="email" autocomplete="username" placeholder="you@example.com" />
        </div>
        <div class="field">
          <label for="password">Password</label>
          <input
            id="password"
            v-model="password"
            type="password"
            autocomplete="new-password"
            placeholder="At least 8 characters"
          />
          <p v-if="passwordTooShort" class="field-hint" style="color: var(--brick)">
            {{ passwordByteLength }}/8 characters minimum
          </p>
        </div>
        <div class="field">
          <label for="confirm">Confirm password</label>
          <input
            id="confirm"
            v-model="confirmPassword"
            type="password"
            autocomplete="new-password"
            placeholder="Repeat password"
          />
          <p v-if="passwordsMismatch" class="field-hint" style="color: var(--brick)">Passwords don't match</p>
        </div>

        <p v-if="errorMessage" class="error-text" role="alert">{{ errorMessage }}</p>

        <button type="submit" class="btn btn-primary" :disabled="submitting" style="width: 100%">
          {{ submitting ? 'Creating account…' : 'Create account' }}
        </button>
      </form>

      <p class="switch">
        Already registered? <router-link to="/login">Log in</router-link>
      </p>
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-5);
}

.auth-panel {
  width: 100%;
  max-width: 24rem;
}

.switch {
  margin: var(--space-4) 0 0;
  font-size: var(--text-sm);
  color: var(--steel);
}
</style>
