<template>
  <div class="page">
    <!-- 标签页：系统账号（可写）/ 三类人员档案（只读查看） -->
    <div class="panel fade-up uv-tabs">
      <button type="button" class="uv-tab" :class="{ on: tab === 'accounts' }" @click="tab = 'accounts'">
        <Icon name="lock" /><span>系统账号</span><b>{{ users.length }}</b>
      </button>
      <button v-for="t in PERSON_TABS" :key="t.key" type="button" class="uv-tab"
              :class="{ on: tab === t.key }" @click="tab = t.key">
        <Icon :name="t.icon" /><span>{{ t.label }}</span><b>{{ counts[t.key] ?? 0 }}</b>
      </button>
      <div class="spacer" />
      <span class="dim tiny">
        {{ tab === 'accounts' ? '登录账号与角色权限（可编辑）' : '人员档案只读查看；维护请前往「人员管理」' }}
      </span>
    </div>

    <!-- 人员档案：只读视角，与「人员管理」页复用同一组件 -->
    <div v-if="tab !== 'accounts'" class="panel fade-up d1">
      <div class="panel-bd">
        <PersonPanel :key="tab" :owner-type="tab" readonly />
      </div>
    </div>

    <div v-else class="panel corner fade-up">
      <div class="panel-hd">
        <span class="panel-title">用户与权限管理</span>
        <div class="spacer" />
        <span class="panel-sub mono">{{ users.length }} 个账号</span>
        <button class="btn btn--sm btn--primary" @click="openCreate"><Icon name="plus" /> 新增用户</button>
        <button class="btn btn--icon btn--sm btn--ghost" title="刷新" @click="load"><Icon name="refresh" /></button>
      </div>
      <div class="panel-bd flush">
        <div v-if="!users.length" class="empty">
          <Icon name="user" />
          <div class="t">暂无用户账号</div>
        </div>
        <div v-else class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr>
                <th style="width: 60px">ID</th>
                <th style="width: 160px">用户名</th>
                <th style="width: 160px">姓名</th>
                <th style="width: 130px">角色</th>
                <th style="width: 150px">状态</th>
                <th style="width: 190px">创建时间</th>
                <th style="width: 250px">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="u in users" :key="u.id">
                <td class="num">{{ u.id }}</td>
                <td class="mono">{{ u.username }}</td>
                <td>{{ u.full_name || '—' }}</td>
                <td><span class="tag" :class="roleTag[u.role] || 'tag--mute'">{{ roleZh[u.role] || u.role }}</span></td>
                <td>
                  <span class="row" style="gap: 9px">
                    <span class="c-dot" :class="{ off: u.disabled }" />
                    <span class="tiny" :class="u.disabled ? 'dim' : 'muted'">{{ u.disabled ? '停用' : '启用' }}</span>
                  </span>
                </td>
                <td class="num">{{ fmt(u.created_at) }}</td>
                <td>
                  <div class="btn-row">
                    <button class="btn btn--xs" @click="openEdit(u)"><Icon name="edit" /> 编辑</button>
                    <button class="btn btn--xs btn--ghost" @click="openPwd(u)"><Icon name="lock" /> 重置密码</button>
                    <label class="sw" :title="u.disabled ? '点击启用' : '点击停用'">
                      <input type="checkbox" :checked="!u.disabled" @change="toggle(u, $event.target.checked)" />
                      <span class="track"><span class="knob" /></span>
                    </label>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 新增用户 -->
    <Modal v-model="createOpen" title="新增用户" max-width="460px">
      <div class="stack" style="gap: 16px">
        <div class="field">
          <label>用户名</label>
          <input v-model.trim="createForm.username" class="input" placeholder="登录账号，需唯一" autocomplete="off" />
        </div>
        <div class="field">
          <label>姓名</label>
          <input v-model.trim="createForm.full_name" class="input" placeholder="真实姓名，如：张老师" />
        </div>
        <div class="field">
          <label>密码</label>
          <input v-model="createForm.password" class="input" type="password" placeholder="至少 8 位" autocomplete="new-password" />
        </div>
        <div class="field">
          <label>角色</label>
          <BaseSelect v-model="createForm.role" :options="roleOptions" placeholder="请选择角色" />
        </div>
        <p class="muted tiny">密码至少 8 位；用户名重复时后端会返回冲突提示。</p>
      </div>
      <template #footer>
        <button class="btn btn--ghost" @click="createOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="saving" @click="saveCreate">
          <Icon name="check" /> {{ saving ? '创建中…' : '创建' }}
        </button>
      </template>
    </Modal>

    <!-- 编辑用户 -->
    <Modal v-model="editOpen" title="编辑用户" max-width="460px">
      <div class="stack" style="gap: 16px">
        <div class="field">
          <label>用户名</label>
          <input class="input" :value="editForm.username" disabled />
        </div>
        <div class="field">
          <label>姓名</label>
          <input v-model.trim="editForm.full_name" class="input" placeholder="真实姓名" />
        </div>
        <div class="field">
          <label>角色</label>
          <BaseSelect v-model="editForm.role" :options="roleOptions" placeholder="请选择角色" />
        </div>
        <label class="sw">
          <input type="checkbox" v-model="editForm.enabled" />
          <span class="track"><span class="knob" /></span>
          <span class="sw-label">{{ editForm.enabled ? '启用该账号' : '停用该账号' }}</span>
        </label>
      </div>
      <template #footer>
        <button class="btn btn--ghost" @click="editOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="saving" @click="saveEdit">
          <Icon name="check" /> {{ saving ? '保存中…' : '保存' }}
        </button>
      </template>
    </Modal>

    <!-- 重置密码 -->
    <Modal v-model="pwdOpen" title="重置密码" max-width="440px">
      <div class="stack" style="gap: 16px">
        <div class="field">
          <label>目标账号</label>
          <input class="input" :value="pwdTarget?.username || ''" disabled />
        </div>
        <div class="field">
          <label>新密码</label>
          <input v-model="pwdForm.password" class="input" type="password" placeholder="至少 8 位" autocomplete="new-password" />
        </div>
        <p class="muted tiny">重置后请通知该用户尽快登录并自行修改密码。新密码至少 8 位。</p>
      </div>
      <template #footer>
        <button class="btn btn--ghost" @click="pwdOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="saving" @click="savePwdReset">
          <Icon name="check" /> {{ saving ? '提交中…' : '确认重置' }}
        </button>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import Icon from '@/ui/Icon.vue'
