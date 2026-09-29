<script setup>
/**
 * 应用外壳：单行顶栏（菱形导航分列标题两侧）+ 底部状态栏。
 *
 * 顶栏布局思路：
 *   8 个导航项按业务分组拆成左右各 4 个，以正菱形（旋转 45° 的方形）造型对称分列在
 *   系统大标题两侧——菱形本身既是导航入口，也充当标题的装饰，整体形成对称构图。
 *   标题与两侧菱形作为一个整体（.tb-core）绝对居中，因此无论右侧用户区多宽，
 *   标题始终落在屏幕正中；用户区单独绝对定位贴右，互不挤压。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from '@/ui/Icon.vue'
import Drawer from '@/ui/Drawer.vue'
import Modal from '@/ui/Modal.vue'
import StatusBar from '@/components/StatusBar.vue'
import LogPanel from '@/components/LogPanel.vue'
import { toast } from '@/ui/toast'
import { useAuthStore } from '@/stores/auth'
import api from '@/api'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

// 系统运行状态（设备 / 能力就绪 / 今日统计 / 日志计数）
const sysStatus = ref(null)
let statusTimer = null
let alarmTimer = null

const navOpen = ref(false)
const userOpen = ref(false)
const pwdOpen = ref(false)
const logOpen = ref(false)
const savingPwd = ref(false)
const userDdEl = ref(null)
const pwdForm = reactive({ old_password: '', new_password: '', confirm: '' })

const NAV = [
  { path: '/dashboard', label: '态势总览', icon: 'target', group: '监测' },
  { path: '/realtime', label: '实时检测', icon: 'video', group: '监测' },
  { path: '/video', label: '视频检测', icon: 'film', group: '监测' },
  { path: '/wall', label: '点位态势墙', icon: 'monitor', group: '监测' },
  { path: '/alarms', label: '报警处置', icon: 'bell', group: '处置' },
  { path: '/history', label: '历史取证', icon: 'folder', group: '处置' },
  { path: '/cameras', label: '摄像头管理', icon: 'sliders', group: '配置' },
  { path: '/users', label: '用户与权限', icon: 'user', group: '配置', adminOnly: true }
]

const roleZh = computed(() => ({ admin: '系统管理员', operator: '值班操作员', viewer: '观察员' }[auth.role] || auth.role))
const initial = computed(() => (auth.user?.full_name || auth.user?.username || '?').slice(0, 1))

/** 按角色过滤可见项 */
function visible(n) {
  return !(n.adminOnly && auth.role !== 'admin')
}

const navItems = computed(() => NAV.filter(visible))
/** 左翼：监测类；右翼：处置与配置类 —— 语义分组，同时满足左右各四项的对称布局 */
const leftNav = computed(() => navItems.value.filter((n) => n.group === '监测'))
const rightNav = computed(() => navItems.value.filter((n) => n.group !== '监测'))

/** 抽屉内导航按分组组织，信息结构更清晰 */
const groups = computed(() => {
  const out = []
  for (const n of navItems.value) {
    let g = out.find((x) => x.name === n.group)
    if (!g) { g = { name: n.group, items: [] }; out.push(g) }
    g.items.push(n)
  }
  return out
})

function go(path) {
  navOpen.value = false
  if (route.path !== path) router.push(path)
}

/** 标签页标题带上当前页面名，方便值班室多标签切换时辨认 */
watch(() => route.meta.title, (t) => {
  document.title = t ? `${t} · 守望` : '守望 · 校园反霸凌智能检测系统'
}, { immediate: true })

async function loadStatus() {
  try {
    sysStatus.value = await api.get('/api/system/status')
  } catch { /* 后端未就绪时保留上次状态 */ }
}

/* ---------------- 新报警的桌面通知与提示音 ----------------
 * 服务端已支持 Webhook 外发（企微/钉钉），这里补上浏览器侧的即时提醒：
 * 即使值班人员停留在历史或配置页，也能第一时间听到/看到新报警。
 */
const notifiedAlarmId = ref(0)

function beep() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext
    if (!Ctx) return
    const ctx = new Ctx()
    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.type = 'sine'
    osc.frequency.value = 880
    gain.gain.setValueAtTime(0.001, ctx.currentTime)
    gain.gain.exponentialRampToValueAtTime(0.22, ctx.currentTime + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.5)
    osc.connect(gain).connect(ctx.destination)
    osc.start()
    osc.stop(ctx.currentTime + 0.55)
    osc.onended = () => ctx.close()
  } catch { /* 未授权音频或不支持时静默 */ }
}

function notifyAlarm(reason) {
  beep()
  try {
    if (!('Notification' in window) || Notification.permission !== 'granted') return
    new Notification('守望 · 霸凌行为报警', { body: reason, tag: 'cab-alarm' })
  } catch { /* 静默 */ }
}

