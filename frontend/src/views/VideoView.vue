<template>
  <div class="page">
    <div class="grid g-main grid-fill">
      <!-- 左：视频检测与回放 -->
      <div class="panel corner fade-up panel-fill">
        <div class="panel-hd">
          <span class="panel-title">视频检测与回放</span>
          <div class="spacer" />
          <template v-if="current">
            <span class="panel-sub mono ellip" :title="current.filename">{{ current.filename }}</span>
            <span class="pill pill--wide" :class="stCls(current.status)">{{ statusZh[current.status] || current.status }}</span>
            <button class="btn btn--xs" @click="backToSelect"><Icon name="upload" /> 重新上传</button>
          </template>
          <span v-else class="panel-sub">上传新视频检测，或从右侧「检测记录」中点选一条回放</span>
        </div>
        <div class="panel-bd">
          <template v-if="current">
            <div class="player">
              <video ref="videoEl" :src="playUrl" controls autoplay @loadedmetadata="onMeta" />
              <!-- 检测中：在视频上盖一层进度，避免"以为卡住了" -->
              <div v-if="current.status === 'processing'" class="player-mask">
                <div class="pm-ring" :style="{ '--p': `${Math.round((current.progress || 0) * 100)}%` }">
                  <b class="mono">{{ Math.round((current.progress || 0) * 100) }}%</b>
                </div>
                <div class="pm-t">正在逐帧检测与语音识别…</div>
                <div class="pm-sub tiny">视觉：{{ current.event_count }} 个行为事件 · 语音：{{ speech.length }} 段转写</div>
              </div>
            </div>

            <!-- 播放控制与统计 -->
            <div class="play-bar">
              <button class="btn btn--xs" :class="mode === 'annotated' ? 'btn--primary' : 'btn--ghost'"
                      :disabled="!procUrl(current)" @click="switchMode('annotated')">
                <Icon name="film" /> 标注视频
              </button>
              <button class="btn btn--xs" :class="mode === 'original' ? 'btn--primary' : 'btn--ghost'"
                      :disabled="!origUrl(current)" @click="switchMode('original')">
                <Icon name="play" /> 原片
              </button>

              <span class="pb-sep" />
              <span class="dim tiny">时长 <b class="mono">{{ fmtDur(current.duration) }}</b></span>
              <span class="dim tiny">事件 <b class="mono">{{ events.length }}</b></span>
              <span class="dim tiny">报警 <b class="mono">{{ current.alarm_count }}</b></span>
              <span class="dim tiny">语音 <b class="mono">{{ speech.length }}</b></span>

              <div class="spacer" />
              <span v-if="speechBusy" class="spk-hint">
                <Icon name="mic" /> 语音识别中 {{ Math.round(speechProgress * 100) }}%
              </span>
              <span v-else-if="speechDone && !speech.length" class="dim tiny">该视频未识别到语音内容</span>
              <button v-if="!speechBusy && !speechDone && current.status !== 'failed'"
                      class="btn btn--xs btn--ghost" @click="startSpeechScan()">
                <Icon name="mic" /> 语音检测
              </button>
              <button class="btn btn--xs btn--ghost" @click="$router.push('/alarms')">查看报警</button>
            </div>
          </template>

          <!-- 未选记录：上传区 -->
          <div v-else class="picker">
            <FileDrop ref="dropEl" :hint="'支持 mp4 / avi / mov / mkv / webm · 单个文件不超过 512 MB'" @file="onFile" />
            <div class="row picker-bar">
              <div class="field" style="width: 220px">
                <label>关联点位（可选）</label>
                <BaseSelect v-model="cameraId" :options="camOptions" placeholder="不关联点位" icon="camera" clearable />
              </div>
              <div class="spacer" />
              <span class="dim tiny">上传后将自动执行：逐帧行为检测 + 人脸识别 + 情绪识别 + 语音关键词检测</span>
              <button class="btn btn--primary" :disabled="!file || uploading" @click="upload">
                <Icon name="cpu" /> {{ uploading ? '正在上传…' : '开始检测' }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- 右上：检测输出（事件流 / 语音转写） -->
      <div class="panel corner fade-up d1 panel-fill">
        <div class="panel-hd">
          <div class="vp-tabs">
            <button type="button" class="vp-tab" :class="{ on: tab === 'events' }" @click="tab = 'events'">
              <Icon name="pulse" /><span>检测事件流</span><b>{{ events.length }}</b>
            </button>
            <button type="button" class="vp-tab" :class="{ on: tab === 'speech' }" @click="tab = 'speech'">
              <Icon name="mic" /><span>语音识别与关键词</span><b>{{ speech.length }}</b>
            </button>
          </div>
          <div class="spacer" />
          <span v-if="current && current.status === 'processing'" class="live-dot"><i />检测中</span>
        </div>
        <div class="panel-bd">
          <div v-if="!current" class="empty" style="padding: 30px 10px">
            <Icon name="film" />
            <div class="t">选择一条检测记录后在此查看输出</div>
          </div>

          <!-- 事件流 -->
          <div v-else-if="tab === 'events'" class="stream">
            <div v-if="!events.length" class="empty" style="padding: 30px 10px">
              <Icon name="pulse" />
              <div class="t">{{ current.status === 'processing' ? '检测中，事件将陆续出现…' : '未检出异常事件' }}</div>
            </div>
            <div v-for="e in events" :key="e.id" class="ecard" :class="`ecard--${lvOf(e)}`" @click="seekTo(e.t)">
              <div class="ecard-ava">
                <img v-if="e.person && e.person.avatar_url" :src="e.person.avatar_url" :alt="e.person.name" loading="lazy" />
                <Icon v-else :name="iconOf(e.event_type)" />
              </div>
              <div class="ecard-main">
                <div class="ecard-hd">
                  <span class="tag" :class="`tag--${lvOf(e)}`">{{ clock(e.t) }}</span>
                  <span class="tag tag--mute">{{ e.label }}</span>
                  <b v-if="e.person" class="ecard-name">{{ e.person.name }}</b>
                  <span v-else class="ecard-unknown">未识别人员</span>
                  <div class="spacer" />
                  <span class="mono tiny dim">{{ Math.round(e.confidence * 100) }}%</span>
                </div>
                <div class="ecard-meta">
                  <template v-if="e.person">
                    <span>{{ e.person.type_label }}</span>
                    <span v-if="e.person.gender">{{ e.person.gender }}</span>
                    <span>{{ e.person.class_name || e.person.department || '—' }}</span>
                    <span class="mono">编号 {{ e.person.no }}</span>
                  </template>
                  <span v-else class="dim">未授权或未建档，仅显示行为线索</span>
                </div>
                <div class="ecard-foot">
                  <span v-if="e.emotion" class="emo-chip" :class="{ neg: e.emotion.negative }" :title="EMOTION_TIP">
                    {{ e.emotion.icon }} {{ e.emotion.label }}<i>{{ Math.round(e.emotion.confidence * 100) }}%</i>
                  </span>
                  <span class="explain-mini" v-if="explainOf(e)">{{ explainOf(e) }}</span>
                  <div class="spacer" />
                  <button class="btn btn--xs btn--ghost"><Icon name="play" /> 定位</button>
                </div>
              </div>
            </div>
          </div>

          <!-- 语音转写 -->
          <div v-else class="stream">
            <div v-if="!speech.length" class="empty" style="padding: 30px 10px">
              <Icon name="mic" />
              <div class="t">{{ speechBusy ? '语音识别中…' : '暂无语音转写' }}</div>
              <div v-if="speechErr" class="dim tiny" style="margin-top: 6px">{{ speechErr }}</div>
            </div>
            <div v-for="s in speech" :key="s.id" class="bubble" :class="lvClass(s)" @click="seekTo(s.start)">
              <div class="bubble-time mono tiny">{{ clock(s.start) }}</div>
              <div class="txt">
                <span v-for="(seg, k) in markSegments(s.text, s)" :key="k" :class="seg.lv ? 'mark mark--' + seg.lv : ''">{{ seg.t }}</span>
              </div>
              <div class="meta">
                <span class="mono">{{ clock(s.start) }} – {{ clock(s.end) }}</span>
                <span v-if="s.hit_keywords.length" class="kw" :class="'kw--' + lvOfSpeech(s)">
                  {{ levelLabel(lvOfSpeech(s)) }}：{{ s.hit_keywords.join('、') }}
                </span>
                <div class="spacer" />
                <button class="btn btn--xs btn--ghost"><Icon name="play" /> 定位</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 底部：检测记录（整行） -->
    <div class="panel corner fade-up d2 rec-panel">
      <div class="panel-hd">
        <span class="panel-title">检测记录</span>
        <span class="panel-sub mono">{{ records.length }} 个</span>
        <div class="spacer" />
        <span class="dim tiny">点击任意一条回放其检测结果与时间轴</span>
        <button class="btn btn--icon btn--sm btn--ghost" title="刷新" @click="load"><Icon name="refresh" /></button>
      </div>
      <div class="panel-bd flush">
        <div v-if="loading" class="empty" style="padding: 24px"><Icon name="refresh" /><div class="t">加载中…</div></div>
        <div v-else-if="!records.length" class="empty" style="padding: 24px">
          <Icon name="film" /><div class="t">暂无检测记录，上传视频后自动分析</div>
        </div>
        <div v-else class="rec-grid">
          <div v-for="r in records" :key="r.id" class="rec" :class="{ on: current && current.id === r.id }" @click="pick(r)">
            <div class="rec-top">
              <span class="rec-name ellip" :title="r.filename">{{ r.filename }}</span>
              <span class="pill pill--wide" :class="stCls(r.status)">{{ statusZh[r.status] || r.status }}</span>
            </div>
            <div class="bar-track">
              <div class="bar-fill" :class="{ ok: r.status === 'done', bad: r.status === 'failed' }"
                   :style="{ width: `${Math.round((r.progress || 0) * 100)}%` }" />
            </div>
            <div class="rec-foot">
              <span class="dim tiny mono">{{ Math.round((r.progress || 0) * 100) }}%</span>
              <span class="dim tiny">事件 {{ r.event_count }} · 报警 {{ r.alarm_count }}</span>
              <div class="spacer" />
              <span class="dim tiny mono">{{ fmt(r.created_at) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * 视频检测：与实时检测对齐的完整离线链路。
 *
 * 与实时检测的对应关系：
 *   实时检测                          视频检测
 *   ─────────────────────────────    ─────────────────────────────
 *   摄像头实时送帧 → WS 推理            本地上传视频 → 后台逐帧推理
 *   麦克风 PCM → 语音 WS                **浏览器解码视频音频轨** → 同一个语音 WS
 *   右侧「语音识别与关键词」            右侧 Tab「语音识别与关键词」
 *   右侧「实时事件流」                  右侧 Tab「检测事件流」
 *   框/骨架画在 canvas 上               后端把标注渲染进视频文件，直接播放
 *
 * 为什么音频要放在浏览器解码：后端只依赖 OpenCV，没有 ffmpeg / 音频解码库。
 * 而浏览器原生就能把 mp4/webm 的音频轨解成 PCM，再按 16k 单声道推给既有的
 * 语音 WS —— 零新增依赖，且完全复用已有的 ASR Provider 与关键词三档引擎。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Icon from '@/ui/Icon.vue'
import FileDrop from '@/ui/FileDrop.vue'
import BaseSelect from '@/ui/BaseSelect.vue'
import { toast } from '@/ui/toast'
import api, { dataUrl, wsUrl, wsProtocols } from '@/api'

const EMOTION_TIP = '基于面部表情的实时估计，仅供参考，非心理诊断；不写入学生档案'

const videoEl = ref(null)
const dropEl = ref(null)
const cameras = ref([])
const cameraId = ref('')
const file = ref(null)
const records = ref([])
const loading = ref(true)
const uploading = ref(false)
const current = ref(null)
const playUrl = ref('')
const mode = ref('annotated')
const tab = ref('events')

const events = ref([])
const speech = ref([])
let lastEventId = 0
let lastSpeechId = 0

const speechBusy = ref(false)
const speechDone = ref(false)
const speechProgress = ref(0)
const speechErr = ref('')
let listTimer = null
let pollTimer = null
let speakTimer = null
let wsAudio = null
let audioCtx = null
let audioAbort = false

const statusZh = { processing: '检测中', done: '已完成', failed: '失败' }
const camOptions = computed(() => cameras.value.map((c) => ({ value: c.id, label: c.name })))
const fmt = (t) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '')
const clock = (sec) => {
  const s = Math.max(0, Number(sec) || 0)
  const m = Math.floor(s / 60)
  return `${String(m).padStart(2, '0')}:${String(Math.floor(s % 60)).padStart(2, '0')}`
}
const fmtDur = (d) => (d ? `${Math.floor(d / 60)}分${Math.round(d % 60)}秒` : '—')
const stCls = (s) => ({ processing: 'pill--handling', done: 'pill--resolved', failed: 'pill--pending' }[s] || 'pill--ignored')
const lvOf = (e) => (e.is_bullying ? 'high' : (e.event_type === 'fall' ? 'medium' : 'low'))
const ICONS = { fall: 'alert', smoke: 'flag', fight: 'alert', bullying: 'alert', argue: 'pulse', crowd: 'layers', person: 'user' }
const iconOf = (t) => ICONS[t] || 'pulse'

/** 事件详情里的可解释指标（欺凌四项 + 手腕速度等）压成一行，供复核时快速判断 */
function explainOf(e) {
  const d = e.detail || {}
  const parts = []
  if (d.bully_score !== undefined) parts.push(`欺凌分 ${Number(d.bully_score).toFixed(2)}`)
  if (d.asymmetry !== undefined) parts.push(`运动不对称 ${Number(d.asymmetry).toFixed(2)}`)
  if (d.retreat !== undefined) parts.push(`退缩 ${Number(d.retreat).toFixed(2)}`)
  if (d.chase !== undefined) parts.push(`追逃 ${Number(d.chase).toFixed(2)}`)
  if (d.wrist_speed !== undefined) parts.push(`手腕速度 ${Number(d.wrist_speed).toFixed(2)}`)
  return parts.slice(0, 4).join(' · ')
}

function personOf(e) { return (e.detail && e.detail.persons && e.detail.persons[0]) || null }
function emotionOf(e) { return (e.detail && e.detail.emotion) || null }

/** 把后端扁平事件补上前端展示需要的派生字段（person / emotion） */
function hydrate(items) {
  return items.map((e) => ({
    ...e,
    person: personOf(e),
    emotion: emotionOf(e)
  }))
}

function origUrl(r) {
  const name = r?.original_path ? String(r.original_path).split(/[\\/]/).pop() : ''
  return name ? dataUrl(`uploads/${name}`) : ''
}
function procUrl(r) { return r?.processed_path ? dataUrl(r.processed_path) : '' }

// ---------------------------------------------------------------- 三档高亮（与实时检测同一套规则）
const LV_NAME = { alarm: '报警', warn: '警告', highlight: '关注' }
const levelLabel = (lv) => LV_NAME[lv] || ''

/**
 * 转写条目的响应档位。
 * 后端只回传命中的词，不逐条回传档位，但**是否生成了报警**是可靠的档位信号：
 * 只有 alarm 档才会生成报警记录，因此有 alarm_id 即为最高档，其余按警告处理。
 */
function lvOfSpeech(s) {
  if (!s?.hit_keywords?.length) return null
  return s.alarm_id ? 'alarm' : 'warn'
}
const lvClass = (s) => {
  const lv = lvOfSpeech(s)
  return lv ? `lv-${lv}` : ''
}

function markSegments(text, s) {
  const src = text || ''
  const words = (s.hit_keywords || []).filter(Boolean)
  const lv = lvOfSpeech(s) || 'warn'
  if (!src || !words.length) return [{ t: src, lv: null }]
  const low = src.toLowerCase()
  const marks = []
  for (const w of words) {
    const key = String(w).toLowerCase()
    if (!key) continue
    for (let from = 0; ;) {
      const i = low.indexOf(key, from)
      if (i < 0) break
      marks.push({ s: i, e: i + key.length, lv })
      from = i + key.length
    }
  }
  if (!marks.length) return [{ t: src, lv: null }]
  marks.sort((a, b) => a.s - b.s || b.e - a.e)
  const out = []
  let cur = 0
  for (const m of marks) {
    if (m.s < cur) continue
    if (m.s > cur) out.push({ t: src.slice(cur, m.s), lv: null })
    out.push({ t: src.slice(m.s, m.e), lv: m.lv })
    cur = m.e
  }
  if (cur < src.length) out.push({ t: src.slice(cur), lv: null })
  return out
}

// ---------------------------------------------------------------- 列表与选择
async function load() {
  try {
    const res = await api.get('/api/video')
    records.value = Array.isArray(res) ? res : (res?.items || [])
    // 当前记录的状态可能刚从 processing 变成 done：同步一次，
    // 否则顶部状态与进度会一直停在旧值
    if (current.value) {
      const fresh = records.value.find((r) => r.id === current.value.id)
      if (fresh) {
        const wasProcessing = current.value.status === 'processing'
        current.value = fresh
        if (wasProcessing && fresh.status === 'done') onDetectFinished(fresh)
      }
    }
  } catch { /* 静默 */ } finally {
    loading.value = false
  }
}

function onFile(f) { file.value = f }

async function upload() {
  if (!file.value) return
  uploading.value = true
  const chosen = file.value
  try {
    const fd = new FormData()
    fd.append('file', chosen)
    const q = cameraId.value ? `?camera_id=${cameraId.value}` : ''
    const rec = await api.post(`/api/video/upload${q}`, fd, { headers: { 'Content-Type': 'multipart/form-data' } })
    toast.ok('上传成功，正在后台逐帧检测')
    file.value = null
    dropEl.value?.reset()
    await load()
    const fresh = records.value.find((r) => r.id === rec.id)
    if (fresh) {
      pick(fresh)
      // 视觉检测在后台跑；语音识别在前端并行做（解码后推给语音 WS）
      startSpeechScan(chosen)
    }
  } catch { /* 拦截器已提示 */ } finally {
    uploading.value = false
  }
}

/** 选择一条记录：拉取其完整时间轴，并默认播放标注视频（无标注则播原片） */
async function pick(r) {
  stopSpeech()
  current.value = r
  mode.value = procUrl(r) ? 'annotated' : 'original'
  playUrl.value = mode.value === 'annotated' ? procUrl(r) : origUrl(r)
  events.value = []
  speech.value = []
  lastEventId = 0
  lastSpeechId = 0
  speechErr.value = ''
  // 不预设"已完成"：历史记录可能是本功能上线前检测的，从未跑过语音识别。
  // 保持为 false，用户才能看到「语音检测」按钮自行补跑。
  speechDone.value = false
  await pullOutput(true)
}

function backToSelect() {
  stopSpeech()
  current.value = null
  playUrl.value = ''
  events.value = []
  speech.value = []
}

function switchMode(m) {
  const r = current.value
  if (!r) return
  const url = m === 'annotated' ? procUrl(r) : origUrl(r)
  if (!url) { toast.warn(m === 'annotated' ? '该记录暂无标注视频' : '该记录暂无原片文件'); return }
  const at = videoEl.value?.currentTime || 0
  mode.value = m
  playUrl.value = url
  // 切换视频源后回到同一时间点，方便"原片 ↔ 标注"对照查看
  requestAnimationFrame(() => {
    if (videoEl.value) { videoEl.value.currentTime = at; videoEl.value.play().catch(() => {}) }
  })
}

function onMeta() { /* 预留：需要按视频尺寸调整叠加层时使用 */ }

/** 跳转到时间轴上的某一秒（点击事件卡片 / 转写条目） */
function seekTo(t) {
  const v = videoEl.value
  if (!v) return
  v.currentTime = Math.max(0, Number(t) || 0)
  v.play().catch(() => {})
}

function onDetectFinished(r) {
  // 检测完成后自动切到标注视频并从头播放 —— 这是用户最想看到的结果
  if (procUrl(r)) {
    mode.value = 'annotated'
    playUrl.value = procUrl(r)
    requestAnimationFrame(() => {
      if (videoEl.value) { videoEl.value.currentTime = 0; videoEl.value.play().catch(() => {}) }
    })
    toast.ok('检测完成，正在播放标注视频')
  } else {
    toast.warn('检测完成，但标注视频生成失败，可播放原片查看')
  }
}

// ---------------------------------------------------------------- 增量拉取输出
async function pullOutput(reset = false) {
  const r = current.value
  if (!r) return
  try {
    const [ev, sp] = await Promise.all([
      api.get(`/api/video/${r.id}/events?since_id=${reset ? 0 : lastEventId}`),
      api.get(`/api/video/${r.id}/speech?since_id=${reset ? 0 : lastSpeechId}`)
    ])
    if (ev.items?.length) {
      events.value.push(...hydrate(ev.items))
      lastEventId = ev.last_id
    }
    if (sp.items?.length) {
      speech.value.push(...sp.items)
      lastSpeechId = sp.last_id
    }
  } catch { /* 静默：轮询失败不该弹提示打断观看 */ }
}

// ---------------------------------------------------------------- 视频语音识别
/** 从视频文件解码音频 → 16k 单声道 PCM → 按 2 倍速推给语音 WS */
async function startSpeechScan(pickedFile) {
  const r = current.value
  if (!r || speechBusy.value) return
  const src = pickedFile || await fetchFileOf(r)
  if (!src) { speechErr.value = '无法读取视频文件，语音检测已跳过'; speechDone.value = true; return }

  speechBusy.value = true
  speechErr.value = ''
  speechDone.value = false
  speechProgress.value = 0
  audioAbort = false

  try {
    const pcm = await decodeToPcm16k(src)
    if (!pcm) {
      speechErr.value = '浏览器无法解码该视频的音频轨（仅 mp4 / webm 支持），视觉检测不受影响'
      speechBusy.value = false
      speechDone.value = true
      return
    }
    if (audioAbort) return
    await streamPcm(r, pcm)
  } catch (e) {
    speechErr.value = `语音识别中断：${e?.message || e}`
    speechBusy.value = false
    speechDone.value = true
  }
}

/** 记录里只存了路径，重新取回原文件用于前端解码 */
async function fetchFileOf(r) {
  const url = origUrl(r)
  if (!url) return null
  try {
    const blob = await api.get(url, { responseType: 'blob' })
    return new File([blob], r.filename || 'video.mp4', { type: blob.type || 'video/mp4' })
  } catch {
    return null
  }
}

async function decodeToPcm16k(f) {
  const AC = window.AudioContext || window.webkitAudioContext
  if (!AC) return null
  let decoded = null
  try {
    audioCtx = audioCtx || new AC()
    decoded = await audioCtx.decodeAudioData(await f.arrayBuffer())
  } catch {
    return null
  }
  if (!decoded || !decoded.length) return null

  // 重采样到 16k 单声道：用 OfflineAudioContext 让浏览器自己做高质量重采样，
  // 比自己写线性插值准确得多，而 ASR 对采样率偏差是敏感的
  const rate = 16000
  const frames = Math.max(1, Math.ceil(decoded.duration * rate))
  const offline = new OfflineAudioContext(1, frames, rate)
  const src = offline.createBufferSource()
  src.buffer = decoded
  src.connect(offline.destination)
  src.start()
  const rendered = await offline.startRendering()
  const f32 = rendered.getChannelData(0)

  const i16 = new Int16Array(f32.length)
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]))
    i16[i] = s < 0 ? s * 0x8000 : s * 0x7fff
  }
  return i16
}

