<script setup>
/**
 * 人员面板：学生 / 教师 / 管理人员的列表、筛选、增删改、单图人脸建档。
 *
 * 抽成组件的理由：三种人员类型的业务字段不同，但**交互完全一致**。
 * 如果各写一遍，就会出现三套分页、三套筛选、三套建档向导 —— 后续改一处要改三遍，
 * 且必然逐渐不一致。这里靠 ownerType 驱动字段差异，只写一份。
 *
 * readonly 用于「用户与权限」页的人员查看视角：那里是只读的
 * （权限页的职责是账号与角色，不该顺带改花名册）。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import Icon from '@/ui/Icon.vue'
import Modal from '@/ui/Modal.vue'
import BaseSelect from '@/ui/BaseSelect.vue'
import FileDrop from '@/ui/FileDrop.vue'
import { toast } from '@/ui/toast'
import { confirm } from '@/ui/confirm'
import api from '@/api'

const props = defineProps({
  ownerType: { type: String, required: true },   // student / teacher / staff
  readonly: { type: Boolean, default: false }
})
const emit = defineEmits(['changed'])

const TYPE_META = {
  student: { label: '学生', noLabel: '学号', icon: 'user', noPlaceholder: '如 20230143' },
  teacher: { label: '教师', noLabel: '工号', icon: 'user', noPlaceholder: '如 T2019007' },
  staff: { label: '管理人员', noLabel: '工号', icon: 'shield', noPlaceholder: '如 S2021001' }
}
const meta = computed(() => TYPE_META[props.ownerType] || TYPE_META.student)

const rows = ref([])
const total = ref(0)
const page = ref(1)
const limit = ref(20)
const loading = ref(false)
const metaInfo = ref(null)
const classes = ref([])
const saving = ref(false)

const filters = reactive({ q: '', class_id: '', status: '', consent: '', has_face: '' })

const pages = computed(() => Math.max(1, Math.ceil(total.value / limit.value)))

// ---------- 枚举（由后端下发，避免前端硬编码状态值）----------
const statusOptions = computed(() => {
  const map = metaInfo.value?.statuses?.[props.ownerType] || {}
  return Object.entries(map).map(([value, label]) => ({ value, label }))
})
const genderOptions = (metaInfo.value?.genders || ['男', '女', '未填']).map((g) => ({ value: g, label: g }))
const consentOptions = computed(() => [
  { value: '', label: '全部同意状态' },
  ...(metaInfo.value?.consent_statuses || []).map((c) => ({ value: c.value, label: c.label }))
])
const faceOptions = [
  { value: '', label: '全部建档状态' },
  { value: 'true', label: '已建档' },
  { value: 'false', label: '未建档' }
]
const classOptions = computed(() => [
  { value: '', label: '全部班级' },
  ...classes.value.map((c) => ({ value: String(c.id), label: `${c.name}（${c.student_count}）` }))
])
const consentSubjectOptions = [
  { value: 'self', label: '本人同意' },
  { value: 'guardian', label: '监护人同意（不满 14 周岁必须选此项）' }
]

async function loadMeta() {
  try {
    metaInfo.value = await api.get('/api/persons/meta')
  } catch { /* 拦截器已提示 */ }
}

async function loadClasses() {
  try { classes.value = await api.get('/api/classes') } catch { /* 静默 */ }
}

