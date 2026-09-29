import axios from 'axios'
import { toast } from '@/ui/toast'

const api = axios.create({ baseURL: '', timeout: 30000 })

api.interceptors.request.use((cfg) => {
  const token = localStorage.getItem('cab_token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

api.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const status = err.response?.status
    const msg = err.response?.data?.detail || err.message || '请求失败'
    if (status === 401 && location.pathname !== '/login') {
      localStorage.removeItem('cab_token')
      localStorage.removeItem('cab_user')
      location.href = '/login'
    } else if (status !== 401) {
      toast.err(typeof msg === 'string' ? msg : '请求失败')
    }
    return Promise.reject(err)
  }
)

/** 构造 WebSocket 地址（走 vite 代理）；鉴权改由子协议携带，不再拼接 ?token= */
export function wsUrl(path) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}${path}`
}

/** WebSocket 子协议：[协议版本, 登录令牌]，供所有实时连接使用 */
export function wsProtocols() {
  return ['cab.v1', localStorage.getItem('cab_token') || '']
}

/** 后端取证媒体地址（截图 / 视频 / 上传原片），需登录 Cookie 同源携带 */
export function dataUrl(relPath) {
  if (!relPath) return ''
  return `/api/media/${String(relPath).replace(/^\/+/, '')}`
}

/** 下载文件：以 blob 方式请求（自动带 Authorization 头），再用临时 <a> 触发保存 */
export async function downloadFile(url, filename) {
  try {
    const blob = await api.get(url, { responseType: 'blob' })
    const objUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objUrl
    a.download = filename || ''
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(objUrl)
  } catch {
    toast.err('下载失败，请稍后重试')
  }
}

export default api
