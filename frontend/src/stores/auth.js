import { defineStore } from 'pinia'
import api from '@/api'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('cab_token') || '',
    user: JSON.parse(localStorage.getItem('cab_user') || 'null'),
  }),
  getters: {
    isLogin: (s) => !!s.token,
    role: (s) => s.user?.role || 'viewer',
  },
  actions: {
    async login(username, password) {
      const res = await api.post('/api/auth/login', { username, password })
      this.token = res.access_token
      localStorage.setItem('cab_token', this.token)
      const me = await api.get('/api/auth/me')
      this.user = me
      localStorage.setItem('cab_user', JSON.stringify(me))
    },
    logout() {
      this.token = ''
      this.user = null
      localStorage.removeItem('cab_token')
      localStorage.removeItem('cab_user')
    },
  },
})
