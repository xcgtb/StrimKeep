import {createRouter, createWebHashHistory, type RouteRecordRaw} from 'vue-router'

const routes: RouteRecordRaw[] = [
  {path: '/', redirect: {name: 'overview'}},
  {path: '/overview', name: 'overview', component: () => import('./Overview.vue'), meta: {title: '媒体总览'}},
  {path: '/explore', name: 'explore', component: () => import('./Explore.vue'), meta: {title: '影视探索'}},
  {path: '/inventory', name: 'inventory', component: () => import('./Inventory.vue'), meta: {title: '库存管理'}},
  {path: '/account', name: 'account', component: () => import('./Account.vue'), meta: {title: '115 网盘'}},
  {path: '/transfer', name: 'transfer', component: () => import('./Intake.vue'), meta: {title: '资源入库'}},
  {path: '/offline', name: 'offline', redirect: '/transfer'},
  {path: '/sharestrm', name: 'sharestrm', component: () => import('./ShareStrm.vue'), meta: {title: '分享 STRM'}},
  {path: '/channels', name: 'channels', component: () => import('./Channels.vue'), meta: {title: '频道监控'}},
  {path: '/organize', name: 'organize', component: () => import('./Organize.vue'), meta: {title: '影视整理'}},
  {path: '/recognize', name: 'recognize', component: () => import('./Recognize.vue'), meta: {title: '识别测试'}},
  {path: '/sync', name: 'sync', component: () => import('./Sync.vue'), meta: {title: 'STRM 管理'}},
  {path: '/tasks', name: 'tasks', component: () => import('./Tasks.vue'), meta: {title: '任务中心'}},
  {path: '/logs', name: 'logs', component: () => import('./Logs.vue'), meta: {title: '实时日志'}},
  {path: '/notifications', name: 'notifications', component: () => import('./Settings.vue'),
   meta: {title: '通知分类', tab: 'telegram'}},
  {path: '/settings', name: 'settings', component: () => import('./Settings.vue'),
   meta: {title: '系统设置', tab: 'services'}},
  {path: '/:pathMatch(.*)*', redirect: {name: 'overview'}},
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior(_to, _from, saved) {
    return saved || {top: 0}
  },
})