async function load() {
  loading.value = true
  try {
    const qs = new URLSearchParams({ owner_type: props.ownerType, page: String(page.value), limit: String(limit.value) })
    if (filters.q) qs.set('q', filters.q)
    if (filters.class_id) qs.set('class_id', filters.class_id)
    if (filters.status) qs.set('status', filters.status)
    if (filters.consent) qs.set('consent', filters.consent)
    if (filters.has_face) qs.set('has_face', filters.has_face)
    const res = await api.get(`/api/persons?${qs}`)
    rows.value = res.items || []
    total.value = res.total || 0
  } catch { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

function search() { page.value = 1; load() }
function resetFilters() {
  Object.assign(filters, { q: '', class_id: '', status: '', consent: '', has_face: '' })
  page.value = 1
  load()
}
function goto(p) {
  page.value = Math.min(pages.value, Math.max(1, p))
  load()
}

// ---------- 新增 / 编辑 ----------
const editOpen = ref(false)
const form = reactive({
  id: null, no: '', name: '', gender: '未填', status: 'active', note: '',
  class_id: '', enroll_year: '', guardian_name: '', guardian_phone: '', guardian_relation: '',
  department: '', title: '', subject: '', position: '', phone: ''
})

function openCreate() {
  Object.assign(form, {
    id: null, no: '', name: '', gender: '未填', status: 'active', note: '',
    class_id: '', enroll_year: '', guardian_name: '', guardian_phone: '', guardian_relation: '',
    department: '', title: '', subject: '', position: '', phone: ''
  })
  editOpen.value = true
}

function openEdit(r) {
  Object.assign(form, {
    id: r.id, no: r.no, name: r.name, gender: r.gender || '未填', status: r.status, note: r.note || '',
    class_id: r.class_id ? String(r.class_id) : '',
    enroll_year: r.enroll_year || '',
    guardian_name: r.guardian_name || '', guardian_phone: r.guardian_phone || '',
    guardian_relation: r.guardian_relation || '',
    department: r.department || '', title: r.title || '', subject: r.subject || '',
    position: r.position || '', phone: r.phone || ''
  })
  editOpen.value = true
}

async function savePerson() {
  if (!form.name.trim()) { toast.warn('请填写姓名'); return }
  if (!form.id && !form.no.trim()) { toast.warn(`请填写${meta.value.noLabel}`); return }
  saving.value = true
  const body = {
    owner_type: props.ownerType,
    name: form.name.trim(),
    gender: form.gender,
    status: form.status,
    note: form.note || null
  }
  if (form.no.trim()) body.no = form.no.trim()
  if (props.ownerType === 'student') {
    body.class_id = form.class_id ? Number(form.class_id) : null
    body.enroll_year = form.enroll_year ? Number(form.enroll_year) : null
    body.guardian_name = form.guardian_name || null
    body.guardian_phone = form.guardian_phone || null
    body.guardian_relation = form.guardian_relation || null
  } else {
    body.department = form.department || null
    body.phone = form.phone || null
    if (props.ownerType === 'teacher') {
      body.title = form.title || null
      body.subject = form.subject || null
    } else {
      body.position = form.position || null
    }
  }
  try {
    if (form.id) await api.patch(`/api/persons/${props.ownerType}/${form.id}`, body)
    else await api.post('/api/persons', body)
    toast.ok(form.id ? '已保存' : '已新增')
    editOpen.value = false
    await Promise.all([load(), loadClasses()])
    emit('changed')
  } catch { /* 拦截器已提示 */ } finally {
    saving.value = false
  }
}

async function removePerson(r) {
  const ok = await confirm({
    title: '删除人员',
    message: `确定删除「${r.name}」吗？其人脸特征与注册照片会一并清除，且不可恢复。`,
    danger: true
  })
  if (!ok) return
  try {
    await api.delete(`/api/persons/${props.ownerType}/${r.id}`)
    toast.ok('已删除')
    await load()
    emit('changed')
  } catch { /* 拦截器已提示 */ }
}

// ---------- 人脸建档（单图向导）----------
const faceOpen = ref(false)
const faceTarget = ref(null)
const faceFile = ref(null)
const faceConsentSubject = ref('self')
const faceReplace = ref(false)
const faceResult = ref(null)
const faceBusy = ref(false)
const faceStatus = ref(null)

async function loadFaceStatus() {
  try { faceStatus.value = await api.get('/api/faces/status') } catch { /* 静默 */ }
}

function openFace(r) {
  faceTarget.value = r
  faceFile.value = null
  faceResult.value = null
  // 已有授权的不再问一次；未建档的默认按监护人同意（初中及以下学生占多数）
  faceConsentSubject.value = r.face?.consent_subject || (props.ownerType === 'student' ? 'guardian' : 'self')
  faceReplace.value = false
  faceOpen.value = true
  if (!faceStatus.value) loadFaceStatus()
}

function onFaceFile(f) { faceFile.value = f; faceResult.value = null }

async function submitFace() {
  if (!faceFile.value) { toast.warn('请先选择一张照片'); return }
  faceBusy.value = true
  const fd = new FormData()
  fd.append('owner_type', props.ownerType)
  fd.append('person_id', String(faceTarget.value.id))
  fd.append('consent_subject', faceConsentSubject.value)
  fd.append('consent_method', 'electronic')
  fd.append('replace', String(faceReplace.value))
  fd.append('file', faceFile.value)
  try {
    const res = await api.post('/api/faces/enroll', fd)
    faceResult.value = res
    if (res.ok) {
      toast.ok(`已为 ${faceTarget.value.name} 完成人脸建档`)
      await load()
      emit('changed')
    } else {
      toast.warn(res.reasons?.[0] || '建档未通过')
    }
  } catch { /* 拦截器已提示 */ } finally {
    faceBusy.value = false
  }
}

async function deleteFace(r) {
  const ok = await confirm({
    title: '注销人脸',
    message: `确定注销「${r.name}」的人脸建档吗？将删除全部人脸特征与照片，人员档案保留。`,
    danger: true
  })
  if (!ok) return
  try {
    await api.delete(`/api/faces/${props.ownerType}/${r.id}`)
    toast.ok('已注销人脸')
    await load()
    emit('changed')
  } catch { /* 拦截器已提示 */ }
}

async function revokeConsent(r) {
  const ok = await confirm({
    title: '撤回人脸信息处理同意',
    message: `撤回后将**立即删除**「${r.name}」的全部人脸特征与照片，且不再参与识别。确定继续吗？`,
    danger: true
  })
  if (!ok) return
  try {
    await api.post(`/api/persons/${props.ownerType}/${r.id}/consent`, {
      granted: false, subject: r.face?.consent_subject || 'self', method: 'electronic',
      note: '人员档案页撤回'
    })
    toast.ok('已撤回同意并清除人脸数据')
    await load()
    emit('changed')
  } catch { /* 拦截器已提示 */ }
}

// ---------- CSV 导入 ----------
const importOpen = ref(false)
const importFile = ref(null)
const importResult = ref(null)

function openImport() {
  importFile.value = null
  importResult.value = null
  importOpen.value = true
}

function onImportFile(f) { importFile.value = f; importResult.value = null }

function downloadTemplate() {
  const header = props.ownerType === 'student'
    ? '学号,姓名,性别,班级,入学年份,监护人,监护人电话,与监护人关系,状态'
    : (props.ownerType === 'teacher' ? '工号,姓名,性别,部门,职称,任教科目,电话,状态' : '工号,姓名,性别,部门,职务,电话,状态')
  const sample = props.ownerType === 'student'
    ? '20230143,张三,男,初二(3)班,2023,张父,13800000000,父,active'
    : 'T2019007,李老师,女,语文组,一级教师,语文,13800000000,active'
  const blob = new Blob(['\ufeff' + header + '\n' + sample + '\n'], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${meta.value.label}导入模板.csv`
  a.click()
  URL.revokeObjectURL(url)
}

async function submitImport() {
  if (!importFile.value) { toast.warn('请选择 CSV 文件'); return }
  saving.value = true
  const fd = new FormData()
  fd.append('file', importFile.value)
  try {
    const res = await api.post(`/api/persons/import?owner_type=${props.ownerType}`, fd)
    importResult.value = res
    toast.ok(`导入完成：新增 ${res.created}，更新 ${res.updated}，跳过 ${res.skipped_count}`)
    await Promise.all([load(), loadClasses()])
    emit('changed')
  } catch { /* 拦截器已提示 */ } finally {
    saving.value = false
  }
}

// ---------- 详情 ----------
const detailOpen = ref(false)
const detail = ref(null)
const detailConsents = ref([])

async function openDetail(r) {
  try {
    const [person, consents] = await Promise.all([
      api.get(`/api/persons/${props.ownerType}/${r.id}`),
      api.get(`/api/persons/${props.ownerType}/${r.id}/consents`).catch(() => [])
    ])
    detail.value = person
    detailConsents.value = consents || []
    detailOpen.value = true
  } catch { /* 拦截器已提示 */ }
}

function statusTag(s) {
  return s === 'active' ? 'tag--ok' : 'tag--mute'
}
function consentTag(c) {
  return c === 'granted' ? 'tag--ok' : (c === 'revoked' ? 'tag--high' : 'tag--mute')
}
function avatarOf(r) { return r.face?.avatar_url || '' }
function genderText(g) { return g || '—' }
function lineOf(r) {
  if (props.ownerType === 'student') return r.class_name || '未分班'
  return r.department || (r.title || r.position) || '—'
}

watch(() => props.ownerType, () => {
  page.value = 1
  resetFilters()
})

onMounted(() => {
  loadMeta().then(loadClasses).then(load)
})
</script>

<template>
  <div class="pp">
    <!-- 工具栏 -->
    <div class="pp-bar">
      <div class="pp-search">
        <Icon name="search" />
        <input
          v-model.trim="filters.q"
          :placeholder="`搜索姓名 / ${meta.noLabel}`"
          @keyup.enter="search"
        />
      </div>
      <BaseSelect v-if="ownerType === 'student'" v-model="filters.class_id" :options="classOptions" style="width: 170px" />
      <BaseSelect v-model="filters.status" :options="[{ value: '', label: '全部状态' }, ...statusOptions]" style="width: 130px" />
      <BaseSelect v-model="filters.consent" :options="consentOptions" style="width: 150px" />
      <BaseSelect v-model="filters.has_face" :options="faceOptions" style="width: 140px" />
      <button class="btn btn--sm" @click="search"><Icon name="search" /> 查询</button>
      <button class="btn btn--sm btn--ghost" @click="resetFilters"><Icon name="refresh" /> 重置</button>
      <div class="spacer" />
      <template v-if="!readonly">
        <button class="btn btn--sm btn--ghost" @click="openImport"><Icon name="upload" /> 批量导入</button>
        <button class="btn btn--sm btn--primary" @click="openCreate"><Icon name="plus" /> 新增{{ meta.label }}</button>
      </template>
    </div>

    <!-- 列表 -->
    <div v-if="loading && !rows.length" class="empty" style="padding: 40px">
      <Icon name="refresh" /><div class="t">加载中…</div>
    </div>
    <div v-else-if="!rows.length" class="empty" style="padding: 40px">
      <Icon :name="meta.icon" />
      <div class="t">暂无{{ meta.label }}数据</div>
      <div class="dim tiny" style="margin-top: 6px">可通过「新增{{ meta.label }}」或「批量导入」建立档案</div>
    </div>
    <div v-else class="tbl-wrap">
      <table class="tbl">
        <thead>
          <tr>
            <th style="width: 58px">头像</th>
            <th style="width: 110px">姓名</th>
            <th style="width: 120px">{{ meta.noLabel }}</th>
            <th style="width: 66px">性别</th>
            <th style="width: 160px">{{ ownerType === 'student' ? '班级' : '部门' }}</th>
            <th style="width: 90px">状态</th>
            <th style="width: 120px">人脸建档</th>
            <th style="width: 120px">同意状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>
              <div class="pp-ava">
                <img v-if="avatarOf(r)" :src="avatarOf(r)" :alt="r.name" loading="lazy" />
                <span v-else class="pp-ava-ph"><Icon name="user" /></span>
              </div>
            </td>
            <td><b>{{ r.name }}</b></td>
            <td class="mono">{{ r.no }}</td>
            <td>{{ genderText(r.gender) }}</td>
            <td>{{ lineOf(r) }}</td>
            <td><span class="tag" :class="statusTag(r.status)">{{ r.status_label }}</span></td>
            <td>
              <span v-if="r.face.enrolled" class="tag tag--ok" :title="`质量分 ${r.face.quality}`">
                {{ r.face.template_count }} 张模板
              </span>
              <span v-else class="tag tag--mute">未建档</span>
            </td>
            <td>
              <span class="tag" :class="consentTag(r.face.consent_status)">
                {{ r.face.consent_label }}<template v-if="r.face.consent_subject_label">·{{ r.face.consent_subject_label }}</template>
              </span>
            </td>
            <td>
              <div class="btn-row">
                <button class="btn btn--xs btn--ghost" @click="openDetail(r)"><Icon name="eye" /> 详情</button>
                <template v-if="!readonly">
                  <button class="btn btn--xs" @click="openEdit(r)"><Icon name="edit" /> 编辑</button>
                  <button class="btn btn--xs btn--primary" @click="openFace(r)">
                    <Icon name="camera" /> {{ r.face.enrolled ? '补拍' : '录人脸' }}
                  </button>
                  <button v-if="r.face.consent_status === 'granted'" class="btn btn--xs btn--ghost" title="撤回同意并删除人脸数据" @click="revokeConsent(r)">
                    <Icon name="lock" /> 撤回
                  </button>
                  <button v-if="r.face.enrolled" class="btn btn--xs btn--ghost" title="注销人脸（保留人员档案）" @click="deleteFace(r)">
                    <Icon name="trash" />
                  </button>
                  <button class="btn btn--xs btn--danger" title="删除人员" @click="removePerson(r)"><Icon name="trash" /></button>
                </template>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 分页 -->
    <div v-if="total > limit" class="pp-page">
      <span class="dim tiny">共 {{ total }} 条 · 第 {{ page }} / {{ pages }} 页</span>
      <div class="spacer" />
      <button class="btn btn--xs btn--ghost" :disabled="page <= 1" @click="goto(1)">首页</button>
      <button class="btn btn--xs btn--ghost" :disabled="page <= 1" @click="goto(page - 1)">上一页</button>
      <button class="btn btn--xs btn--ghost" :disabled="page >= pages" @click="goto(page + 1)">下一页</button>
      <button class="btn btn--xs btn--ghost" :disabled="page >= pages" @click="goto(pages)">末页</button>
    </div>

    <!-- 新增 / 编辑 -->
    <Modal v-model="editOpen" :title="(form.id ? '编辑' : '新增') + meta.label" max-width="640px">
      <div class="pp-form">
        <div class="field">
          <label>{{ meta.noLabel }}</label>
          <input v-model.trim="form.no" class="input" :placeholder="meta.noPlaceholder" />
        </div>
        <div class="field">
          <label>姓名</label>
          <input v-model.trim="form.name" class="input" placeholder="真实姓名" />
        </div>
        <div class="field">
          <label>性别</label>
          <BaseSelect v-model="form.gender" :options="genderOptions" />
        </div>
        <div class="field">
          <label>状态</label>
          <BaseSelect v-model="form.status" :options="statusOptions" />
        </div>

        <template v-if="ownerType === 'student'">
          <div class="field">
            <label>班级</label>
            <BaseSelect v-model="form.class_id" :options="classOptions.filter(c => c.value)" placeholder="未分班" clearable />
          </div>
          <div class="field">
            <label>入学年份</label>
            <input v-model.trim="form.enroll_year" class="input" placeholder="如 2023" />
          </div>
          <div class="field">
            <label>监护人</label>
            <input v-model.trim="form.guardian_name" class="input" placeholder="监护人姓名" />
          </div>
          <div class="field">
            <label>监护人电话</label>
            <input v-model.trim="form.guardian_phone" class="input" placeholder="联系电话" />
          </div>
          <div class="field">
            <label>与监护人关系</label>
            <input v-model.trim="form.guardian_relation" class="input" placeholder="父 / 母 / 其他" />
          </div>
        </template>
        <template v-else>
          <div class="field">
            <label>部门</label>
            <input v-model.trim="form.department" class="input" placeholder="如 语文组 / 德育处" />
          </div>
          <div v-if="ownerType === 'teacher'" class="field">
            <label>职称</label>
            <input v-model.trim="form.title" class="input" placeholder="如 一级教师" />
          </div>
          <div v-if="ownerType === 'teacher'" class="field">
            <label>任教科目</label>
            <input v-model.trim="form.subject" class="input" placeholder="如 语文" />
          </div>
          <div v-if="ownerType === 'staff'" class="field">
            <label>职务</label>
            <input v-model.trim="form.position" class="input" placeholder="如 德育处主任" />
          </div>
          <div class="field">
            <label>电话</label>
            <input v-model.trim="form.phone" class="input" placeholder="联系电话" />
          </div>
        </template>

        <div class="field pp-form-full">
          <label>备注</label>
          <input v-model.trim="form.note" class="input" placeholder="选填" />
        </div>
      </div>
      <p class="muted tiny" style="margin-top: 10px">
        人员档案与人脸特征是两件事：先建档案，再单独「录人脸」。人脸识别不是唯一方式，
        未建档的人员依然可以正常参与检测与报警。
      </p>
      <template #footer>
        <button class="btn btn--ghost" @click="editOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="saving" @click="savePerson">
          <Icon name="check" /> {{ saving ? '保存中…' : '保存' }}
        </button>
      </template>
    </Modal>

    <!-- 人脸建档向导 -->
    <Modal v-model="faceOpen" title="人脸建档" max-width="620px">
      <div v-if="faceTarget" class="stack" style="gap: 14px">
        <div class="pp-target">
          <div class="pp-ava pp-ava--lg">
            <img v-if="avatarOf(faceTarget)" :src="avatarOf(faceTarget)" :alt="faceTarget.name" />
            <span v-else class="pp-ava-ph"><Icon name="user" /></span>
          </div>
          <div>
            <div class="pp-target-name">{{ faceTarget.name }}</div>
            <div class="dim tiny">
              {{ faceTarget.no }} · {{ lineOf(faceTarget) }} · {{ genderText(faceTarget.gender) }}
            </div>
          </div>
          <div class="spacer" />
          <span v-if="faceTarget.face.enrolled" class="tag tag--ok">已建档 {{ faceTarget.face.template_count }} 张</span>
          <span v-else class="tag tag--mute">未建档</span>
        </div>

        <div v-if="faceStatus && !faceStatus.identity.ready" class="pp-warn">
          <Icon name="alert" />
          <div>
            <b>人脸识别模型未就绪</b>
            <div class="tiny">{{ faceStatus.identity.error || '请先下载 SFace 权重' }}</div>
          </div>
        </div>

        <FileDrop
          v-if="!faceTarget.face.enrolled || faceReplace || faceResult"
          accept=".jpg,.jpeg,.png,.webp,.bmp"
          title="拖拽一张正面清晰照片到此处，或点击选择"
          hint="支持 jpg / png / webp，建议人脸占画面 1/3 以上"
          :max-mb="8"
          @file="onFaceFile"
        />

        <div class="field">
          <label>同意主体</label>
          <BaseSelect v-model="faceConsentSubject" :options="consentSubjectOptions" />
        </div>
        <p class="muted tiny" style="margin: -4px 0 0">
          按《人脸识别技术应用安全管理办法》，处理人脸信息需取得单独同意；不满 14 周岁须由监护人同意。
          同意将随本次建档一并登记并留痕。
        </p>

        <label v-if="faceTarget.face.enrolled" class="sw">
          <input type="checkbox" v-model="faceReplace" />
          <span class="track"><span class="knob" /></span>
          <span class="sw-label">重新采集（清空旧模板后写入）</span>
        </label>

        <!-- 结果：质量报告 -->
        <div v-if="faceResult" class="pp-result" :class="faceResult.ok ? 'is-ok' : 'is-bad'">
          <div class="pp-result-hd">
            <Icon :name="faceResult.ok ? 'check' : 'alert'" />
            <b>{{ faceResult.ok ? '建档成功' : '未通过质量校验' }}</b>
            <div class="spacer" />
            <span v-if="faceResult.quality" class="mono tiny">质量分 {{ faceResult.quality.score }}</span>
          </div>
          <div v-if="faceResult.quality" class="pp-metrics">
            <span>人脸 {{ faceResult.quality.face_px }}px</span>
            <span>清晰度 {{ faceResult.quality.sharpness }}</span>
            <span>亮度 {{ faceResult.quality.brightness }}</span>
            <span>正脸偏移 {{ faceResult.quality.pose_offset }}</span>
          </div>
          <ul v-if="faceResult.reasons && faceResult.reasons.length" class="pp-reasons">
            <li v-for="(why, i) in faceResult.reasons" :key="i">{{ why }}</li>
          </ul>
          <div v-if="faceResult.duplicate" class="pp-dup">
            <Icon name="alert" />
            检测到相似人员：<b>{{ faceResult.duplicate.name }}</b>
            （{{ faceResult.duplicate.class_name || faceResult.duplicate.no }}，相似度 {{ faceResult.duplicate.score }}）——
            请确认是否为同一人，避免重复建档。
          </div>
        </div>
      </div>
      <template #footer>
        <button class="btn btn--ghost" @click="faceOpen = false">关闭</button>
        <button class="btn btn--primary" :disabled="faceBusy || !faceFile" @click="submitFace">
          <Icon name="camera" /> {{ faceBusy ? '处理中…' : '开始建档' }}
        </button>
      </template>
    </Modal>

    <!-- 批量导入 -->
    <Modal v-model="importOpen" title="批量导入" max-width="560px">
      <div class="stack" style="gap: 14px">
        <p class="muted tiny" style="margin: 0">
          导入 <b>{{ meta.label }}</b> 名单（CSV，UTF-8 或 GBK 均可）。表头支持中文，
          编号或姓名缺失的行会被跳过并列出原因；编号已存在的行将更新其信息。
        </p>
        <button class="btn btn--sm btn--ghost" @click="downloadTemplate"><Icon name="download" /> 下载导入模板</button>
        <FileDrop
          accept=".csv,.txt"
          title="拖拽 CSV 文件到此处，或点击选择"
          hint="表头示例：编号,姓名,性别,班级"
          :max-mb="4"
          @file="onImportFile"
        />
        <div v-if="importResult" class="pp-result is-ok">
          <div class="pp-result-hd">
            <Icon name="check" />
            <b>新增 {{ importResult.created }} · 更新 {{ importResult.updated }} · 跳过 {{ importResult.skipped_count }}</b>
          </div>
          <ul v-if="importResult.skipped && importResult.skipped.length" class="pp-reasons">
            <li v-for="(s, i) in importResult.skipped.slice(0, 12)" :key="i">第 {{ s.row }} 行：{{ s.reason }}</li>
          </ul>
        </div>
      </div>
      <template #footer>
        <button class="btn btn--ghost" @click="importOpen = false">关闭</button>
        <button class="btn btn--primary" :disabled="saving || !importFile" @click="submitImport">
          <Icon name="upload" /> {{ saving ? '导入中…' : '开始导入' }}
        </button>
      </template>
    </Modal>

    <!-- 详情 -->
    <Modal v-model="detailOpen" title="人员详情" max-width="620px">
      <div v-if="detail" class="stack" style="gap: 14px">
        <div class="pp-target">
          <div class="pp-ava pp-ava--lg">
            <img v-if="avatarOf(detail)" :src="avatarOf(detail)" :alt="detail.name" />
            <span v-else class="pp-ava-ph"><Icon name="user" /></span>
          </div>
          <div>
            <div class="pp-target-name">{{ detail.name }}</div>
            <div class="dim tiny">{{ detail.type_label }} · {{ detail.no }}</div>
          </div>
          <div class="spacer" />
          <span class="tag" :class="consentTag(detail.face.consent_status)">{{ detail.face.consent_label }}</span>
        </div>

        <div class="pp-kv">
          <div><span>性别</span><b>{{ genderText(detail.gender) }}</b></div>
          <div><span>{{ ownerType === 'student' ? '班级' : '部门' }}</span><b>{{ lineOf(detail) }}</b></div>
          <div v-if="detail.enroll_year"><span>入学年份</span><b>{{ detail.enroll_year }}</b></div>
          <div v-if="detail.title"><span>职称</span><b>{{ detail.title }}</b></div>
          <div v-if="detail.subject"><span>任教科目</span><b>{{ detail.subject }}</b></div>
          <div v-if="detail.position"><span>职务</span><b>{{ detail.position }}</b></div>
          <div v-if="detail.phone"><span>电话</span><b>{{ detail.phone }}</b></div>
          <div v-if="detail.guardian_name"><span>监护人</span><b>{{ detail.guardian_name }}（{{ detail.guardian_relation || '—' }}）</b></div>
          <div v-if="detail.guardian_phone"><span>监护人电话</span><b>{{ detail.guardian_phone }}</b></div>
          <div><span>人脸模板</span><b>{{ detail.face.template_count }} 张</b></div>
        </div>

        <div>
          <div class="pp-sub-t">人脸模板</div>
          <div v-if="detail.templates && detail.templates.length" class="pp-tpls">
            <span v-for="t in detail.templates" :key="t.id" class="tag tag--info">
              #{{ t.id }} · 质量 {{ t.quality }} · {{ t.model }}
            </span>
          </div>
          <div v-else class="dim tiny">尚未建档</div>
        </div>

        <div>
          <div class="pp-sub-t">同意留痕</div>
          <div v-if="detailConsents.length" class="pp-consents">
            <div v-for="c in detailConsents" :key="c.id" class="pp-consent-row">
              <span class="tag" :class="c.action === 'grant' ? 'tag--ok' : 'tag--high'">{{ c.action_label }}</span>
              <span class="tiny muted">{{ c.subject_label }}</span>
              <span class="spacer" />
              <span class="tiny dim mono">{{ (c.created_at || '').replace('T', ' ').slice(0, 19) }}</span>
            </div>
          </div>
          <div v-else class="dim tiny">尚无同意记录（未登记同意前不会采集人脸特征）</div>
        </div>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.pp { display: flex; flex-direction: column; gap: 12px; }

.pp-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pp-bar .spacer { flex: 1; }
.pp-search {
  display: flex; align-items: center; gap: 7px;
  padding: 0 10px; height: 32px; width: 220px;
  border: 1px solid var(--line-2); border-radius: var(--r-sm);
  background: var(--bg-inset);
}
.pp-search > svg { width: 14px; height: 14px; color: var(--tx-3); flex-shrink: 0; }
.pp-search input {
  flex: 1; min-width: 0; border: none; background: none; outline: none;
  color: var(--tx-1); font-size: 12.5px; font-family: inherit;
}
.pp-search input::placeholder { color: var(--tx-3); }

/* 头像：定尺寸 + 圆角，避免无照片时布局跳动 */
.pp-ava {
  width: 34px; height: 34px; border-radius: 8px; overflow: hidden;
  border: 1px solid var(--line-2); background: var(--bg-inset);
  display: grid; place-items: center;
}
.pp-ava--lg { width: 52px; height: 52px; border-radius: 11px; }
.pp-ava img { width: 100%; height: 100%; object-fit: cover; display: block; }
.pp-ava-ph { display: grid; place-items: center; color: var(--tx-3); }
.pp-ava-ph svg { width: 17px; height: 17px; }
.pp-ava--lg .pp-ava-ph svg { width: 22px; height: 22px; }

.pp-page { display: flex; align-items: center; gap: 8px; padding-top: 4px; }
.pp-page .spacer { flex: 1; }

.pp-form { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.pp-form-full { grid-column: 1 / -1; }

.pp-target { display: flex; align-items: center; gap: 12px; }
.pp-target .spacer { flex: 1; }
.pp-target-name { font-size: 15px; font-weight: 700; }

.pp-warn {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 10px 12px; border-radius: var(--r-sm);
  border: 1px solid rgba(255, 176, 32, 0.35); background: var(--warn-soft);
  color: #ffe0a8;
}
.pp-warn > svg { width: 16px; height: 16px; flex-shrink: 0; margin-top: 2px; }

.pp-result { padding: 11px 13px; border-radius: var(--r-sm); border: 1px solid var(--line-2); }
.pp-result.is-ok { border-color: rgba(46, 230, 168, 0.4); background: var(--ok-soft); }
.pp-result.is-bad { border-color: rgba(255, 77, 109, 0.42); background: var(--danger-soft); }
.pp-result-hd { display: flex; align-items: center; gap: 8px; }
.pp-result-hd svg { width: 15px; height: 15px; }
.pp-result-hd .spacer { flex: 1; }
.pp-metrics { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 7px; font-size: 11.5px; color: var(--tx-2); }
.pp-reasons { margin: 7px 0 0; padding-left: 18px; font-size: 11.5px; color: var(--tx-2); }
.pp-dup {
  display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
  margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--line-2);
  font-size: 12px; color: #ffd9a3;
}
.pp-dup svg { width: 14px; height: 14px; }

.pp-kv { display: grid; grid-template-columns: 1fr 1fr; gap: 9px 16px; }
.pp-kv > div { display: flex; gap: 8px; font-size: 12.5px; }
.pp-kv span { color: var(--tx-3); min-width: 68px; }
.pp-kv b { color: var(--tx-1); font-weight: 600; }
.pp-sub-t { font-size: 11px; letter-spacing: 0.1em; color: var(--tx-3); margin-bottom: 6px; text-transform: uppercase; }
.pp-tpls { display: flex; flex-wrap: wrap; gap: 6px; }
.pp-consents { display: flex; flex-direction: column; gap: 6px; }
.pp-consent-row { display: flex; align-items: center; gap: 9px; font-size: 12px; }
.pp-consent-row .spacer { flex: 1; }
</style>
