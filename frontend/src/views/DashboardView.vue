<template>
  <div class="page">
    <!-- KPI -->
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

    <!-- 主区：吃满剩余高度，图表/列表随视口伸缩 -->
    <div class="grid g-main mt fade-up d1 grid-fill">
      <div class="panel corner panel-fill">
        <div class="panel-hd">
          <span class="panel-title">近 24 小时行为类型分布</span>
          <div class="spacer" />
          <span class="panel-sub mono">{{ totalEvents }} 条事件</span>
        </div>
        <div class="panel-bd">
          <div v-if="!hasBreakdown" class="empty">
            <Icon name="layers" />
            <div class="t">暂无检测事件</div>
          </div>
          <div v-show="hasBreakdown" ref="barEl" class="chart-fill" />
        </div>
      </div>

      <div class="panel corner panel-fill">
        <div class="panel-hd">
          <span class="panel-title">最近报警</span>
          <div class="spacer" />
          <button class="btn btn--xs btn--ghost" @click="$router.push('/alarms')">全部处置</button>
        </div>
        <div class="panel-bd" style="padding: 12px">
          <div v-if="!stats.recent_alarms.length" class="empty" style="padding: 34px 10px">
            <Icon name="shield" />
            <div class="t">暂无报警，校园平安</div>
          </div>
          <div v-else class="alarm-list">
            <button v-for="a in stats.recent_alarms" :key="a.id" class="alarm-item" @click="$router.push('/alarms')">
              <span class="tag" :class="`tag--${a.level}`">{{ levelZh[a.level] }}</span>
              <div style="min-width: 0; flex: 1">
                <div class="ellip">{{ a.reason }}</div>
                <div class="dim tiny mono ellip">{{ fmt(a.created_at) }}</div>
              </div>
              <span class="pill" :class="`pill--${a.status}`">{{ statusZh[a.status] }}</span>
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 次区 -->
    <div class="grid g-2 mt fade-up d2 grid-fill">
      <div class="panel panel-fill">
        <div class="panel-hd">
          <span class="panel-title">点位运行状态</span>
          <div class="spacer" />
          <span class="panel-sub mono">{{ stats.camera_online }}/{{ stats.camera_total }} 在线</span>
        </div>
        <div class="panel-bd flush">
          <div v-if="!cameras.length" class="empty"><Icon name="camera" /><div class="t">尚未配置摄像头</div></div>
          <ul v-else class="cams">
            <li v-for="c in cameras" :key="c.id">
              <span class="c-dot" :class="{ off: !c.enabled }" />
              <div style="min-width: 0">
                <div class="ellip">{{ c.name }}</div>
                <div class="dim tiny">{{ c.location || '未设置位置' }}</div>
              </div>
              <span class="tag tag--mute">{{ srcZh[c.source_type] || c.source_type }}</span>
            </li>
          </ul>
        </div>
      </div>

      <div class="panel panel-fill">
        <div class="panel-hd">
          <span class="panel-title">报警处置概况</span>
          <div class="spacer" />
          <span class="panel-sub mono">累计 {{ alarmTotal }} 条</span>
        </div>
        <div class="panel-bd">
          <div v-if="!alarmTotal" class="empty"><Icon name="shield" /><div class="t">暂无需处置的报警</div></div>
          <div v-show="alarmTotal" ref="donutEl" class="chart-fill" />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, nextTick } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, PieChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import Icon from '@/ui/Icon.vue'
import api from '@/api'

// 按需注册，避免打包整个 ECharts
echarts.use([BarChart, PieChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const stats = reactive({
  camera_online: 0, camera_total: 0, today_events: 0, today_alarms: 0,
  pending_alarms: 0, event_type_breakdown: {}, recent_alarms: []
})
const cameras = ref([])
const alarms = ref([])
const barEl = ref(null)
const donutEl = ref(null)
let bar = null
let donut = null
let timer = null

const levelZh = { high: '高危', medium: '中警', low: '提示' }
const statusZh = { pending: '待处置', handling: '处置中', resolved: '已解决', ignored: '已忽略' }
const typeZh = { fall: '跌倒', smoke: '抽烟', bullying: '欺凌', fight: '打架', argue: '争吵', crowd: '人员聚集', person: '人员', normal: '正常' }
const srcZh = { webcam: '本机摄像头', rtsp: 'RTSP', file: '视频文件' }
const typeColor = { bullying: '#ff1e56', fight: '#ff4d6d', argue: '#ff8a3d', fall: '#ffb020', smoke: '#d7c341', crowd: '#9b7bff', person: '#2fd6f0' }

const fmt = (t) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '')
const totalEvents = computed(() => Object.values(stats.event_type_breakdown || {}).reduce((a, b) => a + b, 0))
const hasBreakdown = computed(() => Object.keys(stats.event_type_breakdown || {}).length > 0)
const alarmTotal = computed(() => alarms.value.length)

const tiles = computed(() => [
  { label: '在线摄像头', icon: 'camera', value: stats.camera_online, unit: `/${stats.camera_total}`, foot: '实时视频源接入', tint: 'rgba(47,214,240,.16)', color: '#6fe0f5' },
  { label: '今日检测事件', icon: 'pulse', value: stats.today_events, foot: '全部行为累计', tint: 'rgba(59,130,246,.16)', color: '#7fa8ff' },
  { label: '今日报警', icon: 'bell', value: stats.today_alarms, foot: '触发报警次数', tint: 'rgba(255,77,109,.16)', color: '#ff8fa3' },
  { label: '待处置报警', icon: 'alert', value: stats.pending_alarms, foot: '需值班人员跟进', tint: 'rgba(255,176,32,.16)', color: '#ffc861' }
])

async function load() {
  try {
    const s = await api.get('/api/dashboard/stats')
    Object.assign(stats, s)
    await nextTick()
    drawBar()
  } catch { /* 静默 */ }
}

async function loadAux() {
  try { cameras.value = await api.get('/api/cameras') } catch { /* 静默 */ }
  try {
    alarms.value = await api.get('/api/alarms')
    await nextTick()
    drawDonut()
  } catch { /* 静默 */ }
}

function drawBar() {
  if (!barEl.value || !hasBreakdown.value) return
  if (!bar) bar = echarts.init(barEl.value)
  const entries = Object.entries(stats.event_type_breakdown)
  bar.setOption({
    backgroundColor: 'transparent',
    grid: { left: 6, right: 18, top: 26, bottom: 6, containLabel: true },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#111a2a',
      borderColor: 'rgba(122,162,220,.28)',
      textStyle: { color: '#e8f0fc', fontSize: 12 }
    },
    xAxis: {
      type: 'category',
      data: entries.map(([k]) => typeZh[k] || k),
      axisLine: { lineStyle: { color: 'rgba(122,162,220,.2)' } },
      axisTick: { show: false },
      axisLabel: { color: '#8fa0bd', fontSize: 12 }
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: 'rgba(122,162,220,.09)' } },
      axisLabel: { color: '#5b6a85', fontSize: 11 }
    },
    series: [
      {
        type: 'bar',
        barWidth: 30,
        itemStyle: {
          borderRadius: [7, 7, 0, 0],
          color: (p) => {
            const key = entries[p.dataIndex][0]
            const c = typeColor[key] || '#2fd6f0'
            return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: c },
              { offset: 1, color: 'rgba(0,0,0,0.06)' }
            ])
          }
        },
        label: { show: true, position: 'top', color: '#8fa0bd', fontSize: 11 },
        data: entries.map(([, v]) => v)
      }
    ]
  })
}

