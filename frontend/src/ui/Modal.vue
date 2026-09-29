<template>
  <Teleport to="body">
    <div v-if="modelValue" class="mask" @click.self="close">
      <div class="modal" :style="{ maxWidth: maxWidth }">
        <div class="modal-hd">
          <slot name="icon" />
          <h3>{{ title }}</h3>
          <button class="x" @click="close" aria-label="关闭"><Icon name="close" style="width: 15px; height: 15px" /></button>
        </div>
        <div class="modal-bd"><slot /></div>
        <div v-if="$slots.footer" class="modal-ft"><slot name="footer" /></div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import Icon from './Icon.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: '' },
  maxWidth: { type: String, default: '560px' }
})
const emit = defineEmits(['update:modelValue'])

function close() { emit('update:modelValue', false) }
</script>
