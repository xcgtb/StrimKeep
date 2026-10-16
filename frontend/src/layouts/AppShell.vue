<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  Activity,
  Sun,
  Moon,
  Menu,
  X,
  LogOut,
  ChevronRight,
  ChevronDown,
  Search,
  LayoutDashboard,
  Layers,
  ListTodo,
  Settings2,
  ArrowDownUp,
  LibraryBig,
} from '@lucide/vue';
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
const expandedGroups = ref<Set<string>>(new Set(['工作台', '媒体加工']));
function expandMobileMenu() {
  expandedGroups.value = new Set(navigation.map((group) => group.group));
  open.value = true;
}
function toggleGroup(name: string) {
  const next = new Set(expandedGroups.value);
  next.has(name) ? next.delete(name) : next.add(name);
  expandedGroups.value = next;
}
watch(
  () => route.path,
  (path) => {
    const group = navigation.find((g) => g.items.some((item) => item.to === path));
    if (group && !expandedGroups.value.has(group.group)) {
      expandedGroups.value = new Set([...expandedGroups.value, group.group]);
    }
  },
  { immediate: true },
);
const dock = [
  { to: '/dashboard', name: '总览', icon: LayoutDashboard },
  { to: '/resources/receive', name: '接收', icon: Layers },
  { to: '/organize/workspace', name: '整理', icon: ArrowDownUp },
  { to: '/media/library', name: '片库', icon: LibraryBig },
  { to: '/tasks', name: '任务', icon: ListTodo },
];
const activeGroup = computed(() =>
  navigation.find((g) => g.items.some((i) => i.to === route.path)),
);
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
  <div class="app-shell template-shell">
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
          <button
            type="button"
            class="nav-group-toggle"
            :aria-expanded="expandedGroups.has(group.group)"
            @click="toggleGroup(group.group)"
          >
            <span>{{ group.group }}</span
            ><span class="nav-group-count">{{ group.items.length }}</span
            ><ChevronDown :size="14" :class="{ expanded: expandedGroups.has(group.group) }" />
          </button>
          <RouterLink
            v-for="item in expandedGroups.has(group.group) ? group.items : []"
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
          <button class="icon-button mobile-menu" aria-label="展开导航" @click="expandMobileMenu">
            <Menu :size="21" /></button
          ><span>{{ route.meta.group }}</span
          ><ChevronRight :size="14" /><strong>{{ route.meta.title }}</strong>
        </div>
        <div class="topbar-actions">
          <span
            class="topbar-readiness"
            :class="{ pending: session.runtime.readiness?.ready !== true }"
            ><span
              class="status-dot"
              :class="{ muted: session.runtime.readiness?.ready !== true }"
            ></span
            >{{ session.runtime.readiness?.ready === true ? '服务就绪' : '等待服务就绪' }}</span
          >
          <button class="global-search-button" aria-label="全局搜索功能" @click="searchOpen = true">
            <Search :size="15" /><span>搜索功能</span><kbd>⌘ / Ctrl K</kbd></button
          ><button
            class="icon-button"
            :aria-label="ui.theme === 'light' ? '切换深色主题' : '切换浅色主题'"
            @click="ui.toggleTheme"
          >
            <Moon v-if="ui.theme === 'light'" :size="18" /><Sun v-else :size="18" /></button
          ><RouterLink to="/settings" class="icon-button topbar-settings" aria-label="打开系统设置"
            ><Settings2 :size="18"
          /></RouterLink>
        </div>
      </header>
      <main id="main-content" class="page">
        <nav v-if="activeGroup" class="module-chips" aria-label="当前模块页面">
          <RouterLink
            v-for="item in activeGroup.items"
            :key="item.to"
            :to="item.to"
            :class="{ current: item.to === route.path }"
            >{{ item.name }}</RouterLink
          >
        </nav>
        <RouterView v-slot="{ Component }"
          ><component :is="Component" :key="route.path"
        /></RouterView>
      </main>
      <footer class="page-footer">StrimKeep <span>接收 · 整理 · 入库 · 治理</span></footer>
    </div>
  </div>
  <nav v-if="!open" class="mobile-dock" aria-label="快捷导航">
    <RouterLink
      v-for="item in dock"
      :key="item.to"
      :to="item.to"
      :class="{ current: route.path === item.to }"
      ><component :is="item.icon" :size="20" /><span>{{ item.name }}</span></RouterLink
    >
  </nav>
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
