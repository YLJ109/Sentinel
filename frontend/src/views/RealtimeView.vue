<template>
  <div class="page">
    <div v-if="alarm" class="banner fade-up">
      <div class="banner-ico"><Icon name="alert" /></div>
      <div class="banner-txt" style="flex: 1">
        <b>霸凌行为报警</b>
        <div class="r">{{ alarm.reason }}<span class="dim"> · 编号 #{{ alarm.alarm_id }}</span></div>
      </div>
      <button class="btn btn--sm btn--danger" @click="$router.push('/alarms')">立即处置</button>
      <button class="btn btn--sm btn--ghost" @click="alarm = null">关闭</button>
    </div>

    <div class="grid g-main grid-fill">
      <!-- 视觉检测 -->
      <div class="panel corner fade-up panel-fill">
        <div class="panel-hd">
          <span class="panel-title">视觉行为检测</span>
          <div class="spacer" />
          <span class="panel-sub mono">{{ inferMs ? `${inferMs.toFixed(1)} ms` : '— ms' }} · {{ people }} 人 · 人脸 {{ faces.length }}<span v-if="skipped"> · 跳过推理（{{ skipReasonText }}）</span> · 运动 {{ motionText }}</span>
          <span class="status-chip" :class="{ warn: !running || wsState !== 'open' }">
            <span class="dot" />{{ chipText }}
          </span>
          <button v-if="running && wsState !== 'open'" class="btn btn--xs" :disabled="wsState === 'connecting'" @click="connectDetect">
            <Icon name="refresh" /> {{ wsState === 'connecting' ? '连接中…' : '重连检测通道' }}
          </button>
        </div>
        <div class="panel-bd">
          <div class="row" style="margin-bottom: 14px">
            <BaseSelect
              v-model="cameraId"
              :options="camOptions"
              placeholder="未关联点位"
              icon="camera"
              clearable
              style="width: 220px"
            />
            <div class="spacer" />
            <button v-if="!running" class="btn btn--primary" @click="start">
              <Icon name="video" /> 开启摄像头
            </button>
            <button v-else class="btn btn--danger" @click="stopAll"><Icon name="close" /> 停止检测</button>
          </div>

          <!-- 舞台随剩余高度自适应：宽高同时受限时按比例取最大可用尺寸 -->
          <div class="stage-wrap">
            <div
              class="stage-video"
              :style="{ aspectRatio: stageRatio, '--ar': arNum }"
            >
              <!-- 视频与骨架画布各自水平镜像；检测框不镜像，改为在 boxStyle 里镜像 x 坐标。
                   这样框位置与镜像后的画面仍然对齐，而框内的标签文字保持正向。 -->
              <video ref="videoEl" autoplay playsinline muted @loadedmetadata="onVideoMeta" />
              <div v-if="running" class="scan" />
              <!-- 骨架叠加层：按视频固有像素尺寸绘制，CSS 100% 铺满，容器比例与视频一致故 1:1 映射 -->
              <canvas ref="canvasEl" class="skeleton" />
              <!-- 人脸检测叠加层：细边单独标识 -->
              <div v-for="(f, i) in faces" :key="`f${i}`" class="bbox face" :style="boxStyle(f.bbox)">
                <span class="lb">{{ f.confidence ? `人脸 ${Math.round(f.confidence * 100)}%` : '人脸' }}</span>
              </div>
              <div v-for="b in renderBoxes" :key="b.key" class="bbox" :class="b.type" :style="boxStyle([b.x1, b.y1, b.x2, b.y2])">
                <span class="lb">#{{ b.trackId }} {{ b.label }} {{ Math.round(b.conf * 100) }}%</span>
              </div>
              <span class="corner c-tl" /><span class="corner c-tr" /><span class="corner c-bl" /><span class="corner c-br" />
              <div v-if="running" class="hud">REC · {{ fps }} FPS · {{ frames }} FRAMES</div>
              <div v-if="!running" class="off">
                <div>
                  <Icon name="video" />
                  <div class="t">摄像头未开启</div>
                  <div class="dim tiny" style="margin-top: 6px">点击「开启摄像头」授权后开始实时检测</div>
                </div>
              </div>
            </div>
          </div>

          <div class="row mt">
            <label class="sw">
              <input type="checkbox" :checked="audioOn" @change="toggleAudio($event.target.checked)" />
              <span class="track"><span class="knob" /></span>
              <span class="sw-label">语音实时监听</span>
            </label>
            <div class="spacer" />
            <span class="dim tiny">已采集 {{ frames }} 帧 · 送检 {{ sent }} 次</span>
          </div>
        </div>
      </div>

      <!-- 右栏：两栏面板按剩余高度均分，内部转写与事件流各自滚动 -->
      <div class="stack fill">
        <div class="panel fade-up d1 panel-fill">
          <div class="panel-hd">
            <span class="panel-title">语音识别与关键词</span>
            <div class="spacer" />
            <span class="panel-sub mono">{{ chats.length }}</span>
          </div>
          <div class="panel-bd">
            <div v-if="!chats.length && !partialText" class="empty" style="padding: 30px 10px">
              <Icon name="mic" />
              <div class="t">开启语音监听后显示转写内容</div>
            </div>
            <div v-else class="stream">
              <div v-if="partialText" class="bubble bubble--live">
                <div class="txt">{{ partialText }}</div>
                <div class="meta"><span class="mono">实时转写中…</span></div>
              </div>
              <div v-for="(m, i) in chats" :key="i" class="bubble" :class="{ hit: m.hits.length }">
                <div class="txt">{{ m.text }}</div>
                <div class="meta">
                  <span class="mono">{{ clock(m.t) }}</span>
                  <span v-if="m.hits.length" class="kw">命中：{{ m.hits.join('、') }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="panel fade-up d2 panel-fill">
          <div class="panel-hd">
            <span class="panel-title">实时事件流</span>
            <div class="spacer" />
            <span class="panel-sub mono">{{ events.length }}</span>
          </div>
          <div class="panel-bd">
            <div v-if="!events.length" class="empty" style="padding: 30px 10px">
              <Icon name="pulse" />
              <div class="t">暂无异常事件</div>
            </div>
            <div v-else class="feed">
              <div v-for="(e, i) in events" :key="i" class="feed-row">
                <span class="tag" :class="`tag--${e.lv}`">{{ e.label }}</span>
                <span class="mono tiny muted">{{ Math.round(e.confidence * 100) }}%</span>
                <span v-if="e.track_ids && e.track_ids.length" class="tiny dim mono">#{{ e.track_ids.join(' #') }}</span>
                <span class="t">{{ clock(e.t) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import Icon from '@/ui/Icon.vue'
import BaseSelect from '@/ui/BaseSelect.vue'
import { toast } from '@/ui/toast'
import { setCamera, setMic } from '@/stores/device'
import api, { wsUrl, wsProtocols } from '@/api'

const cameras = ref([])
const cameraId = ref('')
const route = useRoute()
const videoEl = ref(null)
const canvasEl = ref(null)
const running = ref(false)
const audioOn = ref(false)
const boxes = ref([])
const behaviors = ref([])
const faces = ref([])
const people = ref(0)
const inferMs = ref(0)
const skipped = ref(false)
const skipReason = ref('')
const motion = ref(0)
const wsState = ref('idle')  // idle / connecting / open / closed / error
const stageRatio = ref('16 / 9')
/** 宽高比数值形式，供舞台按容器的 cqh 反推最大可用宽度，宽高同时受限时也不变形 */
const arNum = computed(() => {
  const m = String(stageRatio.value).split('/').map((s) => Number(s.trim()))
  return m[0] && m[1] ? m[0] / m[1] : 16 / 9
})
const chats = reactive([])
const events = reactive([])
const partialText = ref('')
const alarm = ref(null)
const fps = ref(0)
const frames = ref(0)
const sent = ref(0)

let wsDetect = null
let wsAudio = null
let audioCtx = null
let audioStream = null
let audioSrcNode = null
let workletNode = null
let stream = null
let rafId = null
let lastTick = 0
let frameCount = 0
// 检测/语音通道的自动重连：只记录次数用于退避，定时器句柄用于停止时取消
let detectRetry = 0
let detectRetryTimer = null
let audioRetryTimer = null

const camOptions = computed(() => cameras.value.map((c) => ({ value: c.id, label: `${c.name}（${c.location || '未设置位置'}）` })))
const lvOf = { fight: 'high', argue: 'high', fall: 'medium', smoke: 'low', crowd: 'low' }
const clock = (t) => new Date(t).toLocaleTimeString('zh-CN', { hour12: false })

// 跳过推理原因中文映射
const SKIP_TEXT = { no_motion: '画面静止', rate_limited: '限频' }
const skipReasonText = computed(() => SKIP_TEXT[skipReason.value] || skipReason.value || '未知')
const motionText = computed(() => (Number.isFinite(motion.value) ? motion.value.toFixed(2) : '—'))

/** 顶栏状态胶囊：把"检测通道没连上"明确暴露出来，避免静默无框 */
const chipText = computed(() => {
  if (!running.value) return '待机'
  if (wsState.value === 'open') return '采集中'
  if (wsState.value === 'connecting') return '连接检测通道…'
  return '检测通道未连接'
})

// 各事件类型对应的框/骨架颜色（person 为青色，异常行为用对应色）
const TYPE_COLOR = { person: '#2fd6f0', fight: '#ff4d6d', argue: '#ff8a3d', fall: '#ffb020', smoke: '#d7c341', crowd: '#9b7bff' }

// COCO-17 骨架连线
const SKELETON = [
  [0, 1], [0, 2],            // 鼻-左眼 / 鼻-右眼
  [3, 5], [4, 6],            // 左耳-左肩 / 右耳-右肩
  [5, 7], [7, 9], [6, 8], [8, 10], // 左肩-左肘-左腕 / 右肩-右肘-右腕
  [5, 11], [6, 12],          // 左肩-左髋 / 右肩-右髋
  [11, 13], [13, 15], [12, 14], [14, 16], // 左髋-左膝-左踝 / 右髋-右膝-右踝
  [5, 6], [11, 12]           // 左肩-右肩 / 左髋-右髋
]

// 关键点有效性：置信度达标、且不是 (0,0) 占位坐标
// （模型对"不可见"的关键点会给 (0,0) 但置信度仍有 0.3+，只按置信度过滤会让骨架飞到左上角）
const validKpt = (k) => !!k && k[2] >= 0.3 && !(k[0] <= 0 && k[1] <= 0)

/* ---------------- 检测框 / 骨架的平滑跟随 ----------------
 * 推理只有 4~12 fps，检测结果本身还有像素级抖动。若按结果直接摆放，框会一格一格跳。
 * 这里给每条轨迹维护一份"显示值"，在 60fps 的 rAF 里以指数缓动逼近最新检测结果：
 *   - 补出两次推理之间的连续运动 → 跟着人走，丝滑
 *   - 指数平滑天然滤掉单帧抖动   → 防抖
 *   - 偶发漏检时保留一小段时间再移除 → 框不会闪
 * 关键点用同一条时间常数一起缓动，骨架才不会一格一格跳。
 */
const SMOOTH_TAU = 0.1   // 时间常数（秒）：越小跟得越紧，越大越平滑
const SMOOTH_TTL = 0.6   // 目标消失后继续显示多久（秒），抑制漏检闪烁

const smoothMap = reactive(new Map())   // key -> {x1,y1,x2,y2,kpts,ok,type,label,conf,trackId,seen}
let overlayRaf = null
let overlayLast = 0

/** 本次推理的目标位置（也是标签/类型的来源） */
const targets = computed(() => {
  const behs = behaviors.value || []
  return (boxes.value || []).map((b) => {
    const beh = behs.find((h) => Array.isArray(h.track_ids) && h.track_ids.includes(b.track_id))
    return {
      key: `t${b.track_id}`,
      trackId: b.track_id,
      type: beh ? beh.event_type : 'person',
      label: beh ? beh.label : b.label,
      conf: beh ? beh.confidence : b.confidence,
      bbox: b.bbox,
      kpts: b.kpts
    }
  })
})

/** 实际渲染用的框（平滑后的位置 + 最新一次结果的标签） */
const renderBoxes = computed(() => Array.from(smoothMap.entries()).map(([key, v]) => ({ key, ...v })))

function stepOverlay(now) {
  overlayRaf = requestAnimationFrame(stepOverlay)
  const dt = overlayLast ? Math.min(0.1, (now - overlayLast) / 1000) : 1 / 60
  overlayLast = now
  // 指数缓动系数：与帧间隔无关，掉帧时也不会突然追赶
  const alpha = 1 - Math.exp(-dt / SMOOTH_TAU)
  const nowSec = now / 1000

  for (const t of targets.value) {
    const cur = smoothMap.get(t.key)
    if (!cur) {
      smoothMap.set(t.key, {
        trackId: t.trackId, type: t.type, label: t.label, conf: t.conf,
        x1: t.bbox[0], y1: t.bbox[1], x2: t.bbox[2], y2: t.bbox[3],
        kpts: t.kpts ? t.kpts.map((k) => [k[0], k[1], k[2]]) : null,
        ok: t.kpts ? t.kpts.map((k) => validKpt(k)) : null,
        seen: nowSec
      })
      continue
    }
    cur.x1 += (t.bbox[0] - cur.x1) * alpha
    cur.y1 += (t.bbox[1] - cur.y1) * alpha
    cur.x2 += (t.bbox[2] - cur.x2) * alpha
    cur.y2 += (t.bbox[3] - cur.y2) * alpha

    if (t.kpts && cur.kpts && cur.kpts.length === t.kpts.length && cur.ok) {
      for (let i = 0; i < t.kpts.length; i++) {
        const tk = t.kpts[i]
        const ck = cur.kpts[i]
        const good = validKpt(tk)
        if (good && !cur.ok[i]) {
          // 该关键点首次（或重新）变得可信：直接落位。
          // 这里绝不能缓动 —— 失效点的坐标可能还停在 (0,0) 占位处，
          // 缓动就会从左上角一路飞回人身上，正是"飞线"的来源。
          ck[0] = tk[0]
          ck[1] = tk[1]
          cur.ok[i] = true
        } else if (good) {
          ck[0] += (tk[0] - ck[0]) * alpha
          ck[1] += (tk[1] - ck[1]) * alpha
        } else {
          // 目标点不可信：只标失效，坐标原地保留，供恢复时无缝续上
          cur.ok[i] = false
        }
        ck[2] = good ? tk[2] : 0
      }
    } else {
      cur.kpts = t.kpts ? t.kpts.map((k) => [k[0], k[1], k[2]]) : null
      cur.ok = t.kpts ? t.kpts.map((k) => validKpt(k)) : null
    }
    cur.type = t.type
    cur.label = t.label
    cur.conf = t.conf
    cur.seen = nowSec
  }

  for (const [key, v] of smoothMap) {
    if (nowSec - v.seen > SMOOTH_TTL) smoothMap.delete(key)
  }
  drawSkeletons()
}

function startOverlayLoop() {
  if (overlayRaf) return
  overlayLast = 0
  overlayRaf = requestAnimationFrame(stepOverlay)
}

function stopOverlayLoop() {
  if (overlayRaf) cancelAnimationFrame(overlayRaf)
  overlayRaf = null
  overlayLast = 0
  smoothMap.clear()
}

// 框样式：位置由平滑后的值给出，key 用轨迹 id，保证同一个框始终跟着同一个人
function boxStyle(bbox) {
  if (!bbox || bbox.length < 4) return { display: 'none' }
  const [x1, y1, x2, y2] = bbox
  // 画面做了水平镜像（视频与骨架画布），框要同步镜像 x 才能继续贴在人身上；
  // 镜像放在这里用数值算，而不是套 transform，否则框里的标签文字会跟着反掉。
  return { left: `${(1 - x2) * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%` }
}

async function loadCameras() {
  try { cameras.value = await api.get('/api/cameras') } catch { /* 静默 */ }
}

/** 应用深链参数 /realtime?camera=<id>（点位态势墙等入口会带该参数跳转过来）。
 *
 * 之前该参数被完全忽略，从态势墙点进来永远是"未关联点位"，报警也就无法归属到点位。
 * 这里做一次存在性校验，避免 URL 里带了已删除的点位 id。
 */
function applyRouteCamera() {
  const raw = route.query.camera
  if (raw === undefined || raw === null || raw === '') return
  const id = Number(raw)
  if (Number.isFinite(id) && cameras.value.some((c) => c.id === id)) cameraId.value = id
}

async function start() {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720 } })
  } catch {
    toast.err('无法访问摄像头，请检查浏览器权限')
    setCamera(false)
    return
  }
  videoEl.value.srcObject = stream
  running.value = true
  setCamera(true)
  connectDetect()
  loop()
  startOverlayLoop()
  toast.ok('摄像头已开启，正在实时检测')
}

