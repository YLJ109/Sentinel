<template>
  <div class="page">
    <div class="grid g-main grid-fill">
      <!-- 左：视频检测与播放（同一个盒子：未选片时是大选择区，选片后切换为播放器） -->
      <div class="panel corner fade-up panel-fill">
        <div class="panel-hd">
          <span class="panel-title">视频检测与播放</span>
          <div class="spacer" />
          <template v-if="playing">
            <span class="panel-sub mono ellip" :title="playing.filename">{{ playing.filename }}</span>
            <span class="pill pill--wide" :class="stCls(playing.status)">{{ statusZh[playing.status] || playing.status }}</span>
            <button class="btn btn--xs" @click="backToSelect"><Icon name="upload" /> 重新选择</button>
          </template>
          <span v-else class="panel-sub">上传新视频检测，或从右侧「检测记录」中点选一条播放</span>
        </div>
        <div class="panel-bd">
          <div v-if="playing" class="player">
            <video :src="playUrl" controls autoplay />
          </div>

          <div v-else class="picker">
            <FileDrop ref="dropEl" :hint="'支持 mp4 / avi / mov / mkv / webm · 单个文件不超过 512 MB'" @file="onFile" />
            <div class="row picker-bar">
              <div class="field" style="width: 220px">
                <label>关联点位（可选）</label>
                <BaseSelect v-model="cameraId" :options="camOptions" placeholder="不关联点位" icon="camera" clearable />
              </div>
              <div class="spacer" />
              <button class="btn btn--primary" :disabled="!file || uploading" @click="upload">
                <Icon name="cpu" /> {{ uploading ? '正在上传…' : '开始检测' }}
              </button>
            </div>
          </div>

          <div v-if="playing" class="play-meta">
            <span class="dim tiny">时长 <b class="mono">{{ playing.duration ? `${playing.duration.toFixed(1)}s` : '—' }}</b></span>
            <span class="dim tiny">命中 <b class="mono">{{ playing.event_count }}</b> 个事件</span>
            <span class="dim tiny">触发 <b class="mono">{{ playing.alarm_count }}</b> 次报警</span>
            <div class="spacer" />
            <button class="btn btn--xs" :disabled="!procUrl(playing)" @click="play(playing, true)">
              <Icon name="film" /> 标注视频
            </button>
            <button class="btn btn--xs btn--ghost" :disabled="!origUrl(playing)" @click="play(playing, false)">
              <Icon name="play" /> 原片
            </button>
            <button class="btn btn--xs btn--ghost" @click="$router.push('/alarms')">查看报警</button>
          </div>
        </div>
      </div>

      <!-- 右：检测记录 -->
      <div class="panel corner fade-up d1 panel-fill">
        <div class="panel-hd">
          <span class="panel-title">检测记录</span>
          <span class="panel-sub mono">{{ records.length }} 个</span>
          <div class="spacer" />
          <button class="btn btn--icon btn--sm btn--ghost" title="刷新" @click="load"><Icon name="refresh" /></button>
        </div>
        <div class="panel-bd flush">
          <div v-if="loading" class="empty">
            <Icon name="refresh" />
            <div class="t">加载中…</div>
          </div>
          <div v-else-if="!records.length" class="empty">
            <Icon name="film" />
            <div class="t">暂无检测记录，上传视频后自动分析</div>
          </div>
          <ul v-else class="rec-list">
            <li
              v-for="r in records"
              :key="r.id"
              class="rec"
              :class="{ on: playing && playing.id === r.id }"
              @click="pick(r)"
            >
              <div class="rec-top">
                <span class="rec-name ellip" :title="r.filename">{{ r.filename }}</span>
                <span class="pill pill--wide" :class="stCls(r.status)">{{ statusZh[r.status] || r.status }}</span>
              </div>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  :class="{ ok: r.status === 'done', bad: r.status === 'failed' }"
                  :style="{ width: `${Math.round((r.progress || 0) * 100)}%` }"
                />
              </div>
              <div class="rec-foot">
                <span class="dim tiny mono">{{ Math.round((r.progress || 0) * 100) }}%</span>
                <span class="dim tiny">事件 {{ r.event_count }} · 报警 {{ r.alarm_count }}</span>
                <div class="spacer" />
                <span class="dim tiny mono">{{ fmt(r.created_at) }}</span>
              </div>
            </li>
          </ul>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Icon from '@/ui/Icon.vue'
import FileDrop from '@/ui/FileDrop.vue'
import BaseSelect from '@/ui/BaseSelect.vue'
import { toast } from '@/ui/toast'
import api, { dataUrl } from '@/api'

