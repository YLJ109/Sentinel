<template>
  <div class="page">
    <!-- Tab 栏：三类取证记录切换 -->
    <div class="tabs fade-up">
      <button
        v-for="t in tabs"
        :key="t.key"
        type="button"
        class="tab"
        :class="{ on: active === t.key }"
        @click="switchTab(t.key)"
      >
        <Icon :name="t.icon" />
        <span>{{ t.label }}</span>
        <span class="cnt mono">{{ st[t.key].total }}</span>
      </button>
      <div class="spacer" />
      <button v-if="active === 'events'" class="btn btn--sm" @click="exportFeedback">
        <Icon name="download" /> 导出反馈样本
      </button>
      <button class="btn btn--icon btn--sm btn--ghost" title="刷新" @click="load(active)">
        <Icon name="refresh" />
      </button>
    </div>

    <div class="panel corner fade-up d1 panel-fill mt">
      <!-- ============ 检测事件历史 ============ -->
      <template v-if="active === 'events'">
        <div class="panel-hd">
          <span class="panel-title">检测事件历史</span>
          <div class="spacer" />
          <span class="panel-sub mono">共 {{ st.events.total }} 条</span>
        </div>
        <div class="panel-bd flush">
          <div class="filter-bar">
            <div class="input-affix" style="width: 210px">
              <Icon name="search" />
              <input v-model.trim="st.events.keyword" class="input input--flush" placeholder="搜索行为 / 判定特征" @keyup.enter="resetPage('events')" />
              <button v-if="st.events.keyword" class="tail" title="清空" @click="st.events.keyword = ''; resetPage('events')"><Icon name="close" /></button>
            </div>
            <BaseSelect v-model="st.events.event_type" :options="typeOptions" placeholder="全部行为" icon="layers" clearable style="width: 150px" @change="resetPage('events')" />
            <BaseSelect v-model="st.events.camera_id" :options="camOptions" placeholder="全部摄像头" icon="camera" clearable style="width: 190px" @change="resetPage('events')" />
            <label class="cb">
              <input type="checkbox" v-model="st.events.bullying_only" @change="resetPage('events')" />
              <span class="box"><Icon name="check" /></span>
              仅霸凌行为
            </label>
            <div class="seg">
              <button v-for="r in ranges" :key="r.value" :class="{ on: st.events.range === r.value }" @click="setRange('events', r.value)">{{ r.label }}</button>
            </div>
            <input v-model="st.events.dateFrom" type="date" class="input input--flush date-input" @change="applyRange('events')" />
            <span class="dim tiny">至</span>
            <input v-model="st.events.dateTo" type="date" class="input input--flush date-input" @change="applyRange('events')" />
            <button class="btn btn--xs" @click="resetPage('events')"><Icon name="search" /> 查询</button>
          </div>

          <div v-if="st.events.selected.length" class="bulk-bar">
            <Icon name="check" />
            已选 <b class="mono">{{ st.events.selected.length }}</b> 条
            <div class="spacer" />
            <button class="btn btn--xs btn--ghost" @click="clearSel('events')">取消选择</button>
            <button class="btn btn--xs btn--danger" @click="removeSelected('events')"><Icon name="trash" /> 批量删除</button>
          </div>

          <div v-if="st.events.loading" class="empty"><Icon name="refresh" /><div class="t">加载中…</div></div>
          <div v-else-if="!st.events.rows.length" class="empty"><Icon name="layers" /><div class="t">暂无符合条件的检测事件</div></div>
          <div v-else class="tbl-wrap scroll-fill">
            <table class="tbl">
              <thead>
                <tr>
                  <th style="width: 44px"><label class="cb"><input type="checkbox" :checked="allSel('events')" @change="toggleAll('events', $event.target.checked)" /><span class="box"><Icon name="check" /></span></label></th>
                  <th style="width: 64px">ID</th>
                  <th style="width: 110px">行为</th>
                  <th style="width: 130px">置信度</th>
                  <th style="width: 110px">编号</th>
                  <th style="width: 120px">视频时间点</th>
                  <th style="width: 180px">检测时间</th>
                  <th style="width: 130px">性质</th>
                  <th style="width: 90px" />
                </tr>
              </thead>
              <tbody>
                <tr v-for="e in st.events.rows" :key="e.id" :class="{ 'row-on': st.events.selected.includes(e.id) }">
                  <td><label class="cb"><input type="checkbox" :checked="st.events.selected.includes(e.id)" @change="toggleOne('events', e.id, $event.target.checked)" /><span class="box"><Icon name="check" /></span></label></td>
                  <td class="num">{{ e.id }}</td>
                  <td><span class="tag" :class="`tag--${e.is_bullying ? 'high' : 'medium'}`">{{ e.label }}</span></td>
                  <td>
                    <div class="row" style="gap: 8px">
                      <div class="bar-track" style="width: 56px">
                        <div class="bar-fill" :class="{ bad: e.is_bullying }" :style="{ width: `${Math.round(e.confidence * 100)}%` }" />
                      </div>
                      <span class="num tiny">{{ (e.confidence * 100).toFixed(1) }}%</span>
                    </div>
                  </td>
                  <td class="num">{{ e.camera_id ? `#${e.camera_id}` : '—' }}</td>
                  <td class="num">{{ e.frame_time ? `${e.frame_time}s` : '—' }}</td>
                  <td class="num tiny">{{ fmt(e.created_at) }}</td>
                  <td>
                    <span class="pill pill--wide" :class="e.is_bullying ? 'pill--pending' : 'pill--handling'">
                      {{ e.is_bullying ? '霸凌行为' : '关注行为' }}
                    </span>
                  </td>
                  <td>
                    <button class="btn btn--xs btn--ghost" title="删除该条" @click="removeOne('events', e)"><Icon name="trash" /></button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="pager">
            <span class="dim tiny">共 <b class="mono">{{ st.events.total }}</b> 条 · 第 <b class="mono">{{ st.events.page + 1 }}</b> / <b class="mono">{{ pages('events') }}</b> 页</span>
            <div class="spacer" />
            <div class="seg">
              <button v-for="n in pageSizes" :key="n" :class="{ on: st.events.size === n }" @click="setSize('events', n)">{{ n }} 条/页</button>
            </div>
            <button class="btn btn--xs" :disabled="st.events.page <= 0 || st.events.loading" @click="go('events', st.events.page - 1)">
              <Icon name="chev-right" class="flip" /> 上一页
            </button>
            <button class="btn btn--xs" :disabled="st.events.page + 1 >= pages('events') || st.events.loading" @click="go('events', st.events.page + 1)">
              下一页 <Icon name="chev-right" />
            </button>
          </div>
        </div>
      </template>

      <!-- ============ 语音对话记录 ============ -->
      <template v-else-if="active === 'chats'">
        <div class="panel-hd">
          <span class="panel-title">语音对话记录</span>
          <div class="spacer" />
          <span class="panel-sub mono">共 {{ st.chats.total }} 条</span>
        </div>
        <div class="panel-bd flush">
          <div class="filter-bar">
            <div class="input-affix" style="width: 240px">
              <Icon name="search" />
              <input v-model.trim="st.chats.keyword" class="input input--flush" placeholder="搜索转写内容 / 命中关键词" @keyup.enter="resetPage('chats')" />
              <button v-if="st.chats.keyword" class="tail" title="清空" @click="st.chats.keyword = ''; resetPage('chats')"><Icon name="close" /></button>
            </div>
            <BaseSelect v-model="st.chats.camera_id" :options="camOptions" placeholder="全部摄像头" icon="camera" clearable style="width: 190px" @change="resetPage('chats')" />
            <div class="seg">
              <button v-for="r in ranges" :key="r.value" :class="{ on: st.chats.range === r.value }" @click="setRange('chats', r.value)">{{ r.label }}</button>
            </div>
            <input v-model="st.chats.dateFrom" type="date" class="input input--flush date-input" @change="applyRange('chats')" />
            <span class="dim tiny">至</span>
            <input v-model="st.chats.dateTo" type="date" class="input input--flush date-input" @change="applyRange('chats')" />
            <button class="btn btn--xs" @click="resetPage('chats')"><Icon name="search" /> 查询</button>
          </div>

          <div v-if="st.chats.selected.length" class="bulk-bar">
            <Icon name="check" />
            已选 <b class="mono">{{ st.chats.selected.length }}</b> 条
            <div class="spacer" />
            <button class="btn btn--xs btn--ghost" @click="clearSel('chats')">取消选择</button>
            <button class="btn btn--xs btn--danger" @click="removeSelected('chats')"><Icon name="trash" /> 批量删除</button>
          </div>

          <div v-if="st.chats.loading" class="empty"><Icon name="refresh" /><div class="t">加载中…</div></div>
          <div v-else-if="!st.chats.rows.length" class="empty"><Icon name="mic" /><div class="t">暂无语音转写记录</div></div>
          <div v-else class="tbl-wrap scroll-fill">
            <table class="tbl">
              <thead>
                <tr>
                  <th style="width: 44px"><label class="cb"><input type="checkbox" :checked="allSel('chats')" @change="toggleAll('chats', $event.target.checked)" /><span class="box"><Icon name="check" /></span></label></th>
                  <th style="width: 64px">ID</th>
                  <th>转写内容</th>
                  <th style="width: 200px">命中关键词</th>
                  <th style="width: 110px">编号</th>
                  <th style="width: 180px">记录时间</th>
                  <th style="width: 90px" />
                </tr>
              </thead>
              <tbody>
                <tr v-for="c in st.chats.rows" :key="c.id" :class="{ 'row-on': st.chats.selected.includes(c.id) }">
                  <td><label class="cb"><input type="checkbox" :checked="st.chats.selected.includes(c.id)" @change="toggleOne('chats', c.id, $event.target.checked)" /><span class="box"><Icon name="check" /></span></label></td>
                  <td class="num">{{ c.id }}</td>
                  <td :style="c.hit_keywords ? 'color:#ff8fa3' : ''">{{ c.text }}</td>
                  <td class="kw-cell">
                    <span v-if="c.hit_keywords" class="tag tag--high">{{ c.hit_keywords }}</span>
                    <span v-else class="dim">—</span>
                  </td>
                  <td class="num">{{ c.camera_id ? `#${c.camera_id}` : '—' }}</td>
                  <td class="num tiny">{{ fmt(c.created_at) }}</td>
                  <td>
                    <button class="btn btn--xs btn--ghost" title="删除该条" @click="removeOne('chats', c)"><Icon name="trash" /></button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="pager">
            <span class="dim tiny">共 <b class="mono">{{ st.chats.total }}</b> 条 · 第 <b class="mono">{{ st.chats.page + 1 }}</b> / <b class="mono">{{ pages('chats') }}</b> 页</span>
            <div class="spacer" />
            <div class="seg">
              <button v-for="n in pageSizes" :key="n" :class="{ on: st.chats.size === n }" @click="setSize('chats', n)">{{ n }} 条/页</button>
            </div>
            <button class="btn btn--xs" :disabled="st.chats.page <= 0 || st.chats.loading" @click="go('chats', st.chats.page - 1)">
              <Icon name="chev-right" class="flip" /> 上一页
            </button>
            <button class="btn btn--xs" :disabled="st.chats.page + 1 >= pages('chats') || st.chats.loading" @click="go('chats', st.chats.page + 1)">
              下一页 <Icon name="chev-right" />
            </button>
          </div>
        </div>
      </template>

      <!-- ============ 视频检测记录 ============ -->
      <template v-else>
        <div class="panel-hd">
          <span class="panel-title">视频检测记录</span>
          <div class="spacer" />
          <span class="panel-sub mono">共 {{ st.videos.total }} 个</span>
        </div>
        <div class="panel-bd flush">
          <div class="filter-bar">
            <div class="input-affix" style="width: 220px">
              <Icon name="search" />
              <input v-model.trim="st.videos.keyword" class="input input--flush" placeholder="搜索文件名" @keyup.enter="resetPage('videos')" />
              <button v-if="st.videos.keyword" class="tail" title="清空" @click="st.videos.keyword = ''; resetPage('videos')"><Icon name="close" /></button>
            </div>
            <div class="seg">
              <button v-for="s in statusOptions" :key="s.value" :class="{ on: st.videos.status === s.value }" @click="setVideoStatus(s.value)">{{ s.label }}</button>
            </div>
            <div class="seg">
              <button v-for="r in ranges" :key="r.value" :class="{ on: st.videos.range === r.value }" @click="setRange('videos', r.value)">{{ r.label }}</button>
            </div>
            <input v-model="st.videos.dateFrom" type="date" class="input input--flush date-input" @change="applyRange('videos')" />
            <span class="dim tiny">至</span>
            <input v-model="st.videos.dateTo" type="date" class="input input--flush date-input" @change="applyRange('videos')" />
            <button class="btn btn--xs" @click="resetPage('videos')"><Icon name="search" /> 查询</button>
          </div>

          <div v-if="st.videos.selected.length" class="bulk-bar">
            <Icon name="check" />
            已选 <b class="mono">{{ st.videos.selected.length }}</b> 条
            <span class="dim tiny">（删除记录会一并清理磁盘上的原片与标注片）</span>
            <div class="spacer" />
            <button class="btn btn--xs btn--ghost" @click="clearSel('videos')">取消选择</button>
            <button class="btn btn--xs btn--danger" @click="removeSelected('videos')"><Icon name="trash" /> 批量删除</button>
          </div>

          <div v-if="st.videos.loading" class="empty"><Icon name="refresh" /><div class="t">加载中…</div></div>
          <div v-else-if="!st.videos.rows.length" class="empty"><Icon name="film" /><div class="t">暂无视频检测记录</div></div>
          <div v-else class="tbl-wrap scroll-fill">
            <table class="tbl">
              <thead>
                <tr>
                  <th style="width: 44px"><label class="cb"><input type="checkbox" :checked="allSel('videos')" @change="toggleAll('videos', $event.target.checked)" /><span class="box"><Icon name="check" /></span></label></th>
                  <th style="width: 64px">ID</th>
                  <th>文件</th>
                  <th style="width: 110px">时长</th>
                  <th style="width: 140px">事件/报警</th>
                  <th style="width: 130px">状态</th>
                  <th style="width: 180px">时间</th>
                  <th style="width: 210px" />
                </tr>
              </thead>
              <tbody>
                <tr v-for="v in st.videos.rows" :key="v.id" :class="{ 'row-on': st.videos.selected.includes(v.id) }">
                  <td><label class="cb"><input type="checkbox" :checked="st.videos.selected.includes(v.id)" @change="toggleOne('videos', v.id, $event.target.checked)" /><span class="box"><Icon name="check" /></span></label></td>
                  <td class="num">{{ v.id }}</td>
                  <td class="ellip" :title="v.filename">{{ v.filename }}</td>
                  <td class="num">{{ v.duration ? `${v.duration.toFixed(1)}s` : '—' }}</td>
                  <td class="num">{{ v.event_count }} / {{ v.alarm_count }}</td>
                  <td>
                    <span class="pill pill--wide" :class="v.status === 'done' ? 'pill--resolved' : v.status === 'processing' ? 'pill--handling' : 'pill--pending'">
                      {{ statusZh[v.status] || v.status }}
                    </span>
                  </td>
                  <td class="num tiny">{{ fmt(v.created_at) }}</td>
                  <td>
                    <div class="btn-row">
                      <button class="btn btn--xs" :disabled="!origUrl(v)" @click="play(v, false)"><Icon name="play" /> 播放</button>
                      <button v-if="procUrl(v)" class="btn btn--xs btn--ghost" @click="play(v, true)"><Icon name="film" /> 标注</button>
                      <button class="btn btn--xs btn--ghost" title="删除该条" @click="removeOne('videos', v)"><Icon name="trash" /></button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="pager">
            <span class="dim tiny">共 <b class="mono">{{ st.videos.total }}</b> 个 · 第 <b class="mono">{{ st.videos.page + 1 }}</b> / <b class="mono">{{ pages('videos') }}</b> 页</span>
            <div class="spacer" />
            <div class="seg">
              <button v-for="n in pageSizes" :key="n" :class="{ on: st.videos.size === n }" @click="setSize('videos', n)">{{ n }} 条/页</button>
            </div>
            <button class="btn btn--xs" :disabled="st.videos.page <= 0 || st.videos.loading" @click="go('videos', st.videos.page - 1)">
              <Icon name="chev-right" class="flip" /> 上一页
            </button>
            <button class="btn btn--xs" :disabled="st.videos.page + 1 >= pages('videos') || st.videos.loading" @click="go('videos', st.videos.page + 1)">
              下一页 <Icon name="chev-right" />
            </button>
          </div>
        </div>
      </template>
    </div>

    <Modal v-model="playOpen" :title="playTitle" max-width="820px">
      <video v-if="playUrl" :src="playUrl" controls autoplay style="width: 100%; border-radius: 10px; display: block" />
      <dl v-if="playing" class="kv mt">
        <dt>文件</dt><dd class="ellip">{{ playing.filename }}</dd>
        <dt>时长</dt><dd class="mono">{{ playing.duration ? `${playing.duration.toFixed(1)}s` : '—' }}</dd>
        <dt>检测结果</dt><dd>命中 {{ playing.event_count }} 个事件，触发 {{ playing.alarm_count }} 次报警</dd>
      </dl>
    </Modal>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import Icon from '@/ui/Icon.vue'
