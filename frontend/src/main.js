import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App)

/* ---------------- 前端异常归集 ----------------
 * 把前端异常上报到后端日志缓冲（POST /api/system/logs/client），
 * 这样"哪里出错了"在控制台的「运行日志与异常」面板里一次看全，不必再开浏览器控制台。
 *
 * 做了去重与限流：同一异常 30 秒内只上报一次、每分钟最多 10 条。
 * 这既避免异常风暴刷满日志，也避免"上报失败 → 再次抛异常 → 又上报"的死循环。
 */
const recentErrors = new Map()
let windowStart = Date.now()
let windowCount = 0

function reportError(level, source, message, stack) {
  if (!message) return
  const key = `${source}|${message}`
  const now = Date.now()
  if (now - (recentErrors.get(key) || 0) < 30000) return
  recentErrors.set(key, now)
  if (recentErrors.size > 200) recentErrors.clear()

  if (now - windowStart > 60000) {
    windowStart = now
    windowCount = 0
  }
  if (windowCount >= 10) return
  windowCount += 1

  try {
    fetch('/api/system/logs/client', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        level,
        source,
        message: String(message).slice(0, 2000),
        stack: stack ? String(stack).slice(0, 4000) : null
      })
    }).catch(() => { /* 上报失败静默，不能再抛 */ })
  } catch { /* 同上 */ }
}

// Vue 组件内异常
app.config.errorHandler = (err, _instance, info) => {
  reportError('error', `vue:${info}`, err?.message || String(err), err?.stack)
}
// 未捕获异常
window.addEventListener('error', (e) => {
  if (e?.message) reportError('error', 'window.onerror', e.message, e.error?.stack)
})
// 未处理的 Promise 拒绝
window.addEventListener('unhandledrejection', (e) => {
  const r = e?.reason
  reportError('error', 'unhandledrejection', r?.message || String(r), r?.stack)
})

app.use(createPinia()).use(router).mount('#app')
