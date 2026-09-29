<template>
  <div class="toasts">
    <div v-for="t in toasts" :key="t.id" class="toast" :class="t.type">
      <Icon class="ico" :name="iconOf(t.type)" />
      <span>{{ t.text }}</span>
    </div>
  </div>
</template>

<script setup>
import { toasts } from './toast'
import Icon from './Icon.vue'

const iconOf = (t) => ({ ok: 'check', err: 'close', warn: 'alert', info: 'signal' }[t] || 'signal')
</script>