function streamPcm(rec, i16) {
  return new Promise((resolve) => {
    const sock = new WebSocket(wsUrl('/api/ws/audio'), wsProtocols())
    wsAudio = sock
    sock.binaryType = 'arraybuffer'
    let off = 0
    const CHUNK = 3200          // 0.2s @16k
    const SPEED = 2             // 以 2 倍速推送：10 分钟视频约 5 分钟识别完
    const TICK = (CHUNK / 16000) * 1000 / SPEED

    const finish = () => {
      if (speakTimer) { clearInterval(speakTimer); speakTimer = null }
      try { sock.readyState === 1 && sock.send(JSON.stringify({ flush: true })) } catch { /* noop */ }
      setTimeout(() => { try { sock.close() } catch { /* noop */ } }, 1500)
      speechBusy.value = false
      speechDone.value = true
      speechProgress.value = 1
      if (wsAudio === sock) wsAudio = null
      resolve()
    }

    sock.onopen = () => {
      sock.send(JSON.stringify({ video_id: rec.id, camera_id: rec.camera_id ?? null }))
      speakTimer = setInterval(() => {
        if (audioAbort) { finish(); return }
        if (sock.readyState !== 1) return
        if (off >= i16.length) { finish(); return }
        const n = Math.min(CHUNK, i16.length - off)
        sock.send(i16.subarray(off, off + n))
        off += n
        speechProgress.value = off / i16.length
      }, TICK)
    }

    sock.onmessage = (ev) => {
      let d
      try { d = JSON.parse(ev.data) } catch { return }
      if (d.type === 'error') {
        speechErr.value = d.detail || '语音识别服务不可用'
        finish()
      }
      // 成功的转写**不在这里入列**：后端已把它写进 chat_logs，
      // 1.5 秒后的轮询会连同行 id 一起取回来。若这里也 push 一份，
      // 界面上每条转写会出现两次，且两份的 key 体系不同、无法去重。
    }
    sock.onerror = () => { speechErr.value = '语音通道连接失败'; finish() }
    sock.onclose = () => { if (speechBusy.value) { speechBusy.value = false; speechDone.value = true; resolve() } }
  })
}

