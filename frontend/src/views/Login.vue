<script setup>
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore, ApiError } from '../stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const email = ref('')
const password = ref('')
const submitting = ref(false)
const errorMessage = ref('')

async function handleSubmit() {
  errorMessage.value = ''
  if (!email.value || !password.value) {
    errorMessage.value = 'Enter both an email and a password.'
    return
  }
  submitting.value = true
  try {
    await auth.login(email.value.trim(), password.value)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    router.push(redirect)
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      errorMessage.value = 'Incorrect email or password.'
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
      <h1>Log in</h1>

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
            autocomplete="current-password"
            placeholder="••••••••"
          />
        </div>

        <p v-if="errorMessage" class="error-text" role="alert">{{ errorMessage }}</p>

        <button type="submit" class="btn btn-primary" :disabled="submitting" style="width: 100%">
          {{ submitting ? 'Logging in…' : 'Log in' }}
        </button>
      </form>

      <p class="switch">
        No account yet? <router-link to="/register">Register one</router-link>
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
