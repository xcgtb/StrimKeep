<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router';
import { navigation, flatNavigation } from './config/navigation';
import { injectIcons } from './config/icons';
import { get } from './api/client';
import { object, text } from './api/domain';

const route = useRoute();
const router = useRouter();
const sidebarOpen = ref(false);
const theme = ref<'light' | 'dark'>('light');
const runtime = ref<Record<string, unknown>>({});
const runtimeOpen = ref(false);
const version = ref('');
const driveOpen = ref<Set<string>>(new Set(['cloud']));

const current = computed(
  () => flatNavigation.find((item) => item.path === route.path) || flatNavigation[0]!,
);
const ready = computed(() => {
  const readiness = object(runtime.value.readiness);
  return readiness.ready === true;
});
const healthText = computed(() => (ready.value ? '服务正常' : '等待连接'));
const botText = computed(() => {
  const bot = object(runtime.value.bot);
  if (!bot || !Object.keys(bot).length) return '未配置';
  return bot.running ? '运行中' : '未运行';
});
const taskText = computed(() => {
  const task = object(runtime.value.task);
  if (!task || !task.name) return '空闲';
  return String(task.name);
});
function isDriveActive(item: { children?: { path: string }[] }) {
  return (item.children || []).some((child) => child.path === route.path);
}
function toggleDrive(id: string) {
  const next = new Set(driveOpen.value);
  next.has(id) ? next.delete(id) : next.add(id);
  driveOpen.value = next;
}

function applyTheme(next: 'light' | 'dark') {
  theme.value = next;
  document.documentElement.setAttribute('data-theme', next);
  try {
    localStorage.setItem('strimkeep-theme', next);
  } catch {
    /* 隐私模式忽略 */
  }
}
function toggleTheme() {
  applyTheme(theme.value === 'dark' ? 'light' : 'dark');
}
async function loadRuntime() {
  try {
    runtime.value = (await get<Record<string, unknown>>('/api/runtime/status')) || {};
    version.value = text(object(runtime.value).version);
  } catch {
    runtime.value = {};
  }
}
async function logout() {
  await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' });
  router.replace('/login');
}
const paint = () => nextTick(() => injectIcons(document));

watch(
  () => route.fullPath,
  () => {
    sidebarOpen.value = false;
    for (const item of navigation) {
      if (item.children && isDriveActive(item) && !driveOpen.value.has(item.id)) {
        driveOpen.value = new Set([...driveOpen.value, item.id]);
      }
    }
    void paint();
  },
);
onMounted(() => {
  try {
    const saved = localStorage.getItem('strimkeep-theme');
    applyTheme(saved === 'dark' ? 'dark' : 'light');
  } catch {
    applyTheme('light');
  }
  void loadRuntime();
  void paint();
});
</script>
<template>
  <div class="app-wrap">
    <div class="mask" :class="{ show: sidebarOpen }" @click="sidebarOpen = false"></div>
    <aside class="sidebar" :class="{ open: sidebarOpen }">
      <div class="side-title">
        <span class="brand-mark" aria-hidden="true">S</span>
        <div><b>StrimKeep</b><small>影视库管理与治理</small></div>
        <span class="v">{{ version }}</span>
      </div>
      <nav>
        <template v-for="item in navigation" :key="item.id">
          <div v-if="item.children" class="nav-drive">
            <button
              class="nav-drive-toggle"
              type="button"
              :aria-expanded="driveOpen.has(item.id) || isDriveActive(item)"
              @click="toggleDrive(item.id)"
            >
              <span class="icon" :data-icon="item.icon"></span>
              <span class="nav-label">{{ item.name }}</span>
              <span aria-hidden="true">⌄</span>
            </button>
            <div v-show="driveOpen.has(item.id) || isDriveActive(item)" class="nav-drive-children">
              <RouterLink
                v-for="child in item.children"
                :key="child.id"
                :to="child.path"
                :class="{ active: route.path === child.path }"
                @click="sidebarOpen = false"
              >
                <span class="icon" :data-icon="child.icon"></span>
                <span class="nav-label">{{ child.name }}</span>
              </RouterLink>
            </div>
          </div>
          <RouterLink
            v-else
            :to="item.path"
            :class="{ active: route.path === item.path }"
            @click="sidebarOpen = false"
          >
            <span class="icon" :data-icon="item.icon"></span>
            <span class="nav-label">{{ item.name }}</span>
          </RouterLink>
        </template>
      </nav>
      <div class="side-foot">
        <button type="button" data-icon="moon" @click="toggleTheme">
          {{ theme === 'dark' ? '浅色模式' : '深色模式' }}
        </button>
        <button type="button" data-icon="logout" @click="logout">退出系统</button>
      </div>
    </aside>

    <main style="flex: 1; min-width: 0">
      <header class="header">
        <div class="left">
          <button class="menu-btn" data-icon="menu" @click="sidebarOpen = true"></button>
          <span class="header-product">StrimKeep</span>
          <h2>{{ current.name }}</h2>
          <span class="header-version">{{ version }}</span>
        </div>
        <div class="right">
          <details
            class="runtime-popover"
            :open="runtimeOpen"
            @toggle="runtimeOpen = ($event.target as HTMLDetailsElement).open"
          >
            <summary class="header-health" :data-tone="ready ? 'ok' : 'unknown'">{{
              healthText
            }}</summary>
            <div class="runtime-panel">
              <div class="runtime-panel-heading">
                <span>运行状态</span>
                <button
                  type="button"
                  @click="
                    runtimeOpen = false;
                    router.push('/history');
                  "
                >
                  查看日志 ↗
                </button>
              </div>
              <div class="status-line"><span>服务连接</span><b>{{ healthText }}</b></div>
              <div class="status-line"><span>Telegram 机器人</span><b>{{ botText }}</b></div>
              <div class="status-line"><span>当前任务</span><b>{{ taskText }}</b></div>
            </div>
          </details>
          <button
            class="theme-btn"
            :data-icon="theme === 'dark' ? 'sun' : 'moon'"
            title="切换主题"
            @click="toggleTheme"
          ></button>
        </div>
      </header>
      <div class="main"><RouterView /></div>
    </main>
  </div>
</template>
