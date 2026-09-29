<template>
  <div ref="root" class="dd" :class="{ open }">
    <button type="button" class="dd-btn" @click="toggle">
      <Icon v-if="icon" :name="icon" style="width: 15px; height: 15px; color: var(--tx-3)" />
      <span v-if="current" class="ellip">{{ current.label }}</span>
      <span v-else class="ph">{{ placeholder }}</span>
      <Icon name="chev-down" class="caret" />
    </button>
    <div v-if="open" class="dd-menu">
      <div v-if="clearable" class="dd-opt" @click="pick(emptyOpt)">
        <span class="muted">{{ placeholder }}</span>
        <Icon v-if="isEmpty" name="check" class="tick" />
      </div>
      <div
        v-for="o in normalized"
        :key="String(o.value)"
        class="dd-opt"
        :class="{ on: o.value === modelValue }"
        @click="pick(o)"
      >
        <span class="ellip">{{ o.label }}</span>
        <Icon v-if="o.value === modelValue" name="check" class="tick" />
      </div>
      <div v-if="!normalized.length && !clearable" class="dd-opt dim">暂无可选项</div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import Icon from './Icon.vue'

const props = defineProps({
  modelValue: { type: [String, Number, null], default: '' },
  options: { type: Array, default: () => [] },
  placeholder: { type: String, default: '请选择' },
  icon: { type: String, default: '' },
  clearable: { type: Boolean, default: false }
})
const emit = defineEmits(['update:modelValue', 'change'])

const root = ref(null)
const open = ref(false)

const emptyOpt = { value: '', label: props.placeholder }

const normalized = computed(() =>
  props.options.map((o) => (typeof o === 'object' && o !== null ? o : { value: o, label: String(o) }))
)
const current = computed(() => normalized.value.find((o) => o.value === props.modelValue) || null)
const isEmpty = computed(() => props.modelValue === '' || props.modelValue === null || props.modelValue === undefined)

function toggle() { open.value = !open.value }
function pick(o) {
  open.value = false
  if (o === emptyOpt) { emit('update:modelValue', ''); emit('change', ''); return }
  emit('update:modelValue', o.value)
  emit('change', o.value)
}
function onDocClick(e) { if (root.value && !root.value.contains(e.target)) open.value = false }

onMounted(() => document.addEventListener('mousedown', onDocClick))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocClick))
</script>

<style scoped>
/* .ellip 已收敛到全局 style.css 的通用工具类 */
</style>
