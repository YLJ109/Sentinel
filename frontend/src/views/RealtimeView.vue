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
        <!-- 点位选择与启停按钮并进标题栏：原先它们单独占 .panel-bd 里的第一行，
             视觉上等于"标题栏 + 控制行"两个盒子，白白吃掉舞台的高度。
             并进来后整个面板只剩一个头部盒子，舞台也更高。
             注意 BaseSelect 要显式 flex-shrink: 0 —— 它是 div 不是 .btn，
             不锁住会在空间不足时被压扁，而中间的 .panel-sub 会先省略。 -->
        <div class="panel-hd">
          <span class="panel-title">视觉行为检测</span>
          <BaseSelect
            v-model="cameraId"
            :options="camOptions"
            placeholder="未关联点位"
            icon="camera"
            clearable
            style="width: 200px; flex-shrink: 0"
          />
          <div class="spacer" />
          <span class="panel-sub mono">{{ inferMs ? `${inferMs.toFixed(1)} ms` : '— ms' }} · {{ people }} 人 · 人脸 {{ faces.length }}<span v-if="skipped"> · 跳过推理（{{ skipReasonText }}）</span> · 运动 {{ motionText }}</span>
          <span class="status-chip" :class="{ warn: !running || wsState !== 'open' }">
            <span class="dot" />{{ chipText }}
          </span>
          <button v-if="running && wsState !== 'open'" class="btn btn--xs" :disabled="wsState === 'connecting'" @click="connectDetect">
            <Icon name="refresh" /> {{ wsState === 'connecting' ? '连接中…' : '重连' }}
          </button>
          <button v-if="!running" class="btn btn--sm btn--primary" @click="start">
            <Icon name="video" /> 开启摄像头
          </button>
          <button v-else class="btn btn--sm btn--danger" @click="stopAll"><Icon name="close" /> 停止检测</button>
        </div>
        <div class="panel-bd">
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
              <!-- 人脸检测叠加层：已识别时直接打姓名，未识别只标"人脸" -->
              <div v-for="(f, i) in faces" :key="`f${i}`" class="bbox face" :style="boxStyle(f.bbox)">
                <span class="lb">
                  <template v-if="f.person">{{ f.person.name }}<template v-if="f.person.class_name"> · {{ f.person.class_name }}</template></template>
                  <template v-else>人脸{{ f.confidence ? ` ${Math.round(f.confidence * 100)}%` : '' }}</template>
                  <em v-if="f.emotion" class="lb-emo" :title="EMOTION_TIP">{{ f.emotion.icon }} {{ f.emotion.label }}</em>
                </span>
              </div>
              <div v-for="b in renderBoxes" :key="b.key" class="bbox" :class="b.type" :style="boxStyle([b.x1, b.y1, b.x2, b.y2])">
                <span class="lb">
                  <template v-if="b.person">
                    <b class="lb-name">{{ b.person.name }}</b><template v-if="b.person.class_name"> · {{ b.person.class_name }}</template>
                  </template>
                  <template v-else>#{{ b.trackId }}</template>
                  {{ b.label }} {{ Math.round(b.conf * 100) }}%
                  <em v-if="b.emotion" class="lb-emo" :title="EMOTION_TIP">{{ b.emotion.icon }} {{ b.emotion.label }}</em>
                </span>
              </div>
              <span class="corner c-tl" /><span class="corner c-tr" /><span class="corner c-bl" /><span class="corner c-br" />
              <div v-if="running" class="hud">REC · {{ fps }} FPS · {{ frames }} FRAMES</div>
              <!-- 人数徽标：与后端迟滞平滑后的人数一致，投屏时一眼可读 -->
              <div v-if="running" class="pcount" :class="{ live: people > 0 }">
                <b>{{ people }}</b><span>人在场</span>
              </div>
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

          <!-- 判定依据：把模型给出的量化指标摊开，而不是只抛一个黑盒结论。
               值班老师能看到"为什么判成欺凌"（运动不对称度、退缩、追逃、压制姿态），
               既方便人工复核，也是答辩时最有说服力的一屏。 -->
          <div v-if="explain" class="explain">
            <div class="explain-hd">
              <span class="tag" :class="`tag--${lvOf[explain.type] || 'low'}`">{{ explain.label }}</span>
              <span class="dim tiny">置信度 {{ Math.round(explain.conf * 100) }}%</span>
              <div class="spacer" />
              <span class="dim tiny">判定依据</span>
            </div>
            <div class="explain-rows">
              <div v-for="r in explain.rows" :key="r.k" class="explain-row">
                <span class="k">{{ r.k }}</span>
                <span class="bar"><i :style="{ width: r.pct + '%', background: r.color }" /></span>
                <span class="v mono">{{ r.v }}</span>
              </div>
            </div>
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
              <div v-if="partialText" class="bubble bubble--live" :class="lvClass(partialHits)">
                <div class="txt"><span v-for="(s, k) in markSegments(partialText, partialHits)" :key="k" :class="s.lv ? 'mark mark--' + s.lv : ''">{{ s.t }}</span></div>
                <div class="meta">
                  <span class="mono">实时转写中…</span>
                  <span v-if="partialHits.length" class="kw" :class="'kw--' + topLevel(partialHits)">{{ levelLabel(topLevel(partialHits)) }}</span>
                </div>
              </div>
              <div v-for="(m, i) in chats" :key="i" class="bubble" :class="lvClass(m.hits)">
                <div class="txt"><span v-for="(s, k) in markSegments(m.text, m.hits)" :key="k" :class="s.lv ? 'mark mark--' + s.lv : ''">{{ s.t }}</span></div>
                <div class="meta">
                  <span class="mono">{{ clock(m.t) }}</span>
                  <span v-if="m.hits.length" class="kw" :class="'kw--' + topLevel(m.hits)">
                    {{ levelLabel(topLevel(m.hits)) }}：{{ hitText(m.hits) }}
                  </span>
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
              <div v-for="(e, i) in events" :key="i" class="ecard" :class="`ecard--${e.lv}`">
                <div class="ecard-ava">
                  <img v-if="e.primary && e.primary.avatar_url" :src="e.primary.avatar_url" :alt="e.primary.name" loading="lazy" />
                  <Icon v-else name="user" />
                </div>
                <div class="ecard-main">
                  <div class="ecard-hd">
                    <span class="tag" :class="`tag--${e.lv}`">{{ e.label }}</span>
                    <b v-if="e.primary" class="ecard-name">{{ e.primary.name }}</b>
                    <span v-else class="ecard-unknown">未识别人员</span>
                    <div class="spacer" />
                    <span class="mono tiny dim">{{ clock(e.t) }}</span>
                  </div>
                  <div class="ecard-meta">
                    <template v-if="e.primary">
                      <span>{{ e.primary.type_label }}</span>
                      <span v-if="e.primary.gender">{{ e.primary.gender }}</span>
                      <span>{{ e.primary.class_name || e.primary.department || '—' }}</span>
                      <span class="mono">编号 {{ e.primary.no }}</span>
                    </template>
                    <span v-else class="dim">未授权或未建档，仅显示行为线索（不影响报警）</span>
                  </div>
                  <div class="ecard-foot">
                    <span v-if="e.emotion" class="emo-chip" :class="{ neg: e.emotion.negative }" :title="EMOTION_TIP">
                      {{ e.emotion.icon }} {{ e.emotion.label }}
                      <i>{{ Math.round(e.emotion.confidence * 100) }}%</i>
                    </span>
                    <span class="mono tiny dim">置信 {{ Math.round(e.confidence * 100) }}%</span>
                    <span v-if="e.track_ids && e.track_ids.length" class="tiny dim mono">#{{ e.track_ids.join(' #') }}</span>
                    <div class="spacer" />
                    <button class="btn btn--xs btn--ghost" @click="goAlarms()">
                      <Icon name="bell" /> 处置
                    </button>
                  </div>
                </div>
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
import { useRoute, useRouter } from 'vue-router'
import Icon from '@/ui/Icon.vue'
import BaseSelect from '@/ui/BaseSelect.vue'
import { toast } from '@/ui/toast'
import { setCamera, setMic } from '@/stores/device'
import api, { wsUrl, wsProtocols } from '@/api'

