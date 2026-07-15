import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/Login.vue'),
    meta: { public: true },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('../views/Register.vue'),
    meta: { public: true },
  },
  {
    path: '/',
    name: 'dashboard',
    component: () => import('../views/Dashboard.vue'),
  },
  {
    path: '/upc',
    name: 'upc',
    component: () => import('../views/Upc.vue'),
  },
  {
    path: '/refresh',
    name: 'refresh',
    component: () => import('../views/Refresh.vue'),
  },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('../views/Chat.vue'),
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.meta.public && auth.isAuthenticated) {
    return { name: 'dashboard' }
  }
  return true
})

export default router