function stopSpeech() {
  audioAbort = true
  if (speakTimer) { clearInterval(speakTimer); speakTimer = null }
  if (wsAudio) { try { wsAudio.close() } catch { /* noop */ } wsAudio = null }
  speechBusy.value = false
}

onMounted(async () => {
  load()
  try { cameras.value = await api.get('/api/cameras') } catch { /* 静默 */ }
  listTimer = setInterval(load, 3000)        // 跟踪各任务进度
  pollTimer = setInterval(() => pullOutput(), 1500)   // 增量刷新右侧输出
})
onUnmounted(() => {
  if (listTimer) clearInterval(listTimer)
  if (pollTimer) clearInterval(pollTimer)
  stopSpeech()
  try { audioCtx?.close() } catch { /* noop */ }
})
</script>

<style scoped>
/* .ellip 已收敛到全局 style.css 的通用工具类 */

/* 选择区：未选片时占满整个盒子 */
.picker { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: 12px; }
.picker :deep(.drop) {
  flex: 1; min-height: 150px;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  padding: 20px;
}
.picker :deep(.drop-ico) { width: 60px; height: 60px; border-radius: 17px; }
.picker :deep(.drop-ico svg) { width: 28px; height: 28px; }
.picker :deep(.drop .t1) { font-size: 15px; }
.picker-bar { flex-shrink: 0; align-items: flex-end; }

