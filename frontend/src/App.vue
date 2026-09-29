<template>
  <!-- 正常渲染；子组件渲染异常时切换为降级面板，避免整页白屏 -->
  <template v-if="!renderError">
    <router-view />
  </template>
  <div v-else class="crash-wrap">
    <div class="panel corner crash">
      <div class="panel-bd crash-bd">
        <div class="crash-ico"><Icon name="alert" /></div>
        <h3>页面出现异常，请刷新重试</h3>
        <p class="muted tiny">该页面在渲染时发生错误，已阻止白屏。若刷新后仍无法恢复，请联系系统管理员。</p>
        <p v-if="errText" class="crash-detail mono">{{ errText }}</p>
        <button class="btn btn--primary" style="margin-top: 16px" @click="reload">
          <Icon name="refresh" /> 刷新页面
        </button>
      </div>
    </div>
  </div>
  <ToastHost />
  <ConfirmHost />
</template>

<script setup>
import { computed, onErrorCaptured, ref } from 'vue'
import { ToastHost, ConfirmHost, Icon } from '@/ui'

// 捕获子组件渲染阶段抛出的异常
const renderError = ref(null)
const errText = computed(() => {
  const e = renderError.value
  if (!e) return ''
  return e.message ? String(e.message) : String(e)
})

onErrorCaptured((err, instance, info) => {
  console.error('[页面渲染异常]', info, err)
  renderError.value = err
  return false // 阻止继续向上冒泡
})

function reload() {
  location.reload()
}
</script>

<style scoped>
.crash-wrap {
  position: relative;
  z-index: 1;
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
}
.crash { max-width: 460px; width: 100%; }
.crash-bd { text-align: center; padding: 34px 26px; }
.crash-ico {
  width: 54px; height: 54px; margin: 0 auto 16px;
  border-radius: 15px;
  display: grid; place-items: center;
  background: var(--danger-soft);
  border: 1px solid rgba(255, 77, 109, 0.35);
}
.crash-ico :deep(.ico) { width: 26px; height: 26px; color: var(--danger); }
.crash h3 { font-family: var(--font-display); font-size: 16px; margin-bottom: 8px; }
.crash-detail {
  margin: 12px 0 0;
  padding: 8px 10px;
  border-radius: var(--r-md);
  background: var(--bg-inset);
  border: 1px dashed var(--line-2);
  font-size: 11.5px;
  color: var(--tx-3);
  word-break: break-all;
}
</style>
