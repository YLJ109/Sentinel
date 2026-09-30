<script setup>
/**
 * 系统设置：检测能力开关 + 推理设备 + 全部可调参数 + 点位管理。
 *
 * 页面结构（顶部标签页切换）：
 *   【参数配置】左侧锚点导航 + 右侧分组
 *     1. 检测能力   —— 原底部状态栏上的开关搬到这里
 *     2. 推理设备   —— CPU / CUDA 切换
 *     3. 参数分组   —— 由后端 /api/settings/schema 下发，前端不硬编码参数清单
 *     4. 只读项     —— 绑定在模型上的参数（如输入尺寸），显示当前值与改法
 *   【点位管理】直接复用 CamerasView 组件（摄像头增删改与设备扫描）
 *
 * 为什么把「摄像头管理」并进来而不是继续单列在顶栏：
 *   它本质是**部署期配置**（新增点位、填 RTSP 地址、绑定本机设备），
 *   与阈值调参同属"配置"心智；并进来后顶栏得以保持左 5 右 5 的对称布局。
 *
 * 参数清单为什么不写在前端：后端新增一个可调参数只需要改一处，
 * 前端自动出现对应控件，避免两边清单不同步导致的"设置了但没这个参数"。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import Icon from '@/ui/Icon.vue'
import Modal from '@/ui/Modal.vue'
import CamerasView from '@/views/CamerasView.vue'
import { toast } from '@/ui/toast'
import { useAuthStore } from '@/stores/auth'
import { deviceState } from '@/stores/device'
import api from '@/api'

const auth = useAuthStore()
const canWrite = computed(() => auth.role === 'admin' || auth.role === 'operator')
const isAdmin = computed(() => auth.role === 'admin')

// 标签页：参数配置 / 点位管理
const tab = ref('params')

const loading = ref(false)
const groups = ref([])
const readonly = ref([])
const overriddenCount = ref(0)

// 待提交的改动（只记录被改过的项，避免全量提交）
const dirty = reactive({})
const saving = ref(false)

// ---------- 能力开关与设备（来自 system 接口）----------
const caps = ref([])
const device = computed(() => sysStatus.value?.device || {})
const sysStatus = ref(null)
const switching = ref(false)
const busyCap = ref('')

const visionCaps = computed(() => caps.value.filter((c) => c.group !== 'audio'))
const audioCaps = computed(() => caps.value.filter((c) => c.group === 'audio'))

const deviceOptions = computed(() => {
  const cuda = !!device.value.cuda_available
  return [
    { value: 'auto', label: '自动', hint: '有 CUDA 优先用 GPU', disabled: false },
    { value: 'cuda:0', label: 'CUDA 0', hint: 'GPU 推理（FP16）', disabled: !cuda },
    { value: 'cpu', label: 'CPU', hint: '纯 CPU 推理', disabled: false }
  ]
})

async function loadSystem() {
  try {
    sysStatus.value = await api.get('/api/system/status')
    caps.value = sysStatus.value.capabilities || []
  } catch { /* 静默 */ }
}

async function toggleCap(c) {
  if (!canWrite.value || busyCap.value) return
  busyCap.value = c.key
  try {
    const res = await api.put(`/api/system/capabilities/${c.key}`, { enabled: !c.enabled })
    toast.ok(`${res.label} 已${res.enabled ? '开启' : '关闭'}`)
    await loadSystem()
  } catch { /* 拦截器已提示 */ } finally {
    busyCap.value = ''
  }
}

async function pickDevice(opt) {
  if (!isAdmin.value) { toast.warn('仅管理员可切换推理设备'); return }
  if (opt.disabled || switching.value || opt.value === (device.value.selected || 'auto')) return
  switching.value = true
  try {
    const res = await api.put('/api/system/device', { device: opt.value })
    toast.ok(`推理设备已切换为 ${res.device}${res.half ? '（FP16）' : ''}`)
    await loadSystem()
  } catch { /* 拦截器已提示 */ } finally {
    switching.value = false
  }
}

