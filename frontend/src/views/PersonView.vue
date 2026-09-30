<script setup>
/**
 * 人员管理：学生 / 教师 / 管理人员三张花名册 + 人脸建档入口 + 合规自查。
 *
 * 为什么把「合规自查」放在这个页面的顶部：
 *   人脸识别最大的落地阻力不是技术，而是"学校自己说不清有没有做对"。
 *   把同意覆盖率、PIA 复评时间、告知标识这些指标直接摆在建档入口旁边，
 *   既方便日常自查，也便于对外说明。同意书模板与告知书一并在这里取。
 */
import { computed, onMounted, ref } from 'vue'
import Icon from '@/ui/Icon.vue'
import Modal from '@/ui/Modal.vue'
import PersonPanel from '@/components/PersonPanel.vue'
import { toast } from '@/ui/toast'
import api from '@/api'

const tab = ref('student')
const counts = ref({})
const overview = ref(null)
const faceStatus = ref(null)
const noticeOpen = ref(false)
const notice = ref(null)
const auditOpen = ref(false)
const auditRows = ref([])
const piaOpen = ref(false)
const piaRows = ref([])

const TABS = [
  { key: 'student', label: '学生', icon: 'user' },
  { key: 'teacher', label: '教师', icon: 'user' },
  { key: 'staff', label: '管理人员', icon: 'shield' }
]

const totalPeople = computed(() => (counts.value.student || 0) + (counts.value.teacher || 0) + (counts.value.staff || 0))
const consentPct = computed(() => {
  const o = overview.value
  if (!o || !o.people.total) return 0
  return Math.round((o.consent.granted / o.people.total) * 100)
})

async function loadAll() {
  try {
    const meta = await api.get('/api/persons/meta')
    counts.value = meta.counts || {}
  } catch { /* 拦截器已提示 */ }
  try { overview.value = await api.get('/api/compliance/overview') } catch { /* 静默 */ }
  try { faceStatus.value = await api.get('/api/faces/status') } catch { /* 静默 */ }
}

async function showNotice() {
  try {
    notice.value = await api.get('/api/compliance/notice')
    noticeOpen.value = true
  } catch { /* 拦截器已提示 */ }
}

async function showAudit() {
  try {
    const res = await api.get('/api/compliance/audit?limit=80')
    auditRows.value = res.items || []
    auditOpen.value = true
  } catch { /* 拦截器已提示 */ }
}

async function showPia() {
  try {
    const res = await api.get('/api/compliance/pia')
    piaRows.value = res.items || []
    piaOpen.value = true
  } catch { /* 拦截器已提示 */ }
}

async function rebuildIndex() {
  try {
    const res = await api.post('/api/faces/reindex')
    toast.ok(`索引已重建：${res.templates} 条模板`)
    loadAll()
  } catch { /* 拦截器已提示 */ }
}

function switchTab(k) {
  if (tab.value === k) return
  tab.value = k
}

onMounted(loadAll)
</script>

