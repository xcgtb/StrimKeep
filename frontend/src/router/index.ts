import { createRouter, createWebHashHistory } from 'vue-router';
import { useSession } from '../stores/session';
import { useUi } from '../stores/ui';
export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: () => import('../views/LoginView.vue'), meta: { title: '登录' } },
    { path: '/', redirect: '/dashboard' },
    {
      path: '/dashboard',
      component: () => import('../views/DashboardView.vue'),
      meta: { title: '运行总览', group: '工作空间' },
    },
    {
      path: '/media/explore',
      component: () => import('../views/ExploreView.vue'),
      meta: { title: '影视探索', group: '媒体中心' },
    },
    {
      path: '/media/library',
      component: () => import('../views/LibraryView.vue'),
      meta: { title: '片库映射', group: '媒体中心' },
    },
    {
      path: '/media/subscriptions',
      component: () => import('../views/SubscriptionsView.vue'),
      meta: { title: '追更订阅', group: '媒体中心' },
    },
    {
      path: '/resources/files',
      component: () => import('../views/FilesView.vue'),
      meta: { title: '115 文件管理', group: '我的网盘' },
    },
    {
      path: '/tasks',
      component: () => import('../views/TasksView.vue'),
      meta: { title: '任务中心', group: '系统状态' },
    },
    {
      path: '/settings',
      component: () => import('../views/SettingsView.vue'),
      meta: { title: '连接与设置', group: '全局设置' },
    },
    {
      path: '/resources',
      component: () => import('../views/ResourcesView.vue'),
      meta: { title: '资源与工具', group: '全部功能' },
    },
    {
      path: '/resources/receive',
      component: () => import('../views/ReceiveView.vue'),
      meta: { title: '资源接收', group: '我的网盘' },
    },
    {
      path: '/resources/share',
      component: () => import('../views/ReceiveView.vue'),
      meta: { title: '分享免转存', group: '我的网盘' },
    },
    {
      path: '/resources/channels',
      component: () => import('../views/ChannelsView.vue'),
      meta: { title: '频道与机器人', group: '我的网盘' },
    },
    {
      path: '/organize/workspace',
      component: () => import('../views/OrganizeView.vue'),
      meta: { title: '媒体整理', group: '影视整理' },
    },
    {
      path: '/organize/naming',
      component: () => import('../views/NamingView.vue'),
      meta: { title: '自定义命名模板', group: '影视整理' },
    },
    {
      path: '/organize/quality',
      component: () => import('../views/Quality8View.vue'),
      meta: { title: '整理八维与洗版', group: '影视整理' },
    },
    {
      path: '/organize/automation',
      component: () => import('../views/AutomationView.vue'),
      meta: { title: '整理自动化', group: '影视整理' },
    },
    {
      path: '/media/strm',
      component: () => import('../views/StrmView.vue'),
      meta: { title: 'STRM 资源', group: '媒体中心' },
    },
    {
      path: '/tasks/ingestion',
      component: () => import('../views/IngestionView.vue'),
      meta: { title: 'Emby 入库跟踪', group: '入库记录' },
    },
    {
      path: '/tasks/reports',
      component: () => import('../views/ReportsView.vue'),
      meta: { title: '入库汇报与晨报', group: '入库记录' },
    },
    {
      path: '/logs',
      component: () => import('../views/LogsView.vue'),
      meta: { title: '运行日志', group: '日志中心' },
    },
    {
      path: '/governance',
      component: () => import('../views/GovernanceView.vue'),
      meta: { title: '治理决策', group: '双库治理' },
    },
    {
      path: '/governance/rules',
      component: () => import('../views/GovernanceSettingsView.vue'),
      meta: { title: '七维与治理规则', group: '双库治理' },
    },
    {
      path: '/governance/cleanup',
      component: () => import('../views/CleanupView.vue'),
      meta: { title: '目录与残留清理', group: '双库治理' },
    },
    {
      path: '/settings/accounts',
      component: () => import('../views/AccountsView.vue'),
      meta: { title: '115 账号与目录', group: '我的网盘' },
    },
    {
      path: '/settings/cloud',
      component: () => import('../views/CloudSettingsView.vue'),
      meta: { title: '整理与播放设置', group: 'STRM 与整理' },
    },
    {
      path: '/settings/external',
      component: () => import('../views/ExternalView.vue'),
      meta: { title: '外部资源服务', group: '全局设置' },
    },
    {
      path: '/system',
      component: () => import('../views/SystemView.vue'),
      meta: { title: '系统状态', group: '系统状态' },
    },
    {
      path: '/workflow',
      component: () => import('../views/WorkflowView.vue'),
      meta: { title: '业务流程图', group: '系统状态' },
    },
    { path: '/resources/plugins', redirect: '/resources' },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
});
router.beforeEach(async (to) => {
  if (to.path === '/login') return true;
  const session = useSession();
  try {
    await session.check();
  } catch (e) {
    useUi().notify(e instanceof Error ? e.message : '无法连接服务', 'error');
    return { path: '/login', query: { redirect: to.fullPath } };
  }
  if (!session.authenticated) return { path: '/login', query: { redirect: to.fullPath } };
});
router.afterEach((to) => (document.title = `${String(to.meta.title || '媒体工作台')} · StrimKeep`));
