<template>
  <div class="page">
    <p v-if="hint" class="hint fade-up">
      <Icon name="alert" style="width: 14px; height: 14px" />
      {{ hint }}
      <button class="btn btn--xs" :disabled="scanning" @click="scan">
        <Icon name="refresh" /> {{ scanning ? '扫描中…' : '授权并扫描' }}
      </button>
    </p>

    <div class="panel corner fade-up panel-fill">
      <div class="panel-hd">
        <span class="panel-title">本机摄像头 / 视频源</span>
        <div class="spacer" />
        <span class="panel-sub mono">{{ online }}/{{ cameras.length }} 启用</span>
        <button class="btn btn--sm" :disabled="scanning" @click="scan">
          <Icon name="refresh" /> {{ scanning ? '扫描中…' : '扫描本机摄像头' }}
        </button>
        <button class="btn btn--sm btn--primary" @click="openCreate"><Icon name="plus" /> 新增点位</button>
      </div>
      <div class="panel-bd flush">
        <div v-if="!cameras.length" class="empty">
          <Icon name="camera" />
          <div class="t">未发现摄像头，点击「扫描本机摄像头」获取真实设备</div>
        </div>
        <div v-else class="tbl-wrap scroll-fill">
          <table class="tbl">
            <thead>
              <tr>
                <th style="width: 72px">ID</th>
                <th style="width: 110px">编号</th>
                <th style="width: 220px">摄像头名称</th>
                <th style="width: 160px">安装位置</th>
                <th style="width: 120px">类型</th>
                <th>设备 ID / 源地址</th>
                <th style="width: 110px">启用</th>
                <th style="width: 150px" />
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in cameras" :key="c.id">
                <td class="num">{{ c.id }}</td>
                <td class="num">{{ c.code || `#${c.id}` }}</td>
                <td><span class="row" style="gap: 9px"><span class="c-dot" :class="{ off: !c.enabled }" />{{ c.name }}</span></td>
                <td>{{ c.location || '—' }}</td>
                <td><span class="tag tag--mute">{{ typeZh[c.source_type] || c.source_type }}</span></td>
                <td class="num ellip" :title="c.source_url || c.device_id">{{ c.source_url || c.device_id || '—' }}</td>
                <td>
                  <label class="sw">
                    <input type="checkbox" :checked="c.enabled" @change="toggle(c, $event.target.checked)" />
                    <span class="track"><span class="knob" /></span>
                  </label>
                </td>
                <td>
                  <div class="btn-row">
                    <button class="btn btn--xs" @click="openEdit(c)"><Icon name="edit" /> 编辑</button>
                    <button class="btn btn--xs btn--ghost" @click="remove(c)"><Icon name="trash" /> 删除</button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <Modal v-model="open" :title="form.id ? '编辑摄像头' : '新增点位'" max-width="480px">
      <div class="stack" style="gap: 14px">
        <div class="field">
          <label>摄像头名称</label>
          <input v-model.trim="form.name" class="input" placeholder="如：本机摄像头 / 教学楼A栋走廊" />
        </div>
        <div class="field">
          <label>编号（留空则显示 #ID）</label>
          <input v-model.trim="form.code" class="input" placeholder="如：CAM-01" maxlength="16" />
        </div>
        <div class="field">
          <label>安装位置</label>
          <input v-model.trim="form.location" class="input" placeholder="如：3层东侧" />
        </div>
        <div class="field">
          <label>接入类型</label>
          <BaseSelect v-model="form.source_type" :options="typeOptions" placeholder="请选择类型" />
        </div>
        <div v-if="form.source_type !== 'webcam'" class="field">
          <label>源地址</label>
          <input v-model.trim="form.source_url" class="input" placeholder="rtsp://user:pass@192.168.1.10:554/stream1" />
        </div>
      </div>
      <template #footer>
        <button class="btn btn--ghost" @click="open = false">取消</button>
        <button class="btn btn--primary" :disabled="saving" @click="save">
          <Icon name="check" /> {{ saving ? '保存中…' : '保存' }}
        </button>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import Icon from '@/ui/Icon.vue'
import Modal from '@/ui/Modal.vue'
import BaseSelect from '@/ui/BaseSelect.vue'
import { toast } from '@/ui/toast'
import { confirm } from '@/ui/confirm'
import api from '@/api'

