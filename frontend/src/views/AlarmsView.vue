<template>
  <div class="page">
    <div class="panel corner fade-up">
      <div class="panel-hd">
        <span class="panel-title">报警处置</span>
        <div class="spacer" />
        <div class="seg">
          <button v-for="f in filters" :key="f.value" :class="{ on: status === f.value }" @click="setFilter(f.value)">
            {{ f.label }}
          </button>
        </div>
        <button class="btn btn--icon btn--sm btn--ghost" @click="load" title="刷新"><Icon name="refresh" /></button>
      </div>

      <!-- 日期范围筛选 -->
      <div class="filter-bar">
        <div class="seg">
          <button v-for="r in ranges" :key="r.value" :class="{ on: rangeKind === r.value }" @click="setRange(r.value)">
            {{ r.label }}
          </button>
        </div>
        <span class="dim tiny">起始</span>
        <input v-model="dateFrom" type="date" class="input input--flush date-input" @change="applyRange" />
        <span class="dim tiny">结束</span>
        <input v-model="dateTo" type="date" class="input input--flush date-input" @change="applyRange" />
        <button v-if="dateFrom || dateTo" class="btn btn--xs btn--ghost" @click="clearRange">清除日期</button>
      </div>

      <!-- 批量操作条 -->
      <div v-if="selected.length" class="bulk-bar">
        <Icon name="check" />
        <span>已选中 <b class="mono">{{ selected.length }}</b> 条报警</span>
        <div class="spacer" />
        <button class="btn btn--xs" @click="bulkStatus('handling')"><Icon name="clock" /> 批量处置中</button>
        <button class="btn btn--xs btn--ok" @click="bulkStatus('resolved')"><Icon name="check" /> 批量已解决</button>
        <button class="btn btn--xs btn--ghost" @click="askIgnore(selected.slice())"><Icon name="eye-off" /> 批量忽略</button>
        <button class="btn btn--xs btn--ghost" @click="selected = []">取消选择</button>
      </div>

      <div class="panel-bd flush">
        <div v-if="loading" class="empty">
          <Icon name="refresh" />
          <div class="t">加载中…</div>
        </div>
        <div v-else-if="!rows.length" class="empty">
          <Icon name="shield" />
          <div class="t">当前筛选条件下暂无报警记录</div>
        </div>
        <template v-else>
          <div class="tbl-wrap">
            <table class="tbl">
              <thead>
                <tr>
                  <th style="width: 44px">
                    <label class="cb" title="全选当前页">
                      <input type="checkbox" :checked="allSelected" @change="toggleAll" />
                      <span class="box"><Icon name="check" /></span>
                    </label>
                  </th>
                  <th style="width: 60px">编号</th>
                  <th style="width: 84px">级别</th>
                  <th>触发原因</th>
                  <th style="width: 80px">来源</th>
                  <th style="width: 130px">状态</th>
                  <th style="width: 172px">触发时间</th>
                  <th style="width: 190px" />
                </tr>
              </thead>
              <tbody>
                <tr v-for="a in rows" :id="`alarm-row-${a.id}`" :key="a.id" :class="{ 'row-focus': highlight === a.id }">
                  <td>
                    <label class="cb">
                      <input type="checkbox" :checked="selected.includes(a.id)" @change="toggleRow(a.id)" />
                      <span class="box"><Icon name="check" /></span>
                    </label>
                  </td>
                  <td class="num">#{{ a.id }}</td>
                  <td><span class="tag" :class="`tag--${a.level}`">{{ levelZh[a.level] || a.level }}</span></td>
                  <td class="ellip" :title="a.reason">{{ a.reason }}</td>
                  <td><span class="tag tag--mute">{{ srcZh[a.source] || a.source }}</span></td>
                  <td><span class="pill" :class="`pill--${a.status}`">{{ statusZh[a.status] || a.status }}</span></td>
                  <td class="num tiny">{{ fmt(a.created_at) }}</td>
                  <td>
                    <div class="btn-row">
                      <button class="btn btn--xs btn--primary" @click="openCase(a.id)"><Icon name="folder" /> 取证档案</button>
                      <button class="btn btn--xs btn--ghost" @click="toggleMenu($event, a.id)">更多 <Icon name="chev-down" /></button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- 分页控件 -->
          <div class="pager">
            <span class="dim tiny">共 <b class="mono">{{ total }}</b> 条 · 第 <b class="mono">{{ page + 1 }}</b> / <b class="mono">{{ pageCount }}</b> 页</span>
            <div class="spacer" />
            <div class="seg">
              <button v-for="n in pageSizes" :key="n" :class="{ on: size === n }" @click="setSize(n)">{{ n }} 条/页</button>
            </div>
            <button class="btn btn--xs" :disabled="page <= 0 || loading" @click="go(page - 1)">
              <Icon name="chev-right" class="flip" /> 上一页
            </button>
            <button class="btn btn--xs" :disabled="page + 1 >= pageCount || loading" @click="go(page + 1)">
              下一页 <Icon name="chev-right" />
            </button>
          </div>
        </template>
      </div>
    </div>

    <!-- 行内「更多」菜单 -->
    <Teleport to="body">
      <div v-if="menu.open" class="menu-mask" @click="menu.open = false" @contextmenu.prevent="menu.open = false" />
      <div v-if="menu.open" class="dd-menu row-menu" :style="{ left: `${menu.x}px`, top: `${menu.y}px` }">
        <div class="dd-opt" @click="menuPick('handling')"><Icon name="clock" /> 标记处置中</div>
        <div class="dd-opt" @click="menuPick('resolved')"><Icon name="check" /> 标记已解决</div>
        <div class="dd-opt" @click="menuPick('ignored')"><Icon name="eye-off" /> 忽略 / 误报反馈</div>
      </div>
    </Teleport>

    <!-- 取证档案 -->
    <Drawer v-model="caseOpen" :title="`报警取证档案 #${cur?.alarm?.id ?? ''}`">
      <div v-if="caseLoading" class="empty">
        <Icon name="refresh" />
        <div class="t">正在加载取证档案…</div>
      </div>
      <template v-else-if="cur">
        <div class="row wrap" style="margin-bottom: 18px">
          <button class="btn btn--sm" @click="exportZip(cur.alarm.id)"><Icon name="download" /> 导出证据包(zip)</button>
          <div class="spacer" />
          <span class="dim tiny">{{ (cur.evidence || []).length }} 个取证文件</span>
        </div>

        <div class="section">
          <div class="section-t">报警信息</div>
          <dl class="kv">
            <dt>级别</dt><dd><span class="tag" :class="`tag--${cur.alarm.level}`">{{ levelZh[cur.alarm.level] || cur.alarm.level }}</span></dd>
            <dt>原因</dt><dd>{{ cur.alarm.reason }}</dd>
            <dt>来源</dt><dd>{{ srcZh[cur.alarm.source] || cur.alarm.source }}</dd>
            <dt>时间</dt><dd class="mono">{{ fmt(cur.alarm.created_at) }}</dd>
            <dt>状态</dt><dd><span class="pill" :class="`pill--${cur.alarm.status}`">{{ statusZh[cur.alarm.status] || cur.alarm.status }}</span></dd>
            <dt>处置人</dt><dd>{{ cur.alarm.handler_id ? `用户 #${cur.alarm.handler_id}` : '—' }}</dd>
            <dt>反馈标签</dt><dd>{{ feedbackZh[cur.alarm.feedback_label] || '—' }}</dd>
            <template v-if="cur.alarm.ignored_reason">
              <dt>忽略原因</dt><dd>{{ cur.alarm.ignored_reason }}</dd>
            </template>
          </dl>
        </div>

        <div class="section">
          <div class="section-t">取证材料</div>
          <div v-if="shots.length" class="shots">
            <div v-for="(s, i) in shots" :key="s" class="shot" @click="preview = s">
              <img :src="s" :alt="`取证截图 ${i + 1}`" loading="lazy" />
              <div class="cap">取证帧 {{ i + 1 }}</div>
            </div>
          </div>
          <div v-for="c in clips" :key="c" class="clip-box mt-s">
            <video class="clip" :src="dataUrl(c)" controls preload="metadata" />
            <div class="tiny dim mono mt-s">{{ c }}</div>
          </div>
          <div v-for="t in texts" :key="t.path" class="text-ev mt-s">
            <div class="tiny dim mono">{{ t.path }}</div>
            <pre class="text-body">{{ t.content }}</pre>
          </div>
          <div v-for="o in others" :key="o" class="tiny mono dim mt-s">{{ o }}</div>
          <div v-if="!evidenceCount" class="empty" style="padding: 24px">
            <Icon name="image" />
            <div class="t">该报警暂无取证文件</div>
          </div>
        </div>

        <div class="section">
          <div class="section-t">关联对话内容（语音转写）</div>
          <div v-if="cur.chatlogs.length" class="stream">
            <div v-for="c in cur.chatlogs" :key="c.id" class="bubble" :class="{ hit: c.hit_keywords }">
              <div class="txt">{{ c.text }}</div>
              <div class="meta">
                <span class="mono">{{ (c.start_time || 0).toFixed(1) }}s</span>
                <span v-if="c.hit_keywords" class="kw">命中：{{ c.hit_keywords }}</span>
              </div>
            </div>
          </div>
          <div v-else class="empty" style="padding: 24px"><Icon name="mic" /><div class="t">无语音记录</div></div>
        </div>

        <div class="section">
          <div class="section-t">处置备注</div>
          <textarea v-model="note" class="textarea" rows="4" placeholder="填写处置经过、联系班主任、家长沟通结果等…" />
          <button class="btn btn--primary mt-s" @click="saveNote"><Icon name="check" /> 保存备注</button>
        </div>
      </template>
    </Drawer>

    <!-- 忽略 / 误报反馈 -->
    <Modal v-model="ignore.open" title="忽略报警 · 误报反馈" max-width="460px">
      <div class="stack" style="gap: 16px">
        <div class="field">
          <label>忽略原因</label>
          <div class="seg">
            <button
              v-for="o in feedbackOptions"
              :key="o.value"
              :class="{ on: ignore.label === o.value }"
              @click="ignore.label = o.value"
            >
              {{ o.label }}
            </button>
          </div>
        </div>
        <div class="field">
          <label>备注说明（可选）</label>
          <textarea v-model="ignore.reason" class="textarea" rows="3" placeholder="补充判定依据，如：课间正常追逐打闹，无欺凌意图" />
        </div>
        <div class="muted tiny">将把 {{ ignore.ids.length }} 条报警标记为「已忽略」，并记录人工反馈标签。</div>
      </div>
      <template #footer>
        <button class="btn btn--ghost" @click="ignore.open = false">取消</button>
        <button class="btn btn--danger" :disabled="ignore.saving" @click="confirmIgnore">
          <Icon name="eye-off" /> {{ ignore.saving ? '提交中…' : '确认忽略' }}
        </button>
      </template>
    </Modal>

    <Lightbox v-model:src="preview" />
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import Icon from '@/ui/Icon.vue'
import Drawer from '@/ui/Drawer.vue'
import Modal from '@/ui/Modal.vue'
import Lightbox from '@/ui/Lightbox.vue'
import { toast } from '@/ui/toast'
import api, { dataUrl, downloadFile } from '@/api'