import Modal from '@/ui/Modal.vue'
import BaseSelect from '@/ui/BaseSelect.vue'
import { toast } from '@/ui/toast'
import { confirm } from '@/ui/confirm'
import api, { dataUrl, downloadFile } from '@/api'

const tabs = [
  { key: 'events', label: '检测事件历史', icon: 'layers' },
  { key: 'chats', label: '语音对话记录', icon: 'mic' },
  { key: 'videos', label: '视频检测记录', icon: 'film' }
]
const active = ref('events')

/** 三类记录各自独立的查询状态：切 Tab 不会互相干扰 */
function blank() {
  return {
    rows: [], total: 0, page: 0, size: 20, loading: false, selected: [],
    keyword: '', dateFrom: '', dateTo: '', range: ''
  }
}
const st = reactive({
  events: { ...blank(), event_type: '', camera_id: '', bullying_only: false },
  chats: { ...blank(), camera_id: '' },
  videos: { ...blank(), status: '' }
})

const pageSizes = [10, 20, 50]
const ranges = [
  { value: 'today', label: '今日' },
  { value: '7d', label: '近7天' },
  { value: '30d', label: '近30天' }
]
const statusZh = { processing: '检测中', done: '已完成', failed: '失败' }
const statusOptions = [
  { value: '', label: '全部状态' },
  { value: 'processing', label: '检测中' },
  { value: 'done', label: '已完成' },
  { value: 'failed', label: '失败' }
]
const typeOptions = [
  { value: 'fight', label: '打架' },
  { value: 'argue', label: '争吵' },
  { value: 'fall', label: '跌倒' },
  { value: 'smoke', label: '抽烟' },
  { value: 'crowd', label: '人员聚集' }
]
const cameras = ref([])
const camOptions = computed(() => cameras.value.map((c) => ({ value: c.id, label: `#${c.id} ${c.name}` })))