const cameras = ref([])
const open = ref(false)
const saving = ref(false)
const scanning = ref(false)
const hint = ref('')
const typeZh = { webcam: '本机摄像头', rtsp: 'RTSP 网络', file: '视频文件' }
const typeOptions = [
  { value: 'webcam', label: '本机摄像头' },
  { value: 'rtsp', label: 'RTSP 网络摄像头' },
  { value: 'file', label: '视频文件' }
]
const form = reactive({ id: null, name: '', code: '', location: '', source_type: 'webcam', source_url: '' })

const online = computed(() => cameras.value.filter((c) => c.enabled).length)

async function load() {
  try { cameras.value = await api.get('/api/cameras') } catch { /* 静默 */ }
}

/** 枚举本机真实视频设备；ask 为 true 时先申请一次授权，以便拿到真实设备名称 */
async function enumerateVideoInputs(ask) {
  if (!navigator.mediaDevices?.enumerateDevices) return null
  if (ask) {
    try {
      const s = await navigator.mediaDevices.getUserMedia({ video: true })
      s.getTracks().forEach((t) => t.stop())
    } catch {
      toast.warn('未授权摄像头，将按设备数量同步但名称可能为空')
    }
  }
  const all = await navigator.mediaDevices.enumerateDevices()
  const seen = new Set()
  return all
    .filter((d) => d.kind === 'videoinput')
    .filter((d) => {
      const key = d.deviceId || `${d.groupId}:${d.label}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
}

/** 把本机设备同步到后端摄像头表，保证条目数与真实设备数一致 */
async function syncFromDevices(ask, silent) {
  scanning.value = true
  try {
    const list = await enumerateVideoInputs(ask)
    if (list === null) {
      if (!silent) toast.warn('当前浏览器不支持设备枚举')
      return
    }
    const devices = list.map((d, i) => ({
      device_id: d.deviceId || `videoinput-${i}`,
      label: (d.label || '').trim(),
      index: i
    }))
    cameras.value = await api.post('/api/cameras/sync', { devices })
    hint.value = devices.some((d) => !d.label) && devices.length
      ? '浏览器未返回设备名称（未授权时名称会隐藏）。点击右侧按钮授权后即可显示真实名称。'
      : ''
    if (!silent) toast.ok(`已同步 ${devices.length} 个本机摄像头`)
  } catch { /* 拦截器已提示 */ } finally {
    scanning.value = false
  }
}

const scan = () => syncFromDevices(true, false)

function openCreate() {
  Object.assign(form, { id: null, name: '', code: '', location: '', source_type: 'webcam', source_url: '' })
  open.value = true
}

function openEdit(c) {
  Object.assign(form, {
    id: c.id, name: c.name, code: c.code || '', location: c.location || '',
    source_type: c.source_type, source_url: c.source_url || ''
  })
  open.value = true
}

async function save() {
  if (!form.name) { toast.warn('请填写摄像头名称'); return }
  saving.value = true
  const body = { name: form.name, code: form.code, location: form.location, source_type: form.source_type, source_url: form.source_url }
  try {
    if (form.id) await api.patch(`/api/cameras/${form.id}`, body)
    else await api.post('/api/cameras', body)
    toast.ok('已保存')
    open.value = false
    load()
  } catch { /* 拦截器已提示 */ } finally {
    saving.value = false
  }
}

async function toggle(c, v) {
  try {
    await api.patch(`/api/cameras/${c.id}`, { enabled: v })
    c.enabled = v
    toast.ok(v ? '已启用' : '已停用')
  } catch { /* 拦截器已提示 */ }
}

async function remove(c) {
  const ok = await confirm({ title: '删除摄像头', text: `确认删除「${c.name}」？该操作不可撤销。`, okText: '删除', danger: true })
  if (!ok) return
  try {
    await api.delete(`/api/cameras/${c.id}`)
    toast.ok('已删除')
    load()
  } catch { /* 拦截器已提示 */ }
}

onMounted(async () => {
  await load()
  // 静默对齐一次，保证展示数量与本机真实设备一致
  await syncFromDevices(false, true)
  await load()
})
</script>

<style scoped>
/* .c-dot / .ellip 已收敛到全局 style.css；此处只保留本页的宽度差异 */
.ellip { max-width: 300px; }
.hint {
  display: flex; align-items: center; gap: 9px;
  margin: 0 0 10px;
  padding: 8px 12px;
  border-radius: var(--r-md);
  border: 1px solid rgba(255, 176, 32, 0.32);
  background: var(--warn-soft);
  color: #ffc861;
  font-size: 12.5px;
}
.hint :deep(.ico) { flex-shrink: 0; }
</style>
