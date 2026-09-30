<script setup>
/**
 * 关键词管理：词表的增删改查、批量导入与命中统计。
 *
 * 三档响应是本页的核心概念，因此把它做成最显眼的一行筛选：
 *   alarm     命中即报警（明确的伤害威胁与求救）
 *   warn      仅警告提示（侮辱/孤立/勒索/恐吓）
 *   highlight 仅高亮（可疑肢体动作，作为复核线索）
 *
 * 词表堆到几千条后，真正的风险不是漏报而是误报泛滥 ——
 * 因此"档位"比"词条数量"更值得被看见。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import Icon from '@/ui/Icon.vue'
import Modal from '@/ui/Modal.vue'
import { toast } from '@/ui/toast'
import { useAuthStore } from '@/stores/auth'
import api from '@/api'

const auth = useAuthStore()
/** 写操作后端要求 admin/operator；viewer 只读，前端提前置灰避免"点了必然 403" */
const canWrite = computed(() => auth.role === 'admin' || auth.role === 'operator')
const canDelete = computed(() => auth.role === 'admin')

const rows = ref([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = 50

const counts = reactive({ alarm: 0, warn: 0, highlight: 0, total: 0 })
const meta = reactive({ categories: [], category_labels: {}, level_labels: {} })

const level = ref('')       // 空 = 全部
const category = ref('')
const enabled = ref('')
const keyword = ref('')
const order = ref('hit')

const LEVELS = [
  { value: 'alarm', label: '报警', hint: '命中即触发报警' },
  { value: 'warn', label: '警告', hint: '仅警告提示，不报警' },
  { value: 'highlight', label: '高亮', hint: '仅高亮，作为复核线索' }
]

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

function qs() {
  const p = new URLSearchParams()
  p.set('offset', String((page.value - 1) * pageSize))
  p.set('limit', String(pageSize))
  p.set('order', order.value)
  if (level.value) p.set('level', level.value)
  if (category.value) p.set('category', category.value)
  if (enabled.value !== '') p.set('enabled', enabled.value)
  if (keyword.value.trim()) p.set('keyword', keyword.value.trim())
  return p.toString()
}

async function load() {
  loading.value = true
  try {
    const res = await api.get(`/api/keywords?${qs()}`)
    rows.value = res.items || []
    total.value = res.total || 0
    Object.assign(counts, res.counts || {})
  } catch { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

async function loadMeta() {
  try {
    const m = await api.get('/api/keywords/meta')
    Object.assign(meta, m)
  } catch { /* 静默 */ }
}

function search() { page.value = 1; load() }
function setLevel(v) { level.value = level.value === v ? '' : v; search() }
function go(p) { page.value = Math.min(totalPages.value, Math.max(1, p)); load() }

onMounted(async () => {
  await loadMeta()
  await load()
})

// ---------------- 新增 / 编辑 ----------------
const editOpen = ref(false)
const saving = ref(false)
const form = reactive({ id: null, word: '', category: 'custom', level: 'warn', note: '' })

function openCreate() {
  Object.assign(form, { id: null, word: '', category: 'custom', level: 'warn', note: '' })
  editOpen.value = true
}

function openEdit(r) {
  Object.assign(form, { id: r.id, word: r.word, category: r.category, level: r.level, note: r.note || '' })
  editOpen.value = true
}

async function save() {
  if (!form.word.trim()) { toast.warn('请输入关键词'); return }
  saving.value = true
  try {
    const body = { word: form.word.trim(), category: form.category, level: form.level, note: form.note || null }
    if (form.id) await api.patch(`/api/keywords/${form.id}`, body)
    else await api.post('/api/keywords', body)
    toast.ok(form.id ? '已更新' : '已新增')
    editOpen.value = false
    await load()
  } catch { /* 拦截器已提示 */ } finally {
    saving.value = false
  }
}

async function toggleEnabled(r) {
  try {
    await api.patch(`/api/keywords/${r.id}`, { enabled: !r.enabled })
    toast.ok(r.enabled ? '已停用' : '已启用')
    await load()
  } catch { /* 拦截器已提示 */ }
}

async function remove(r) {
  if (!canDelete.value) { toast.warn('仅管理员可删除词条'); return }
  try {
    await api.delete(`/api/keywords/${r.id}`)
    toast.ok('已删除')
    await load()
  } catch { /* 拦截器已提示（内置词会返回"请改为停用"） */ }
}

// ---------------- 批量导入 ----------------
const bulkOpen = ref(false)
const bulkText = ref('')
const bulkForm = reactive({ category: 'custom', level: 'warn' })
const bulkResult = ref(null)
const bulkSaving = ref(false)

async function doBulk() {
  if (!bulkText.value.trim()) { toast.warn('请粘贴词条'); return }
  bulkSaving.value = true
  try {
    bulkResult.value = await api.post('/api/keywords/bulk', {
      text: bulkText.value, category: bulkForm.category, level: bulkForm.level
    })
    toast.ok(`导入完成：新增 ${bulkResult.value.added} 条`)
    await load()
  } catch { /* 拦截器已提示 */ } finally {
    bulkSaving.value = false
  }
}

async function resetPreset() {
  try {
    const r = await api.post('/api/keywords/reset-preset')
    toast.ok(`已恢复内置词表（${r.changed} 条被还原）`)
    await load()
  } catch { /* 拦截器已提示 */ }
}

const catLabel = (c) => meta.category_labels[c] || c
</script>

<template>
  <div class="page">
    <div class="panel corner fade-up">
      <div class="panel-hd">
        <span class="panel-title">关键词管理</span>
        <div class="spacer" />
        <span class="panel-sub mono">共 {{ counts.total || total }} 条</span>
        <button class="btn btn--icon btn--sm btn--ghost" title="刷新" @click="load"><Icon name="refresh" /></button>
      </div>

      <!-- 三档概览：点击即筛选，档位比词条数量更值得被看见 -->
      <div class="level-bar">
        <button class="lv-chip" :class="{ on: level === '', }" @click="setLevel('')">
          <span class="lv-dot all" />全部 <b class="mono">{{ counts.total || 0 }}</b>
        </button>
        <button v-for="l in LEVELS" :key="l.value"
                class="lv-chip" :class="[`lv-${l.value}`, { on: level === l.value }]"
                :title="l.hint" @click="setLevel(l.value)">
          <span class="lv-dot" />{{ l.label }} <b class="mono">{{ counts[l.value] || 0 }}</b>
        </button>
        <div class="spacer" />
        <button class="btn btn--xs btn--primary" :disabled="!canWrite" @click="openCreate">
          <Icon name="plus" /> 新增词条
        </button>
        <button class="btn btn--xs" :disabled="!canWrite" @click="bulkOpen = true">
          <Icon name="upload" /> 批量导入
        </button>
        <button class="btn btn--xs btn--ghost" :disabled="!canDelete" @click="resetPreset">
          恢复内置词
        </button>
      </div>

      <!-- 筛选 -->
      <div class="filter-bar">
        <input v-model="keyword" class="input input--flush kw-search" placeholder="搜索词条…" @keyup.enter="search" />
        <select v-model="category" class="input input--flush kw-select" @change="search">
          <option value="">全部分类</option>
          <option v-for="c in meta.categories" :key="c" :value="c">{{ catLabel(c) }}</option>
        </select>
        <select v-model="enabled" class="input input--flush kw-select" @change="search">
          <option value="">全部状态</option>
          <option value="true">已启用</option>
          <option value="false">已停用</option>
        </select>
        <select v-model="order" class="input input--flush kw-select" @change="search">
          <option value="hit">按命中次数</option>
          <option value="new">按新增时间</option>
          <option value="word">按词条排序</option>
        </select>
        <button class="btn btn--xs btn--ghost" @click="search"><Icon name="search" /> 查询</button>
        <div class="spacer" />
        <span class="dim tiny">按命中次数排序 —— 最常命中的词最值得关注</span>
      </div>

      <div class="panel-bd flush">
        <div v-if="loading" class="empty"><Icon name="refresh" /><div class="t">加载中…</div></div>
        <div v-else-if="!rows.length" class="empty">
          <Icon name="mic" />
          <div class="t">当前筛选条件下没有词条</div>
        </div>
        <div v-else class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr>
                <th>词条</th>
                <th style="width: 110px">分类</th>
                <th style="width: 96px">响应档位</th>
                <th style="width: 96px">命中次数</th>
                <th style="width: 88px">状态</th>
                <th style="width: 150px">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in rows" :key="r.id" :class="{ off: !r.enabled }">
                <td>
                  <span class="kw-word">{{ r.word }}</span>
                  <span v-if="r.is_preset" class="kw-preset" title="内置词：可停用，不可删除">内置</span>
                  <div v-if="r.note" class="dim tiny ellip">{{ r.note }}</div>
                </td>
                <td><span class="tag tag--mute">{{ catLabel(r.category) }}</span></td>
                <td><span class="lv-tag" :class="`lv-${r.level}`">{{ meta.level_labels[r.level] || r.level }}</span></td>
                <td><span class="mono" :class="{ hot: r.hit_count > 20 }">{{ r.hit_count }}</span></td>
                <td>
                  <button class="mini-sw" :class="{ on: r.enabled }" :disabled="!canWrite"
                          :title="r.enabled ? '点击停用' : '点击启用'" @click="toggleEnabled(r)">
                    <span class="knob" />
                  </button>
                </td>
                <td>
                  <button class="btn btn--xs btn--ghost" :disabled="!canWrite" @click="openEdit(r)">
                    <Icon name="edit" /> 编辑
                  </button>
                  <button class="btn btn--xs btn--ghost danger" :disabled="!canDelete" title="删除词条"
                          @click="remove(r)">
                    <Icon name="trash" />
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="totalPages > 1" class="pager">
        <button class="btn btn--xs btn--ghost" :disabled="page <= 1" @click="go(page - 1)">上一页</button>
        <span class="mono">第 {{ page }} / {{ totalPages }} 页</span>
        <button class="btn btn--xs btn--ghost" :disabled="page >= totalPages" @click="go(page + 1)">下一页</button>
      </div>
    </div>

    <!-- 新增 / 编辑 -->
    <Modal v-model="editOpen" :title="form.id ? '编辑关键词' : '新增关键词'" max-width="480px">
      <div class="stack" style="gap: 14px">
        <div class="field">
          <label>关键词</label>
          <input v-model="form.word" class="input" placeholder="例如：打死你" maxlength="32" />
        </div>
        <div class="row" style="gap: 12px">
          <div class="field" style="flex: 1">
            <label>分类</label>
            <select v-model="form.category" class="input">
              <option v-for="c in meta.categories" :key="c" :value="c">{{ catLabel(c) }}</option>
            </select>
          </div>
          <div class="field" style="flex: 1">
            <label>响应档位</label>
            <select v-model="form.level" class="input">
              <option v-for="l in LEVELS" :key="l.value" :value="l.value">{{ l.label }}</option>
            </select>
          </div>
        </div>
        <p class="muted tiny">{{ (LEVELS.find((l) => l.value === form.level) || {}).hint }}</p>
        <div class="field">
          <label>备注（可选）</label>
          <input v-model="form.note" class="input" placeholder="为什么加这个词" maxlength="120" />
        </div>
      </div>
      <template #footer>
        <button class="btn btn--ghost" @click="editOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="saving" @click="save">
          <Icon name="check" /> {{ saving ? '提交中…' : '保存' }}
        </button>
      </template>
    </Modal>

    <!-- 批量导入 -->
    <Modal v-model="bulkOpen" title="批量导入关键词" max-width="560px">
      <div class="stack" style="gap: 14px">
        <div class="row" style="gap: 12px">
          <div class="field" style="flex: 1">
            <label>分类</label>
            <select v-model="bulkForm.category" class="input">
              <option v-for="c in meta.categories" :key="c" :value="c">{{ catLabel(c) }}</option>
            </select>
          </div>
          <div class="field" style="flex: 1">
            <label>响应档位</label>
            <select v-model="bulkForm.level" class="input">
              <option v-for="l in LEVELS" :key="l.value" :value="l.value">{{ l.label }}</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label>词条列表</label>
          <textarea v-model="bulkText" class="input kw-textarea" rows="8"
                    placeholder="每行一个词，也支持用顿号或逗号分隔&#10;例如：&#10;打你&#10;揍你&#10;孤立他" />
        </div>
        <p class="muted tiny">已存在的词会自动跳过，导入后会给出逐行结果，不会静默丢弃。</p>
        <div v-if="bulkResult" class="bulk-result">
          <div>新增 <b class="mono ok">{{ bulkResult.added }}</b> 条，跳过 <b class="mono">{{ bulkResult.skipped }}</b> 条</div>
          <ul v-if="bulkResult.details && bulkResult.details.length">
            <li v-for="(d, i) in bulkResult.details.slice(0, 8)" :key="i">
              <span class="mono">{{ d.word }}</span> — {{ d.reason }}
            </li>
          </ul>
        </div>
      </div>
      <template #footer>
        <button class="btn btn--ghost" @click="bulkOpen = false; bulkResult = null">关闭</button>
        <button class="btn btn--primary" :disabled="bulkSaving" @click="doBulk">
          <Icon name="upload" /> {{ bulkSaving ? '导入中…' : '开始导入' }}
        </button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
/* ---------- 三档概览条 ---------- */
.level-bar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
  background: var(--bg-inset);
}
.lv-chip {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 5px 11px; border-radius: 999px;
  border: 1px solid var(--line-2); background: var(--bg-raise);
  color: var(--tx-2); font-size: 12px; cursor: pointer;
  transition: border-color 0.16s, background 0.16s, color 0.16s;
}
.lv-chip:hover { border-color: var(--line-3); }
.lv-chip.on { border-color: var(--acc); color: var(--tx-1); background: rgba(47, 214, 240, 0.1); }
.lv-chip b { color: var(--tx-1); }
.lv-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--tx-3); }
.lv-chip.lv-alarm .lv-dot { background: #ff4d6d; }
.lv-chip.lv-warn .lv-dot { background: #ffb020; }
.lv-chip.lv-highlight .lv-dot { background: #9b7bff; }
.lv-chip.all .lv-dot { background: var(--acc); }

/* ---------- 筛选 ---------- */
.kw-search { width: 220px; }
.kw-select { width: 132px; }

/* ---------- 表格 ---------- */
.kw-word { font-weight: 600; color: var(--tx-1); }
.kw-preset {
  margin-left: 7px; padding: 1px 6px; border-radius: 4px;
  font-size: 10px; color: var(--tx-3);
  background: rgba(122, 162, 220, 0.14);
}
.lv-tag { padding: 2px 9px; border-radius: 5px; font-size: 11.5px; }
.lv-tag.lv-alarm { background: rgba(255, 77, 109, 0.16); color: #ff8fa3; }
.lv-tag.lv-warn { background: rgba(255, 176, 32, 0.16); color: #ffc861; }
.lv-tag.lv-highlight { background: rgba(155, 123, 255, 0.16); color: #b9a6ff; }
tr.off .kw-word { color: var(--tx-3); text-decoration: line-through; }
.mono.hot { color: #ff8fa3; font-weight: 600; }

/* 行内启停开关 */
.mini-sw {
  position: relative; width: 36px; height: 20px; padding: 0;
  border-radius: 999px; border: 1px solid var(--line-2);
  background: var(--bg-inset); cursor: pointer; transition: background 0.18s, border-color 0.18s;
}
.mini-sw .knob {
  position: absolute; top: 2px; left: 2px; width: 14px; height: 14px;
  border-radius: 50%; background: var(--tx-3); transition: transform 0.18s, background 0.18s;
}
.mini-sw.on { background: rgba(47, 214, 240, 0.2); border-color: var(--acc); }
.mini-sw.on .knob { transform: translateX(16px); background: var(--acc); }
.mini-sw:disabled { opacity: 0.5; cursor: not-allowed; }

.btn--ghost.danger:hover { color: #ff8fa3; border-color: rgba(255, 77, 109, 0.4); }

/* ---------- 批量导入 ---------- */
.kw-textarea { min-height: 150px; resize: vertical; line-height: 1.6; font-size: 12.5px; }
.bulk-result {
  padding: 10px 12px; border-radius: var(--r-sm);
  background: var(--bg-inset); border: 1px solid var(--line-2);
  font-size: 12px; color: var(--tx-2);
}
.bulk-result .ok { color: #6fe0f5; }
.bulk-result ul { margin: 7px 0 0; padding-left: 16px; color: var(--tx-3); }
.bulk-result li { margin-top: 3px; }

/* ---------- 分页 ---------- */
.pager {
  display: flex; align-items: center; justify-content: center; gap: 14px;
  padding: 10px; border-top: 1px solid var(--line);
  font-size: 12px; color: var(--tx-3);
}
</style>
