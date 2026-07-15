// Thin fetch wrapper shared by every page.
//
// - Base URL comes from VITE_API_BASE_URL (falls back to localhost:8000 so
//   `npm run dev` works out of the box against `docker compose up api`).
// - Every call attaches `Authorization: Bearer <token>` automatically, read
//   straight from localStorage rather than importing the Pinia store —
//   importing `useAuthStore` here would work too, but reading the persisted
//   value directly avoids any import-order/circularity risk between the
//   store module and this module (the store itself calls into this file).
// - A 401 means the token is gone/expired/revoked: clear it and hard-redirect
//   to /login. This is intentionally a plain `window.location` redirect
//   rather than a router.push — client.js is called from Pinia actions that
//   may run outside a component context, and a full reload also guarantees
//   any in-memory state tied to the stale session is wiped.

const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const TOKEN_KEY = 'scout.auth.token'

// WS endpoint (used by Chat.vue's WS /chat/stream connection) — same host
// as BASE_URL, ws(s) scheme instead of http(s). `VITE_WS_BASE_URL` is an
// explicit override for setups where the WS endpoint genuinely differs
// (e.g. behind a separate load balancer); nothing in docker-compose.yml
// sets it today, so the common case is just deriving it from
// VITE_API_BASE_URL, same "baked in at Vite build time" rule as BASE_URL.
const WS_BASE_URL = import.meta.env.VITE_WS_BASE_URL || BASE_URL.replace(/^http/, 'ws')

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token)
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
}

class ApiError extends Error {
  constructor(message, status, body) {
    super(message)
    this.status = status
    this.body = body
  }
}

async function request(path, { method = 'GET', body, auth = true, headers = {} } = {}) {
  const finalHeaders = { ...headers }
  let finalBody = body

  if (body !== undefined) {
    finalHeaders['Content-Type'] = 'application/json'
    finalBody = JSON.stringify(body)
  }

  if (auth) {
    const token = getToken()
    if (token) {
      finalHeaders['Authorization'] = `Bearer ${token}`
    }
  }

  let res
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      method,
      headers: finalHeaders,
      body: finalBody,
    })
  } catch (networkErr) {
    throw new ApiError(
      `Could not reach the API at ${BASE_URL} — is it running? (${networkErr.message})`,
      0,
      null,
    )
  }

  if (res.status === 401 && auth) {
    setToken(null)
    if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
      window.location.href = '/login'
    }
    throw new ApiError('Session expired. Please log in again.', 401, null)
  }

  const contentType = res.headers.get('content-type') || ''
  const payload = contentType.includes('application/json') ? await res.json() : await res.text()

  if (!res.ok) {
    const detail =
      (payload && typeof payload === 'object' && payload.detail) ||
      (typeof payload === 'string' && payload) ||
      `Request failed with status ${res.status}`
    throw new ApiError(typeof detail === 'string' ? detail : JSON.stringify(detail), res.status, payload)
  }

  return payload
}

export const api = {
  get: (path, opts) => request(path, { ...opts, method: 'GET' }),
  post: (path, body, opts) => request(path, { ...opts, method: 'POST', body }),
}

export { ApiError, BASE_URL, WS_BASE_URL }