// 读取视频实际宽高比写入舞台，并同步骨架 canvas 固有像素尺寸
function onVideoMeta() {
  const v = videoEl.value
  if (!v || !v.videoWidth || !v.videoHeight) return
  stageRatio.value = `${v.videoWidth} / ${v.videoHeight}`
  const cv = canvasEl.value
  if (cv) {
    cv.width = v.videoWidth
    cv.height = v.videoHeight
  }
}

function loop() {
  rafId = requestAnimationFrame(loop)
  frameCount++
  frames.value++
  const now = performance.now()
  if (now - lastTick >= 1000) {
    fps.value = frameCount
    frameCount = 0
    lastTick = now
  }
  if (frames.value % 15 === 0 && wsDetect?.readyState === 1) sendFrame()
}

function sendFrame() {
  const v = videoEl.value
  if (!v || !v.videoWidth) return
  const c = document.createElement('canvas')
  c.width = 640
  c.height = Math.round((640 * v.videoHeight) / v.videoWidth)
  c.getContext('2d').drawImage(v, 0, 0, c.width, c.height)
  wsDetect.send(JSON.stringify({ camera_id: cameraId.value || null, image_b64: c.toDataURL('image/jpeg', 0.7) }))
  sent.value++
}

function connectDetect() {
  if (detectRetryTimer) { clearTimeout(detectRetryTimer); detectRetryTimer = null }
  if (wsDetect) { try { wsDetect.close() } catch { /* noop */ } }
  wsState.value = 'connecting'

  // 用局部变量持有本次连接，回调里统一做"sock === wsDetect"判等。
  // 重连时旧 socket 的 close 事件会在新连接建立之后才派发，若不判等，
  // 旧回调会把 wsDetect 清成 null → sendFrame() 的 readyState 检查永远不成立 →
  // 界面显示"采集中"却一个框都没有（这正是之前"重连后检测假死"的根因）。
  const sock = new WebSocket(wsUrl('/api/ws/detect'), wsProtocols())
  wsDetect = sock

  sock.onopen = () => {
    if (wsDetect !== sock) return
    detectRetry = 0
    wsState.value = 'open'
  }
  sock.onmessage = (ev) => {
    if (wsDetect !== sock) return   // 旧连接的迟到消息直接丢弃，避免污染当前画面
    let d
    try { d = JSON.parse(ev.data) } catch { return }
    if (d.error) return
    boxes.value = d.boxes || []
    behaviors.value = d.behaviors || []
    faces.value = d.faces || []
    people.value = d.people ?? 0
    inferMs.value = Number(d.infer_ms) || 0
    skipped.value = !!d.skipped
    skipReason.value = d.skip_reason || ''
    motion.value = d.motion ?? 0
    for (const h of behaviors.value) {
      if (['fight', 'argue', 'fall', 'smoke', 'crowd'].includes(h.event_type)) {
        events.unshift({ ...h, lv: lvOf[h.event_type] || 'low', t: Date.now() })
        if (events.length > 60) events.pop()
      }
    }
    if (d.alarm) alarm.value = d.alarm
  }
  sock.onclose = () => {
    if (wsDetect !== sock) return
    wsDetect = null
    if (wsState.value === 'idle') return
    wsState.value = 'closed'
    // 摄像头仍在采集时自动重连（1s→2s→4s→8s 封顶），无需用户手动点"重连检测通道"。
    // 关闭摄像头（stopAll）会把 wsState 置为 idle，从而终止重连。
    if (running.value) {
      const delay = Math.min(8000, 1000 * 2 ** detectRetry)
      detectRetry += 1
      detectRetryTimer = setTimeout(() => { if (running.value) connectDetect() }, delay)
    }
  }
  sock.onerror = () => {
    if (wsDetect === sock && wsState.value === 'connecting') wsState.value = 'error'
  }
}