const route = useRoute()

const rows = ref([])
const total = ref(0)
const page = ref(0)
const size = ref(50)
const loading = ref(false)
const status = ref('')
const dateFrom = ref('')
const dateTo = ref('')
const rangeKind = ref('')
const selected = ref([])
const highlight = ref(null)

const caseOpen = ref(false)
const caseLoading = ref(false)
const cur = ref(null)
const note = ref('')
const texts = ref([])
const preview = ref('')

const menu = reactive({ open: false, id: null, x: 0, y: 0 })
const ignore = reactive({ open: false, ids: [], label: 'false_positive', reason: '', saving: false })

const pageSizes = [20, 50, 100]
const filters = [
  { value: '', label: '全部' },
  { value: 'pending', label: '待处置' },
  { value: 'handling', label: '处置中' },
  { value: 'resolved', label: '已解决' },
  { value: 'ignored', label: '已忽略' }
]
const ranges = [
  { value: 'today', label: '今日' },
  { value: '7d', label: '近7天' },
  { value: '30d', label: '近30天' }
]
const feedbackOptions = [
  { value: 'false_positive', label: '误报' },
  { value: 'not_bullying', label: '非霸凌行为' },
  { value: 'other', label: '其他' }
]
const levelZh = { high: '高危', medium: '中警', low: '提示' }
const statusZh = { pending: '待处置', handling: '处置中', resolved: '已解决', ignored: '已忽略' }
const srcZh = { video: '视觉', audio: '语音', manual: '人工' }
const feedbackZh = { false_positive: '误报', not_bullying: '非霸凌行为', confirmed: '确认霸凌', other: '其他' }

