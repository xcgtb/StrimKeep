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
  Workflow,
} from '@lucide/vue';
export const navigation = [
  {
    group: '工作台',
    items: [
      { to: '/dashboard', name: '运行总览', icon: LayoutDashboard },
      { to: '/resources', name: '资源工作台', icon: Layers },
      { to: '/workflow', name: '业务流程图', icon: Workflow },
    ],
  },
  {
    group: '资源接收',
    items: [
      { to: '/resources/receive', name: '资源接收', icon: CloudDownload },
      { to: '/resources/share', name: '分享免转存', icon: Share2 },
      { to: '/resources/channels', name: '频道监控', icon: Radio },
    ],
  },
  {
    group: '115 网盘',
    items: [
      { to: '/resources/files', name: '文件与目录', icon: FolderOpen },
      { to: '/settings/accounts', name: '115 账号管理', icon: KeyRound },
    ],
  },
  {
    group: '媒体加工',
    items: [
      { to: '/organize/workspace', name: '影视整理', icon: ArrowDownUp },
      { to: '/organize/naming', name: '自定义命名模板', icon: PanelsTopLeft },
      { to: '/organize/quality', name: '整理八维与洗版', icon: ShieldCheck },
      { to: '/organize/automation', name: '整理自动化', icon: CalendarClock },
      { to: '/media/strm', name: 'STRM 生成与播放', icon: FileVideo },
      { to: '/tasks/ingestion', name: 'Emby 入库跟踪', icon: ClipboardCheck },
    ],
  },
  {
    group: '片库管理',
    items: [
      { to: '/media/explore', name: '影视探索', icon: Compass },
      { to: '/media/library', name: '片库映射', icon: LibraryBig },
      { to: '/media/subscriptions', name: '追更订阅', icon: Bell },
    ],
  },
  {
    group: '智能治理',
    items: [
      { to: '/governance', name: '治理总览与扫描', icon: ShieldCheck },
      { to: '/governance/rules', name: '七维质量规则', icon: Settings2 },
      { to: '/governance/cleanup', name: '清理计划', icon: Trash2 },
    ],
  },
  {
    group: '运行与设置',
    items: [
      { to: '/tasks', name: '任务中心', icon: ListTodo },
      { to: '/logs', name: '实时日志', icon: ScrollText },
      { to: '/tasks/reports', name: '入库汇报', icon: FileText },
      { to: '/system', name: '系统状态', icon: Activity },
      { to: '/settings', name: '连接与 API', icon: Settings2 },
      { to: '/settings/cloud', name: '整理与播放参数', icon: PanelsTopLeft },
      { to: '/settings/external', name: '外部资源服务', icon: Plug },
    ],
  },
];
export const allNavigation = navigation.flatMap((group) =>
  group.items.map((item) => ({ ...item, group: group.group })),
);