// ---------- 参数 ----------
async function loadSchema() {
  loading.value = true
  try {
    const s = await api.get('/api/settings/schema')
    groups.value = s.groups || []
    readonly.value = s.readonly || []
    overriddenCount.value = s.overridden_count || 0
    for (const k of Object.keys(dirty)) delete dirty[k]
  } catch { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

function setValue(item, v) {
  if (!canWrite.value) return
  if (item.admin_only && !isAdmin.value) { toast.warn('该参数仅管理员可修改'); return }
  dirty[item.key] = v
}

function isDirty(item) {
  return Object.prototype.hasOwnProperty.call(dirty, item.key)
}

async function save() {
  const values = { ...dirty }
  if (!Object.keys(values).length) { toast.warn('没有需要保存的改动'); return }
  saving.value = true
  try {
    const res = await api.patch('/api/settings', { values })
    const failed = res.failed || []
    if (failed.length) {
      toast.warn(`${res.updated || 0} 项已保存，${failed.length} 项被拒绝：${failed[0].reason}`)
    } else {
      toast.ok(`已保存 ${res.updated} 项参数`)
    }
    await loadSchema()
  } catch { /* 拦截器已提示 */ } finally {
    saving.value = false
  }
}

const resetOpen = ref(false)
async function doReset() {
  try {
    const r = await api.post('/api/settings/reset')
    toast.ok(`已恢复默认（${r.reset} 项）`)
    resetOpen.value = false
    await loadSchema()
  } catch { /* 拦截器已提示 */ }
}

function scrollTo(key) {
  const el = document.getElementById(`sec-${key}`)
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

const dirtyCount = computed(() => Object.keys(dirty).length)
const deviceText = computed(() => {
  const d = device.value
  if (!d.device) return '未就绪'
  if (String(d.device).startsWith('cuda')) {
    const gpu = String(d.gpu || '').replace(/^NVIDIA\s+/i, '').replace(/^GeForce\s+/i, '')
    return `CUDA · ${gpu || d.device}${d.gpu_memory_gb ? ` · ${d.gpu_memory_gb}G` : ''}`
  }
  return 'CPU 推理'
})

onMounted(async () => {
  await Promise.all([loadSystem(), loadSchema()])
})
</script>

<template>
  <div class="page">
    <!-- 标签页：参数配置 / 点位管理 -->
    <div class="panel fade-up set-tabs">
      <button type="button" class="set-tab" :class="{ on: tab === 'params' }" @click="tab = 'params'">
        <Icon name="sliders" /><span>参数配置</span>
      </button>
      <button type="button" class="set-tab" :class="{ on: tab === 'cameras' }" @click="tab = 'cameras'">
        <Icon name="camera" /><span>点位管理</span>
      </button>
      <div class="spacer" />
      <span class="dim tiny">
        {{ tab === 'params' ? '检测能力 / 推理设备 / 阈值参数，改动立即生效并持久化' : '摄像头点位的增删改与本机设备扫描' }}
      </span>
    </div>

    <!-- 点位管理：直接复用摄像头页面组件，避免两套实现 -->
    <CamerasView v-if="tab === 'cameras'" class="is-embedded fade-up" />

    <div v-show="tab === 'params'" class="settings-grid">
      <!-- 左：锚点导航 -->
      <aside class="side panel fade-up">
        <div class="side-hd">设置分组</div>
        <button class="side-item" @click="scrollTo('caps')"><Icon name="shield" /> 检测能力</button>
        <button class="side-item" @click="scrollTo('device')"><Icon name="cpu" /> 推理设备</button>
        <button v-for="g in groups" :key="g.key" class="side-item" @click="scrollTo(g.key)">
          <Icon name="sliders" /> {{ g.label }}
        </button>
        <button v-if="readonly.length" class="side-item" @click="scrollTo('readonly')">
          <Icon name="lock" /> 只读信息
        </button>
      </aside>

      <!-- 右：内容 -->
      <div class="side-body">
        <!-- 顶部操作条 -->
        <div class="panel fade-up actions-bar">
          <span class="dim tiny">
            共 <b class="mono">{{ groups.reduce((n, g) => n + g.items.length, 0) }}</b> 个可调参数，
            当前已覆盖 <b class="mono">{{ overriddenCount }}</b> 项
          </span>
          <div class="spacer" />
          <span v-if="dirtyCount" class="dirty-hint">
            <Icon name="edit" /> {{ dirtyCount }} 项未保存
          </span>
          <button class="btn btn--xs btn--ghost" :disabled="!isAdmin" @click="resetOpen = true">
            恢复默认
          </button>
          <button class="btn btn--xs btn--primary" :disabled="!canWrite || !dirtyCount || saving" @click="save">
            <Icon name="check" /> {{ saving ? '保存中…' : '保存改动' }}
          </button>
        </div>

        <!-- 1. 检测能力 -->
        <section id="sec-caps" class="panel fade-up">
          <div class="panel-hd">
            <span class="panel-title">检测能力</span>
            <div class="spacer" />
            <span class="panel-sub">关闭后引擎不再做对应计算，设置会持久化</span>
          </div>
          <div class="panel-bd">
            <div class="cap-grid">
              <button v-for="c in [...visionCaps, ...audioCaps]" :key="c.key"
                      class="cap-card" :class="{ off: !c.enabled, locked: !canWrite }"
                      :disabled="!!busyCap || !canWrite" @click="toggleCap(c)">
                <span class="cap-dot" :class="!c.enabled || !c.ready ? 'off' : (c.degraded ? 'warn' : 'ok')" />
                <div class="cap-main">
                  <div class="cap-name">{{ c.label }}</div>
                  <div class="cap-detail ellip">{{ c.detail || (c.ready ? '就绪' : '不可用') }}</div>
                </div>
                <span class="cap-sw" :class="{ on: c.enabled }"><i /></span>
              </button>
            </div>
          </div>
        </section>

        <!-- 2. 推理设备 -->
        <section id="sec-device" class="panel fade-up">
          <div class="panel-hd">
            <span class="panel-title">推理设备</span>
            <div class="spacer" />
            <span class="panel-sub mono">{{ deviceText }}</span>
          </div>
          <div class="panel-bd">
            <div class="dev-row">
              <button v-for="o in deviceOptions" :key="o.value"
                      class="dev-card" :class="{ on: (device.selected || 'auto') === o.value, disabled: o.disabled }"
                      :disabled="o.disabled || switching || !isAdmin" @click="pickDevice(o)">
                <div class="dev-name">{{ o.label }}</div>
                <div class="dev-hint">{{ o.hint }}</div>
              </button>
            </div>
            <p class="muted tiny" style="margin-top: 10px">
              切换设备会丢弃并重新加载模型权重，期间约数秒不可用；仅管理员可操作。
            </p>
          </div>
        </section>

        <!-- 3. 参数分组 -->
        <section v-for="g in groups" :id="`sec-${g.key}`" :key="g.key" class="panel fade-up">
          <div class="panel-hd">
            <span class="panel-title">{{ g.label }}</span>
            <div class="spacer" />
            <span class="panel-sub mono">{{ g.items.length }} 项</span>
          </div>
          <div class="panel-bd">
            <div class="param-list">
              <div v-for="it in g.items" :key="it.key" class="param-row" :class="{ changed: isDirty(it) }">
                <div class="p-label">
                  <span>{{ it.label }}</span>
                  <span v-if="it.admin_only" class="badge-admin" title="仅管理员可修改">管理员</span>
                  <span v-if="it.overridden" class="badge-custom" title="已覆盖 .env 默认值">已自定义</span>
                  <div v-if="it.hint" class="p-hint">{{ it.hint }}</div>
                </div>

                <div class="p-control">
                  <!-- 开关 -->
                  <button v-if="it.type === 'bool'" class="sw-lg" :class="{ on: !!it.value }"
                          :disabled="!canWrite || (it.admin_only && !isAdmin)"
                          @click="setValue(it, !it.value)">
                    <i />
                  </button>

                  <!-- 数值：滑杆 + 数字框 -->
                  <template v-else-if="it.type === 'int' || it.type === 'float'">
                    <input class="rng" type="range" :min="it.min" :max="it.max"
                           :step="it.step || (it.type === 'int' ? 1 : 0.01)"
                           :value="it.value" :disabled="!canWrite || (it.admin_only && !isAdmin)"
                           @input="setValue(it, it.type === 'int' ? parseInt($event.target.value, 10) : parseFloat($event.target.value))" />
                    <input class="num mono" type="number" :min="it.min" :max="it.max"
                           :step="it.step || (it.type === 'int' ? 1 : 0.01)"
                           :value="it.value" :disabled="!canWrite || (it.admin_only && !isAdmin)"
                           @change="setValue(it, it.type === 'int' ? parseInt($event.target.value, 10) : parseFloat($event.target.value))" />
                  </template>

                  <!-- JSON（如隐私遮蔽区域） -->
                  <textarea v-else-if="it.type === 'json'" class="json-box mono" rows="2"
                            :value="it.value" :disabled="!canWrite || (it.admin_only && !isAdmin)"
                            @change="setValue(it, $event.target.value)" />

                  <input v-else class="input" :value="it.value"
                         :disabled="!canWrite || (it.admin_only && !isAdmin)"
                         @change="setValue(it, $event.target.value)" />
                </div>
              </div>
            </div>
          </div>
        </section>

        <!-- 4. 只读信息 -->
        <section v-if="readonly.length" id="sec-readonly" class="panel fade-up">
          <div class="panel-hd">
            <span class="panel-title">只读信息</span>
            <div class="spacer" />
            <span class="panel-sub">这些参数绑定在已加载的模型上，改后需重载或重启</span>
          </div>
          <div class="panel-bd">
            <div class="ro-list">
              <div v-for="r in readonly" :key="r.key" class="ro-row">
                <span class="ro-label">{{ r.label }}</span>
                <span class="mono ro-value">{{ r.value }}</span>
                <div class="ro-hint">{{ r.hint }}</div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>

    <!-- 恢复默认确认 -->
    <Modal v-model="resetOpen" title="恢复默认设置" max-width="440px">
      <p class="muted">
        将清除全部 <b class="mono">{{ overriddenCount }}</b> 项运行时覆盖，
        恢复为部署时 <span class="mono">.env</span> 中设定的基线值。
      </p>
      <p class="muted tiny" style="margin-top: 8px">
        注意：恢复的是 <b>.env 基线</b>而不是代码内常量，因此校方在 .env 里做的定制不会被抹掉。
      </p>
      <template #footer>
        <button class="btn btn--ghost" @click="resetOpen = false">取消</button>
        <button class="btn btn--primary" @click="doReset"><Icon name="check" /> 确认恢复</button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
.settings-grid { display: grid; grid-template-columns: 190px minmax(0, 1fr); gap: 14px; align-items: start; }

/* 右侧为纵向排列的一叠面板。这里必须有 gap：
   .side-body 一旦没有样式，内部的 section.panel 就退化成普通块级元素首尾相接，
   面板与面板之间完全没有间距，看起来像"盒子的上下 padding 没生效"。
   间距值取 14px，与左侧锚点栏的 gap 保持一致。 */
.side-body { display: flex; flex-direction: column; gap: 14px; min-width: 0; }

/* 标签页：与人员管理页的 Tab 保持同一套视觉语言 */
.set-tabs { display: flex; align-items: center; gap: 8px; padding: 9px 12px; margin-bottom: 14px; }
.set-tabs .spacer { flex: 1; }
.set-tab {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 7px 15px;
  border: 1px solid var(--line-2); border-radius: 999px;
  background: none; color: var(--tx-2);
  font-family: inherit; font-size: 13px; cursor: pointer;
  transition: border-color 0.16s, background 0.16s, color 0.16s;
}
.set-tab > svg { width: 14px; height: 14px; }
.set-tab:hover { border-color: var(--line-3); background: var(--bg-raise); }
.set-tab.on {
  color: #eafaff; border-color: rgba(47, 214, 240, 0.7);
  background: linear-gradient(135deg, rgba(47, 214, 240, 0.22), rgba(59, 130, 246, 0.1));
  box-shadow: 0 0 14px rgba(47, 214, 240, 0.2);
}
@media (max-width: 1100px) { .settings-grid { grid-template-columns: 1fr; } .side { display: none; } }

/* ---------- 左侧锚点 ---------- */
.side { position: sticky; top: 0; padding: 10px; }
.side-hd { font-size: 11px; letter-spacing: 0.08em; color: var(--tx-3); padding: 4px 8px 8px; }
.side-item {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 8px 10px; margin-bottom: 3px;
  border: 0; border-radius: var(--r-sm); background: none;
  color: var(--tx-2); font-size: 12.5px; cursor: pointer; text-align: left;
  transition: background 0.16s, color 0.16s;
}
.side-item:hover { background: var(--bg-raise); color: var(--tx-1); }
.side-item :deep(svg) { width: 15px; height: 15px; flex-shrink: 0; }

/* ---------- 顶部操作条 ---------- */
.actions-bar { display: flex; align-items: center; gap: 12px; padding: 10px 14px; }
.dirty-hint { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: #ffc861; }
.dirty-hint :deep(svg) { width: 14px; height: 14px; }

/* ---------- 能力卡片 ---------- */
.cap-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; }
.cap-card {
  display: flex; align-items: center; gap: 10px;
  padding: 11px 12px; border-radius: var(--r-md);
  border: 1px solid var(--line-2); background: var(--bg-inset);
  cursor: pointer; text-align: left;
  transition: border-color 0.16s, background 0.16s, opacity 0.16s;
}
.cap-card:hover { border-color: var(--line-3); background: var(--bg-raise); }
.cap-card.off { opacity: 0.5; border-style: dashed; }
.cap-card.locked { cursor: not-allowed; }
.cap-main { min-width: 0; flex: 1; }
.cap-name { font-size: 13px; color: var(--tx-1); font-weight: 600; }
.cap-detail { font-size: 11px; color: var(--tx-3); margin-top: 2px; }
.cap-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.cap-dot.ok { background: #4ade80; box-shadow: 0 0 8px rgba(74, 222, 128, 0.6); }
.cap-dot.warn { background: #ffc861; }
.cap-dot.off { background: var(--tx-3); }
.cap-sw { width: 34px; height: 19px; border-radius: 999px; background: var(--bg-raise); border: 1px solid var(--line-2); position: relative; flex-shrink: 0; }
.cap-sw i { position: absolute; top: 2px; left: 2px; width: 13px; height: 13px; border-radius: 50%; background: var(--tx-3); transition: transform 0.18s, background 0.18s; }
.cap-sw.on { background: rgba(47, 214, 240, 0.2); border-color: var(--acc); }
.cap-sw.on i { transform: translateX(15px); background: var(--acc); }

/* ---------- 设备卡片 ---------- */
.dev-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.dev-card {
  padding: 12px; border-radius: var(--r-md); text-align: left; cursor: pointer;
  border: 1px solid var(--line-2); background: var(--bg-inset);
  transition: border-color 0.16s, background 0.16s;
}
.dev-card:hover:not(.disabled) { border-color: var(--line-3); background: var(--bg-raise); }
.dev-card.on { border-color: var(--acc); background: rgba(47, 214, 240, 0.08); }
.dev-card.disabled { opacity: 0.4; cursor: not-allowed; }
.dev-name { font-size: 13.5px; font-weight: 600; color: var(--tx-1); }
.dev-hint { font-size: 11px; color: var(--tx-3); margin-top: 3px; }

/* ---------- 参数行 ---------- */
.param-list { display: flex; flex-direction: column; gap: 2px; }
.param-row {
  display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px;
  align-items: center; padding: 9px 10px; border-radius: var(--r-sm);
  transition: background 0.16s;
}
.param-row:hover { background: var(--bg-inset); }
.param-row.changed { background: rgba(255, 176, 32, 0.08); box-shadow: inset 2px 0 0 #ffc861; }
@media (max-width: 900px) { .param-row { grid-template-columns: 1fr; gap: 8px; } }
.p-label { font-size: 12.5px; color: var(--tx-2); min-width: 0; }
.p-hint { font-size: 11px; color: var(--tx-3); margin-top: 2px; }
.badge-admin, .badge-custom {
  margin-left: 7px; padding: 1px 6px; border-radius: 4px; font-size: 10px;
}
.badge-admin { background: rgba(255, 77, 109, 0.16); color: #ff8fa3; }
.badge-custom { background: rgba(47, 214, 240, 0.16); color: #6fe0f5; }
.p-control { display: flex; align-items: center; gap: 10px; }
.rng { flex: 1; min-width: 0; accent-color: var(--acc); }
.num { width: 74px; flex-shrink: 0; padding: 4px 7px; border-radius: 6px;
       border: 1px solid var(--line-2); background: var(--bg-inset); color: var(--tx-1); font-size: 12px; }
/* 开关（大号） */
.sw-lg { width: 40px; height: 22px; border-radius: 999px; background: var(--bg-inset);
         border: 1px solid var(--line-2); position: relative; cursor: pointer; flex-shrink: 0;
         transition: background 0.18s, border-color 0.18s; }
.sw-lg i { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%;
           background: var(--tx-3); transition: transform 0.18s, background 0.18s; }
.sw-lg.on { background: rgba(47, 214, 240, 0.2); border-color: var(--acc); }
.sw-lg.on i { transform: translateX(18px); background: var(--acc); }
.json-box { width: 100%; min-height: 46px; resize: vertical; padding: 6px 8px; font-size: 11.5px;
            border-radius: 6px; border: 1px solid var(--line-2); background: var(--bg-inset); color: var(--tx-1); }

/* ---------- 只读项 ---------- */
.ro-list { display: flex; flex-direction: column; gap: 8px; }
.ro-row { display: grid; grid-template-columns: 150px 120px minmax(0, 1fr); gap: 12px; align-items: center;
          padding: 9px 10px; border-radius: var(--r-sm); background: var(--bg-inset); }
@media (max-width: 900px) { .ro-row { grid-template-columns: 1fr; } }
.ro-label { font-size: 12.5px; color: var(--tx-2); }
.ro-value { font-size: 12.5px; color: var(--tx-1); }
.ro-hint { font-size: 11px; color: var(--tx-3); }
</style>
