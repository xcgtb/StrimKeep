<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  Diamond,
  Sun,
  Moon,
  Menu,
  X,
  LogOut,
  ChevronDown,
  Search,
  LayoutDashboard,
  Settings2,
  ArrowDownUp,
  CloudDownload,
  Compass,
  ScrollText,
} from '@lucide/vue';
import { useSession } from '../stores/session';
import { useUi } from '../stores/ui';
import {
  navigation,
  allNavigation,
  matchesNavigation,
  searchFunctions,
  moduleName,
  moduleNavigation,
} from '../config/navigation';
import { readData, object } from '../api/domain';
import ModalDialog from '../components/ModalDialog.vue';
const embedded = new URLSearchParams(window.location.search).get('embedded') === '1';
const route = useRoute(),
  router = useRouter(),
  session = useSession(),
  ui = useUi(),
  open = ref(false),
  searchOpen = ref(false),
  search = ref('');
const expandedGroups = ref<Set<string>>(new Set());
function expandMobileMenu() {
  expandedGroups.value = new Set(['我的网盘']);
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
    const group =
      navigation.find((g) => g.items.some((item) => item.to.split('?')[0] === path)) ||
      (moduleName(path, route.query.section) === 'organize' ? navigation[1] : undefined);
    if (group && !group.directTo && !expandedGroups.value.has(group.group)) {
      expandedGroups.value = new Set([...expandedGroups.value, group.group]);
    }
  },
  { immediate: true },
);
const dock = [
  { to: '/dashboard', name: '总览', icon: LayoutDashboard },
  { to: '/resources/receive?tab=batch', name: '转存', icon: CloudDownload },
  { to: '/organize/workspace', name: '整理', icon: ArrowDownUp },
  { to: '/media/explore', name: '发现', icon: Compass },
  { to: '/logs', name: '日志', icon: ScrollText },
];
const activePageTitle = computed(() => {
  const exact = allNavigation.find(
    (item) => item.to.includes('?') && matchesNavigation(item.to, route.path, route.query),
  );
  if (exact) return exact.name;
  const plain = allNavigation.find((item) => item.to === route.path);
  return plain?.name || String(route.meta.title || '');
});
function isChildActive(to: string) {
  return matchesNavigation(to, route.path, route.query);
}
const workspaceModule = computed(() => moduleName(route.path, route.query.section));
const moduleTabs = computed(() => moduleNavigation[workspaceModule.value] || []);
function isGroupActive(group: (typeof navigation)[number]) {
  if (group.group === '我的网盘')
    return (
      route.path === '/settings/accounts' ||
      (route.path.startsWith('/resources/') && route.path !== '/resources/plugins') ||
      workspaceModule.value === 'organize' ||
      workspaceModule.value === 'strm'
    );
  if (group.group === '双库治理') return workspaceModule.value === 'governance';
  if (group.group === '入库记录') return workspaceModule.value === 'ingestion';
  if (group.group === '系统状态') return workspaceModule.value === 'system';
  if (group.group === '全局设置') return workspaceModule.value === 'settings';
  return route.path === group.directTo;
}
const version = computed(() => session.runtime.version?.match(/^\d+\.\d+\.\d+/)?.[0] || '本地开发');
const searchResults = computed(() => searchFunctions(search.value));
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
  <div class="app-shell template-shell" :class="{ embedded }">
    <button v-if="open" class="nav-scrim" aria-label="关闭导航" @click="open = false"></button>
    <aside class="sidebar" :class="{ expanded: open }">
      <RouterLink to="/dashboard" class="brand" @click="open = false"
        ><span class="brand-mark"><Diamond :size="24" /></span
        ><span>StrimKeep<small>NAS · MEDIA WORKSPACE</small></span></RouterLink
      ><button class="icon-button mobile-close" aria-label="关闭导航" @click="open = false">
        <X :size="20" />
      </button>
      <nav aria-label="主导航">
        <div v-for="group in navigation" :key="group.group" class="nav-group">
          <RouterLink
            v-if="group.directTo"
            :to="group.directTo"
            class="nav-direct"
            :class="{ current: isGroupActive(group) }"
            @click="open = false"
          >
            <component :is="group.icon" :size="18" />
            <span>{{ group.group }}</span>
          </RouterLink>
          <template v-else>
            <button
              type="button"
              class="nav-group-toggle nav-cloud-toggle"
              :class="{ current: isGroupActive(group) }"
              :aria-expanded="expandedGroups.has(group.group)"
              @click="toggleGroup(group.group)"
            >
              <component :is="group.icon" :size="18" />
              <span>{{ group.group }}</span>
              <ChevronDown :size="15" :class="{ expanded: expandedGroups.has(group.group) }" />
            </button>
            <div v-if="expandedGroups.has(group.group)" class="nav-children">
              <RouterLink
                v-for="item in group.items"
                :key="item.to"
                :to="item.to"
                :class="{
                  current:
                    isChildActive(item.to) ||
                    (item.to === '/organize/workspace' && workspaceModule === 'organize'),
                }"
                @click="open = false"
              >
                <component :is="item.icon" :size="16" />
                <span>{{ item.name }}</span>
              </RouterLink>
            </div>
          </template>
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
          ><strong>{{ activePageTitle }}</strong>
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
        <nav v-if="moduleTabs.length" class="module-navigation" aria-label="模块功能">
          <RouterLink
            v-for="item in moduleTabs"
            :key="item.to"
            :to="item.to"
            :class="{ current: isChildActive(item.to) }"
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
      :class="{ current: route.path === item.to.split('?')[0] }"
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
