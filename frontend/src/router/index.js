import { createRouter, createWebHistory } from 'vue-router'
import AppShell from '@/components/AppShell.vue'

const routes = [
  { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { public: true } },
  {
    path: '/',
    component: AppShell,
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', name: 'dashboard', component: () => import('@/views/DashboardView.vue'), meta: { title: '态势总览' } },
      { path: 'realtime', name: 'realtime', component: () => import('@/views/RealtimeView.vue'), meta: { title: '实时检测' } },
      { path: 'video', name: 'video', component: () => import('@/views/VideoView.vue'), meta: { title: '视频检测' } },
      { path: 'alarms', name: 'alarms', component: () => import('@/views/AlarmsView.vue'), meta: { title: '报警处置' } },
      { path: 'history', name: 'history', component: () => import('@/views/HistoryView.vue'), meta: { title: '历史取证' } },
      { path: 'cameras', name: 'cameras', component: () => import('@/views/CamerasView.vue'), meta: { title: '点位管理' } },
      { path: 'wall', name: 'wall', component: () => import('@/views/CameraWallView.vue'), meta: { title: '点位态势墙' } },
      { path: 'keywords', name: 'keywords', component: () => import('@/views/KeywordView.vue'), meta: { title: '关键词管理' } },
      { path: 'persons', name: 'persons', component: () => import('@/views/PersonView.vue'), meta: { title: '人员管理' } },
      { path: 'settings', name: 'settings', component: () => import('@/views/SettingsView.vue'), meta: { title: '系统设置' } },
      { path: 'users', name: 'users', component: () => import('@/views/UsersView.vue'), meta: { title: '用户与权限' } }
    ]
  },
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' }
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  const token = localStorage.getItem('cab_token')
  if (!to.meta.public && !token) return { name: 'login' }
  if (to.name === 'login' && token) return { name: 'dashboard' }
})

export default router