/* ---------------- 骨架绘制 ---------------- */
function drawSkeletons() {
  const cv = canvasEl.value
  const v = videoEl.value
  if (!cv) return
  // canvas 宽高按视频实际像素尺寸设置，再由 CSS 100% 铺满适配 object-fit: cover
  if (v && v.videoWidth) {
    if (cv.width !== v.videoWidth) cv.width = v.videoWidth
    if (cv.height !== v.videoHeight) cv.height = v.videoHeight
  }
  const ctx = cv.getContext('2d')
  const w = cv.width
  const h = cv.height
  ctx.clearRect(0, 0, w, h)
  if (!w || !h) return
  const dotR = Math.max(2, w / 300)
  const lineW = Math.max(2, w / 480)
  for (const p of renderBoxes.value) {
    if (!p.kpts) continue
    const color = TYPE_COLOR[p.type] || TYPE_COLOR.person
    ctx.strokeStyle = color
    ctx.fillStyle = color
    ctx.lineWidth = lineW
    ctx.lineCap = 'round'
    // 骨架连线：任一关键点无效则跳过该连线
    for (const [a, b] of SKELETON) {
      const ka = p.kpts[a]
      const kb = p.kpts[b]
      if (!validKpt(ka) || !validKpt(kb)) continue
      ctx.beginPath()
      ctx.moveTo(ka[0] * w, ka[1] * h)
      ctx.lineTo(kb[0] * w, kb[1] * h)
      ctx.stroke()
    }
    // 关键点
    for (const k of p.kpts) {
      if (!validKpt(k)) continue
      ctx.beginPath()
      ctx.arc(k[0] * w, k[1] * h, dotR, 0, Math.PI * 2)
      ctx.fill()
    }
  }
}