/** 轮询最新待处置报警；首次只记录基线，避免刷新页面即误弹 */
async function watchNewAlarms() {
  try {
    const page = await api.get('/api/alarms?status=pending&limit=1')
    const latest = page?.items?.[0]
    if (!latest) return
    if (notifiedAlarmId.value === 0) {
      notifiedAlarmId.value = latest.id
      return
    }
    if (latest.id > notifiedAlarmId.value) {
      notifiedAlarmId.value = latest.id
      loadStatus()
      notifyAlarm(latest.reason)
    }
  } catch { /* 静默 */ }
}

/** 浏览器要求通知权限由用户手势触发，这里在首次点击时申请一次 */
function askNotifyOnce() {
  try {
    if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission()
  } catch { /* 静默 */ }
  document.removeEventListener('click', askNotifyOnce)
}

async function logout() {
  userOpen.value = false
  navOpen.value = false
  try { await api.post('/api/auth/logout') } catch { /* 后端未就绪时仍继续本地登出 */ }
  auth.logout()
  router.push('/login')
}

function openPwd() {
  userOpen.value = false
  Object.assign(pwdForm, { old_password: '', new_password: '', confirm: '' })
  pwdOpen.value = true
}

async function savePwd() {
  if (!pwdForm.old_password || !pwdForm.new_password) { toast.warn('请填写原密码与新密码'); return }
  if (pwdForm.new_password.length < 8) { toast.warn('新密码至少 8 位'); return }
  if (pwdForm.new_password !== pwdForm.confirm) { toast.warn('两次输入的新密码不一致'); return }
  savingPwd.value = true
  try {
    await api.post('/api/auth/password', { old_password: pwdForm.old_password, new_password: pwdForm.new_password })
    toast.ok('密码修改成功')
    pwdOpen.value = false
  } catch { /* 拦截器已提示 */ } finally {
    savingPwd.value = false
  }
}

function onDocClick(e) {
  if (userDdEl.value && !userDdEl.value.contains(e.target)) userOpen.value = false
}

onMounted(() => {
  loadStatus()
  statusTimer = setInterval(loadStatus, 20000)
  watchNewAlarms()
  alarmTimer = setInterval(watchNewAlarms, 10000)
  document.addEventListener('click', askNotifyOnce)
  document.addEventListener('mousedown', onDocClick)
})

