export interface NavItem {
  id: string;
  name: string;
  icon: string;
  path: string;
  children?: NavItem[];
}
export const navigation: NavItem[] = [
  { id: 'dashboard', name: '媒体总览', icon: 'grid', path: '/dashboard' },
  {
    id: 'cloud',
    name: '我的网盘',
    icon: 'folder',
    path: '/cloud/files',
    children: [
      { id: 'cloud-accounts', name: '115 网盘配置', icon: 'gear', path: '/cloud/accounts' },
      { id: 'cloud-channels', name: '115 频道监控', icon: 'list', path: '/cloud/channels' },
      { id: 'cloud-receive', name: '链接转存', icon: 'share', path: '/cloud/receive' },
      { id: 'cloud-offline', name: '离线转存', icon: 'download', path: '/cloud/offline' },
      { id: 'cloud-organize', name: '影视整理', icon: 'film', path: '/cloud/organize' },
      { id: 'cloud-strm', name: 'STRM 生成', icon: 'file', path: '/cloud/strm' },
      { id: 'cloud-share', name: '分享 STRM 生成与整理', icon: 'share', path: '/cloud/share' },
      { id: 'cloud-files', name: '文件管理', icon: 'folder', path: '/cloud/files' },
    ],
  },
  { id: 'explore', name: '影视探索', icon: 'film', path: '/explore' },
  { id: 'mapping', name: '片库映射', icon: 'map', path: '/mapping' },
  {
    id: 'governance',
    name: '双库治理',
    icon: 'scale',
    path: '/governance',
    children: [
      { id: 'governance-main', name: '双库治理', icon: 'scale', path: '/governance' },
      { id: 'orphans', name: '目录清理', icon: 'trash', path: '/orphans' },
      { id: 'plans', name: '计划存档', icon: 'archive', path: '/plans' },
    ],
  },
  { id: 'history', name: '日志中心', icon: 'list', path: '/history' },
  { id: 'subscribe', name: '追更订阅', icon: 'bell', path: '/subscribe' },
  { id: 'daily', name: '入库记录', icon: 'file', path: '/daily' },
  { id: 'system', name: '系统状态', icon: 'activity', path: '/system' },
  { id: 'settings', name: '全局设置', icon: 'gear', path: '/settings' },
];
export const flatNavigation = navigation.flatMap((item) =>
  item.children ? [item, ...item.children] : [item],
);