function clearSkeleton() {
  const cv = canvasEl.value
  if (cv) cv.getContext('2d').clearRect(0, 0, cv.width, cv.height)
}

/* ---------------- 语音：真实音频流 ---------------- */
function toggleAudio(on) {
  audioOn.value = on
  if (on) startAudio()
  else stopAudio()
}

async function startAudio() {
  try {
    audioStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }
    })
  } catch {
    toast.err('无法访问麦克风，请检查浏览器权限')
    audioOn.value = false
    setMic(false)
    return
  }

  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)()
    // Vite 下必须用 new URL(..., import.meta.url) 才能正确打包 worklet
    await audioCtx.audioWorklet.addModule(new URL('../audio/pcm-worklet.js', import.meta.url))
  } catch {
    toast.err('音频处理器加载失败，请更新浏览器后重试')
    cleanupAudio()
    audioOn.value = false
    setMic(false)
    return
  }

  audioSrcNode = audioCtx.createMediaStreamSource(audioStream)
  workletNode = new AudioWorkletNode(audioCtx, 'pcm-16k', {
    processorOptions: { inputSampleRate: audioCtx.sampleRate }
  })
  // 仅把音频送入 worklet 做重采样，不连到 destination，避免啸叫
  audioSrcNode.connect(workletNode)
  workletNode.port.onmessage = (ev) => {
    if (wsAudio?.readyState === 1) wsAudio.send(ev.data)
  }

  connectAudioWs()
  setMic(true)
  toast.ok('语音监听已开启，正在实时转写')
}