const route = useRoute()
const router = useRouter()

/** 情绪说明：面部表情不等于真实情绪，必须让使用者知道这个边界 */
const EMOTION_TIP = '基于面部表情的实时估计，仅供参考，非心理诊断；不写入学生档案'

function goAlarms() {
  if (route.path !== '/alarms') router.push('/alarms')
}

const cameras = ref([])
const cameraId = ref('')
const videoEl = ref(null)
const canvasEl = ref(null)
const running = ref(false)
const audioOn = ref(false)
const boxes = ref([])
const behaviors = ref([])
// 每收到一批新检测结果自增：平滑层据此区分「新结果」与「同一结果的重复渲染」，
// 只在真正拿到新数据时更新速度估计
const detectSeq = ref(0)
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
const partialHits = ref([])
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
const lvOf = { bullying: 'high', fight: 'high', argue: 'high', fall: 'medium', smoke: 'low', crowd: 'low' }
const clock = (t) => new Date(t).toLocaleTimeString('zh-CN', { hour12: false })

// ---------------------------------------------------------------- 关键词三档高亮
// 词表扩到几千条后，满屏同色高亮等于没有分级。这里按后端的 level 分三档着色：
//   alarm 红（触发报警）/ warn 橙（警告提示）/ highlight 紫（仅复核线索）
const LV_RANK = { highlight: 0, warn: 1, alarm: 2 }
const LV_NAME = { alarm: '报警', warn: '警告', highlight: '关注' }

