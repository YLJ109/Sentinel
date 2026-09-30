<template>
  <div class="page">
    <!-- 汇总 -->
    <div class="tiles fade-up">
      <div v-for="(t, i) in tiles" :key="t.label" class="tile" :style="{ '--tint': t.tint, animationDelay: `${i * 0.05}s` }">
        <div class="tile-top">
          <div class="tile-ico"><Icon :name="t.icon" :style="{ color: t.color }" /></div>
          <span class="tile-label">{{ t.label }}</span>
        </div>
        <div class="tile-val">{{ t.value }}<span v-if="t.unit" class="unit">{{ t.unit }}</span></div>
        <div class="tile-foot">{{ t.foot }}</div>
      </div>
    </div>

    <div class="panel corner mt fade-up d1 panel-fill">
      <div class="panel-hd">
        <span class="panel-title">点位态势墙</span>
        <div class="spacer" />
        <span class="panel-sub">双击卡片上的名称 / 编号 / 位置可直接修改</span>
        <span class="panel-sub mono">{{ online }}/{{ items.length }} 启用</span>
        <button class="btn btn--sm" :disabled="scanning" @click="scan">
          <Icon name="refresh" /> {{ scanning ? '扫描中…' : '同步本机摄像头' }}
        </button>
        <button class="btn btn--icon btn--sm btn--ghost" title="刷新" @click="load"><Icon name="refresh" /></button>
      </div>
      <div class="panel-bd">
        <div v-if="!items.length" class="empty">
          <Icon name="monitor" />
          <div class="t">未发现摄像头，点击「同步本机摄像头」获取真实设备</div>
        </div>
        <div v-else class="wall">
          <div v-for="it in items" :key="it.cam.id" class="panel cam-card">
            <div class="cam-hd">
              <span class="c-dot" :class="{ off: !it.cam.enabled }" />
              <input
                v-if="editing.id === it.cam.id && editing.field === 'name'"
                v-model="draft"
                class="inline-input"
                maxlength="64"
                placeholder="摄像头名称"
                @click.stop
                @keyup.enter="commit(it.cam)"
                @keyup.esc="cancelEdit()"
                @blur="commit(it.cam)"
              />
              <span v-else class="cam-name ellip" title="双击修改名称" @dblclick.stop="startEdit(it.cam, 'name')">{{ it.cam.name }}</span>
              <div class="spacer" />
              <input
                v-if="editing.id === it.cam.id && editing.field === 'code'"
                v-model="draft"
                class="inline-input inline-input--code"
                maxlength="16"
                placeholder="编号"
                @click.stop
                @keyup.enter="commit(it.cam)"
                @keyup.esc="cancelEdit()"
                @blur="commit(it.cam)"
              />
              <span v-else class="tag tag--mute mono code-tag" title="双击修改编号" @dblclick.stop="startEdit(it.cam, 'code')">
                编号 {{ it.cam.code || `#${it.cam.id}` }}
              </span>
            </div>
            <div class="cam-bd">
              <div class="cam-loc muted tiny">
                <Icon name="target" style="width: 13px; height: 13px" />
                <input
                  v-if="editing.id === it.cam.id && editing.field === 'location'"
                  v-model="draft"
                  class="inline-input"
                  maxlength="128"
                  placeholder="安装位置"
                  @click.stop
                  @keyup.enter="commit(it.cam)"
                  @keyup.esc="cancelEdit()"
                  @blur="commit(it.cam)"
                />
                <span v-else class="ellip loc-text" title="双击修改安装位置" @dblclick.stop="startEdit(it.cam, 'location')">
                  {{ it.cam.location || '未设置安装位置' }}
                </span>
              </div>

              <div class="cam-metrics">
                <div class="metric">
                  <div class="m-val mono">{{ it.today }}</div>
                  <div class="m-lab">今日事件</div>
                </div>
                <div class="metric">
                  <div class="m-val mono" :class="{ alert: it.pending > 0 }">{{ it.pending }}</div>
                  <div class="m-lab">待处置报警</div>
                </div>
              </div>

              <div class="cam-latest">
                <template v-if="it.latest">
                  <span class="tag tag--low">{{ typeZh[it.latest.event_type] || it.latest.event_type }}</span>
                  <span class="dim tiny mono ellip">{{ fmt(it.latest.created_at) }}</span>
                </template>
                <span v-else class="dim tiny">暂无检测记录</span>
              </div>

              <div class="btn-row cam-acts">
                <button class="btn btn--xs btn--primary" @click="openRealtime(it.cam.id)">
                  <Icon name="play" /> 在实时检测中打开
                </button>
                <button class="btn btn--xs btn--ghost" @click="router.push('/alarms')">
                  <Icon name="bell" /> 该点位报警
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import Icon from '@/ui/Icon.vue'
import { toast } from '@/ui/toast'
import api from '@/api'