/* 播放区按剩余高度自适应，视频等比完整显示 */
.player {
  position: relative;
  flex: 1; min-height: 150px;
  display: grid; place-items: center;
  background: #03060c;
  border: 1px solid var(--line-2);
  border-radius: var(--r-md);
  overflow: hidden;
}
.player video { width: 100%; height: 100%; object-fit: contain; display: block; }

/* 检测中遮罩：给出明确的"在跑"信号，避免被误解为卡死 */
.player-mask {
  position: absolute; inset: 0;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px;
  background: rgba(3, 6, 12, 0.62);
  -webkit-backdrop-filter: blur(3px);
  backdrop-filter: blur(3px);
}
.pm-ring {
  width: 92px; height: 92px; border-radius: 50%;
  display: grid; place-items: center;
  background:
    conic-gradient(var(--acc) var(--p), rgba(122, 162, 220, 0.16) 0) border-box;
  -webkit-mask: radial-gradient(circle, transparent 0 34px, #000 35px);
  mask: radial-gradient(circle, transparent 0 34px, #000 35px);
}
.pm-ring b { -webkit-mask: none; mask: none; font-size: 15px; color: #dff7ff; }
.pm-t { font-size: 13px; color: var(--tx-1); }
.pm-sub { color: var(--tx-3); }

.play-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.play-bar b { color: var(--tx-1); }
.pb-sep { width: 1px; height: 14px; background: var(--line-2); margin: 0 2px; }
.spk-hint { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--acc); }
.spk-hint svg { width: 13px; height: 13px; }

/* 输出区 Tab */
.vp-tabs { display: flex; align-items: center; gap: 7px; }
.vp-tab {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 12px;
  border: 1px solid var(--line-2); border-radius: 999px;
  background: none; color: var(--tx-2);
  font-family: inherit; font-size: 12.5px; cursor: pointer;
  transition: border-color 0.16s, background 0.16s, color 0.16s;
}
.vp-tab > svg { width: 13px; height: 13px; }
.vp-tab > b { font-family: var(--font-mono); font-size: 11px; color: var(--tx-3); }
.vp-tab:hover { border-color: var(--line-3); background: var(--bg-raise); }
.vp-tab.on {
  color: #eafaff; border-color: rgba(47, 214, 240, 0.7);
  background: linear-gradient(135deg, rgba(47, 214, 240, 0.22), rgba(59, 130, 246, 0.1));
}
.vp-tab.on > b { color: #bfeaff; }

.live-dot { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--ok); }
.live-dot > i {
  width: 7px; height: 7px; border-radius: 50%; background: var(--ok);
  box-shadow: 0 0 8px var(--ok);
  animation: breathe 1.4s ease-in-out infinite;
}

/* 输出流：与实时检测共用 .ecard / .bubble 全局样式，仅补少量差异 */
.stream { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
.stream .ecard { cursor: pointer; }
.explain-mini { font-size: 11px; color: var(--tx-3); }

/* 转写气泡：覆写全局 .bubble 的布局，加上时间轴列 */
.stream .bubble { cursor: pointer; }
.bubble-time { color: var(--tx-3); margin-bottom: 4px; }

/* 记录列表：底部整行，用网格铺开，避免单列时右侧大片留白 */
.rec-panel { flex-shrink: 0; max-height: 34vh; display: flex; flex-direction: column; margin-top: 14px; }
.rec-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 0;
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}
.rec {
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
  border-right: 1px solid var(--line);
  cursor: pointer;
  transition: background 0.16s, box-shadow 0.16s;
}
.rec:hover { background: rgba(47, 214, 240, 0.045); }
.rec.on { background: rgba(47, 214, 240, 0.1); box-shadow: inset 2px 0 0 var(--acc); }
.rec-top { display: flex; align-items: center; gap: 10px; margin-bottom: 7px; }
.rec-name { flex: 1; min-width: 0; font-size: 13px; }
.rec-foot { display: flex; align-items: center; gap: 10px; margin-top: 6px; }

.pill--wide { min-width: 68px; justify-content: center; flex-shrink: 0; }
</style>
