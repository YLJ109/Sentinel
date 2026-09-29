import { reactive } from 'vue'

/** 全局确认框（替代组件库的 MessageBox.confirm），返回 Promise<boolean> */
export const confirmState = reactive({
  open: false,
  title: '',
  text: '',
  okText: '确认',
  cancelText: '取消',
  danger: false,
  _resolve: null
})

export function confirm(opts = {}) {
  confirmState.open = true
  confirmState.title = opts.title || '请确认'
  confirmState.text = opts.text || ''
  confirmState.okText = opts.okText || '确认'
  confirmState.cancelText = opts.cancelText || '取消'
  confirmState.danger = !!opts.danger
  return new Promise((resolve) => { confirmState._resolve = resolve })
}

export function settle(value) {
  confirmState.open = false
  const r = confirmState._resolve
  confirmState._resolve = null
  if (r) r(value)
}