function connectAudioWs() {
  if (audioRetryTimer) { clearTimeout(audioRetryTimer); audioRetryTimer = null }
  if (wsAudio) { try { wsAudio.close() } catch { /* noop */ } }
  const sock = new WebSocket(wsUrl('/api/ws/audio'), wsProtocols())
  wsAudio = sock
  sock.binaryType = 'arraybuffer'
  sock.onopen = () => { if (wsAudio === sock) sendCameraCtl() }
  sock.onmessage = (ev) => {
    if (wsAudio !== sock) return
    let d
    try { d = JSON.parse(ev.data) } catch { return }
    if (d.type === 'error') {
      toast.err(d.detail ? `语音识别不可用：${d.detail}` : '语音识别服务不可用')
      stopAudio()
      return
    }
    if (d.type === 'partial') {
      partialText.value = d.text || ''
      return
    }
    if (d.type === 'final') {
      partialText.value = ''
      if (d.text) {
        chats.unshift({ text: d.text, hits: d.keywords || [], t: Date.now() })
        if (chats.length > 100) chats.pop()
      }
      if (d.alarm) alarm.value = d.alarm
    }
  }
  sock.onclose = () => {
    if (wsAudio !== sock) return
    wsAudio = null
    // 语音通道是被动长连接，网络抖动 / 后端重启 / 代理空闲超时都会把它断开。
    // 原来只把引用置空：界面仍显示"语音监听中"，麦克风数据却再也发不出去，且没有任何提示，
    // 属于典型的静默失效。这里在监听仍然开启时自动重连并给出提示。
    if (!audioOn.value) return
    toast.warn('语音通道已断开，正在重连…')
    audioRetryTimer = setTimeout(() => { if (audioOn.value) connectAudioWs() }, 1000)
  }
}

