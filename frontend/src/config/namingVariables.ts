/** Backend allowlist: app/cloud/naming.py FIELDS. Keep this in sync when the API evolves. */
export interface NamingVariable {
  key: string;
  name: string;
  explanation: string;
  example: string;
  category: '作品信息' | '季集信息' | '画质版本' | '文件属性';
  usage: string;
}
const v = (
  key: string,
  name: string,
  explanation: string,
  example: string,
  category: NamingVariable['category'],
  usage = '电影与剧集',
): NamingVariable => ({ key, name, explanation, example, category, usage });
export const namingVariables: NamingVariable[] = [
  v('title', '作品名称', 'TMDB 或文件识别出的影片／剧集名称', '深海来信', '作品信息'),
  v(
    'original_title',
    '原始片名',
    '可用时使用 TMDB 原名；不保证每部作品都有',
    'Deep Sea Letters',
    '作品信息',
  ),
  v('year', '上映／首播年份', '作品匹配得到的年份；无法识别时不猜测', '2026', '作品信息'),
  v('tmdb_id', 'TMDB 编号', '用于区分同名作品的 TMDB 作品 ID', '314159', '作品信息'),
  v('category', '归档分类', '整理分类引擎产生的分类名称', '日韩剧', '作品信息'),
  v('region', '地区', '地区归档规则确定的国家或地区', 'JP', '作品信息'),
  v('season', '季号', '未经补零的季序号，例如第 1 季为 1', '1', '季集信息', '仅剧集与季目录'),
  v(
    'season:02d',
    '两位季号',
    '固定两位，不足补 0；第 1 季为 01',
    '01',
    '季集信息',
    '仅剧集与季目录',
  ),
  v('season:03d', '三位季号', '固定三位，不足补 0', '001', '季集信息', '仅剧集与季目录'),
  v('episode', '集号', '未经补零的集序号', '2', '季集信息', '仅剧集文件名'),
  v('episode:02d', '两位集号', '第 2 集显示为 02', '02', '季集信息', '仅剧集文件名'),
  v('episode:03d', '三位集号', '第 2 集显示为 002', '002', '季集信息', '仅剧集文件名'),
  v(
    'episode_title',
    '分集标题',
    '该集已识别出的标题，可能为空',
    '新的出发',
    '季集信息',
    '仅剧集文件名',
  ),
  v('resolution', '分辨率', '视频画质分辨率，例如 2160p', '2160p', '画质版本'),
  v('source', '资源类型', 'WEB-DL、BluRay、Remux 等片源信息', 'WEB-DL', '画质版本'),
  v('codec', '视频编码（简写）', '兼容原有文件命名的主视频编码', 'H265', '画质版本'),
  v('video_codec', '视频编码', '媒体信息识别出的具体视频编码', 'H265', '画质版本'),
  v('audio_codec', '音频编码', '音轨格式（不能单独代表声道数量）', 'E-AC-3', '画质版本'),
  v('dynamic_range', '动态范围', 'HDR、Dolby Vision 或 SDR，取识别结果', 'HDR10', '画质版本'),
  v('fps', '帧率', '视频每秒帧数', '24', '画质版本'),
  v('bitdepth', '色深', '8bit、10bit 等位深信息', '10bit', '画质版本'),
  v('release_group', '发布组', '资源文件名中识别出的发布或压制小组', 'GROUP', '画质版本'),
  v(
    'ext',
    '视频扩展名',
    '视频文件名模板必须保留此变量，原样保留扩展名',
    '.mkv',
    '文件属性',
    '电影与剧集文件名（必填）',
  ),
  v('original_name', '原始文件名', '原始视频文件名称', 'Deep.Sea.2026.mkv', '文件属性'),
  v('width', '视频宽度', '横向像素数量', '3840', '文件属性'),
  v('height', '视频高度', '纵向像素数量', '2160', '文件属性'),
  v('audio_channels', '音频声道数', '音轨的声道数量', '6', '文件属性'),
  v('audio_layout', '声道布局', '5.1 或 7.1 等布局信息', '5.1', '文件属性'),
  v('duration', '视频时长', '媒体信息提供的时长，通常为秒', '3600', '文件属性'),
];
export const namingTemplateFields = [
  { key: 'movie_directory_template', name: '电影作品目录', hint: '例如 片名 (年份) [tmdb-编号]' },
  { key: 'movie_file_template', name: '电影文件名', hint: '必须包含 {ext}，不可使用季集号' },
  { key: 'tv_directory_template', name: '剧集作品目录', hint: '每部剧的顶层作品文件夹' },
  { key: 'season_directory_template', name: '剧集季目录', hint: '必须包含 {season} 或格式化季号' },
  { key: 'tv_file_template', name: '剧集文件名', hint: '必须包含 {ext}，可使用季集号' },
] as const;
export type NamingTemplateKey = (typeof namingTemplateFields)[number]['key'];

export function validateTemplate(key: NamingTemplateKey, value: string): string {
  if (!value.trim()) return '模板不能为空';
  if (value.length > 500) return '模板不能超过 500 个字符';
  const allowed = new Set(namingVariables.map((v) => v.key.split(':')[0]));
  const used = new Set<string>();
  let invalid = '';
  const literal = value.replace(
    /\{\{|\}\}|\{([^{}]*)\}|[{}]/g,
    (match, token: string | undefined) => {
      if (match === '{{' || match === '}}') return '';
      if (token === undefined) {
        invalid = '模板括号不匹配';
        return '';
      }
      const parts = /^([a-z_]+)(?::(02d?|03d?))?$/.exec(token);
      if (
        !parts ||
        !allowed.has(parts[1]!) ||
        (parts[2] && !['season', 'episode'].includes(parts[1]!))
      )
        invalid = '含未知变量或不支持的格式';
      else used.add(parts[1]!);
      return '';
    },
  );
  if (invalid) return invalid;
  if (/[\\/<>:"|?*\x00-\x1f]/.test(literal)) return '模板只填写名称，不能包含路径分隔符或非法字符';
  if (!used.size) return '模板至少包含一个变量';
  if (key.endsWith('file_template') && !used.has('ext')) return '视频文件名模板必须包含 {ext}';
  if (key.startsWith('movie_') && (used.has('season') || used.has('episode')))
    return '电影模板不可使用季号或集号';
  if (key === 'season_directory_template' && !used.has('season'))
    return '季目录模板必须包含 {season}';
  return '';
}
export function sampleTemplate(value: string): string {
  const values = Object.fromEntries(namingVariables.map((v) => [v.key, v.example]));
  return value.replace(/\{\{|\}\}|\{([^{}]*)\}/g, (match, token: string | undefined) =>
    match === '{{' ? '{' : match === '}}' ? '}' : String(values[token || ''] ?? ''),
  );
}
