import {
  LayoutDashboard,
  HardDrive,
  KeyRound,
  Radio,
  Link2,
  ArrowDownUp,
  FileVideo,
  Share2,
  FolderOpen,
  Compass,
  LibraryBig,
  ShieldCheck,
  ScrollText,
  Bell,
  ClipboardCheck,
  Activity,
  Settings2,
  Trash2,
  PanelsTopLeft,
  Plug,
  FileText,
  SlidersHorizontal,
  WandSparkles,
  CalendarClock,
  Workflow,
} from '@lucide/vue';

// 1.5.12 为功能与 API 基线；10 模块 HTML 提供布局，所有数据使用实际 API。
// 只保留用户确认的 10 个顶级菜单，其中只有「我的网盘」需要展开子栏。
export const navigation = [
  {
    group: '媒体总览',
    icon: LayoutDashboard,
    directTo: '/dashboard',
    items: [{ to: '/dashboard', name: '媒体总览', icon: LayoutDashboard }],
  },
  {
    group: '我的网盘',
    icon: HardDrive,
    directTo: '',
    items: [
      { to: '/settings/accounts', name: '115 网盘配置', icon: KeyRound },
      { to: '/resources/channels', name: '115 频道监控', icon: Radio },
      { to: '/resources/receive', name: '资源接收', icon: Link2 },
      { to: '/organize', name: '影视整理', icon: ArrowDownUp },
      { to: '/media/strm', name: 'STRM 生成', icon: FileVideo },
      { to: '/resources/share', name: '分享 STRM', icon: Share2 },
      { to: '/resources/files', name: '文件管理', icon: FolderOpen },
    ],
  },
  {
    group: '影视探索',
    icon: Compass,
    directTo: '/media/explore',
    items: [{ to: '/media/explore', name: '影视探索', icon: Compass }],
  },
  {
    group: '片库映射',
    icon: LibraryBig,
    directTo: '/media/library',
    items: [{ to: '/media/library', name: '片库映射', icon: LibraryBig }],
  },
  {
    group: '双库治理',
    icon: ShieldCheck,
    directTo: '/governance',
    items: [
      { to: '/governance', name: '双库治理', icon: ShieldCheck },
      { to: '/governance/rules', name: '七维治理明细', icon: Settings2 },
      { to: '/governance/cleanup', name: '目录清理计划', icon: Trash2 },
    ],
  },
  {
    group: '日志中心',
    icon: ScrollText,
    directTo: '/logs',
    items: [{ to: '/logs', name: '日志中心', icon: ScrollText }],
  },
  {
    group: '追更订阅',
    icon: Bell,
    directTo: '/media/subscriptions',
    items: [{ to: '/media/subscriptions', name: '追更订阅', icon: Bell }],
  },
  {
    group: '入库记录',
    icon: ClipboardCheck,
    directTo: '/tasks/reports',
    items: [
      { to: '/tasks/reports', name: '入库记录', icon: ClipboardCheck },
      { to: '/tasks/ingestion', name: 'Emby 入库核验', icon: ClipboardCheck },
    ],
  },
  {
    group: '系统状态',
    icon: Activity,
    directTo: '/system',
    items: [
      { to: '/system', name: '系统状态', icon: Activity },
      { to: '/tasks', name: '任务回执', icon: FileText },
    ],
  },
  {
    group: '全局设置',
    icon: Settings2,
    directTo: '/settings',
    items: [
      { to: '/settings', name: '全局设置', icon: Settings2 },
      { to: '/settings/cloud', name: '整理与播放参数', icon: PanelsTopLeft },
      { to: '/settings/external', name: '外部资源', icon: Plug },
    ],
  },
];

