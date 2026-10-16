<script setup lang="ts">
import { ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { Activity, ArrowRight, LockKeyhole } from '@lucide/vue';
import { useSession } from '../stores/session';
const username = ref('admin'),
  password = ref(''),
  error = ref(''),
  busy = ref(false);
const session = useSession(),
  router = useRouter(),
  route = useRoute();
async function submit() {
  busy.value = true;
  error.value = '';
  try {
    await session.login(username.value, password.value);
    password.value = '';
    const target = String(route.query.redirect || '/dashboard');
    await router.replace(
      target.startsWith('/') && !target.startsWith('//') && !target.startsWith('/login')
        ? target
        : '/dashboard',
    );
  } catch (e) {
    error.value = (e as Error).message;
    password.value = '';
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <main class="login-page">
    <div class="login-intro">
      <span class="brand-mark"><Activity :size="28" /></span
      ><span class="eyebrow">YOUR MEDIA, IN ORDER.</span>
      <h1>资源有序，<br />观影从容。</h1>
      <p>连接资源与片库，让接收、整理与治理<br />成为一个清晰的工作流程。</p>
      <div class="login-decoration"><span></span><span></span><span></span></div>
    </div>
    <form class="login-card" @submit.prevent="submit">
      <LockKeyhole :size="26" class="accent" />
      <h2>登录 StrimKeep</h2>
      <p>使用已有的管理账号继续</p>
      <label>用户名<input v-model="username" autocomplete="username" required /></label
      ><label
        >管理密码<input v-model="password" type="password" autocomplete="current-password"
      /></label>
      <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      <button class="button primary" :disabled="busy">
        {{ busy ? '正在登录…' : '进入工作台' }}<ArrowRight :size="16" /></button
      ><small>Vue 3 前端预览 · 使用现有服务会话</small>
    </form>
  </main>
</template>
