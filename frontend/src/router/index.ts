import { createRouter, createWebHashHistory } from 'vue-router';

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    // —— 1.4.7 视觉层模块 ——
    { path: '/dashboard', component: () => import('../views/DashboardView.vue') },
    { path: '/mapping', component: () => import('../views/MappingView.vue') },
    { path: '/explore', component: () => import('../views/ExploreView.vue') },
    { path: '/governance', component: () => import('../views/GovernanceView.vue') },
    { path: '/orphans', component: () => import('../views/OrphansView.vue') },
    { path: '/plans', component: () => import('../views/PlansView.vue') },
    { path: '/history', component: () => import('../views/HistoryView.vue') },
    { path: '/subscribe', component: () => import('../views/SubscribeView.vue') },
    { path: '/daily', component: () => import('../views/DailyView.vue') },
    { path: '/system', component: () => import('../views/SystemView.vue') },
        // —— 网盘：按 1.4.7 样式逐页手写，接口取 1.5.9 参数 ——
    {
      path: '/cloud',
      component: () => import('../views/CloudLayout.vue'),
      children: [
        { path: '', redirect: '/cloud/files' },
        { path: 'files', component: () => import('../views/FilesView.vue') },
        { path: 'strm', component: () => import('../views/StrmView.vue') },
        { path: 'organize', component: () => import('../views/OrganizeView.vue') },
        { path: 'receive', component: () => import('../views/ReceiveView.vue') },
        { path: 'offline', redirect: '/cloud/receive?tab=offline' },
        { path: 'share', component: () => import('../views/ShareView.vue') },
        { path: 'channels', component: () => import('../views/ChannelsView.vue') },
        { path: 'accounts', component: () => import('../views/AccountsView.vue') },
      ],
    },
    { path: '/settings', component: () => import('../views/SettingsView.vue') },
    { path: '/:rest(.*)*', redirect: '/dashboard' },
  ],
});