import Modal from '@/ui/Modal.vue'
import BaseSelect from '@/ui/BaseSelect.vue'
import PersonPanel from '@/components/PersonPanel.vue'
import { toast } from '@/ui/toast'
import { useAuthStore } from '@/stores/auth'
import api from '@/api'

const auth = useAuthStore()
const router = useRouter()

const users = ref([])
const saving = ref(false)
const createOpen = ref(false)
const editOpen = ref(false)
const pwdOpen = ref(false)
const pwdTarget = ref(null)

// 标签页：系统账号为可写视图，其余三类人员档案为只读查看视角。
// 复用 PersonPanel 的 readonly 模式，避免在这里再写一套人员表格。
const tab = ref('accounts')
const PERSON_TABS = [
  { key: 'student', label: '学生', icon: 'user' },
  { key: 'teacher', label: '教师', icon: 'user' },
  { key: 'staff', label: '管理人员', icon: 'shield' }
]
const counts = ref({})

async function loadCounts() {
  try {
    const meta = await api.get('/api/persons/meta')
    counts.value = meta.counts || {}
  } catch { /* 静默 */ }
}

const roleZh = { admin: '系统管理员', operator: '值班操作员', viewer: '观察员' }
const roleTag = { admin: 'tag--info', operator: 'tag--low', viewer: 'tag--mute' }
const roleOptions = [
  { value: 'admin', label: '系统管理员' },
  { value: 'operator', label: '值班操作员' },
  { value: 'viewer', label: '观察员' }
]