const playOpen = ref(false)
const playUrl = ref('')
const playTitle = ref('视频回放')
const playing = ref(null)

const fmt = (t) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '')
const pages = (kind) => Math.max(1, Math.ceil(st[kind].total / st[kind].size))
const allSel = (kind) => st[kind].rows.length > 0 && st[kind].selected.length === st[kind].rows.length

/** 原始视频地址：uploads/ 下的文件名 */
function origUrl(v) {
  const name = v?.original_path ? String(v.original_path).split(/[\\/]/).pop() : ''
  return name ? dataUrl(`uploads/${name}`) : ''
}
/** 标注视频地址 */
function procUrl(v) {
  return v?.processed_path ? dataUrl(v.processed_path) : ''
}

function ymd(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
/** 把本地日历日转换成带时区偏移的 ISO 串。
 *
 * 后端数据库按 UTC 存时间，日期筛选参数也只认带偏移的 ISO（无偏移会被当成 UTC 零点）。
 * 之前直接拼 "2026-09-30T00:00:00"，等于把本地日界当成了 UTC 日界，
 * 与站点差一个时区：本地 00:00~08:00 产生的事件会被判成"前一天"而漏查。
 * JS 对不带偏移的 date-time 形式按本地时间解析，toISOString() 正好给出 UTC 等价时刻。
 */
function localDayIso(dateStr, end = false) {
  const d = new Date(`${dateStr}T${end ? '23:59:59.999' : '00:00:00'}`)
  return Number.isNaN(d.getTime()) ? '' : d.toISOString()
}
function setTimeParams(p, s) {
  const from = s.dateFrom && localDayIso(s.dateFrom)
  const to = s.dateTo && localDayIso(s.dateTo, true)
  if (from) p.set('date_from', from)
  if (to) p.set('date_to', to)
}

async function load(kind) {
  const s = st[kind]
  s.loading = true
  const p = new URLSearchParams()
  if (s.keyword) p.set('keyword', s.keyword)
  if (s.camera_id) p.set('camera_id', s.camera_id)
  if (kind === 'events' && s.event_type) p.set('event_type', s.event_type)
  if (kind === 'events' && s.bullying_only) p.set('bullying_only', 'true')
  if (kind === 'videos' && s.status) p.set('status', s.status)
  setTimeParams(p, s)
  p.set('offset', String(s.page * s.size))
  p.set('limit', String(s.size))
  const url = { events: '/api/history/events', chats: '/api/history/chatlogs', videos: '/api/history/videos' }[kind]
  try {
    const res = await api.get(`${url}?${p}`)
    s.rows = res?.items || []
    s.total = res?.total || 0
    // 当前页被删空时回退一页，避免停留在空白页
    if (!s.rows.length && s.page > 0) {
      s.page = Math.max(0, Math.min(s.page - 1, pages(kind) - 1))
      s.loading = false
      return load(kind)
    }
  } catch {
    s.rows = []
    s.total = 0
  } finally {
    s.loading = false
  }
  s.selected = s.selected.filter((id) => s.rows.some((r) => r.id === id))
}

function switchTab(kind) {
  active.value = kind
  if (!st[kind].rows.length && !st[kind].loading) load(kind)
}

function go(kind, page) {
  if (page < 0 || page >= pages(kind)) return
  st[kind].page = page
  st[kind].selected = []
  load(kind)
}
function setSize(kind, n) {
  st[kind].size = n
  st[kind].page = 0
  st[kind].selected = []
  load(kind)
}
function resetPage(kind) {
  st[kind].page = 0
  st[kind].selected = []
  load(kind)
}
function setRange(kind, value) {
  const s = st[kind]
  if (s.range === value) {
    s.range = ''
    s.dateFrom = ''
    s.dateTo = ''
  } else {
    const to = new Date()
    const from = new Date()
    if (value === '7d') from.setDate(from.getDate() - 6)
    if (value === '30d') from.setDate(from.getDate() - 29)
    s.range = value
    s.dateFrom = ymd(from)
    s.dateTo = ymd(to)
  }
  resetPage(kind)
}
function applyRange(kind) {
  st[kind].range = ''
  resetPage(kind)
}
function setVideoStatus(value) {
  st.videos.status = value
  resetPage('videos')
}

function toggleOne(kind, id, on) {
  const sel = st[kind].selected
  const i = sel.indexOf(id)
  if (on && i < 0) sel.push(id)
  if (!on && i >= 0) sel.splice(i, 1)
}
function toggleAll(kind, on) {
  st[kind].selected = on ? st[kind].rows.map((r) => r.id) : []
}
function clearSel(kind) {
  st[kind].selected = []
}

const DEL_URL = { events: '/api/history/events', chats: '/api/history/chatlogs', videos: '/api/history/videos' }

async function removeOne(kind, row) {
  const name = kind === 'events' ? `事件 #${row.id}（${row.label}）` : kind === 'chats' ? `对话 #${row.id}` : `视频 #${row.id}（${row.filename}）`
  const ok = await confirm({ title: '删除取证记录', text: `确认删除${name}？该操作不可撤销。`, okText: '删除', danger: true })
  if (!ok) return
  try {
    await api.delete(`${DEL_URL[kind]}/${row.id}`)
    toast.ok('已删除')
    load(kind)
  } catch { /* 拦截器已提示 */ }
}

async function removeSelected(kind) {
  const ids = [...st[kind].selected]
  if (!ids.length) return
  const ok = await confirm({ title: '批量删除', text: `确认删除选中的 ${ids.length} 条记录？该操作不可撤销。`, okText: '删除', danger: true })
  if (!ok) return
  try {
    const res = await api.post(`${DEL_URL[kind]}/delete`, { ids })
    toast.ok(`已删除 ${res?.deleted ?? ids.length} 条`)
    st[kind].selected = []
    load(kind)
  } catch { /* 拦截器已提示 */ }
}

function play(v, annotated) {
  const url = annotated ? procUrl(v) : origUrl(v)
  if (!url) return
  playUrl.value = url
  playTitle.value = annotated ? `标注视频 · ${v.filename}` : `视频回放 · ${v.filename}`
  playing.value = v
  playOpen.value = true
}

function exportFeedback() {
  // downloadFile 内部已处理鉴权与失败提示
  downloadFile('/api/history/export/feedback', 'feedback_samples.jsonl')
}

onMounted(async () => {
  // 三类记录都取一次首页，Tab 徽标的条数才准确
  load('events')
  load('chats')
  load('videos')
  try { cameras.value = await api.get('/api/cameras') } catch { /* 静默 */ }
})
</script>

<style scoped>
.ellip { max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* Tab 栏 */
.tabs {
  display: flex; align-items: center; gap: 6px;
  padding: 5px;
  border-radius: var(--r-lg);
  background: var(--bg-inset);
  border: 1px solid var(--line-2);
  flex-shrink: 0;
}
.tab {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 9px 16px;
  border: none; background: none; cursor: pointer;
  border-radius: var(--r-md);
  font-family: inherit; font-size: 13.5px;
  color: var(--tx-2);
  transition: background 0.18s, color 0.18s, box-shadow 0.18s;
}
.tab :deep(.ico) { width: 16px; height: 16px; }
.tab:hover { color: var(--tx-1); }
.tab.on {
  background: linear-gradient(135deg, rgba(47, 214, 240, 0.22), rgba(59, 130, 246, 0.14));
  color: #d9f7ff;
  box-shadow: inset 0 0 0 1px rgba(47, 214, 240, 0.3);
}
.tab .cnt {
  min-width: 30px; padding: 1px 7px;
  border-radius: 20px;
  background: rgba(122, 162, 220, 0.14);
  font-size: 11.5px; color: var(--tx-2);
  text-align: center;
}
.tab.on .cnt { background: rgba(47, 214, 240, 0.22); color: #d9f7ff; }

.ellip { max-width: 340px; }
/* .filter-bar / .date-input / .bulk-bar / .pager / .flip 已收敛到全局 style.css；
   此处仅保留本页表格列的宽度差异 */

/* 状态 / 关键词列统一加宽，文字不换行 */
.pill--wide { min-width: 76px; justify-content: center; }
.kw-cell { white-space: nowrap; }
.tbl tbody tr.row-on { background: rgba(47, 214, 240, 0.09); }
</style>