const fmt = (t) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '')
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / size.value)))
const allSelected = computed(() => rows.value.length > 0 && rows.value.every((r) => selected.value.includes(r.id)))

const shots = computed(() => (cur.value?.evidence || []).filter((p) => /\.(jpg|jpeg|png)$/i.test(p)).map((p) => dataUrl(p)))
const clips = computed(() => (cur.value?.evidence || []).filter((p) => /\.(mp4|webm|mov)$/i.test(p)))
const others = computed(() =>
  (cur.value?.evidence || []).filter((p) => !/\.(jpg|jpeg|png|mp4|webm|mov|txt)$/i.test(p))
)
const evidenceCount = computed(() => shots.value.length + clips.value.length + texts.value.length + others.value.length)

/** 读取转写文本证据（媒体接口支持 Bearer 鉴权） */
async function loadTexts(paths) {
  if (!paths.length) return []
  const token = localStorage.getItem('cab_token') || ''
  const out = []
  for (const p of paths) {
    try {
      const res = await fetch(dataUrl(p), { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      out.push({ path: p, content: res.ok ? (await res.text()).trim() || '（空文本）' : '（读取失败）' })
    } catch {
      out.push({ path: p, content: '（读取失败）' })
    }
  }
  return out
}

/** 本地日历日 → 带时区偏移的 ISO 串。
 * 后端按 UTC 存时间且只认带偏移的 ISO，直接拼 "T00:00:00" 会被当成 UTC 零点，
 * 与站点差一个时区，本地凌晨的报警会被漏查。 */
function localDayIso(dateStr, end = false) {
  const d = new Date(`${dateStr}T${end ? '23:59:59.999' : '00:00:00'}`)
  return Number.isNaN(d.getTime()) ? '' : d.toISOString()
}

async function load() {
  loading.value = true
  const p = new URLSearchParams()
  if (status.value) p.set('status', status.value)
  const from = dateFrom.value && localDayIso(dateFrom.value)
  const to = dateTo.value && localDayIso(dateTo.value, true)
  if (from) p.set('date_from', from)
  if (to) p.set('date_to', to)
  p.set('offset', String(page.value * size.value))
  p.set('limit', String(size.value))
  try {
    const res = await api.get(`/api/alarms?${p}`)
    rows.value = res?.items || []
    total.value = res?.total || 0
    const ids = rows.value.map((r) => r.id)
    selected.value = selected.value.filter((id) => ids.includes(id))
  } catch {
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function setFilter(v) {
  status.value = v
  page.value = 0
  load()
}

function ymd(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function setRange(kind) {
  if (kind === rangeKind.value) {
    rangeKind.value = ''
    dateFrom.value = ''
    dateTo.value = ''
  } else {
    const to = new Date()
    const from = new Date()
    if (kind === '7d') from.setDate(from.getDate() - 6)
    if (kind === '30d') from.setDate(from.getDate() - 29)
    rangeKind.value = kind
    dateFrom.value = ymd(from)
    dateTo.value = ymd(to)
  }
  page.value = 0
  load()
}

function applyRange() {
  rangeKind.value = ''
  page.value = 0
  load()
}

function clearRange() {
  rangeKind.value = ''
  dateFrom.value = ''
  dateTo.value = ''
  page.value = 0
  load()
}

function go(p) {
  if (p < 0 || p >= pageCount.value) return
  page.value = p
  load()
}

function setSize(n) {
  size.value = n
  page.value = 0
  load()
}

function toggleAll(e) {
  selected.value = e.target.checked ? rows.value.map((r) => r.id) : []
}

function toggleRow(id) {
  const i = selected.value.indexOf(id)
  if (i >= 0) selected.value.splice(i, 1)
  else selected.value.push(id)
}

function toggleMenu(e, id) {
  if (menu.open && menu.id === id) {
    menu.open = false
    return
  }
  const r = e.currentTarget.getBoundingClientRect()
  const w = 190
  const h = 126
  menu.id = id
  menu.x = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8))
  menu.y = r.bottom + 6 + h > window.innerHeight ? Math.max(8, r.top - h - 6) : r.bottom + 6
  menu.open = true
}

function closeMenu() {
  menu.open = false
}

function menuPick(s) {
  const id = menu.id
  menu.open = false
  if (s === 'ignored') {
    askIgnore([id])
    return
  }
  const row = rows.value.find((r) => r.id === id)
  if (row) setStatus(row, s)
}

async function setStatus(row, s) {
  try {
    await api.patch(`/api/alarms/${row.id}`, { status: s })
    toast.ok(`已标记为「${statusZh[s]}」`)
    load()
  } catch { /* 拦截器已提示 */ }
}

async function bulkStatus(s) {
  const ids = selected.value.slice()
  if (!ids.length) return
  try {
    const res = await api.post(`/api/alarms/bulk/status?status=${s}`, ids)
    toast.ok(`已将 ${res?.updated ?? ids.length} 条报警标记为「${statusZh[s]}」`)
    selected.value = []
    load()
  } catch { /* 拦截器已提示 */ }
}

function askIgnore(ids) {
  ignore.ids = ids
  ignore.label = 'false_positive'
  ignore.reason = ''
  ignore.open = true
}

async function confirmIgnore() {
  const ids = ignore.ids.slice()
  if (!ids.length) return
  ignore.saving = true
  const feedback = { feedback_label: ignore.label, ignored_reason: ignore.reason || null }
  try {
    if (ids.length === 1) {
      await api.patch(`/api/alarms/${ids[0]}`, { ...feedback, status: 'ignored' })
    } else {
      await api.post('/api/alarms/bulk/status?status=ignored', ids)
      await Promise.all(ids.map((id) => api.patch(`/api/alarms/${id}`, feedback)))
    }
    toast.ok(`已忽略 ${ids.length} 条报警`)
    ignore.open = false
    selected.value = []
    load()
  } catch { /* 拦截器已提示 */ } finally {
    ignore.saving = false
  }
}

async function openCase(id) {
  caseOpen.value = true
  caseLoading.value = true
  cur.value = null
  texts.value = []
  try {
    const data = await api.get(`/api/history/case/${id}`)
    cur.value = data
    note.value = data?.alarm?.note || ''
    texts.value = await loadTexts((data?.evidence || []).filter((p) => /\.txt$/i.test(p)))
  } catch {
    caseOpen.value = false
  } finally {
    caseLoading.value = false
  }
}

function exportZip(id) {
  // downloadFile 内部已处理鉴权与失败提示
  downloadFile(`/api/history/export/alarm/${id}`, `alarm_${id}_evidence.zip`)
}

async function saveNote() {
  if (!cur.value) return
  try {
    await api.patch(`/api/alarms/${cur.value.alarm.id}`, { note: note.value })
    toast.ok('处置备注已保存')
    if (cur.value.alarm) cur.value.alarm.note = note.value
  } catch { /* 拦截器已提示 */ }
}

/** 从路由 query 的 focus 直达某条报警：高亮并打开取证档案 */
async function focusAlarm(id) {
  highlight.value = id
  await nextTick()
  document.getElementById(`alarm-row-${id}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  setTimeout(() => { highlight.value = null }, 2000)
  await openCase(id)
}

onMounted(async () => {
  window.addEventListener('scroll', closeMenu, true)
  window.addEventListener('resize', closeMenu)
  const focus = Number(route.query.focus)
  if (focus) status.value = ''
  await load()
  if (focus) await focusAlarm(focus)
})

onBeforeUnmount(() => {
  window.removeEventListener('scroll', closeMenu, true)
  window.removeEventListener('resize', closeMenu)
})
</script>

<style scoped>
/* .filter-bar / .date-input / .bulk-bar / .pager / .flip 已收敛到全局 style.css；
   此处仅保留本页表格列的宽度差异 */
.ellip { max-width: 360px; }
.menu-mask { position: fixed; inset: 0; z-index: 150; }
.row-menu { position: fixed; right: auto; width: 190px; max-height: none; z-index: 151; }
.tbl tbody tr.row-focus {
  background: rgba(47, 214, 240, 0.14);
  box-shadow: inset 2px 0 0 var(--acc), inset 0 0 0 1px rgba(47, 214, 240, 0.35);
}
.clip-box { display: flex; flex-direction: column; }
.clip { width: 100%; border-radius: var(--r-md); border: 1px solid var(--line-2); background: #05080f; display: block; }
.text-ev { padding: 10px 12px; border-radius: var(--r-md); background: var(--bg-inset); border: 1px solid var(--line); }
.text-body { margin: 8px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12.5px; color: var(--tx-2); max-height: 220px; overflow-y: auto; }
</style>
