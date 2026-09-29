<script setup>
/**
 * 运行日志与异常面板。
 *
 * 数据来自后端进程内的日志环形缓冲（GET /api/system/logs），
 * 其中既包含后端各模块的异常，也包含前端 window.onerror / Vue 错误，
 * 因此"哪里出错了"只需要看这一个地方。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Icon from '@/ui/Icon.vue'
import { toast } from '@/ui/toast'
import { confirm } from '@/ui/confirm'
import api from '@/api'

const POLL_MS = 5000

const level = ref('')          // '' 全部 / ERROR / WARN / INFO
const items = ref([])
const total = ref(0)
const errorCount = ref(0)
const warnCount = ref(0)
const loading = ref(true)
const expanded = ref(new Set())
let timer = null

const LEVELS = [
  { key: '', label: '全部' },
  { key: 'ERROR', label: '错误' },
  { key: 'WARN', label: '警告' },
  { key: 'INFO', label: '信息' }
]

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const q = new URLSearchParams({ limit: '200' })
    if (level.value) q.set('level', level.value)
    const data = await api.get(`/api/system/logs?${q.toString()}`)
    items.value = data.items || []
    total.value = data.total || 0
    errorCount.value = data.error_count || 0
    warnCount.value = data.warning_count || 0
  } catch { /* 拦截器已提示网络异常 */ } finally {
    loading.value = false
  }
}

function levelClass(lv) {
  if (lv === 'ERROR' || lv === 'CRITICAL') return 'err'
  if (lv === 'WARNING') return 'warn'
  return 'info'
}

function levelZh(lv) {
  return { ERROR: '错误', CRITICAL: '严重', WARNING: '警告', INFO: '信息' }[lv] || lv
}

function toggle(id) {
  const s = new Set(expanded.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  expanded.value = s
}

function switchLevel(k) {
  level.value = k
  load()
}

async function clearAll() {
  const ok = await confirm({
    title: '清空日志',
    text: '将清空进程内最近日志记录（不影响磁盘日志文件与数据库记录）。确认继续？',
    danger: true
  })
  if (!ok) return
  try {
    await api.post('/api/system/logs/clear')
    toast.ok('日志已清空')
    load()
  } catch { /* 拦截器已提示 */ }
}

const hasError = computed(() => errorCount.value > 0)

onMounted(() => {
  load()
  timer = setInterval(() => load(true), POLL_MS)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="logp">
    <div class="logp-bar">
      <div class="seg">
        <button v-for="l in LEVELS" :key="l.key" :class="{ on: level === l.key }" @click="switchLevel(l.key)">
          {{ l.label }}
        </button>
      </div>
      <div class="logp-stat">
        <span class="sb-cap" :title="'错误条数'">
          <span class="sb-dot" :class="hasError ? 'err' : 'off'" />
          <span>错误 {{ errorCount }}</span>
        </span>
        <span class="sb-cap" :title="'警告条数'">
          <span class="sb-dot" :class="warnCount ? 'warn' : 'off'" />
          <span>警告 {{ warnCount }}</span>
        </span>
      </div>
    </div>

    <div class="logp-acts">
      <button class="btn btn--ghost btn--sm" @click="load()"><Icon name="refresh" /> 刷新</button>
      <button class="btn btn--ghost btn--sm" @click="clearAll"><Icon name="trash" /> 清空</button>
      <span class="muted tiny" style="margin-left: auto">每 5 秒自动刷新 · 显示最近 {{ total }} 条</span>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="!items.length" class="empty">
      <Icon name="check" style="width: 26px; height: 26px; color: var(--ok)" />
      <div>暂无{{ level ? '该级别的' : '' }}日志记录</div>
      <div class="muted tiny">出现异常或告警时会自动记录在这里</div>
    </div>

    <div v-else class="logp-list">
      <div v-for="e in items" :key="e.id" class="log-row" :class="levelClass(e.level)">
        <div class="log-hd" @click="toggle(e.id)">
          <span class="log-lv">{{ levelZh(e.level) }}</span>
          <span class="log-time mono">{{ e.time }}</span>
          <span class="log-src ellip" :title="e.logger">{{ e.logger }}</span>
          <Icon v-if="e.traceback" name="chev-down" class="log-chev" :class="{ open: expanded.has(e.id) }" />
        </div>
        <div class="log-msg">{{ e.message }}</div>
        <pre v-if="e.traceback && expanded.has(e.id)" class="log-tb">{{ e.traceback }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* .ellip 已收敛到全局 style.css 的通用工具类 */

.logp { display: flex; flex-direction: column; gap: 10px; min-height: 0; }
.logp-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.logp-stat { display: flex; align-items: center; gap: 12px; margin-left: auto; }
.logp-acts { display: flex; align-items: center; gap: 8px; }

.logp-list { display: flex; flex-direction: column; gap: 6px; }

.log-row {
  border: 1px solid var(--line);
  border-left: 3px solid var(--line-3);
  border-radius: var(--r-sm);
  background: var(--bg-inset);
  padding: 8px 10px;
}
.log-row.err { border-left-color: var(--danger); }
.log-row.warn { border-left-color: var(--warn); }
.log-row.info { border-left-color: rgba(47, 214, 240, 0.45); }

.log-hd { display: flex; align-items: center; gap: 8px; cursor: pointer; }
.log-lv {
  flex-shrink: 0;
  font-size: 11px; font-weight: 700;
  padding: 1px 7px; border-radius: 5px;
  background: rgba(122, 162, 220, 0.14);
  color: var(--tx-2);
}
.log-row.err .log-lv { background: rgba(255, 77, 109, 0.16); color: #ff9db0; }
.log-row.warn .log-lv { background: rgba(255, 191, 71, 0.16); color: #ffd58a; }
.log-row.info .log-lv { background: rgba(47, 214, 240, 0.14); color: #9fe6f7; }
.log-time { font-size: 11.5px; color: var(--tx-3); flex-shrink: 0; }
.log-src { font-size: 11.5px; color: var(--tx-3); min-width: 0; }
.log-chev { width: 14px; height: 14px; color: var(--tx-3); margin-left: auto; flex-shrink: 0; transition: transform 0.2s; }
.log-chev.open { transform: rotate(180deg); }

.log-msg { margin-top: 5px; font-size: 12.5px; color: var(--tx-1); line-height: 1.55; word-break: break-word; }
.log-tb {
  margin: 8px 0 0;
  padding: 8px;
  border-radius: var(--r-sm);
  background: rgba(0, 0, 0, 0.32);
  border: 1px solid var(--line);
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--tx-2);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 220px;
  overflow: auto;
}
</style>
