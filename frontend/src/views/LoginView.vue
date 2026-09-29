<template>
  <div class="login">
    <div class="login-bg">
      <span class="glow g1" /><span class="glow g2" />
      <span class="scanline" />
    </div>

    <!-- 左：品牌与能力介绍 -->
    <section class="hero">
      <div class="hero-brand">
        <div class="brand-mark"><Icon name="shield-alert" style="color: var(--acc); width: 22px; height: 22px" /></div>
        <div>
          <div class="brand-name">守望 · 校园反霸凌智能检测系统</div>
          <div class="brand-sub">Sentinel · Campus Anti-Bullying Platform</div>
        </div>
      </div>

      <h1 class="hero-title">AI 值守每一处<br /><span>校园角落</span></h1>
      <p class="hero-desc">
        视觉行为识别与语音关键词双引擎实时联动，异常即刻报警、自动留存取证链，
        让霸凌无处遁形，让处置有据可依。
      </p>

      <ul class="feats">
        <li v-for="f in feats" :key="f.t">
          <span class="fi"><Icon :name="f.icon" /></span>
          <div>
            <b>{{ f.t }}</b>
            <p>{{ f.d }}</p>
          </div>
        </li>
      </ul>

      <div class="hero-stats">
        <div><b class="mono">5</b><span>类异常行为</span></div>
        <div><b class="mono">24×7</b><span>不间断监测</span></div>
        <div><b class="mono">&lt;1s</b><span>报警响应</span></div>
      </div>
    </section>

    <!-- 右：登录 -->
    <section class="card-wrap">
      <div class="login-card">
        <div class="lc-head">
          <div class="upper">Secure Access</div>
          <h2>登录控制台</h2>
          <p class="muted tiny">请使用学校统一分配的账号登录</p>
        </div>

        <form @submit.prevent="submit">
          <div class="field">
            <label>账号</label>
            <div class="input-affix">
              <Icon name="user" />
              <input v-model.trim="form.username" class="input" placeholder="请输入用户名" autocomplete="username" />
            </div>
          </div>

          <div class="field" style="margin-top: 16px">
            <label>密码</label>
            <div class="input-affix">
              <Icon name="lock" />
              <input
                v-model="form.password"
                class="input"
                :type="show ? 'text' : 'password'"
                placeholder="请输入密码"
                autocomplete="current-password"
              />
              <button type="button" class="tail" @click="show = !show" :aria-label="show ? '隐藏密码' : '显示密码'">
                <Icon :name="show ? 'eye-off' : 'eye'" style="width: 16px; height: 16px" />
              </button>
            </div>
          </div>

          <button class="btn btn--primary btn--block login-btn" :disabled="loading" type="submit">
            <span v-if="loading" class="spin" />
            <Icon v-else name="logout" style="transform: rotate(180deg)" />
            {{ loading ? '正在验证…' : '进入系统' }}
          </button>
        </form>

        <div class="demo-hint">
          <Icon name="cpu" style="width: 14px; height: 14px" />
          演示账号 <b class="mono">admin</b> / <b class="mono">admin123</b>
        </div>

        <div class="lc-foot">
          <span class="status-chip"><span class="dot" />检测引擎在线</span>
          <span class="dim tiny">v1.0.0</span>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import Icon from '@/ui/Icon.vue'
import { toast } from '@/ui/toast'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const loading = ref(false)
const show = ref(false)
const form = reactive({ username: 'admin', password: '' })

const feats = [
  { icon: 'video', t: '视觉行为识别', d: '跌倒 / 打架 / 争吵 / 抽烟 / 人员聚集' },
  { icon: 'mic', t: '语音关键词预警', d: '辱骂威胁与求救关键词即时命中' },
  { icon: 'folder', t: '取证链闭环', d: '截图 · 时间线 · 对话内容一键回放' }
]