// 切换关联点位时，若音频连接已建立则下发控制帧
function sendCameraCtl() {
  if (wsAudio?.readyState === 1) wsAudio.send(JSON.stringify({ camera_id: cameraId.value || null }))
}

function cleanupAudio() {
  if (audioRetryTimer) { clearTimeout(audioRetryTimer); audioRetryTimer = null }
  if (workletNode) {
    workletNode.port.onmessage = null
    try { workletNode.disconnect() } catch { /* noop */ }
    workletNode = null
  }
  if (audioSrcNode) {
    try { audioSrcNode.disconnect() } catch { /* noop */ }
    audioSrcNode = null
  }
  if (audioStream) { audioStream.getTracks().forEach((t) => t.stop()); audioStream = null }
  if (audioCtx) { try { audioCtx.close() } catch { /* noop */ } audioCtx = null }
  if (wsAudio) { try { wsAudio.close() } catch { /* noop */ } wsAudio = null }
  partialText.value = ''
}

function stopAudio() {
  cleanupAudio()
  audioOn.value = false
  setMic(false)
}

watch(cameraId, () => { if (audioOn.value) sendCameraCtl() })
// 已在实时页时从态势墙再次点击某点位，URL 变化也要同步选中项
watch(() => route.query.camera, applyRouteCamera)