const dropEl = ref(null)
const cameras = ref([])
const cameraId = ref('')
const file = ref(null)
const records = ref([])
const loading = ref(true)
const uploading = ref(false)
const playUrl = ref('')
const playing = ref(null)
let timer = null

const statusZh = { processing: '检测中', done: '已完成', failed: '失败' }
const camOptions = computed(() => cameras.value.map((c) => ({ value: c.id, label: c.name })))
const fmt = (t) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '')
const stCls = (s) => ({ processing: 'pill--handling', done: 'pill--resolved', failed: 'pill--pending' }[s] || 'pill--ignored')

/** 原始视频地址：uploads/ 下的文件名 */
function origUrl(r) {
  const name = r?.original_path ? String(r.original_path).split(/[\\/]/).pop() : ''
  return name ? dataUrl(`uploads/${name}`) : ''
}
/** 标注视频地址（检测完成后由后台生成） */
function procUrl(r) {
  return r?.processed_path ? dataUrl(r.processed_path) : ''
}

function onFile(f) { file.value = f }

async function upload() {
  if (!file.value) return
  uploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', file.value)
    const q = cameraId.value ? `?camera_id=${cameraId.value}` : ''
    await api.post(`/api/video/upload${q}`, fd, { headers: { 'Content-Type': 'multipart/form-data' } })
    toast.ok('上传成功，正在后台逐帧检测')
    file.value = null
    dropEl.value?.reset()
    load()
  } catch { /* 拦截器已提示 */ } finally {
    uploading.value = false
  }
}

async function load() {
  try {
    // 该接口返回数组（非分页）
    const res = await api.get('/api/video')
    records.value = Array.isArray(res) ? res : (res?.items || [])
  } catch { /* 静默 */ } finally {
    // 仅首次加载展示占位，避免定时轮询时表格闪空
    loading.value = false
  }
}

/** 点击记录：默认播放原片，并在左侧播放器中加载 */
function pick(r) {
  play(r, false)
}

function play(r, annotated) {
  const url = annotated ? procUrl(r) : origUrl(r)
  if (!url) { toast.warn(annotated ? '该记录暂无标注视频' : '该记录暂无原片文件'); return }
  playUrl.value = url
  playing.value = r
}

/** 回到大选择区，便于换一个视频继续检测 / 播放 */
function backToSelect() {
  playing.value = null
  playUrl.value = ''
}

onMounted(async () => {
  load()
  try { cameras.value = await api.get('/api/cameras') } catch { /* 静默 */ }
  // 每 3 秒刷新，跟踪「检测中」记录的进度
  timer = setInterval(load, 3000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
/* .ellip 已收敛到全局 style.css 的通用工具类 */

/* 选择区：未选片时占满整个盒子，拖拽区尽量大，作为主要的「选择视频」入口 */
.picker { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: 12px; }
.picker :deep(.drop) {
  flex: 1;
  min-height: 160px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.picker :deep(.drop-ico) { width: 60px; height: 60px; border-radius: 17px; }
.picker :deep(.drop-ico svg) { width: 28px; height: 28px; }
.picker :deep(.drop .t1) { font-size: 15px; }
.picker-bar { flex-shrink: 0; }

/* 播放区按剩余高度自适应，视频等比完整显示（object-fit: contain 不裁切） */
.player {
  flex: 1;
  min-height: 140px;
  display: grid;
  place-items: center;
  background: #03060c;
  border: 1px solid var(--line-2);
  border-radius: var(--r-md);
  overflow: hidden;
}
.player video { width: 100%; height: 100%; object-fit: contain; display: block; }

.play-meta { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 10px; }
.play-meta b { color: var(--tx-1); }

/* 检测记录列表：占满面板剩余高度并内部滚动 */
.rec-list { flex: 1; min-height: 0; overflow-y: auto; }
.rec {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
  transition: background 0.16s, box-shadow 0.16s;
}
.rec:hover { background: rgba(47, 214, 240, 0.045); }
.rec.on { background: rgba(47, 214, 240, 0.1); box-shadow: inset 2px 0 0 var(--acc); }
.rec:last-child { border-bottom: none; }
.rec-top { display: flex; align-items: center; gap: 10px; margin-bottom: 7px; }
.rec-name { flex: 1; min-width: 0; font-size: 13px; }
.rec-foot { display: flex; align-items: center; gap: 10px; margin-top: 6px; }

/* 状态列统一加宽，状态文字不换行、视觉对齐 */
.pill--wide { min-width: 68px; justify-content: center; flex-shrink: 0; }
</style>