<template>
  <div class="page">
    <!-- 合规自查条：把"该做的事做了没有"变成一眼可读的数字 -->
    <div class="panel corner fade-up">
      <div class="panel-hd">
        <span class="panel-title">人员管理</span>
        <div class="spacer" />
        <span class="panel-sub mono">
          共 {{ totalPeople }} 人 · 已授权 {{ consentPct }}%
          <template v-if="faceStatus && faceStatus.index && faceStatus.index.persons">
            · 已建档 {{ faceStatus.index.persons }} 人
          </template>
        </span>
        <button class="btn btn--sm btn--ghost" @click="showNotice"><Icon name="lock" /> 告知同意书</button>
        <button class="btn btn--sm btn--ghost" @click="showPia"><Icon name="shield" /> PIA 台账</button>
        <button class="btn btn--sm btn--ghost" @click="showAudit"><Icon name="eye" /> 访问审计</button>
        <button class="btn btn--sm btn--ghost" title="人员或人脸变更后重建内存索引" @click="rebuildIndex">
          <Icon name="refresh" /> 重建索引
        </button>
      </div>

      <div class="panel-bd">
        <div class="cm-strip">
          <div class="cm-cell">
            <span class="cm-k">同意覆盖率</span>
            <b class="cm-v" :class="{ bad: consentPct < 60 }">{{ consentPct }}%</b>
          </div>
          <div class="cm-cell">
            <span class="cm-k">已授权</span><b class="cm-v">{{ overview?.consent.granted ?? '—' }}</b>
          </div>
          <div class="cm-cell">
            <span class="cm-k">已撤回</span><b class="cm-v">{{ overview?.consent.revoked ?? '—' }}</b>
          </div>
          <div class="cm-cell">
            <span class="cm-k">人脸模板</span><b class="cm-v">{{ overview?.face.templates ?? '—' }}</b>
          </div>
          <div class="cm-cell">
            <span class="cm-k">PIA 评估</span>
            <b class="cm-v" :class="{ bad: overview?.pia.expired, warn: !overview?.pia.has_record }">
              {{ overview?.pia.has_record ? (overview.pia.expired ? '已过期' : '有效') : '未开展' }}
            </b>
          </div>
          <div class="cm-cell cm-cell--wide">
            <span class="cm-k">识别引擎</span>
            <b class="cm-v" :class="{ bad: faceStatus && !faceStatus.identity.ready }">
              {{ faceStatus?.identity.ready ? '就绪' : '未就绪' }}
            </b>
            <span class="cm-hint tiny">{{ faceStatus?.identity.ready ? `SFace 128 维 · 阈值 ${faceStatus.config.threshold}` : (faceStatus?.identity.error || '') }}</span>
          </div>
        </div>

        <ul v-if="overview" class="cm-list">
          <li v-for="c in overview.checklist" :key="c.key" :class="{ ok: c.done === true, bad: c.done === false, todo: c.done === null }">
            <Icon :name="c.done === true ? 'check' : (c.done === false ? 'alert' : 'clock')" />
            <span class="cm-l">{{ c.label }}</span>
            <span class="cm-d tiny dim">{{ c.detail }}</span>
          </li>
        </ul>
      </div>
    </div>

    <!-- 花名册 -->
    <div class="panel fade-up d1">
      <div class="panel-hd">
        <div class="pp-tabs">
          <button
            v-for="t in TABS"
            :key="t.key"
            type="button"
            class="pp-tab"
            :class="{ on: tab === t.key }"
            @click="switchTab(t.key)"
          >
            <Icon :name="t.icon" />
            <span>{{ t.label }}</span>
            <b>{{ counts[t.key] ?? 0 }}</b>
          </button>
        </div>
      </div>
      <div class="panel-bd">
        <PersonPanel :key="tab" :owner-type="tab" @changed="loadAll" />
      </div>
    </div>

    <!-- 告知同意书 -->
    <Modal v-model="noticeOpen" title="人脸信息处理告知同意书" max-width="720px">
      <p class="muted tiny" style="margin: 0 0 10px">{{ notice?.note }}</p>
      <pre class="cm-pre">{{ notice?.content || '加载中…' }}</pre>
      <template #footer>
        <button class="btn btn--ghost" @click="noticeOpen = false">关闭</button>
      </template>
    </Modal>

    <!-- PIA 台账 -->
    <Modal v-model="piaOpen" title="个人信息保护影响评估（PIA）台账" max-width="720px">
      <p class="muted tiny" style="margin: 0 0 10px">
        依据《人脸识别技术应用安全管理办法》第九条：事前开展评估并记录处理情况，
        报告与记录至少保存 3 年；目的或方式变更、发生重大安全事件时应重新评估。
      </p>
      <div v-if="piaRows.length" class="cm-pia">
        <div v-for="p in piaRows" :key="p.id" class="cm-pia-row">
          <div class="cm-pia-hd">
            <b>{{ p.version }}</b>
            <span class="tag" :class="p.expired ? 'tag--high' : 'tag--ok'">{{ p.expired ? '已过期' : '有效' }}</span>
            <div class="spacer" />
            <span class="tiny dim mono">{{ (p.created_at || '').slice(0, 10) }}</span>
          </div>
          <div class="tiny muted">范围：{{ p.scope }}</div>
          <div v-if="p.conclusion" class="tiny muted">结论：{{ p.conclusion }}</div>
          <div v-if="p.reviewer" class="tiny dim">评估人：{{ p.reviewer }}</div>
        </div>
      </div>
      <div v-else class="empty" style="padding: 30px">
        <Icon name="shield" />
        <div class="t">尚未开展评估</div>
        <div class="dim tiny" style="margin-top: 6px">可通过 POST /api/compliance/pia 登记评估记录</div>
      </div>
      <template #footer>
        <button class="btn btn--ghost" @click="piaOpen = false">关闭</button>
      </template>
    </Modal>

    <!-- 访问审计 -->
    <Modal v-model="auditOpen" title="敏感数据访问审计" max-width="760px">
      <p class="muted tiny" style="margin: 0 0 10px">谁在什么时候查询/修改过哪些人员信息与人脸数据，用于合规问询时回溯。</p>
      <div v-if="auditRows.length" class="cm-audit">
        <div v-for="a in auditRows" :key="a.id" class="cm-audit-row">
          <span class="tag tag--mute">{{ a.action }}</span>
          <span class="tiny">{{ a.actor || '—' }}</span>
          <span class="tiny dim">{{ a.target }}</span>
          <span class="tiny muted ellip">{{ a.detail }}</span>
          <div class="spacer" />
          <span class="tiny dim mono">{{ (a.created_at || '').replace('T', ' ').slice(5, 19) }}</span>
        </div>
      </div>
      <div v-else class="empty" style="padding: 30px"><Icon name="eye" /><div class="t">暂无审计记录</div></div>
      <template #footer>
        <button class="btn btn--ghost" @click="auditOpen = false">关闭</button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