const router = useRouter()

const items = ref([])
const stats = reactive({ camera_total: 0, camera_online: 0, today_events: 0, pending_alarms: 0 })
const scanning = ref(false)

// 就地编辑：同一时刻只有一个字段处于编辑态
const editing = reactive({ id: null, field: '' })
const draft = ref('')

const typeZh = { fall: '跌倒', smoke: '抽烟', bullying: '欺凌', fight: '打架', argue: '争吵', crowd: '人员聚集', person: '人员', normal: '正常' }

const fmt = (t) => (t ? new Date(t).toLocaleTimeString('zh-CN', { hour12: false }) : '')
const online = computed(() => items.value.filter((x) => x.cam.enabled).length)

const tiles = computed(() => [
  { label: '摄像头总数', icon: 'monitor', value: items.value.length, foot: '本机实际接入设备', tint: 'rgba(47,214,240,.16)', color: '#6fe0f5' },
  { label: '启用摄像头', icon: 'signal', value: online.value, unit: `/${items.value.length}`, foot: '当前处于启用状态', tint: 'rgba(46,230,168,.16)', color: '#63eebd' },
  { label: '今日事件', icon: 'pulse', value: stats.today_events, foot: '全校今日检测累计', tint: 'rgba(59,130,246,.16)', color: '#7fa8ff' },
  { label: '待处置报警', icon: 'alert', value: stats.pending_alarms, foot: '需值班人员跟进', tint: 'rgba(255,176,32,.16)', color: '#ffc861' }
])

/** 今日零点（本地时区）对应的 ISO 时间，用于按天过滤事件 */
function todayIso() {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d.toISOString()
}

async function load() {
  try {
    const [cams, s] = await Promise.all([api.get('/api/cameras'), api.get('/api/dashboard/stats')])
    stats.camera_total = s.camera_total || 0
    stats.camera_online = s.camera_online || 0
    stats.today_events = s.today_events || 0
    stats.pending_alarms = s.pending_alarms || 0
    items.value = (cams || []).map((c) => ({ cam: c, today: 0, latest: null, pending: 0 }))
    await loadDetails()
  } catch { /* 拦截器已提示 */ }
}

/** 逐个点位拉取今日事件与待处置报警 */
async function loadDetails() {
  await Promise.all(items.value.map(async (it) => {
    try {
      const ev = await api.get('/api/history/events', {
        params: { camera_id: it.cam.id, date_from: todayIso(), limit: 1 }
      })
      it.today = ev.total || 0
      it.latest = ev.items?.[0] || null
    } catch { /* 静默 */ }
    try {
      const al = await api.get('/api/alarms', {
        params: { camera_id: it.cam.id, status: 'pending', limit: 1 }
      })
      it.pending = al.total || 0
    } catch { /* 静默 */ }
  }))
}

/** 与本机真实设备对齐（授权后可拿到设备真实名称） */
async function scan() {
  if (!navigator.mediaDevices?.enumerateDevices) { toast.warn('当前浏览器不支持设备枚举'); return }
  scanning.value = true
  try {
    try {
      const s = await navigator.mediaDevices.getUserMedia({ video: true })
      s.getTracks().forEach((t) => t.stop())
    } catch { toast.warn('未授权摄像头，将按设备数量同步') }
    const devices = await enumerateDevices()
    await api.post('/api/cameras/sync', { devices })
    await load()
    toast.ok(`已同步 ${devices.length} 个本机摄像头`)
  } catch { /* 拦截器已提示 */ } finally {
    scanning.value = false
  }
}

