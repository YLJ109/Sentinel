<template>
  <Teleport to="body">
    <div v-if="confirmState.open" class="mask" @click.self="settle(false)">
      <div class="modal" style="max-width: 420px">
        <div class="modal-hd">
          <Icon name="alert" style="width: 18px; height: 18px; color: var(--warn)" />
          <h3>{{ confirmState.title }}</h3>
        </div>
        <div class="modal-bd">
          <p class="muted" style="margin: 0">{{ confirmState.text }}</p>
        </div>
        <div class="modal-ft">
          <button class="btn btn--ghost" @click="settle(false)">{{ confirmState.cancelText }}</button>
          <button class="btn" :class="confirmState.danger ? 'btn--danger' : 'btn--primary'" @click="settle(true)">
            {{ confirmState.okText }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { confirmState, settle } from './confirm'
import Icon from './Icon.vue'
</script>