const createForm = reactive({ username: '', full_name: '', password: '', role: 'viewer' })
const editForm = reactive({ id: null, username: '', full_name: '', role: 'viewer', enabled: true })
const pwdForm = reactive({ password: '' })

const fmt = (t) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '—')

async function load() {
  try { users.value = await api.get('/api/auth/users') } catch { /* 拦截器已提示 */ }
}

function openCreate() {
  Object.assign(createForm, { username: '', full_name: '', password: '', role: 'viewer' })
  createOpen.value = true
}

async function saveCreate() {
  if (!createForm.username) { toast.warn('请填写用户名'); return }
  if (createForm.password.length < 8) { toast.warn('密码至少 8 位'); return }
  saving.value = true
  try {
    await api.post('/api/auth/users', {
      username: createForm.username,
      password: createForm.password,
      full_name: createForm.full_name,
      role: createForm.role
    })
    toast.ok('用户已创建')
    createOpen.value = false
    load()
  } catch { /* 拦截器已提示 */ } finally {
    saving.value = false
  }
}

function openEdit(u) {
  Object.assign(editForm, {
    id: u.id,
    username: u.username,
    full_name: u.full_name || '',
    role: u.role,
    enabled: !u.disabled
  })
  editOpen.value = true
}

async function saveEdit() {
  saving.value = true
  try {
    await api.patch(`/api/auth/users/${editForm.id}`, {
      full_name: editForm.full_name,
      role: editForm.role,
      disabled: !editForm.enabled
    })
    toast.ok('用户信息已保存')
    editOpen.value = false
    load()
  } catch { /* 拦截器已提示 */ } finally {
    saving.value = false
  }
}

function openPwd(u) {
  pwdTarget.value = u
  pwdForm.password = ''
  pwdOpen.value = true
}

async function savePwdReset() {
  if (pwdForm.password.length < 8) { toast.warn('新密码至少 8 位'); return }
  saving.value = true
  try {
    await api.post(`/api/auth/users/${pwdTarget.value.id}/password`, { new_password: pwdForm.password })
    toast.ok('密码已重置')
    pwdOpen.value = false
  } catch { /* 拦截器已提示 */ } finally {
    saving.value = false
  }
}

async function toggle(u, enabled) {
  try {
    await api.patch(`/api/auth/users/${u.id}`, { disabled: !enabled })
    u.disabled = !enabled
    toast.ok(enabled ? '已启用该账号' : '已停用该账号')
  } catch { /* 拦截器已提示 */ }
}

onMounted(() => {
  // 仅管理员可访问，非管理员直接退回总览
  if (auth.role !== 'admin') {
    toast.warn('仅系统管理员可访问用户与权限管理')
    router.replace('/dashboard')
    return
  }
  load()
  loadCounts()
})
</script>

<style scoped>
/* 标签页：与系统设置 / 人员管理页保持同一套视觉语言 */
.uv-tabs { display: flex; align-items: center; gap: 8px; padding: 9px 12px; margin-bottom: 14px; }
.uv-tabs .spacer { flex: 1; }
.uv-tab {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 7px 15px;
  border: 1px solid var(--line-2); border-radius: 999px;
  background: none; color: var(--tx-2);
  font-family: inherit; font-size: 13px; cursor: pointer;
  transition: border-color 0.16s, background 0.16s, color 0.16s;
}
.uv-tab > svg { width: 14px; height: 14px; }
.uv-tab > b { font-family: var(--font-mono); font-size: 11.5px; color: var(--tx-3); }
.uv-tab:hover { border-color: var(--line-3); background: var(--bg-raise); }
.uv-tab.on {
  color: #eafaff; border-color: rgba(47, 214, 240, 0.7);
  background: linear-gradient(135deg, rgba(47, 214, 240, 0.22), rgba(59, 130, 246, 0.1));
  box-shadow: 0 0 14px rgba(47, 214, 240, 0.2);
}
.uv-tab.on > b { color: #bfeaff; }
</style>
