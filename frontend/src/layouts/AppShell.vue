<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { Activity, Sun, Moon, Menu, X, LogOut, ChevronRight, Search } from '@lucide/vue';
import { useSession } from '../stores/session';
import { useUi } from '../stores/ui';
import { navigation, allNavigation } from '../config/navigation';
import { readData, object } from '../api/domain';
import ModalDialog from '../components/ModalDialog.vue';
const route = useRoute(),
  router = useRouter(),
  session = useSession(),
  ui = useUi(),
  open = ref(false),
  searchOpen = ref(false),
  search = ref('');
const version = computed(() => session.runtime.version?.match(/^\d+\.\d+\.\d+/)?.[0] || '本地开发');
const searchResults = computed(() =>
  allNavigation.filter((item) => (item.group + item.name).includes(search.value.trim())),
);
async function logout() {
  try {
    await session.logout();
    await router.replace('/login');
  } catch (e) {
    ui.notify((e as Error).message, 'error');
  }
}
function keys(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    searchOpen.value = !searchOpen.value;
  }
}
onMounted(() => {
  document.addEventListener('keydown', keys);
  void readData('/api/appearance')
    .then((v) =>
      ui.setAppearance(
        Object.fromEntries(
          Object.entries(object(v.appearance)).map(([k, x]) => [k, String(x || '')]),
        ),
      ),
    )
    .catch(() => {});
});
onBeforeUnmount(() => document.removeEventListener('keydown', keys));
</script>
<template>
  <div class="app-shell">
    <button v-if="open" class="nav-scrim" aria-label="关闭导航" @click="open = false"></button>
    <aside class="sidebar" :class="{ expanded: open }">
      <RouterLink to="/dashboard" class="brand" @click="open = false"
        ><span class="brand-mark"><Activity :size="24" /></span
        ><span>StrimKeep<small>媒体自动化管理</small></span></RouterLink
      ><button class="icon-button mobile-close" aria-label="关闭导航" @click="open = false">
        <X :size="20" />
      </button>
      <nav aria-label="主导航">
        <div v-for="group in navigation" :key="group.group" class="nav-group">
          <p>{{ group.group }}</p>
          <RouterLink
            v-for="item in group.items"
            :key="item.to"
            :to="item.to"
            :class="{ current: route.path === item.to }"
            @click="open = false"
            ><component :is="item.icon" :size="17" /><span>{{ item.name }}</span></RouterLink
          >
        </div>
      </nav>
      <div class="sidebar-bottom">
        <span
          class="status-dot"
          :class="{ muted: session.runtime.readiness?.ready === false }"
        ></span
        ><span
          >{{ session.runtime.readiness?.ready === false ? '服务初始化中' : '已连接服务'
          }}<small>{{ version }}</small></span
        ><button class="icon-button" title="退出登录" aria-label="退出登录" @click="logout">
          <LogOut :size="16" />
        </button>
      </div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <div class="breadcrumb">
          <button class="icon-button mobile-menu" aria-label="展开导航" @click="open = true">
            <Menu :size="21" /></button
          ><span>{{ route.meta.group }}</span
          ><ChevronRight :size="14" /><strong>{{ route.meta.title }}</strong>
        </div>
        <div class="topbar-actions">
          <button class="global-search-button" aria-label="全局搜索功能" @click="searchOpen = true">
            <Search :size="15" /><span>搜索功能</span><kbd>⌘ / Ctrl K</kbd></button
          ><button
            class="icon-button"
            :aria-label="ui.theme === 'light' ? '切换深色主题' : '切换浅色主题'"
            @click="ui.toggleTheme"
          >
            <Moon v-if="ui.theme === 'light'" :size="18" /><Sun v-else :size="18" />
          </button>
        </div>
      </header>
      <main id="main-content" class="page">
        <RouterView v-slot="{ Component }"
          ><component :is="Component" :key="route.path"
        /></RouterView>
      </main>
      <footer class="page-footer">StrimKeep <span>接收 · 整理 · 入库 · 治理</span></footer>
    </div>
  </div>
  <ModalDialog v-if="searchOpen" title="搜索功能" @close="searchOpen = false"
    ><input
      v-model="search"
      aria-label="搜索功能名称"
      placeholder="输入接收、整理、日志等功能名称"
    />
    <div class="directory-list">
      <RouterLink
        v-for="item in searchResults"
        :key="item.to"
        :to="item.to"
        @click="
          searchOpen = false;
          open = false;
        "
        ><span>{{ item.name }}</span
        ><small>{{ item.group }}</small></RouterLink
      >
    </div></ModalDialog
  >
</template>
