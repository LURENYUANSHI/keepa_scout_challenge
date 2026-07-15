<script setup>
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()

const links = [
  { to: '/', label: 'Lookup' },
  { to: '/upc', label: 'UPC' },
  { to: '/refresh', label: 'Refresh' },
  { to: '/chat', label: 'Chat' },
]

function handleLogout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <header class="header">
    <div class="shell header-inner">
      <router-link to="/" class="mark" aria-label="Scout home">
        <span class="mark-bars" aria-hidden="true"></span>
        <span class="mark-text">SCOUT</span>
      </router-link>

      <nav class="nav">
        <router-link
          v-for="link in links"
          :key="link.to"
          :to="link.to"
          class="nav-link"
          active-class="nav-link-active"
        >
          {{ link.label }}
        </router-link>
      </nav>

      <div class="account">
        <span v-if="auth.user" class="account-email">{{ auth.user.email }}</span>
        <button type="button" class="btn-text" @click="handleLogout">Log out</button>
      </div>
    </div>
  </header>
  <main class="shell page">
    <slot />
  </main>
</template>

<style scoped>
.header {
  border-bottom: 1px solid var(--line);
  background: var(--raised);
}

.header-inner {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  height: 4.5rem;
}

.mark {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  text-decoration: none;
  color: var(--ink);
}

.mark-bars {
  display: inline-block;
  width: 1.1rem;
  height: 1.35rem;
  border-radius: 3px;
  background: var(--blue);
}

.mark-text {
  font-family: var(--font-display);
  font-weight: 800;
  font-size: var(--text-md);
  letter-spacing: -0.02em;
}

.nav {
  display: flex;
  gap: var(--space-6);
  flex: 1;
}

.nav-link {
  font-family: var(--font-body);
  font-size: var(--text-sm);
  font-weight: 600;
  letter-spacing: -0.005em;
  text-decoration: none;
  color: var(--steel);
  padding: var(--space-2) 0;
  border-bottom: 2px solid transparent;
}

.nav-link:hover {
  color: var(--ink);
}

.nav-link-active {
  color: var(--ink);
  border-bottom-color: var(--blue);
}

.account {
  display: flex;
  align-items: center;
  gap: var(--space-4);
}

.account-email {
  font-family: var(--font-body);
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--steel);
}

@media (max-width: 40rem) {
  .header-inner {
    flex-wrap: wrap;
    height: auto;
    padding-block: var(--space-3);
  }

  .nav {
    order: 3;
    width: 100%;
    gap: var(--space-4);
  }
}
</style>
