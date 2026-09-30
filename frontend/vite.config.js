import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) }
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      // 实时检测 / 语音识别的 WebSocket 走 /api/ws/*，因此这条必须开 ws:true，
      // 否则升级请求会被当普通 HTTP 转发，前端 WS 永远建不起来（且是静默的）。
      '/api': { target: 'http://localhost:8000', changeOrigin: true, ws: true },
      '/data': { target: 'http://localhost:8000', changeOrigin: true }
    }
  },
  // npm run preview（生产构建产物的本地托管，供「一键部署」使用）
  // 必须单独配置：preview 不会继承 server.proxy，缺了它 dist 起来后所有 /api 请求 404、
  // 实时检测通道也建不起来，表现为「页面能开但功能全废」。
  preview: {
    host: '0.0.0.0',
    port: 4173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true, ws: true },
      '/data': { target: 'http://localhost:8000', changeOrigin: true }
    }
  }
})
