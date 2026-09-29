<script setup>
/**
 * 底部状态栏：业务指标 + 采集设备 + 检测能力开关 + 推理设备切换与日志入口。
 *
 * 信息分层（从左到右）：
 *   1. 业务指标：待处置报警、今日事件（原来在顶栏左侧，现下移到这里）
 *   2. 采集设备：摄像头 / 麦克风（浏览器本地状态，后端无从得知）
 *   3. 检测能力：每项可点击开 / 关，关掉后引擎不再做对应计算，开关落库持久化
 *   4. 系统信息：推理设备（可切 CPU / CUDA）、运行时长、日志入口
 *
 * 去重原则：同一信息只出现一次——
 *   设备信息只保留这里（顶栏不再重复）、待处置数不再出现在导航徽标上、
 *   能力汇总不再单列（各能力状态灯本身就是汇总）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import Icon from '@/ui/Icon.vue'
import { toast } from '@/ui/toast'
import { deviceState } from '@/stores/device'
import { useAuthStore } from '@/stores/auth'
import api from '@/api'

const props = defineProps({
  status: { type: Object, default: null }
})
const emit = defineEmits(['open-logs', 'refresh'])
const router = useRouter()
const auth = useAuthStore()

/** 能力开关与推理设备切换都是写操作，后端仅放行 admin / operator。
 * viewer 是只读角色，这里直接置灰并给出原因，避免"点了必然弹 403"的挫败体验。 */
const canOperate = computed(() => auth.role === 'admin' || auth.role === 'operator')