function topLevel(hits) {
  let top = null
  for (const h of hits || []) {
    if (top === null || (LV_RANK[h.lv] ?? 0) > (LV_RANK[top] ?? 0)) top = h.lv
  }
  return top
}
const lvClass = (hits) => (topLevel(hits) ? `lv-${topLevel(hits)}` : '')
const levelLabel = (lv) => LV_NAME[lv] || ''
const hitText = (hits) => (hits || []).map((h) => (h.d ? `${h.w}(疑似)` : h.w)).join('、')

/**
 * 把转写文本切成「普通片段 / 命中片段」，供模板做局部高亮。
 * 注意后端返回的是**归一化文本中的匹配串**（已去标点并转小写），
 * 因此这里用不区分大小写的字面查找还原到原文位置；模糊命中的词在原文里
 * 通常并不字面存在，此时不做局部高亮，只在下方命中行标注"疑似"。
 */
function markSegments(text, hits) {
  const src = text || ''
  if (!src || !hits?.length) return [{ t: src, lv: null }]
  const low = src.toLowerCase()
  const marks = []
  for (const h of hits) {
    const w = (h.w || '').toLowerCase()
    if (!w) continue
    for (let from = 0; ;) {
      const i = low.indexOf(w, from)
      if (i < 0) break
      marks.push({ s: i, e: i + w.length, lv: h.lv })
      from = i + w.length
    }
  }
  if (!marks.length) return [{ t: src, lv: null }]
  // 长词优先：短词与长词重叠时保留长词，避免把「打死你」切碎成「打死」+「你」
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
// 欺凌用比打架更强烈的品红红：它是单向侵害，优先级最高
const TYPE_COLOR = { person: '#2fd6f0', bullying: '#ff1e56', fight: '#ff4d6d', argue: '#ff8a3d', fall: '#ffb020', smoke: '#d7c341', crowd: '#9b7bff' }

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

/* 送检周期：画面跑 30fps、送检跑 10fps（即"每 3 帧送 1 帧"）。
 * 必须定义在平滑层之前：平滑层要拿它当作"速度外推的时长上限"，
 * 而 const 存在暂时性死区，放在后面会在模块初始化时直接抛 ReferenceError。 */
const SEND_INTERVAL_MS = 100

/* ---------------- 检测框 / 骨架的平滑跟随（指数缓动 + 速度外推）----------------
 * 送检只有 10fps，而画面是 30fps 视频 + 60fps 的 rAF 平滑层 —— 两次检测之间目标一直在动。
 * 单纯"缓动追最新值"会让框始终落后真人约半个检测周期，快速走动时肉眼可见地拖尾。
 *
 * 这里在缓动之上叠加一层航位推算（dead reckoning）：
 *   1) 每收到一批新结果，用相邻两次实测中心的位移差估计目标速度，并做指数平滑；
 *   2) 每个渲染帧，把「目标位置」沿速度方向前推一段（封顶一个送检周期）。
 * 于是：检测结果负责纠偏，速度外推负责补出帧间运动 —— 框与骨架连续跟随真人。
 *
 * 三层保护缺一不可：
 *   - 指数缓动        → 滤掉单帧定位抖动（防抖）
 *   - 外推时长封顶    → 漏检时不会按旧速度一路飞出去
 *   - 关键点 ok[] 状态 → 失效点只标失效、坐标原地保留，恢复时直接落位（防"飞线"）
 */
const SMOOTH_TAU = 0.09     // 位置时间常数（秒）：越小越跟手，越大越平滑
const SMOOTH_TTL = 0.6      // 目标消失后继续显示多久（秒），抑制漏检闪烁
const VEL_ALPHA = 0.35      // 速度平滑系数：越小越稳、越大越跟手
const EXTRAP_MAX_SEC = SEND_INTERVAL_MS / 1000   // 外推时长上限 = 一个送检周期

// key -> {x1,y1,x2,y2, vx,vy, px1,py1,px2,py2, seq, detectAt, kpts, ok, type,label,conf,trackId,seen}
//   px1..py2 = 上一次的实测框。算速度必须用实测值，不能用被缓动过的显示值，否则会低估速度
//   detectAt = 最近一次收到结果的时刻（秒），当前外推时长由它推出
const smoothMap = reactive(new Map())
let overlayRaf = null
let overlayLast = 0

/** 本次推理的目标位置（也是标签/类型的来源） */
const targets = computed(() => {
  const behs = behaviors.value || []
  const seq = detectSeq.value
  return (boxes.value || []).map((b) => {
    const beh = behs.find((h) => Array.isArray(h.track_ids) && h.track_ids.includes(b.track_id))
    return {
      key: `t${b.track_id}`,
      trackId: b.track_id,
      seq,
      type: beh ? beh.event_type : 'person',
      label: beh ? beh.label : b.label,
      conf: beh ? beh.confidence : b.confidence,
      bbox: b.bbox,
      kpts: b.kpts,
      // 身份与情绪：由后端在"轨迹级确认"后才下发，未确认时为 null，
      // 前端据此渲染"未识别人员"而不是猜测身份
      person: b.person || null,
      emotion: b.emotion || null
    }
  })
})

/** 实际渲染用的框（平滑后的位置 + 最新一次结果的标签） */
const renderBoxes = computed(() => Array.from(smoothMap.entries()).map(([key, v]) => ({ key, ...v })))

/** 判定依据：把当前最可疑行为的量化指标摊开展示。
 *
 * 只给结论的"黑盒报警"在校园场景里没法用 —— 值班老师必须能自行判断这条报警
 * 是否可信。这里把判定实际用到的原始特征（互动对称性四项、手腕速度、距离比等）
 * 连同归一化后的强度条一并展示，让依据可见、可追问、可复核。
 */
const EXPLAIN_RANK = { bullying: 0, fight: 1, argue: 2, fall: 3, smoke: 4, crowd: 5 }
const explain = computed(() => {
  const list = behaviors.value || []
  if (!list.length) return null
  const hit = [...list].sort(
    (a, b) => (EXPLAIN_RANK[a.event_type] ?? 9) - (EXPLAIN_RANK[b.event_type] ?? 9)
  )[0]
  const d = hit && hit.detail
  if (!d) return null

  // [显示名, 字段, 满量程, 颜色]：满量程用于把数值映射成 0~100 的条长
  const SPEC = [
    ['运动不对称', 'asymmetry', 1, '#ff1e56'],
    ['退缩不对称', 'retreat', 1, '#ff1e56'],
    ['追逃模式', 'chase', 1, '#ff8a3d'],
    ['压制姿态', 'suppression', 1, '#ff8a3d'],
    ['欺凌累积分', 'bully_score', 1, '#2fd6f0'],
    ['手腕速度', 'wrist_speed', 1.5, '#ffb020'],
    ['双人距离比', 'dist_ratio', 2, '#9b7bff'],
    ['手-鼻距离', 'hand_head_ratio', 1.2, '#d7c341']
  ]
  const rows = []
  for (const [label, key, full, color] of SPEC) {
    if (d[key] === undefined || d[key] === null) continue
    const v = Number(d[key])
    if (!Number.isFinite(v)) continue
    rows.push({
      k: label,
      v: v.toFixed(2),
      pct: Math.min(100, Math.max(0, (v / full) * 100)),
      color
    })
  }
  if (!rows.length) return null
  return { type: hit.event_type, label: hit.label, conf: hit.confidence, rows }
})

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
      // 新目标：直接落位。px* 用实测值初始化，避免首帧算出一个巨大的假速度
      smoothMap.set(t.key, {
        trackId: t.trackId, type: t.type, label: t.label, conf: t.conf,
        person: t.person, emotion: t.emotion,
        x1: t.bbox[0], y1: t.bbox[1], x2: t.bbox[2], y2: t.bbox[3],
        vx: 0, vy: 0,
        px1: t.bbox[0], py1: t.bbox[1], px2: t.bbox[2], py2: t.bbox[3],
        seq: t.seq, detectAt: nowSec,
        kpts: t.kpts ? t.kpts.map((k) => [k[0], k[1], k[2]]) : null,
        ok: t.kpts ? t.kpts.map((k) => validKpt(k)) : null,
        seen: nowSec
      })
      continue
    }

    // ---- 拿到一批新检测结果：用相邻两次实测中心的位移估计速度并做指数平滑 ----
    if (t.seq !== cur.seq) {
      const dtDetect = Math.max(1e-3, nowSec - cur.detectAt)
      const dcx = (t.bbox[0] + t.bbox[2]) / 2 - (cur.px1 + cur.px2) / 2
      const dcy = (t.bbox[1] + t.bbox[3]) / 2 - (cur.py1 + cur.py2) / 2
      cur.vx += (dcx / dtDetect - cur.vx) * VEL_ALPHA
      cur.vy += (dcy / dtDetect - cur.vy) * VEL_ALPHA
      cur.px1 = t.bbox[0]; cur.py1 = t.bbox[1]
      cur.px2 = t.bbox[2]; cur.py2 = t.bbox[3]
      cur.seq = t.seq
      cur.detectAt = nowSec
    }

    // ---- 每个渲染帧：目标位置沿速度方向前推 ----
    // 封顶一个送检周期：漏检时不会按旧速度一路飞出去，同时也不会过冲到人的前方
    const ex = Math.min(EXTRAP_MAX_SEC, Math.max(0, nowSec - cur.detectAt))
    const ox = cur.vx * ex
    const oy = cur.vy * ex

    cur.x1 += (t.bbox[0] + ox - cur.x1) * alpha
    cur.y1 += (t.bbox[1] + oy - cur.y1) * alpha
    cur.x2 += (t.bbox[2] + ox - cur.x2) * alpha
    cur.y2 += (t.bbox[3] + oy - cur.y2) * alpha

    if (t.kpts && cur.kpts && cur.kpts.length === t.kpts.length && cur.ok) {
      for (let i = 0; i < t.kpts.length; i++) {
        const tk = t.kpts[i]
        const ck = cur.kpts[i]
        const good = validKpt(tk)
        if (good && !cur.ok[i]) {
          // 该关键点首次（或重新）变得可信：直接落位（含外推偏移）。
          // 这里绝不能缓动 —— 失效点的坐标可能还停在 (0,0) 占位处，
          // 缓动就会从左上角一路飞回人身上，正是"飞线"的来源。
          ck[0] = tk[0] + ox
          ck[1] = tk[1] + oy
          cur.ok[i] = true
        } else if (good) {
          // 骨架与框共用同一份外推偏移整体平移，两者才不会脱节
          ck[0] += (tk[0] + ox - ck[0]) * alpha
          ck[1] += (tk[1] + oy - ck[1]) * alpha
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
    // 身份只前进不后退：识别是间歇性执行的（每 N 次检测才跑一次），
    // 若每次未命中就把姓名清空，标签会以识别周期为频率疯狂闪烁。
    // 因此保留最近一次确认结果，只有后端明确给了新结果才更新。
    if (t.person) cur.person = t.person
    if (t.emotion) cur.emotion = t.emotion
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
    // 固定请求 30fps：画面节奏稳定，送检侧才能按「每 3 帧送 1 帧」得到恒定的 10fps
    stream = await navigator.mediaDevices.getUserMedia({
      video: { width: 1280, height: 720, frameRate: { ideal: 30 } }
    })
  } catch {
    toast.err('无法访问摄像头，请检查浏览器权限')
    setCamera(false)
    return
  }
  videoEl.value.srcObject = stream
  running.value = true
  setCamera(true)
  lastSendAt = 0
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

/* ---------------- 送检节流：画面 30fps、送检 10fps ----------------
 * 画面按摄像头出帧率（30fps）呈现，叠加 60fps 的 rAF 平滑层保证视觉连续；
 * 但送检（上传 + 推理）不必这么频繁 —— 人的动作在 10fps 已足够捕捉，
 * 而每送一帧都要付 drawImage + JPEG 编码 + base64 + WebSocket 传输的代价。
 *
 * 原实现按"每 15 个 rAF 回调送一次"计数，实际送检率会随显示器刷新率漂移
 * （60Hz 屏约 4fps，144Hz 屏可达 9.6fps），送检节奏不稳定、也无法解释给评委听。
 * 这里改为按时间片节流：送检帧率恒定，与刷新率彻底解耦。
 * 周期常量 SEND_INTERVAL_MS 定义在上方（平滑层的外推上限要用到它）。
 */
let lastSendAt = 0

// 复用同一块离屏 canvas：原先每次送帧都 createElement 新建画布再丢弃，
// 持续送帧下会造成稳定的 GC 压力
let grabCanvas = null
let grabCtx = null

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
  if (now - lastSendAt >= SEND_INTERVAL_MS && wsDetect?.readyState === 1) {
    lastSendAt = now
    sendFrame()
  }
}

function sendFrame() {
  const v = videoEl.value
  if (!v || !v.videoWidth) return
  if (!grabCanvas) {
    grabCanvas = document.createElement('canvas')
    grabCtx = grabCanvas.getContext('2d')
  }
  const w = 640
  const h = Math.round((640 * v.videoHeight) / v.videoWidth)
  if (grabCanvas.width !== w || grabCanvas.height !== h) {
    grabCanvas.width = w
    grabCanvas.height = h
  }
  grabCtx.drawImage(v, 0, 0, w, h)
  wsDetect.send(JSON.stringify({
    camera_id: cameraId.value || null,
    image_b64: grabCanvas.toDataURL('image/jpeg', 0.7)
  }))
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
    detectSeq.value++
    faces.value = d.faces || []
    people.value = d.people ?? 0
    inferMs.value = Number(d.infer_ms) || 0
    skipped.value = !!d.skipped
    skipReason.value = d.skip_reason || ''
    motion.value = d.motion ?? 0
    for (const h of behaviors.value) {
      if (['bullying', 'fight', 'argue', 'fall', 'smoke', 'crowd'].includes(h.event_type)) {
        // 把关联轨迹的身份与情绪一并带进事件卡片：
        // 值班老师最需要的不是"#12 与 #15 发生推搡"，而是"张三（初二3班）正在被推搡"。
        const byId = new Map((d.boxes || []).map((b) => [b.track_id, b]))
        const persons = (h.track_ids || []).map((id) => byId.get(id)?.person).filter(Boolean)
        const emotions = (h.track_ids || []).map((id) => byId.get(id)?.emotion).filter(Boolean)
        events.unshift({
          ...h,
          persons,
          primary: persons[0] || null,
          emotion: emotions.find((e) => e.negative) || emotions[0] || null,
          lv: lvOf[h.event_type] || 'low',
          t: Date.now()
        })
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
      partialHits.value = d.hits || []
      return
    }
    if (d.type === 'final') {
      partialText.value = ''
      partialHits.value = []
      if (d.text) {
        chats.unshift({ text: d.text, hits: d.hits || [], level: d.level || null, t: Date.now() })
        if (chats.length > 100) chats.pop()
        // 三档分级提示：alarm 才落到报警链路，warn 只做提示，highlight 仅高亮
        if (d.level === 'alarm') toast.err(`语音报警：命中「${hitText(d.hits)}」`)
        else if (d.level === 'warn') toast.warn(`语音警告：命中「${hitText(d.hits)}」`)
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
  partialHits.value = []
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
/* ---------- 判定依据面板 ----------
   把判定的量化特征摊开展示：值班老师据此判断报警是否可信，
   而不是面对一个无法追问的黑盒结论。 */
.explain {
  margin-top: 12px;
  padding: 11px 12px;
  border-radius: var(--r-md);
  background: var(--bg-inset);
  border: 1px solid var(--line-2);
}
.explain-hd { display: flex; align-items: center; gap: 9px; margin-bottom: 9px; }
.explain-rows { display: flex; flex-direction: column; gap: 6px; }
.explain-row { display: flex; align-items: center; gap: 9px; font-size: 11.5px; }
.explain-row .k { width: 72px; flex-shrink: 0; color: var(--tx-3); }
.explain-row .bar {
  flex: 1; min-width: 0; height: 5px; border-radius: 3px;
  background: rgba(122, 162, 220, 0.14); overflow: hidden;
}
.explain-row .bar i { display: block; height: 100%; border-radius: 3px; transition: width 0.18s; }
.explain-row .v { width: 36px; flex-shrink: 0; text-align: right; color: var(--tx-2); }

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
