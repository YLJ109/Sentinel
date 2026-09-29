<template>
  <Teleport to="body">
    <div v-if="modelValue" class="drawer-mask" @click="close" />
    <aside v-if="modelValue" class="drawer">
      <div class="drawer-hd">
        <h3>{{ title }}</h3>
        <div class="spacer" style="flex: 1" />
        <button class="btn btn--icon btn--sm btn--ghost" @click="close" aria-label="关闭">
          <Icon name="close" />
        </button>
      </div>
      <div class="drawer-bd"><slot /></div>
    </aside>
  </Teleport>
</template>

<script setup>
import Icon from './Icon.vue'

defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: '' }
})
const emit = defineEmits(['update:modelValue'])
function close() { emit('update:modelValue', false) }
</script>