onUnmounted(() => {
  if (statusTimer) clearInterval(statusTimer)
  if (alarmTimer) clearInterval(alarmTimer)
  document.removeEventListener('click', askNotifyOnce)
  document.removeEventListener('mousedown', onDocClick)
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="tb-row1">
        <!-- 小屏导航入口（宽屏时导航为两侧菱形，此按钮隐藏） -->
        <button class="hamburger" aria-label="打开导航" @click="navOpen = true">
          <Icon name="layers" />
        </button>

        <!-- 标题与两侧菱形导航作为一个整体居中 -->
        <div class="tb-core">
          <div class="dnav-group">
            <button
              v-for="n in leftNav"
              :key="n.path"
              type="button"
              class="dnav"
              :class="{ on: route.path === n.path }"
              :title="n.label"
              @click="go(n.path)"
            >
              <span class="dnav-dia"><Icon :name="n.icon" /></span>
              <span class="dnav-label">{{ n.label }}</span>
            </button>
            <span class="orn orn--l" aria-hidden="true">
              <i class="orn-line" /><i class="orn-dot" /><i class="orn-dia" />
            </span>
          </div>

          <div class="tb-title">
            <span class="tb-title-cn">守望</span>
            <span class="tb-title-sub">校园反霸凌智能检测系统</span>
          </div>

          <div class="dnav-group">
            <span class="orn orn--r" aria-hidden="true">
              <i class="orn-dia" /><i class="orn-dot" /><i class="orn-line" />
            </span>
            <button
              v-for="n in rightNav"
              :key="n.path"
              type="button"
              class="dnav"
              :class="{ on: route.path === n.path }"
              :title="n.label"
              @click="go(n.path)"
            >
              <span class="dnav-dia"><Icon :name="n.icon" /></span>
              <span class="dnav-label">{{ n.label }}</span>
            </button>
          </div>
        </div>

        <!-- 右侧：用户模块（时钟在底部状态栏） -->
        <div class="tb-right">
          <div ref="userDdEl" class="dd user-dd" :class="{ open: userOpen }">
            <button type="button" class="dd-btn user-btn" @click="userOpen = !userOpen">
              <span class="who-avatar">{{ initial }}</span>
              <span class="u-name">{{ auth.user?.full_name || auth.user?.username || '未登录' }}</span>
              <Icon name="chev-down" class="caret" />
            </button>
            <div v-if="userOpen" class="dd-menu">
              <div class="dd-head">
                <div class="n ellip">{{ auth.user?.full_name || auth.user?.username || '未登录' }}</div>
                <div class="r">{{ roleZh }} · {{ auth.user?.username || '—' }}</div>
              </div>
              <div class="dd-opt" @click="openPwd">
                <Icon name="lock" style="width: 15px; height: 15px" /> 修改密码
              </div>
              <div v-if="auth.role === 'admin'" class="dd-opt" @click="go('/users')">
                <Icon name="user" style="width: 15px; height: 15px" /> 用户与权限
              </div>
              <div class="dd-opt" @click="logOpen = true">
                <Icon name="alert" style="width: 15px; height: 15px" /> 运行日志
              </div>
              <div class="dd-opt" @click="logout">
                <Icon name="logout" style="width: 15px; height: 15px" /> 退出登录
              </div>
            </div>
          </div>
        </div>
      </div>
    </header>

    <main class="viewport">
      <router-view />
    </main>

    <StatusBar :status="sysStatus" @open-logs="logOpen = true" @refresh="loadStatus" />

    <!-- 小屏抽屉导航 -->
    <Drawer v-model="navOpen" title="导航菜单">
      <div class="m-nav">
        <div class="brand">
          <div class="brand-mark"><Icon name="shield-alert" style="color: var(--acc)" /></div>
          <div>
            <div class="brand-name">守望</div>
            <div class="brand-sub">Campus Sentinel</div>
          </div>
        </div>

        <nav class="nav">
          <template v-for="g in groups" :key="g.name">
            <div class="upper nav-label">{{ g.name }}</div>
            <div
              v-for="n in g.items"
              :key="n.path"
              class="nav-item"
              :class="{ on: route.path === n.path }"
              @click="go(n.path)"
            >
              <Icon :name="n.icon" />
              <span>{{ n.label }}</span>
            </div>
          </template>
        </nav>

        <div class="rail-foot">
          <div class="who">
            <div class="who-avatar">{{ initial }}</div>
            <div style="min-width: 0">
              <div class="who-name ellip">{{ auth.user?.full_name || auth.user?.username || '未登录' }}</div>
              <div class="who-role">{{ roleZh }}</div>
            </div>
          </div>
          <button class="btn btn--ghost btn--sm btn--block" style="margin-top: 10px" @click="logout">
            <Icon name="logout" /> 退出登录
          </button>
        </div>
      </div>
    </Drawer>

    <!-- 运行日志与异常 -->
    <Drawer v-model="logOpen" title="运行日志与异常">
      <LogPanel v-if="logOpen" />
    </Drawer>

    <!-- 修改密码 -->
    <Modal v-model="pwdOpen" title="修改密码" max-width="440px">
      <div class="stack" style="gap: 16px">
        <div class="field">
          <label>原密码</label>
          <input v-model="pwdForm.old_password" class="input" type="password" autocomplete="current-password" placeholder="请输入当前密码" />
        </div>
        <div class="field">
          <label>新密码</label>
          <input v-model="pwdForm.new_password" class="input" type="password" autocomplete="new-password" placeholder="至少 8 位" />
        </div>
        <div class="field">
          <label>确认新密码</label>
          <input v-model="pwdForm.confirm" class="input" type="password" autocomplete="new-password" placeholder="请再次输入新密码" />
        </div>
        <p class="muted tiny">新密码需不少于 8 位，建议包含字母与数字组合。</p>
      </div>
      <template #footer>
        <button class="btn btn--ghost" @click="pwdOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="savingPwd" @click="savePwd">
          <Icon name="check" /> {{ savingPwd ? '提交中…' : '确认修改' }}
        </button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
/* .ellip 已收敛到全局 style.css 的通用工具类 */

/* 顶栏用户菜单 */
.user-dd { position: relative; }
.user-btn { width: auto; padding: 0 8px 0 6px; gap: 9px; }
.user-btn .who-avatar { width: 28px; height: 28px; border-radius: 8px; font-size: 12px; }
.user-btn .u-name { font-size: 13px; font-weight: 600; max-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.user-dd .dd-menu { left: auto; right: 0; width: 216px; }
.dd-head { padding: 8px 11px 11px; border-bottom: 1px solid var(--line); margin-bottom: 6px; }
.dd-head .n { font-size: 13.5px; font-weight: 600; }
.dd-head .r { font-size: 11.5px; color: var(--tx-3); }

/* 抽屉内导航排版 */
.m-nav { display: flex; flex-direction: column; }

/* 窄屏时收起用户名，为顶栏两侧的菱形导航让出空间；
   完整姓名仍可在下拉菜单与抽屉导航中看到，不存在信息缺失。 */
@media (max-width: 1280px) {
  .user-btn .u-name { display: none; }
}
</style>
