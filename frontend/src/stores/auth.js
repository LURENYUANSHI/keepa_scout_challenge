import { defineStore } from 'pinia'
import { api, getToken, setToken, ApiError } from '../api/client'

// Persisted alongside the token so a page refresh doesn't lose "who am I".
// The backend's TokenResponse (app/schemas/auth.py) only returns
// {access_token, expires_at} — there's no /auth/me or user object — so the
// email the person typed at login/register time is the only identity we
// have client-side. Good enough for "logged in as ___" display purposes.
const USER_KEY = 'scout.auth.user'

function loadUser() {
  const raw = localStorage.getItem(USER_KEY)
  return raw ? JSON.parse(raw) : null
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: getToken(),
    user: loadUser(),
    expiresAt: localStorage.getItem('scout.auth.expiresAt') || null,
  }),

  getters: {
    isAuthenticated: (state) => Boolean(state.token),
  },

  actions: {
    _persist(token, email, expiresAt) {
      this.token = token
      this.user = email ? { email } : null
      this.expiresAt = expiresAt || null
      setToken(token)
      if (email) {
        localStorage.setItem(USER_KEY, JSON.stringify({ email }))
      } else {
        localStorage.removeItem(USER_KEY)
      }
      if (expiresAt) {
        localStorage.setItem('scout.auth.expiresAt', expiresAt)
      } else {
        localStorage.removeItem('scout.auth.expiresAt')
      }
    },

    async register(email, password) {
      // POST /auth/register — 201 + auto-issued token (app/routers/auth.py:
      // "register auto-logs-in"), or 409 if the email is already taken, or
      // 400 if the password fails the length policy (8-72 bytes).
      const res = await api.post('/auth/register', { email, password }, { auth: false })
      this._persist(res.access_token, email, res.expires_at)
      return res
    },

    async login(email, password) {
      // POST /auth/login — 200 + fresh token, or 401 on bad credentials
      // (same message for "no such user" and "wrong password" by design).
      const res = await api.post('/auth/login', { email, password }, { auth: false })
      this._persist(res.access_token, email, res.expires_at)
      return res
    },

    logout() {
      this._persist(null, null, null)
    },
  },
})

export { ApiError }