/* 合规自查条：横向指标卡，紧凑但可读 */
.cm-strip {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
}
.cm-cell {
  display: flex; flex-direction: column; gap: 3px;
  padding: 9px 12px;
  border: 1px solid var(--line-2); border-radius: var(--r-md);
  background: linear-gradient(135deg, rgba(47, 214, 240, 0.05), rgba(59, 130, 246, 0.02));
}
.cm-cell--wide { grid-column: span 2; }
.cm-k { font-size: 11px; color: var(--tx-3); letter-spacing: 0.04em; }
.cm-v { font-size: 19px; font-weight: 700; font-family: var(--font-mono); color: var(--acc); line-height: 1.1; }
.cm-v.bad { color: var(--danger); }
.cm-v.warn { color: var(--warn); }
.cm-hint { font-size: 10.5px; color: var(--tx-3); }

.cm-list { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 18px; margin: 14px 0 0; padding: 0; list-style: none; }
.cm-list li { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.cm-list li > svg { width: 13px; height: 13px; flex-shrink: 0; }
.cm-list li.ok > svg { color: var(--ok); }
.cm-list li.bad > svg { color: var(--danger); }
.cm-list li.todo > svg { color: var(--warn); }
.cm-l { color: var(--tx-2); }
.cm-d { margin-left: auto; text-align: right; }

/* 花名册 Tab：与全局 tag 风格一致 */
.pp-tabs { display: flex; align-items: center; gap: 8px; }
.pp-tab {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 7px 15px;
  border: 1px solid var(--line-2); border-radius: 999px;
  background: none; color: var(--tx-2);
  font-family: inherit; font-size: 13px; cursor: pointer;
  transition: border-color 0.16s, background 0.16s, color 0.16s;
}
.pp-tab > svg { width: 14px; height: 14px; }
.pp-tab > b { font-family: var(--font-mono); font-size: 11.5px; color: var(--tx-3); }
.pp-tab:hover { border-color: var(--line-3); background: var(--bg-raise); }
.pp-tab.on {
  color: #eafaff; border-color: rgba(47, 214, 240, 0.7);
  background: linear-gradient(135deg, rgba(47, 214, 240, 0.22), rgba(59, 130, 246, 0.1));
  box-shadow: 0 0 14px rgba(47, 214, 240, 0.2);
}
.pp-tab.on > b { color: #bfeaff; }

.cm-pre {
  max-height: 52vh; overflow: auto; margin: 0;
  padding: 14px 16px; border-radius: var(--r-md);
  border: 1px solid var(--line-2); background: var(--bg-inset);
  font-family: var(--font-body); font-size: 12.5px; line-height: 1.75;
  color: var(--tx-2); white-space: pre-wrap;
}

.cm-pia { display: flex; flex-direction: column; gap: 10px; }
.cm-pia-row { padding: 10px 12px; border: 1px solid var(--line-2); border-radius: var(--r-md); }
.cm-pia-hd { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
.cm-pia-hd .spacer { flex: 1; }

.cm-audit { display: flex; flex-direction: column; max-height: 50vh; overflow: auto; }
.cm-audit-row { display: flex; align-items: center; gap: 9px; padding: 6px 2px; border-bottom: 1px solid var(--line); }
.cm-audit-row .spacer { flex: 1; }
.cm-audit-row .ellip { max-width: 240px; }

@media (max-width: 1200px) {
  .cm-strip { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .cm-cell--wide { grid-column: span 3; }
  .cm-list { grid-template-columns: 1fr; }
}
</style>