/** 停止采集并复位所有状态。
 *
 * ``notify=false`` 用于组件卸载（用户切到别的页面）：
 * 此时弹「已停止检测」既无意义又会被下一个页面看到，属于典型的误导性提示。
 */
function stopAll(notify = true) {
  running.value = false
  setCamera(false)
  if (rafId) cancelAnimationFrame(rafId)
  rafId = null
  stopOverlayLoop()
  stopAudio()
  // 先取消待执行的重连，再置 idle，否则退避中的那次重连会在停止后把通道又拉起来
  if (detectRetryTimer) { clearTimeout(detectRetryTimer); detectRetryTimer = null }
  detectRetry = 0
  if (wsDetect) { try { wsDetect.close() } catch { /* noop */ } wsDetect = null }
  wsState.value = 'idle'
  if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null }
  boxes.value = []
  behaviors.value = []
  faces.value = []
  people.value = 0
  inferMs.value = 0
  skipped.value = false
  skipReason.value = ''
  motion.value = 0
  stageRatio.value = '16 / 9'
  clearSkeleton()
  frames.value = 0
  sent.value = 0
  if (notify) toast.info('已停止检测')
}

loadCameras().then(applyRouteCamera)
onUnmounted(() => stopAll(false))
</script>

<style scoped>
/* 舞台容器吃掉面板剩余高度；作为尺寸查询容器，让视频能按容器高度反推最大可用宽度，
   因此宽高同时受限时按比例取最大尺寸，既撑满空间又不变形（比例与骨架叠加层一致）。 */
.stage-wrap {
  flex: 1;
  min-height: 0;
  display: grid;
  place-items: center;
  container-type: size;
}
.stage-wrap > .stage-video { width: min(100%, calc(100cqh * var(--ar, 1.7778))); }

/* 水平翻转摄像头画面（前置视角更自然）。
   只翻视频与骨架 canvas 这两个"像素层"；检测框是 DOM，若一起翻会让框内标签文字反掉，
   所以框的 x 坐标改在 boxStyle() 里用数值镜像。HUD / 角标 / 提示同样不参与镜像。 */
.stage-video video,
.stage-video .skeleton {
  transform: scaleX(-1);
}

/* 转写与事件流在面板剩余高度内滚动（原为固定 320px 上限，浪费了下半屏） */
.stream, .feed { flex: 1; min-height: 0; max-height: none; }

/* 窄屏恢复自然高度与固定上限，避免被压扁 */
@media (max-width: 900px) {
  .stage-wrap { flex: none; container-type: normal; }
  .stage-wrap > .stage-video { width: 100%; }
  .stream, .feed { flex: none; max-height: 320px; }
}
</style>