/** 枚举本机真实视频设备并转为同步入参（按 deviceId 去重） */
async function enumerateDevices() {
  const all = await navigator.mediaDevices.enumerateDevices()
  const seen = new Set()
  return all
    .filter((d) => d.kind === 'videoinput')
    .filter((d) => {
      const key = d.deviceId || `${d.groupId}:${d.label}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    .map((d, i) => ({ device_id: d.deviceId || `videoinput-${i}`, label: (d.label || '').trim(), index: i }))
}

/** 双击进入就地编辑：聚焦并全选，便于直接覆盖输入 */
async function startEdit(cam, field) {
  editing.id = cam.id
  editing.field = field
  draft.value = (field === 'code' ? cam.code : cam[field]) || ''
  await nextTick()
  const el = document.querySelector('.cam-card .inline-input')
  if (el) { el.focus(); el.select() }
}

function cancelEdit() {
  editing.id = null
  editing.field = ''
}

/** 回车 / 失焦保存；编号留空表示回落显示 #id */
async function commit(cam) {
  if (editing.id !== cam.id) return
  const field = editing.field
  const value = draft.value.trim()
  cancelEdit()
  if (field === 'name' && !value) { toast.warn('摄像头名称不能为空'); return }
  if (String(cam[field] ?? '') === value) return
  try {
    const res = await api.patch(`/api/cameras/${cam.id}`, { [field]: value })
    Object.assign(cam, res)
    toast.ok('已保存')
  } catch { /* 拦截器已提示 */ }
}

function openRealtime(id) {
  router.push('/realtime?camera=' + id)
}

onMounted(async () => {
  await load()
  // 首次进入时静默对齐一次，保证卡片数量与本机真实设备一致
  if (navigator.mediaDevices?.enumerateDevices) {
    try {
      const devices = await enumerateDevices()
      await api.post('/api/cameras/sync', { devices })
      await load()
    } catch { /* 静默 */ }
  }
})
</script>

<style scoped>
/* 卡片墙占满面板剩余高度，点位较多时在面板内部滚动 */
.wall {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  align-content: start;
  gap: 12px;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
.cam-card { display: flex; flex-direction: column; }
.cam-hd {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
}
.cam-name { font-family: var(--font-display); font-size: 14px; font-weight: 600; cursor: text; }
.code-tag { cursor: text; }
.loc-text { cursor: text; }

/* 就地编辑输入框：与卡片风格一致，聚焦时有青色描边 */
.inline-input {
  flex: 1;
  min-width: 0;
  height: 26px;
  padding: 0 8px;
  border-radius: 7px;
  background: var(--bg-inset);
  border: 1px solid rgba(47, 214, 240, 0.55);
  box-shadow: 0 0 0 3px rgba(47, 214, 240, 0.1);
  color: var(--tx-1);
  font-family: inherit;
  font-size: 13px;
  outline: none;
}
.inline-input::placeholder { color: var(--tx-3); }
.inline-input--code {
  flex: none;
  width: 96px;
  font-family: var(--font-mono);
  font-size: 12px;
}
.cam-bd { padding: 11px 12px 12px; display: flex; flex-direction: column; gap: 10px; }
.cam-loc { display: flex; align-items: center; gap: 6px; }
.cam-loc .inline-input { height: 24px; font-size: 12px; }

.cam-metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.metric {
  padding: 9px 11px;
  border-radius: var(--r-md);
  background: var(--bg-inset);
  border: 1px solid var(--line);
}
.m-val { font-size: 21px; font-weight: 700; line-height: 1.1; }
.m-val.alert { color: var(--warn); }
.m-lab { font-size: 11.5px; color: var(--tx-3); margin-top: 3px; }

.cam-latest { display: flex; align-items: center; gap: 9px; min-height: 22px; }

.cam-acts { margin-top: 2px; }

/* .c-dot / .ellip 已收敛到全局 style.css 的通用工具类 */
</style>