// 实时时钟（原在顶栏，现移至状态栏，为顶栏两侧菱形导航腾出空间）
const now = ref(new Date())
let clockTimer = null
const clockTime = computed(() => now.value.toLocaleTimeString('zh-CN', { hour12: false }))
const clockDate = computed(() => now.value.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit', weekday: 'short' }))

const caps = computed(() => props.status?.capabilities || [])
const device = computed(() => props.status?.device || {})
const runtime = computed(() => props.status?.runtime || {})
const logs = computed(() => props.status?.logs || {})

const pending = computed(() => props.status?.alarms?.pending || 0)
const todayEvents = computed(() => props.status?.events?.today || 0)

// 视觉与行为检测能力合并展示；语音能力单独一组
const visionCaps = computed(() => caps.value.filter((c) => c.group !== 'audio'))
const audioCaps = computed(() => caps.value.filter((c) => c.group === 'audio'))

const errorCount = computed(() => logs.value.error_count || 0)
const warnCount = computed(() => logs.value.warning_count || 0)

/** 关闭=灰（不参与计算）；开启时就绪=绿、降级=黄、未就绪=灰 */
function dotClass(c) {
  if (!c.enabled || !c.ready) return 'off'
  return c.degraded ? 'warn' : 'ok'
}

function capTitle(c) {
  const state = [
    c.enabled ? '已开启' : '已关闭',
    c.ready ? (c.degraded ? '降级可用' : '就绪') : '不可用'
  ].join(' · ')
  const hint = canOperate.value ? `点击${c.enabled ? '关闭' : '开启'}该能力` : '当前账号为只读角色，无法修改'
  return `${c.label}：${state}\n${c.detail || ''}\n${hint}`
}

// ---------- 能力开关 ----------
const busyCap = ref('')

async function toggleCap(c) {
  if (!canOperate.value || busyCap.value) return
  busyCap.value = c.key
  try {
    const res = await api.put(`/api/system/capabilities/${c.key}`, { enabled: !c.enabled })
    toast.ok(`${res.label} 已${res.enabled ? '开启' : '关闭'}`)
    emit('refresh')
  } catch { /* 拦截器已提示 */ } finally {
    busyCap.value = ''
  }
}

// ---------- 推理设备切换 ----------
const devOpen = ref(false)
const switching = ref(false)
const devDdEl = ref(null)

const currentDevice = computed(() => device.value.selected || 'auto')

const deviceOptions = computed(() => {
  const cuda = !!device.value.cuda_available
  return [
    { value: 'auto', label: '自动（有 CUDA 优先用 GPU）', disabled: false },
    { value: 'cuda:0', label: 'CUDA 0 · GPU 推理（FP16）', disabled: !cuda },
    { value: 'cpu', label: 'CPU 推理', disabled: false }
  ]
})

async function pickDevice(opt) {
  devOpen.value = false
  if (!canOperate.value) {
    toast.warn('当前账号为只读角色，无法切换推理设备')
    return
  }
  if (opt.disabled || switching.value || opt.value === currentDevice.value) return
  switching.value = true
  try {
    const res = await api.put('/api/system/device', { device: opt.value })
    toast.ok(`推理设备已切换为 ${res.device}${res.half ? '（FP16）' : ''}`)
    emit('refresh')
  } catch { /* 拦截器已提示 */ } finally {
    switching.value = false
  }
}

function onDocClick(e) {
  if (devDdEl.value && !devDdEl.value.contains(e.target)) devOpen.value = false
}

const deviceText = computed(() => {
  if (switching.value) return '切换中…'
  const d = device.value
  if (!d.device) return '设备未就绪'
  if (d.cuda_available && String(d.device).startsWith('cuda')) {
    const gpu = String(d.gpu || '')
      .replace(/^NVIDIA\s+/i, '')
      .replace(/^GeForce\s+/i, '')
      .replace(/\s+Laptop\s+GPU$/i, '')
      .replace(/\s+GPU$/i, '')
    return `CUDA · ${gpu || d.device}${d.gpu_memory_gb ? ` · ${d.gpu_memory_gb}G` : ''}`
  }
  return d.device === 'cpu' ? 'CPU 推理' : `推理设备 ${d.device}`
})

const deviceTitle = computed(() => {
  const d = device.value
  const ready = props.status?.summary || { ready: 0, total: 0 }
  return [
    `推理设备：${d.device || '—'}（点击可切换 CPU / CUDA）`,
    `CUDA 可用：${d.cuda_available ? '是' : '否'}`,
    `GPU：${d.gpu || '—'}`,
    `显存：${d.gpu_memory_gb ? d.gpu_memory_gb + ' GB' : '—'}`,
    `精度：${d.half ? 'FP16' : 'FP32'}`,
    `输入尺寸：${d.imgsz || '—'}`,
    `PyTorch：${d.torch || '—'}`,
    `推理限频：${runtime.value.infer_fps_limit || '—'} FPS`,
    `运动门控：${runtime.value.motion_gate ? '开启' : '关闭'}`,
    `能力就绪：${ready.ready}/${ready.total}`
  ].join('\n')
})

const uptimeText = computed(() => {
  const s = runtime.value.uptime_sec || 0
  if (s < 60) return `运行 ${s}s`
  if (s < 3600) return `运行 ${Math.floor(s / 60)}m`
  return `运行 ${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`
})

function goAlarms() {
  if (router.currentRoute.value.path !== '/alarms') router.push('/alarms')
}

onMounted(() => {
  clockTimer = setInterval(() => { now.value = new Date() }, 1000)
  document.addEventListener('mousedown', onDocClick)
})
onUnmounted(() => {
  if (clockTimer) clearInterval(clockTimer)
  document.removeEventListener('mousedown', onDocClick)
})
</script>

<template>
  <footer class="statusbar">
    <div class="sb-scroll">
      <!-- 1. 业务指标（原顶栏左侧模块下移至此） -->
      <button class="sb-cap sb-metric" :class="{ alert: pending > 0 }"
              title="待处置报警，点击前往处置" @click="goAlarms">
        <span class="sb-dot" :class="pending > 0 ? 'err' : 'off'" />
        <Icon name="bell" />
        <span>待处置</span>
        <b>{{ pending }}</b>
      </button>
      <span class="sb-cap sb-metric" title="今日检测到的事件总数（含正常与异常）">
        <span class="sb-dot" :class="todayEvents > 0 ? 'ok' : 'off'" />
        <Icon name="flag" />
        <span>今日事件</span>
        <b>{{ todayEvents }}</b>
      </span>

      <i class="sb-sep" />

      <!-- 2. 采集设备：浏览器本地状态 -->
      <button class="sb-cap" :title="'浏览器摄像头采集：' + (deviceState.camera ? '已开启' : '未开启')"
              @click="router.push('/realtime')">
        <span class="sb-dot" :class="deviceState.camera ? 'ok' : 'off'" />
        <Icon name="camera" />
        <span>摄像头</span>
        <b>{{ deviceState.camera ? '已开启' : '未开启' }}</b>
      </button>
      <button class="sb-cap" :title="'麦克风与语音识别：' + (deviceState.mic ? '已开启' : '未开启')"
              @click="router.push('/realtime')">
        <span class="sb-dot" :class="deviceState.mic ? 'ok' : 'off'" />
        <Icon name="mic" />
        <span>麦克风</span>
        <b>{{ deviceState.mic ? '已开启' : '未开启' }}</b>
      </button>

      <i class="sb-sep" />

      <!-- 3. 视觉与行为检测能力：点击开关 -->
      <button
        v-for="c in visionCaps"
        :key="c.key"
        type="button"
        class="sb-cap sb-switch"
        :class="{ 'is-off': !c.enabled, busy: busyCap === c.key, 'is-locked': !canOperate }"
        :title="capTitle(c)"
        :disabled="!!busyCap || !canOperate"
        @click="toggleCap(c)"
      >
        <span class="sb-dot" :class="dotClass(c)" />
        <span>{{ c.label }}</span>
      </button>

      <i class="sb-sep" />

      <!-- 语音能力：点击开关 -->
      <button
        v-for="c in audioCaps"
        :key="c.key"
        type="button"
        class="sb-cap sb-switch"
        :class="{ 'is-off': !c.enabled, busy: busyCap === c.key, 'is-locked': !canOperate }"
        :title="capTitle(c)"
        :disabled="!!busyCap || !canOperate"
        @click="toggleCap(c)"
      >
        <span class="sb-dot" :class="dotClass(c)" />
        <span>{{ c.label }}</span>
      </button>
    </div>

    <!-- 4. 系统信息 -->
    <div class="sb-right">
      <span class="sb-clock">
        <span class="t">{{ clockTime }}</span>
        <span class="d">{{ clockDate }}</span>
      </span>

      <!-- 推理设备：点击切换 CPU / CUDA -->
      <div ref="devDdEl" class="dd sb-dd" :class="{ open: devOpen }">
        <button
          type="button"
          class="sb-info mono sb-device"
          :class="{ busy: switching, 'is-locked': !canOperate }"
          :title="deviceTitle"
          :aria-disabled="!canOperate"
          @click="canOperate ? (devOpen = !devOpen) : toast.warn('当前账号为只读角色，无法切换推理设备')"
        >
          <Icon name="cpu" />{{ deviceText }}<Icon name="chev-down" class="caret" />
        </button>
        <div v-if="devOpen" class="dd-menu sb-dd-menu">
          <div class="dd-head-note">推理设备（切换会重载模型，约数秒）</div>
          <div
            v-for="opt in deviceOptions"
            :key="opt.value"
            class="dd-opt"
            :class="{ on: opt.value === currentDevice, 'is-disabled': opt.disabled }"
            @click="pickDevice(opt)"
          >
            <span>{{ opt.label }}</span>
            <Icon v-if="opt.value === currentDevice" name="check" class="tick" />
          </div>
        </div>
      </div>

      <span class="sb-info mono sb-uptime" title="服务运行时长">
        <Icon name="clock" />{{ uptimeText }}
      </span>
      <button class="sb-log" :class="{ alert: errorCount > 0, warn: !errorCount && warnCount > 0 }"
              title="查看运行日志与异常" @click="emit('open-logs')">
        <Icon name="alert" />
        <span>日志</span>
        <b v-if="errorCount">{{ errorCount > 99 ? '99+' : errorCount }}</b>
        <b v-else-if="warnCount">{{ warnCount > 99 ? '99+' : warnCount }}</b>
      </button>
    </div>
  </footer>
</template>

<style scoped>
/* 能力开关：可点击，关闭态整体压暗并加虚线边，一眼能看出"没在算" */
.sb-switch { cursor: pointer; transition: border-color 0.16s, opacity 0.16s, background 0.16s; }
.sb-switch:hover { border-color: var(--line-3); background: var(--bg-raise); }
.sb-switch.is-off { opacity: 0.42; border-style: dashed; }
.sb-switch.is-off span:last-child { text-decoration: line-through; }
.sb-switch.busy { opacity: 0.6; cursor: progress; }
/* 只读角色（viewer）下这些写操作项置灰：后端会返回 403，前端提前给出可理解的视觉状态 */
.sb-switch.is-locked,
.sb-device.is-locked { cursor: not-allowed; opacity: 0.5; }
.sb-switch.is-locked:hover { border-color: var(--line-2); background: none; }

/* 推理设备下拉：状态栏贴底，菜单必须向上展开 */
.sb-dd { position: relative; }
.sb-dd .sb-device { cursor: pointer; }
.sb-dd .sb-device .caret { width: 13px; height: 13px; margin-left: 4px; color: var(--tx-3); transition: transform 0.18s; }
.sb-dd.open .sb-device .caret { transform: rotate(180deg); }
.sb-dd .sb-device.busy { opacity: 0.7; cursor: progress; }
.sb-dd-menu {
  top: auto;
  bottom: calc(100% + 6px);
  left: auto;
  right: 0;
  width: 240px;
  z-index: 120;
}
.sb-dd-menu .dd-head-note {
  padding: 5px 11px 7px;
  font-size: 11px;
  color: var(--tx-3);
  border-bottom: 1px solid var(--line);
  margin-bottom: 5px;
}
.sb-dd-menu .dd-opt.is-disabled { opacity: 0.4; cursor: not-allowed; }
</style>
