import { reactive } from 'vue'

/** 全局轻提示队列（替代组件库的 Message） */
export const toasts = reactive([])

let seq = 0

function push(type, text, ms = 2600) {
  const id = ++seq
  toasts.push({ id, type, text })
  setTimeout(() => {
    const i = toasts.findIndex((t) => t.id === id)
    if (i >= 0) toasts.splice(i, 1)
  }, ms)
}

export const toast = {
  ok: (t, ms) => push('ok', t, ms),
  err: (t, ms) => push('err', t, ms),
  warn: (t, ms) => push('warn', t, ms),
  info: (t, ms) => push('info', t, ms)
}

export default toast
