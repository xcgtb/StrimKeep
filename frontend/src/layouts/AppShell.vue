<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  Clapperboard,
  ChevronRight,
  CheckCircle2,
  CircleAlert,
  Sun,
  Moon,
  Menu,
  LogOut,
  Search,
  X,
} from '@lucide/vue';
import { useSession } from '../stores/session';
import { useUi } from '../stores/ui';
import {
  navigation,
  allNavigation,
  matchesNavigation,
  searchFunctions,
  moduleName,
} from '../config/navigation';
import { readData, object } from '../api/domain';
import ModalDialog from '../components/ModalDialog.vue';
const route = useRoute(),
  router = useRouter(),
  session = useSession(),
  ui = useUi(),
  open = ref(false),
  searchOpen = ref(false),
  search = ref('');
const expandedGroups = ref<Set<string>>(new Set(['我的网盘']));
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
watch(
  () => route.fullPath,
  () => {
    open.value = false;
  },
);
function keys(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    open.value = false;
    searchOpen.value = false;
  }
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
  <div class="layout prototype-shell">
    <button v-if="open" class="veil show" aria-label="关闭导航" @click="open = false"></button>
    <aside class="sidebar" :class="{ 'mobile-open': open }">
      <RouterLink to="/dashboard" class="brand" aria-label="StrimKeep 媒体总览">
        <span class="brandmark"><Clapperboard :size="23" /></span>
        <span
          ><span class="brandname">StrimKeep</span
          ><span class="brandmeta">MEDIA WORKSPACE</span></span
        >
      </RouterLink>
      <button
        v-if="open"
        class="squarebtn sidebar-close"
        aria-label="关闭导航"
        @click="open = false"
      >
        <X :size="18" />
      </button>
      <nav class="navscroll" aria-label="主导航">
        <div class="navgroup">工作空间 / WORKSPACE</div>
        <div v-for="group in navigation" :key="group.group" class="nav-group">
          <RouterLink
            v-if="group.directTo"
            :to="group.directTo"
            class="navitem nav-direct"
            :class="{ active: isGroupActive(group), current: isGroupActive(group) }"
          >
            <component :is="group.icon" :size="18" /><span class="grow">{{ group.group }}</span>
          </RouterLink>
          <template v-else>
            <button
              class="navitem nav-group-toggle"
              :class="{ active: isGroupActive(group), open: expandedGroups.has(group.group) }"
              :aria-expanded="expandedGroups.has(group.group)"
              @click="toggleGroup(group.group)"
            >
              <component :is="group.icon" :size="18" /><span class="grow">{{ group.group }}</span
              ><ChevronRight :size="14" class="navchev" />
            </button>
            <div class="children nav-children" :class="{ open: expandedGroups.has(group.group) }">
              <RouterLink
                v-for="item in group.items"
                :key="item.to"
                :to="item.to"
                class="child"
                :class="{
                  active:
                    isChildActive(item.to) ||
                    (item.to === '/organize/workspace' && workspaceModule === 'organize'),
                  current: isChildActive(item.to),
                }"
              >
                <span class="dot"></span>{{ item.name }}
              </RouterLink>
            </div>
          </template>
        </div>
      </nav>
      <div class="sidefoot">
        <RouterLink to="/system" class="device">
          <span class="nicon"
            ><CheckCircle2
              v-if="session.runtime.readiness?.ready === true"
              :size="18" /><CircleAlert v-else :size="18"
          /></span>
          <span
            ><b>NAS 服务</b
            ><small
              ><span
                class="statusdot"
                :class="{ pending: session.runtime.readiness?.ready !== true }"
              ></span
              >{{
                session.runtime.readiness?.ready === true ? '服务已就绪' : '等待服务就绪'
              }}</small
            ></span
          >
        </RouterLink>
        <div class="version">
          {{ version }}
          <button class="text-link logout" aria-label="退出登录" @click="logout">
            <LogOut :size="13" />退出
          </button>
        </div>
      </div>
    </aside>
    <div class="mainwrap">
      <header class="topbar">
        <div class="crumb">
          <button
            class="squarebtn hamb mobile-menu"
            aria-label="展开导航"
            @click="expandMobileMenu"
          >
            <Menu :size="19" />
          </button>
          <span class="workspace-crumb">工作空间</span
          ><ChevronRight class="workspace-crumb" :size="13" /><b>{{ activePageTitle }}</b>
        </div>
        <div class="topright">
          <button
            class="searchbox global-search-button"
            aria-label="全局搜索功能"
            @click="searchOpen = true"
          >
            <Search :size="16" /><span>快速查找页面</span><kbd>Ctrl K</kbd>
          </button>
          <button
            class="squarebtn"
            :aria-label="ui.theme === 'light' ? '切换深色主题' : '切换浅色主题'"
            @click="ui.toggleTheme"
          >
            <Moon v-if="ui.theme === 'light'" :size="18" /><Sun v-else :size="18" />
          </button>
          <RouterLink to="/settings/accounts" class="avatar" aria-label="账号设置">S</RouterLink>
        </div>
      </header>
      <main id="main-content" class="content">
        <RouterView v-slot="{ Component }"
          ><Transition name="workspace-page" mode="out-in"
            ><div :key="route.path" class="page-body">
              <component :is="Component" /></div></Transition
        ></RouterView>
        <footer class="footerhint">StrimKeep · 一体化媒体资产工作台</footer>
      </main>
    </div>
    <ModalDialog v-if="searchOpen" title="搜索功能" @close="searchOpen = false">
      <label class="search-dialog-input"
        >搜索页面与功能<input
          v-model="search"
          autofocus
          aria-label="搜索功能名称"
          placeholder="搜索治理、目录、播放、整理…"
      /></label>
      <div class="search-results">
        <RouterLink
          v-for="item in searchResults"
          :key="item.to"
          :to="item.to"
          class="searchresult"
          @click="searchOpen = false"
          ><component :is="item.icon" :size="17" /><span
            ><b>{{ item.name }}</b
            ><small>{{ item.group }}</small></span
          ><ChevronRight :size="15"
        /></RouterLink>
      </div>
      <p v-if="!searchResults.length" class="muted-text">没有找到匹配功能。</p>
    </ModalDialog>
  </div>
</template>