async function submit() {
  if (!form.username || !form.password) {
    toast.warn('请输入账号与密码')
    return
  }
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    toast.ok('登录成功，欢迎回来')
    router.push('/dashboard')
  } catch { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 0.95fr);
  height: 100vh;
  overflow: hidden;
  z-index: 1;
}
.login-bg { position: absolute; inset: 0; z-index: 0; pointer-events: none; }
.glow { position: absolute; border-radius: 50%; filter: blur(130px); opacity: 0.32; }
.g1 { width: 560px; height: 560px; background: #1c8fb5; top: -180px; left: -140px; }
.g2 { width: 520px; height: 520px; background: #2b5fd0; bottom: -200px; right: -120px; }
.scanline {
  position: absolute; left: 0; right: 0; height: 140px;
  background: linear-gradient(180deg, transparent, rgba(47, 214, 240, 0.07), transparent);
  animation: down 7s linear infinite;
}
@keyframes down { from { top: -140px; } to { top: 100%; } }

/* 左 */
.hero {
  position: relative;
  z-index: 1;
  padding: 52px 58px;
  display: flex; flex-direction: column; justify-content: center;
  gap: 22px;
}
.hero-brand { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
.hero-title {
  font-family: var(--font-display);
  font-size: clamp(30px, 3.4vw, 46px);
  line-height: 1.18;
  font-weight: 700;
  letter-spacing: 0.01em;
  margin: 0;
}
.hero-title span {
  background: linear-gradient(120deg, var(--acc), #6ea8ff);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.hero-desc { color: var(--tx-2); max-width: 520px; margin: 0; font-size: 14.5px; }

.feats { display: grid; gap: 14px; margin-top: 6px; max-width: 520px; }
.feats li { display: flex; gap: 13px; align-items: flex-start; }
.fi {
  width: 38px; height: 38px; border-radius: 10px; flex-shrink: 0;
  display: grid; place-items: center;
  background: var(--acc-soft); border: 1px solid rgba(47, 214, 240, 0.28);
}
.fi :deep(.ico) { width: 18px; height: 18px; color: var(--acc); }
.feats b { font-size: 14px; }
.feats p { margin: 2px 0 0; font-size: 12.5px; color: var(--tx-3); }

.hero-stats {
  display: flex; gap: 34px; margin-top: 14px;
  padding-top: 22px; border-top: 1px solid var(--line);
}
.hero-stats b { display: block; font-size: 24px; color: var(--acc); line-height: 1.1; }
.hero-stats span { font-size: 12px; color: var(--tx-3); }

/* 右 */
.card-wrap { position: relative; z-index: 1; display: grid; place-items: center; padding: 40px; }
.login-card {
  width: 100%; max-width: 400px;
  padding: 34px 32px 26px;
  border-radius: var(--r-xl);
  background: linear-gradient(180deg, rgba(17, 26, 42, 0.92), rgba(12, 19, 32, 0.96));
  border: 1px solid var(--line-2);
  box-shadow: var(--sh-2);
  -webkit-backdrop-filter: blur(14px);
  backdrop-filter: blur(14px);
  animation: fade-up 0.5s cubic-bezier(0.2, 0.9, 0.3, 1) both;
}
.lc-head { margin-bottom: 24px; }
.lc-head h2 { font-family: var(--font-display); font-size: 23px; margin: 6px 0 5px; }
.login-btn { margin-top: 24px; height: 44px; font-size: 14.5px; letter-spacing: 0.06em; }

.spin {
  width: 15px; height: 15px; border-radius: 50%;
  border: 2px solid rgba(4, 18, 26, 0.35);
  border-top-color: #04121a;
  animation: rot 0.7s linear infinite;
}
@keyframes rot { to { transform: rotate(360deg); } }

.demo-hint {
  display: flex; align-items: center; gap: 8px;
  margin-top: 18px; padding: 10px 12px;
  border-radius: var(--r-md);
  background: var(--bg-inset); border: 1px dashed var(--line-2);
  font-size: 12px; color: var(--tx-2);
}
.demo-hint b { color: var(--acc); }

.lc-foot {
  display: flex; align-items: center; justify-content: space-between;
  margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--line);
}

@media (max-width: 1020px) {
  .login { grid-template-columns: 1fr; overflow-y: auto; }
  .hero { padding: 34px 26px 4px; justify-content: flex-start; gap: 14px; }
  .feats, .hero-stats { display: none; }
  .hero-title { font-size: 30px; }
  .card-wrap { padding: 16px 22px 44px; place-items: start center; }
}
</style>
