<script setup>
/**
 * 底部状态栏：只保留"值班时必须一直看得见"的 8 项。
 *
 * 精简原则（原先这里有 15+ 项，横向挤满、信息噪音大）：
 *   - **可切换的东西全部搬去系统设置页**：检测能力开关、推理设备切换、
 *     各类阈值与帧率。状态栏只做只读回显，点击跳转到对应设置分组。
 *   - 这里只回答三个问题：有没有待处置的报警？采集设备是否在工作？
 *     后端是否健康（能力就绪 / 运行时长 / 日志异常）。
 *
 * 保留项：待处置、今日事件、摄像头、麦克风、能力就绪、推理设备、
 * 运行时长、日志（另有实时时钟，属系统信息不占计数位）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import Icon from '@/ui/Icon.vue'
import { deviceState } from '@/stores/device'

const props = defineProps({
  status: { type: Object, default: null }
})
const emit = defineEmits(['open-logs'])
const router = useRouter()

// 实时时钟（原在顶栏，现移至状态栏，为顶栏两侧菱形导航腾出空间）
const now = ref(new Date())
let clockTimer = null
const clockTime = computed(() => now.value.toLocaleTimeString('zh-CN', { hour12: false }))
const clockDate = computed(() => now.value.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit', weekday: 'short' }))

const caps = computed(() => props.status?.capabilities || [])
const device = computed(() => props.status?.device || {})
const runtime = computed(() => props.status?.runtime || {})
const logs = computed(() => props.status?.logs || {})
const summary = computed(() => props.status?.summary || { ready: 0, total: 0 })

const pending = computed(() => props.status?.alarms?.pending || 0)
const todayEvents = computed(() => props.status?.events?.today || 0)

/** 关闭的能力数量：状态栏不逐项列出，只提示"有几项没在算"，细节去设置页看 */
const offCount = computed(() => caps.value.filter((c) => !c.enabled).length)
const capsTitle = computed(() => {
  const off = caps.value.filter((c) => !c.enabled).map((c) => c.label)
  return [
    `检测能力就绪：${summary.value.ready}/${summary.value.total}`,
    off.length ? `已关闭：${off.join('、')}` : '全部能力已开启',
    '点击前往「系统设置 → 检测能力」开关'
  ].join('\n')
})

const errorCount = computed(() => logs.value.error_count || 0)
const warnCount = computed(() => logs.value.warning_count || 0)

const deviceText = computed(() => {
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
  return [
    `推理设备：${d.device || '—'}`,
    `CUDA 可用：${d.cuda_available ? '是' : '否'}`,
    `GPU：${d.gpu || '—'}`,
    `显存：${d.gpu_memory_gb ? d.gpu_memory_gb + ' GB' : '—'}`,
    `精度：${d.half ? 'FP16' : 'FP32'}`,
    `输入尺寸：${d.imgsz || '—'}`,
    `PyTorch：${d.torch || '—'}`,
    `推理限频：${runtime.value.infer_fps_limit || '—'} FPS`,
    `运动门控：${runtime.value.motion_gate ? '开启' : '关闭'}`,
    '点击前往「系统设置 → 推理设备」切换'
  ].join('\n')
})

const uptimeText = computed(() => {
  const s = runtime.value.uptime_sec || 0
  if (s < 60) return `运行 ${s}s`
  if (s < 3600) return `运行 ${Math.floor(s / 60)}m`
  return `运行 ${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`
})

function go(path) {
  if (router.currentRoute.value.path !== path) router.push(path)
}

onMounted(() => {
  clockTimer = setInterval(() => { now.value = new Date() }, 1000)
})
onUnmounted(() => {
  if (clockTimer) clearInterval(clockTimer)
})
</script>

<template>
  <footer class="statusbar">
    <div class="sb-scroll">
      <!-- 1. 业务指标（原顶栏左侧模块下移至此） -->
      <button class="sb-cap sb-metric" :class="{ alert: pending > 0 }"
              title="待处置报警，点击前往处置" @click="go('/alarms')">
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

      <!-- 2. 采集设备：浏览器本地状态，点击回到实时检测页 -->
      <button class="sb-cap" :title="'浏览器摄像头采集：' + (deviceState.camera ? '已开启' : '未开启')"
              @click="go('/realtime')">
        <span class="sb-dot" :class="deviceState.camera ? 'ok' : 'off'" />
        <Icon name="camera" />
        <span>摄像头</span>
        <b>{{ deviceState.camera ? '已开启' : '未开启' }}</b>
      </button>
      <button class="sb-cap" :title="'麦克风与语音识别：' + (deviceState.mic ? '已开启' : '未开启')"
              @click="go('/realtime')">
        <span class="sb-dot" :class="deviceState.mic ? 'ok' : 'off'" />
        <Icon name="mic" />
        <span>麦克风</span>
        <b>{{ deviceState.mic ? '已开启' : '未开启' }}</b>
      </button>

      <i class="sb-sep" />

      <!-- 3. 运行态摘要：明细（逐项能力开关 / 设备切换）全部在系统设置页 -->
      <button class="sb-cap" :title="capsTitle" @click="go('/settings')">
        <span class="sb-dot" :class="summary.ready >= summary.total && !offCount ? 'ok' : offCount ? 'warn' : 'off'" />
        <Icon name="sliders" />
        <span>检测能力</span>
        <b>{{ summary.ready }}/{{ summary.total }}<template v-if="offCount"> · 关 {{ offCount }}</template></b>
      </button>
    </div>

    <!-- 4. 系统信息 -->
    <div class="sb-right">
      <span class="sb-clock">
        <span class="t">{{ clockTime }}</span>
        <span class="d">{{ clockDate }}</span>
      </span>

      <button class="sb-info mono sb-device" :title="deviceTitle" @click="go('/settings')">
        <Icon name="cpu" />{{ deviceText }}
      </button>

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

      <!-- 账号信息卡：由 AppShell 通过插槽注入。
           放在底部而不是顶栏，是为了让顶栏只剩「标题 + 两侧导航」，
           居中构图不被右侧的用户区挤压；账号本也属于"系统信息"一类。 -->
      <slot name="account" />
    </div>
  </footer>
</template>

<style scoped>
/* 设备信息以按钮形式呈现（点击跳设置页），去掉按钮默认外观只保留悬停反馈 */
.sb-device { cursor: pointer; transition: border-color 0.16s, background 0.16s; }
.sb-device:hover { border-color: var(--line-3); background: var(--bg-raise); }
</style>
