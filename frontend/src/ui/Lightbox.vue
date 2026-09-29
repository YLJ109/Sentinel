<template>
  <Teleport to="body">
    <div v-if="src" class="lightbox" @click.self="close">
      <button class="btn btn--icon x" @click="close" aria-label="关闭"><Icon name="close" /></button>
      <img :src="src" alt="取证截图预览" />
    </div>
  </Teleport>
</template>

<script setup>
import Icon from './Icon.vue'

defineProps({ src: { type: String, default: '' } })
const emit = defineEmits(['update:src'])
function close() { emit('update:src', '') }
</script>
