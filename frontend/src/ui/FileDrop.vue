<template>
  <div
    class="drop"
    :class="{ over }"
    @click="input?.click()"
    @dragover.prevent="over = true"
    @dragleave.prevent="over = false"
    @drop.prevent="onDrop"
  >
    <input ref="input" type="file" :accept="accept" hidden @change="onPick" />
    <div class="drop-ico"><Icon name="upload" /></div>
    <div class="t1">{{ picked ? picked.name : title }}</div>
    <div class="t2">{{ picked ? prettySize : hint }}</div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import Icon from './Icon.vue'
import { toast } from './toast'

const props = defineProps({
  accept: { type: String, default: '.mp4,.avi,.mov,.mkv,.webm' },
  hint: { type: String, default: '支持 mp4 / avi / mov / mkv / webm' },
  // 提示文案可覆盖：人脸注册等图片场景沿用同一组件，避免再写一个几乎相同的拖拽框
  title: { type: String, default: '拖拽视频到此处，或点击选择' },
  maxMB: { type: Number, default: 512 }
})
const emit = defineEmits(['file'])

const input = ref(null)
const over = ref(false)
const picked = ref(null)

const prettySize = computed(() => {
  if (!picked.value) return ''
  const mb = picked.value.size / 1024 / 1024
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(picked.value.size / 1024).toFixed(0)} KB`
})

function handle(f) {
  if (!f) return
  if (props.maxMB && f.size / 1024 / 1024 > props.maxMB) {
    toast.warn(`文件超过 ${props.maxMB} MB 限制`)
    return
  }
  picked.value = f
  emit('file', f)
}

function onPick(e) { handle(e.target.files?.[0]); e.target.value = '' }
function onDrop(e) { over.value = false; handle(e.dataTransfer?.files?.[0]) }

defineExpose({ reset: () => { picked.value = null } })
</script>