// Keep the function catalogue separate from the sidebar hierarchy.
const moduleTools = [
  {
    to: '/settings/cloud?section=quality',
    name: '源文件治理授权',
    group: '影视整理',
    icon: ShieldCheck,
    keywords: '官方授权 删除源文件 OpenAPI AppID 八维 治理',
  },
  {
    to: '/settings/cloud?section=proxy',
    name: 'Emby 反代',
    group: 'STRM 生成',
    icon: PanelsTopLeft,
    keywords: '监听 端口 播放 302',
  },
  {
    to: '/organize/naming',
    name: '自定义命名模板',
    group: '影视整理',
    icon: WandSparkles,
    keywords: '重命名 文件名 目录 模板 变量',
  },
  {
    to: '/organize/quality',
    name: '八维洗版',
    group: '影视整理',
    icon: SlidersHorizontal,
    keywords: '质量 大小 版本 比较 八维',
  },
  {
    to: '/organize/automation',
    name: '自动整理',
    group: '影视整理',
    icon: CalendarClock,
    keywords: '定时 计划 扫描 立即执行 停止',
  },
  {
    to: '/settings/cloud?section=classification',
    name: '归档分类',
    group: '影视整理',
    icon: FolderOpen,
    keywords: '地区 年份 电影 剧集 分类',
  },
  {
    to: '/settings/cloud?section=filters',
    name: '文件过滤',
    group: '影视整理',
    icon: SlidersHorizontal,
    keywords: '大小 关键词 正则 字幕',
  },
  {
    to: '/settings/cloud?section=notifications',
    name: '入库与通知',
    group: '影视整理',
    icon: Bell,
    keywords: 'TG Telegram Emby 海报 通知',
  },
  {
    to: '/resources/receive?tab=bot',
    name: '机器人接收',
    group: '我的网盘',
    icon: Bell,
    keywords: 'TG Telegram 自动整理 白名单',
  },
  {
    to: '/workflow',
    name: '处理流程',
    group: '系统状态',
    icon: Workflow,
    keywords: '一条龙 自动化 流程',
  },
  {
    to: '/resources',
    name: '全部功能',
    group: '系统状态',
    icon: PanelsTopLeft,
    keywords: '工具 功能 搜索',
  },
];
const aliases: Record<string, string> = {
  '/resources/share': '免转存 分享STRM 分享 strm 生成 整理',
  '/resources/receive': '资源接收 链接转存 离线转存 批量链接 115 转存 秒传 磁力 magnet ed2k 下载 离线任务',
  '/governance/rules': '七维 画质 规则 治理 巡检',
  '/tasks/ingestion': 'Emby 入库 路径 核验 连接',
  '/tasks': '任务 进度 回执 失败 重试 继续 停止',
};
export const allNavigation = [
  ...navigation.flatMap((group) =>
    group.items.map((item) => ({ ...item, group: group.group, keywords: aliases[item.to] || '' })),
  ),
  ...moduleTools,
];

export function matchesNavigation(to: string, path: string, query: Record<string, unknown>) {
  const [target, search] = to.split('?');
  if (target !== path) return false;
  return [...new URLSearchParams(search)].every(
    ([key, value]) =>
      String(query[key] ?? (key === 'tab' ? 'batch' : key === 'section' ? 'output' : '')) === value,
  );
}
export function searchFunctions(keyword: string) {
  const words = keyword.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return allNavigation.filter((item) =>
    words.every((word) =>
      `${item.group} ${item.name} ${item.keywords}`.toLocaleLowerCase().includes(word),
    ),
  );
}
export function moduleName(path: string, section?: unknown) {
  if (
    path.startsWith('/organize/') ||
    (path === '/settings/cloud' &&
      ['classification', 'filters', 'naming', 'quality', 'notifications'].includes(String(section)))
  )
    return 'organize';
  if (path.startsWith('/governance')) return 'governance';
  if (path === '/tasks/ingestion' || path === '/tasks/reports') return 'ingestion';
  if (path === '/system' || path === '/tasks' || path === '/workflow' || path === '/resources')
    return 'system';
  if (
    path === '/media/strm' ||
    (path === '/settings/cloud' && (!section || ['output', 'proxy'].includes(String(section))))
  )
    return 'strm';
  if (path === '/settings' || path === '/settings/external' || path === '/settings/cloud')
    return 'settings';
  return '';
}
export const moduleNavigation: Record<string, { to: string; name: string }[]> = {
  // 顺序按数据流排：从哪里扫 → 什么算有效文件 → 怎么命名 → 归到哪 → 怎么择优 → 删什么 → 何时自动跑 → 跑完通知谁
  organize: [
    { to: '/organize/workspace', name: '扫描与整理' },
    { to: '/settings/cloud?section=filters', name: '需要处理的文件类型' },
    { to: '/organize/naming', name: '命名模板' },
    { to: '/settings/cloud?section=classification', name: '归档分类' },
    { to: '/organize/quality', name: '八维与洗版' },
    { to: '/settings/cloud?section=quality', name: '删除与源文件处理' },
    { to: '/organize/automation', name: '自动整理' },
    { to: '/settings/cloud?section=notifications', name: '入库与通知' },
  ],
  governance: [
    { to: '/governance', name: '治理总览' },
    { to: '/governance/rules', name: '七维与巡检' },
    { to: '/governance/cleanup', name: '目录与残留清理' },
  ],
  ingestion: [
    { to: '/tasks/reports', name: '入库记录与晨报' },
    { to: '/tasks/ingestion', name: 'Emby 入库核验' },
  ],
  system: [
    { to: '/system', name: '服务状态' },
    { to: '/tasks', name: '任务与处理记录' },
    { to: '/workflow', name: '处理流程' },
    { to: '/resources', name: '全部功能' },
  ],
  strm: [
    // 「输出与播放设置」已经内嵌在 /media/strm 页面里（OutputSettings 组件），
    // 不再单列一个跳去 /settings/cloud?section=output 的入口，避免同一份配置两个入口。
    { to: '/media/strm', name: '生成与播放引用' },
    { to: '/settings/cloud?section=proxy', name: 'Emby 反代' },
  ],
  settings: [
    { to: '/settings', name: '服务连接与偏好' },
    { to: '/settings/cloud?section=output', name: 'STRM 与播放' },
    { to: '/settings/cloud?section=proxy', name: 'Emby 反代' },
    { to: '/settings/external', name: '外部资源服务' },
  ],
};
