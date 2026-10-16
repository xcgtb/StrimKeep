import {
  LayoutDashboard,
  Compass,
  LibraryBig,
  Bell,
  FolderOpen,
  ListTodo,
  Settings2,
  CloudDownload,
  Share2,
  ArrowDownUp,
  CalendarClock,
  Radio,
  FileVideo,
  ShieldCheck,
  Trash2,
  Activity,
  ClipboardCheck,
  FileText,
  ScrollText,
  KeyRound,
  PanelsTopLeft,
  Plug,
  Layers,
} from '@lucide/vue';
export const navigation = [
  {
    group: '概览',
    items: [
      { to: '/dashboard', name: '运行总览', icon: LayoutDashboard },
      { to: '/resources', name: '资源与工具', icon: Layers },
    ],
  },
  {
    group: '媒体中心',
    items: [
      { to: '/media/explore', name: '影视探索', icon: Compass },
      { to: '/media/library', name: '片库映射', icon: LibraryBig },
      { to: '/media/subscriptions', name: '追更订阅', icon: Bell },
      { to: '/media/strm', name: 'STRM 资源', icon: FileVideo },
    ],
  },
  {
    group: '资源与自动化',
    items: [
      { to: '/resources/receive', name: '资源接收', icon: CloudDownload },
      { to: '/resources/share', name: '分享免转存', icon: Share2 },
      { to: '/resources/files', name: '115 文件管理', icon: FolderOpen },
      { to: '/organize/workspace', name: '媒体整理', icon: ArrowDownUp },
      { to: '/organize/automation', name: '整理自动化', icon: CalendarClock },
      { to: '/resources/channels', name: '频道与机器人', icon: Radio },
    ],
  },
  {
    group: '治理',
    items: [
      { to: '/governance', name: '治理决策', icon: ShieldCheck },
      { to: '/governance/rules', name: '七维与治理规则', icon: Settings2 },
      { to: '/governance/cleanup', name: '目录与残留清理', icon: Trash2 },
    ],
  },
  {
    group: '运行管理',
    items: [
      { to: '/tasks', name: '任务中心', icon: ListTodo },
      { to: '/tasks/ingestion', name: 'Emby 入库跟踪', icon: ClipboardCheck },
      { to: '/tasks/reports', name: '入库汇报与晨报', icon: FileText },
      { to: '/logs', name: '运行日志', icon: ScrollText },
      { to: '/system', name: '系统状态', icon: Activity },
    ],
  },
  {
    group: '设置',
    items: [
      { to: '/settings/accounts', name: '115 账号与目录', icon: KeyRound },
      { to: '/settings', name: '连接与设置', icon: Settings2 },
      { to: '/settings/cloud', name: '整理与播放设置', icon: PanelsTopLeft },
      { to: '/settings/external', name: '外部资源服务', icon: Plug },
    ],
  },
];
export const allNavigation = navigation.flatMap((group) =>
  group.items.map((item) => ({ ...item, group: group.group })),
);