function drawDonut() {
  if (!donutEl.value || !alarmTotal.value) return
  if (!donut) donut = echarts.init(donutEl.value)
  const order = ['pending', 'handling', 'resolved', 'ignored']
  const palette = { pending: '#ffb020', handling: '#2fd6f0', resolved: '#2ee6a8', ignored: '#5b6a85' }
  const data = order
    .map((k) => ({ name: statusZh[k], value: alarms.value.filter((a) => a.status === k).length, itemStyle: { color: palette[k] } }))
    .filter((d) => d.value > 0)
  donut.setOption({
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item', backgroundColor: '#111a2a', borderColor: 'rgba(122,162,220,.28)', textStyle: { color: '#e8f0fc', fontSize: 12 } },
    legend: { bottom: 0, icon: 'circle', textStyle: { color: '#8fa0bd', fontSize: 12 }, itemWidth: 8, itemHeight: 8 },
    series: [
      {
        type: 'pie',
        radius: ['54%', '76%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: false,
        itemStyle: { borderColor: '#0c1320', borderWidth: 3, borderRadius: 5 },
        label: { show: true, position: 'center', formatter: `{a|${alarmTotal.value}}\n{b|报警总数}`, rich: { a: { color: '#e8f0fc', fontSize: 26, fontWeight: 700, fontFamily: 'Chakra Petch' }, b: { color: '#5b6a85', fontSize: 11, padding: [4, 0, 0, 0] } } },
        labelLine: { show: false },
        data
      }
    ]
  })
}

function onResize() { bar?.resize(); donut?.resize() }

onMounted(() => {
  load()
  loadAux()
  timer = setInterval(() => { load(); loadAux() }, 10000)
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  window.removeEventListener('resize', onResize)
  bar?.dispose(); donut?.dispose()
})
</script>

<style scoped>
/* .ellip / .c-dot 已收敛到全局 style.css 的通用工具类 */

/* 本页主区两栏等分：全局 .g-main 为 2.65:1，但行为类型分布过宽时柱状图留白太多，
   这里按 5:5 展示，与「最近报警」等宽 */
.g-main { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
@media (max-width: 900px) {
  .g-main { grid-template-columns: 1fr; }
}

/* 列表在面板剩余高度内滚动（面板高度由 flex 铺满决定），不再固定上限。
   minmax(0, 1fr) 是关键：默认 auto 列会被条目的 min-content（长报警原因不换行）
   撑开，导致整行超出面板宽度；这里允许列收缩到 0，超长内容改由 ellip 截断。 */
.alarm-list {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding-right: 4px;
}
.alarm-item {
  display: flex; align-items: center; gap: 11px;
  min-width: 0;
  width: 100%; text-align: left;
  padding: 9px 11px;
  border-radius: var(--r-md);
  border: 1px solid var(--line);
  background: var(--bg-raise);
  cursor: pointer; color: inherit;
  transition: border-color 0.18s, transform 0.18s, background 0.18s;
}
.alarm-item:hover { border-color: var(--line-3); transform: translateX(2px); }
/* 级别与状态标签保持完整宽度，负责截断的是中间的原因文本 */
.alarm-item .tag, .alarm-item .pill { flex-shrink: 0; }

/* 点位列表同样在剩余高度内滚动 */
.cams { display: grid; flex: 1; min-height: 0; overflow-y: auto; }
.cams li {
  display: flex; align-items: center; gap: 11px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
}
.cams li:last-child { border-bottom: none; }
.cams li > div { flex: 1; }

/* 窄屏下转为自然高度，两个列表恢复滚动上限，避免把整页撑得过长 */
@media (max-width: 900px) {
  .alarm-list { flex: none; max-height: 260px; }
  .cams { flex: none; max-height: 250px; }
}
</style>
