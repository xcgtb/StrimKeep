"""Conservative identity, classification and naming. No filename fallback for failed probing."""
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path


VIDEOS = {'.mkv', '.mp4', '.m4v', '.mov', '.avi', '.ts', '.m2ts', '.webm'}
KNOWN_VIDEOS = VIDEOS | {'.iso', '.wmv', '.flv', '.rmvb', '.mpeg', '.mpg', '.mts'}
SUBTITLES = {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.sup'}
# 转存资源常自带海报与刮削元数据。这些不属于输出内容，整理后留在原目录会让
# 空目录清理无法进行，因此单独识别，供清理阶段一并回收。
PROBE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 115wangpan_ios/36.2.20'

METADATA = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tbn',
            '.nfo', '.xml', '.sfv', '.md5', '.sha1'}
CATEGORIES = ('电影', '剧集', '动漫', '综艺', '儿童节目', '纪录片', '演唱会')
REGIONS = ('国产', '欧美', '日韩', '其他')
WESTERN = set('US GB FR DE IT ES PT CA AU NZ IE AT BE CH DK FI IS NO SE NL LU GR PL CZ HU RO BG UA RU BR MX AR CL CO'.split())


from .core import NeedsReview  # noqa: F401  （re-export：历史代码仍从 media 取它）
from .audio import naming_track


def component(value, limit=150):
    text = unicodedata.normalize('NFC', str(value))
    text = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', text).strip(' .')
    if not text or text in ('.', '..'):
        raise NeedsReview('名称缺失或不安全。')
    # Linux filename limit is in bytes, not characters; leave room for extensions.
    return text.encode('utf-8')[:limit].decode('utf-8', errors='ignore').rstrip(' .')


def normal(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value).casefold())


CN_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
SEASON_MARK = re.compile(r'(?i)(?:\bS(\d{1,2})\b|第\s*([一二三四五六七八九十\d]{1,3})\s*季|Season\s*(\d{1,2}))')
# 只表示季号的目录（Season 1 / S02 / 第二季），作品名在上一层。
SEASON_DIR = re.compile(r'(?i)^(?:Season\s*\d{1,2}|S\d{1,2}|第\s*[一二三四五六七八九十\d]{1,3}\s*季)$')
IDENTITY = re.compile(r'(?i)(?:tmdb(?:id)?[=: -]|\{tmdb[- ])(\d+)')
# 中文发布名常见的质量/音轨尾巴，去掉才能拿目录名去检索。
CN_QUALITY_TAIL = re.compile(r'(?:杜比视界|杜比全景声|国语|粤语|中字|简繁|双语|原盘|重制|合集)\s*$')
QUALITY_TAIL = re.compile(r'(?i)\b(?:2160p|1080p|720p|4k|web[ ._-]?dl|webrip|bluray|blu-ray|remux|hdr|dovi|hevc|h[ .]?26[45]|avc|aac|flac|ddp?|truehd|atmos)\b.*$')

# 同一平台在发布名里有缩写、全名、中文三种写法。命名统一成播放器里最好认的
PLATFORM_NAMES = (
    ('Apple TV+', ('ATVP', 'AppleTV', 'Apple TV+', 'Apple TV Plus')),
    ('Amazon', ('AMZN', 'Amazon')),
    ('Disney+', ('DSNP', 'DisneyPlus', 'Disney+', 'Disney Plus')),
    ('Max', ('HMAX', 'HBOMax', 'HBO Max')),
    ('Netflix', ('NF', 'Netflix')),
    ('iTunes', ('iTunes',)),
    ('Paramount+', ('PMTP', 'ParamountPlus', 'Paramount+')),
    ('Hulu', ('HULU',)),
    ('Crunchyroll', ('CR', 'Crunchyroll')),
    ('TVING', ('TVING',)),
    ('friDay', ('friDay',)),
    ('KKTV', ('KKTV',)),
    ('MyVideo', ('MyVideo',)),
    ('Hami', ('Hami',)),
    ('CATCHPLAY', ('CATCHPLAY',)),
    ('Viu', ('Viu',)),
    ('Baha', ('Baha', '巴哈')),
    ('Abema', ('Abema',)),
    ('LINE TV', ('LINETV', 'LINE TV')),
    ('myTV SUPER', ('myTVSuper',)),
    # 中文平台名一律用 ASCII 大写：文件名里夹中文会让搜索和排序都变难看。
    ('TENCENT', ('WeTV', '腾讯')),
    ('IQIYI', ('IQ', 'iQIYI', '爱奇艺')),
    ('YOUKU', ('YOUKU', '优酷')),
    ('BILIBILI', ('BILI', 'Bilibili', '哔哩哔哩')),
)

# 这些词长得像发布组但其实是规格尾巴：WEB-DL 的 -DL、WEBRip 的 -Rip 都在其中。
GROUP_STOP = {'DL', 'WEB', 'WEBRIP', 'RIP', 'RAY', 'HD', 'FHD', 'UHD', 'HDR', 'DV', 'AVC',
              'HEVC', 'H264', 'H265', 'X264', 'X265', 'AAC', 'DDP', 'DD', 'AC3', 'DTS', 'MA',
              'HRA', 'REMUX', 'BLURAY', 'TRUEHD', 'ATMOS', '10BIT', 'HDR10', 'P5', 'P7', 'P8',
              'SDR', 'H26510BIT'}


def _alias_pattern(alias):
    return r'[ ._-]?'.join(re.escape(part) for part in alias.casefold().split(' '))


def platform_of(name):
    """发布名里的平台标记 → 展示名；多种写法命中同一平台时取最靠前的。"""
    low = str(name or '').casefold()
    hits = []
    for display, aliases in PLATFORM_NAMES:
        for alias in aliases:
            match = re.search(r'(?<![a-z0-9])' + _alias_pattern(alias) + r'(?![a-z0-9])', low)
            if match:
                hits.append((match.start(), display))
                break
    return min(hits)[1] if hits else ''


def source_of(name):
    """资源类型 → 规范写法：区分 UHD/BluRay/REMUX 的组合，不再一律大写。"""
    text = str(name or '')
    bluray = re.search(r'(?i)\b(?:UHD[ ._-]*)?(?:BLU[ ._-]?RAY|BDREMUX|BD[ ._-]?REMUX)\b', text)
    remux = re.search(r'(?i)\b(?:UHD[ ._-]*)?(?:BD)?REMUX\b', text)
    uhd = re.search(r'(?i)\bUHD\b', text)
    if bluray:
        base = 'UHD BluRay' if uhd else 'BluRay'
        return base + ' REMUX' if (remux or 'REMUX' in bluray[0].upper()) else base
    if remux:
        return 'REMUX'
    for pattern, label in ((r'(?i)\bWEB[ ._-]?DL\b', 'WEB-DL'), (r'(?i)\bWEBRIP\b', 'WEBRip'),
                           (r'(?i)\bHDTV\b', 'HDTV'), (r'(?i)\bDVD(?:RIP)?\b', 'DVD')):
        if re.search(pattern, text):
            return label
    return ''


# 真实媒体/字幕扩展名。只有命中它才剥后缀：分享来的文件名常常没有扩展名
# （`...S01E46-1080p.WEB-DL-H264-AAC@ADWeb`），见 release_group 的说明。
MEDIA_SUFFIXES = ('.mkv', '.mp4', '.m2ts', '.mts', '.ts', '.avi', '.rmvb', '.rm', '.wmv',
                  '.flv', '.mov', '.mpg', '.mpeg', '.m4v', '.webm', '.vob', '.iso', '.tp',
                  '.f4v', '.3gp', '.strm', '.nfo', '.srt', '.ass', '.ssa', '.sup', '.idx')


def media_stem(name):
    """文件名主干：只剥真实媒体扩展名，不按「最后一个点」硬切。

    `Path(name).stem` 会把没有扩展名的分享文件名最后一段当后缀剥掉：
    `…S01E46-1080p.WEB-DL-H264-AAC@ADWeb` → `…S01E46-1080p`，画质、编码、组别
    全消失（现场：转生成猫的大叔，通知里组别是 `DL-H264-AAC@ADWeb`，
    落盘文件名却没有任何组别）。
    """
    text = str(name or '').strip()
    low = text.casefold()
    for suffix in MEDIA_SUFFIXES:
        if low.endswith(suffix):
            return text[:-len(suffix)]
    return text


def release_group(name):
    """结尾的 -Group / @Group；排除 WEB-DL 这类规格尾巴。

    真实发布名有三种写法，都要能认出组别：
    `…DDP 5.1-NTb`（`-` 直接跟组名）、
    `…DDP 5.1-SCDIY@HiveWeb`（`@` 前面压制者、后面站点）、
    `…WEB-DL-H264-AAC@ADWeb`（组名直接挂在编码后面，中间只有 `@`）。
    """
    stem = media_stem(name)
    # 发布组紧跟在规格后面（DDP5.1-ParkTV、H.265-NTb），所以不能用字母边界卡，
    # 改由 GROUP_STOP 排除 WEB-DL/DTS-HD 这类被 '-' 拆开的规格词。
    match = re.search(r'-([A-Za-z][A-Za-z0-9@_]{1,40})$', stem)
    if match:
        value = match.group(1).rsplit('@', 1)[-1]
    else:
        # `AAC@ADWeb`、`DDP5.1@ADWeb`：`-` 后面还夹着点号，退一步只取 `@` 之后那段。
        match = re.search(r'@([A-Za-z][A-Za-z0-9_]{1,40})$', stem)
        value = match.group(1) if match else ''
    if not value or value.upper() in GROUP_STOP:
        return ''
    return value


def dynamic_label(media):
    value = media.get('dynamic_range')
    label = {'Dolby Vision HDR': 'DoVi HDR', 'Dolby Vision': 'DoVi', 'HDR10+': 'HDR10+',
             'HDR Vivid': 'HDR Vivid', 'HDR10': 'HDR10', 'HDR PQ': 'HDR', 'HLG': 'HLG', 'SDR': 'SDR'}.get(value)
    if not label:
        return ''
    if value in ('Dolby Vision', 'Dolby Vision HDR') and media.get('dolby_vision_profile') is not None:
        label += ' P' + str(media['dolby_vision_profile'])
    return label



def season_number(mark):
    raw = mark.group(1) or mark.group(3) or mark.group(2) or ''
    return CN_NUM.get(raw) if raw in CN_NUM else (int(raw) if raw.isdigit() else None)


def numeral_variants(query):
    """中文数字结尾的两种兜底查询：转阿拉伯数字、去尾数（数字当季号）。

    「法证先锋一」→ [('法证先锋1', None), ('法证先锋', 1)]：
    TMDB 上有的部数是独立条目（法证先锋3/4/5），有的只是同剧分季，两种都要试。
    """
    text = str(query or '').strip()
    if not text or not re.search(r'[一二三四五六七八九十]$', text):
        return []
    digit = CN_NUM[text[-1]]
    variants = []
    arabic = text[:-1] + str(digit)
    if arabic != text:
        variants.append((arabic, None))
    stripped = text[:-1].strip()
    if stripped:
        variants.append((stripped, digit))
    return variants


def _folder_title(text):
    """目录名 → (标题, 年份, tmdb id)。去掉方括号标记、年份和质量尾巴。"""
    identity = IDENTITY.search(text)
    tmdb_id = int(identity[1]) if identity else None
    text = re.sub(r'[\[【][^\]】]*[\]】]', ' ', text)
    text = re.sub(r'\{[^}]*\}', ' ', text)
    years = re.findall(r'(?<![0-9])((?:19|20)\d{2})(?![0-9])', text)
    year = int(years[-1]) if years else None
    # 发布说明紧跟年份时，必须在去掉年份之前识别其位置，避免把作品标题
    # 中的「国语」「字幕」等文字误当成标签删掉。
    text = re.sub(
        r'([\[(（]\s*(?:19|20)\d{2}\s*[\])）])'
        r'(?:(?:[\s._-]*)(?:国粤双语|国语中字|粤语中字|国语双语|'
        r'简繁字幕|繁中字幕|简中字幕|中文字幕|中英字幕|国语|粤语|双语|中字)'
        r'(?=$|[\s._-]|(?:国粤双语|国语中字|粤语中字|简繁字幕|繁中字幕|简中字幕|中文字幕|中英字幕)))+',
        r'\1 ', text, flags=re.I)
    text = re.sub(r'[\[(（]?\s*(?:19|20)\d{2}\s*[\])）]?', ' ', text)
    text = QUALITY_TAIL.sub(' ', text)
    while True:
        stripped = CN_QUALITY_TAIL.sub('', text).strip()
        if stripped == text.strip():
            break
        text = stripped
    title = re.sub(r'[._]', ' ', text)
    title = re.sub(r'\s+', ' ', title).strip(' -–—()[]{}·')
    return title, year, tmdb_id


# 这些是流程目录而不是作品目录：拿它们当剧名只会制造误匹配。
GENERIC_DIRS = {'未识别', '待整理', '转存', '转存待整理', '归档', '媒体库', '影视媒体库',
                '下载', '最近接收', 'strimkeep同步', '分享影视库', '分享strm未识别', '分享strm待整理'}
GENERIC_KEYS = {normal(x).casefold() for x in GENERIC_DIRS}


def folder_context(folder):
    """从父目录路径推断（剧名, 年份, 季号, tmdb id）。

    支持「作品目录/Season 1」这种常见层级：末尾的季目录只提供季号，
    作品名与 ID 取上一层，避免只看到 Season 1 就把作品名和 ID 丢掉。
    取不到像样的标题就返回 None。
    """
    text = str(folder or '').strip().strip('/\\')
    if not text:
        return None
    parts = [p.strip() for p in re.split(r'[\\/]+', text) if p.strip()]
    season = None
    while parts and SEASON_DIR.match(parts[-1]):
        mark = SEASON_MARK.search(parts[-1])
        season = season_number(mark) if mark else season
        parts.pop()
    if not parts:
        return None
    # 命中条件按可信度递减——带 tmdb id 的最可信，其次带年份，最后才是裸标题；
    # 只有整条路径都没有线索时才退化成「最近的一层」。
    fallback = None
    for depth in range(1, len(parts) + 1):
        work = parts[-depth]
        mark = SEASON_MARK.search(work)
        part_season = season
        stripped = work
        if mark:
            part_season = season_number(mark) or season
            stripped = (work[:mark.start()] + ' ' + work[mark.end():]).strip()
        title, year, tmdb_id = _folder_title(stripped)
        if not title or normal(title).casefold() in GENERIC_KEYS:
            continue
        context = {'title': title, 'year': year, 'season': part_season}
        if tmdb_id:
            context['tmdb_id'] = tmdb_id
        if tmdb_id:
            return context
        if fallback is None or (year and not fallback.get('year')):
            fallback = context
        if year:
            # 带年份的作品目录已经够可靠，不再往上找更粗的层级。
            return context
    return fallback


def infer_episode(stem, title=''):
    """文件名只有集号时推断集数：纯数字、EP12、第12集、或「剧名 (12)」。"""
    text = re.sub(r'[._]', ' ', stem).strip()
    numbered = re.search(r'(?i)(?:\bEP?|第)\s*(\d{1,4})\s*(?:集|话|話)?$', text)
    if numbered:
        return int(numbered.group(1))
    if re.fullmatch(r'\d{1,4}', text):
        return int(text)
    if title:
        prefix = re.sub(r'\s+', ' ', title).strip()
        rest = text
        if prefix and text.lower().startswith(prefix.lower()):
            rest = text[len(prefix):]
        rest = rest.strip(' -–—_()[]{}·')
        if re.fullmatch(r'\d{1,4}', rest):
            return int(rest)
    return None


def parse_name(name, folder=None):
    stem = Path(name).stem
    context = folder_context(folder)
    episode = re.search(r'(?i)(?<![a-z0-9])S(\d{1,2})[ ._-]*E(\d{1,3})(?:(?:[ ._-]*E|-)(\d{1,3})(?![a-z0-9]))?(?!\d)', stem)
    # Support S01E01-E02; reject ambiguous tail or bare absolute episode numbers.
    season = int(episode[1]) if episode else None
    first = int(episode[2]) if episode else None
    last = int(episode[3]) if episode and episode[3] else first
    if episode and (first == 0 or last < first or last - first > 30):
        raise NeedsReview('季集编号无效或合并集范围不可靠。')
    # 中文/短集号：第12集、第12话、EP15、E08。没有季号时按父目录季号，
    if not episode:
        marked = (re.search(r'第\s*(\d{1,4})\s*[集话話]', stem)
                  or re.search(r'(?i)(?<![a-z0-9])E(?:P)?[ ._-]?(\d{1,4})(?![a-z0-9])', stem))
        if marked and 1 <= int(marked[1]) <= 2000:
            episode = marked
            first = last = int(marked[1])
            mark = SEASON_MARK.search(stem)
            season = ((season_number(mark) if mark else None)
                      or (context.get('season') if context else None) or 1)
    technical = re.search(r'(?i)(?<![a-z0-9])(?:2160p|1080p|720p|4k|web[ ._-]?dl|bluray|hevc)(?![a-z0-9])', stem)
    # 年份可能排在 SxxExx 之后（如「剧名.S01E01.2026.2160p」），所以只以技术规格
    # 为边界；用季集号截断会把这类年份整段丢掉。
    cutoff = technical.start() if technical else len(stem)
    # Prefer an explicit parenthesized release year; numeric titles such as 1917
    # must not become the release year when followed by (2019).
    explicit_years = list(re.finditer(r'[\[(（]((?:19|20)\d{2})[\])）]', stem[:cutoff]))
    plain_years = list(re.finditer(r'(?<![a-zA-Z0-9])((?:19|20)\d{2})(?![a-zA-Z0-9])', stem[:cutoff]))
    year_match = (explicit_years or plain_years or [None])[-1]
    identity = IDENTITY.search(stem)
    file_tmdb = int(identity[1]) if identity else None
    folder_tmdb = context.get('tmdb_id') if context else None
    # 文件和目录各带身份时，只有完全一致才继续；冲突一律停手，交给人工核查。
    if file_tmdb and folder_tmdb and file_tmdb != folder_tmdb:
        raise NeedsReview('文件名与父目录的 TMDB 标识不一致，已停止自动命名。')
    if episode and season and context and context.get('season') and context['season'] != season:
        raise NeedsReview('文件季号与父目录季号不一致，已停止自动命名。')
    cuts = [m.start() for m in (episode, year_match, identity) if m]
    title = stem[:min(cuts)] if cuts else re.split(r'(?i)[ ._-](?:2160p|1080p|720p|4k|web[ .-]?dl|bluray|h[ .]?26[45]|hevc)\b', stem)[0]
    # 先清噪音再去边：直接 strip 会把 "[Group]" 削成 "Group]"，之后再也认不出方括号。
    title = title_noise(re.sub(r'[._]', ' ', title))
    title = strip_episode_suffix(title)
    # 文件名只剩集号（或标题为空）时，用可靠的父目录补作品名；目录至少要有
    # 年份、季号或 tmdb id 之一才算可靠，避免把「未识别」这种目录名当剧名。
    reliable_folder = bool(context and (folder_tmdb or context.get('year') or context.get('season')))
    if (not title or re.fullmatch(r'\d{1,4}', title.strip())) and reliable_folder:
        title = context['title']
        if not file_tmdb and not folder_tmdb:
            identity_source = 'ancestor_directory'
    identity_source = 'file_tmdb_id' if file_tmdb else (
        'context_tmdb_id' if folder_tmdb else 'filename')
    if not title and not identity and not folder_tmdb:
        raise NeedsReview('原始名称没有可靠的作品检索线索。')
    # 文件名没有季集、但父目录像剧名（整季一个文件夹很常见）：用目录当剧名、文件序号当集号。
    # 只在没有显式 tmdb id、且序号能被明确读出来时启用；其余一律走原来的判定。
    if not episode and not identity:
        if context:
            number = infer_episode(stem, context['title'])
            if number and 1 <= number <= 2000:
                return {'query': context['title'], 'year': context['year'], 'media_type': 'tv',
                        'season': context['season'] or 1, 'episode': number, 'end_episode': number,
                        'tmdb_id': folder_tmdb,
                        'identity_source': 'context_tmdb_id' if folder_tmdb else 'ancestor_directory'}
    if not episode and re.search(r'(?i)(?:\b(?:S\d{1,2}|E\d{1,3}|OVA|SP|special)\b|第\s*\d+\s*[集话季])', stem):
        raise NeedsReview('疑似分集节目，但缺少明确 SxxExx 季集映射。')
    # 目录身份只用于分集；电影文件不会被塞进剧集目录的身份。
    tmdb_id = file_tmdb or (folder_tmdb if (episode or (context and context.get('season'))) else None)
    # 目录年份只补给分集；电影文件不继承剧集目录的年份。
    year = int(year_match[1]) if year_match else (context.get('year') if (context and episode) else None)
    return {'query': title, 'year': year,
            'media_type': 'tv' if episode else 'movie', 'season': season,
            'episode': first, 'end_episode': last, 'tmdb_id': tmdb_id,
            'identity_source': identity_source}



TITLE_NOISE = re.compile(r'(?i)(?:(?<=[\s._-])|^)(?:电视剧|电视版|连续剧|动画片|动画|电影|高清|超清|蓝光|原盘|完整版|未删减|国语|粤语|双语|中字|简繁|HD)(?=$|[\s._-])')


def title_noise(title):
    """去掉标题里的发布噪音：[组名]、季标记、电视剧/HD 这类非作品词。"""
    text = re.sub(r'^[\[【][^\]】]*[\]】]\s*', '', str(title or ''))
    text = text.strip(' -–—([{')
    mark = SEASON_MARK.search(text)
    if mark:
        text = (text[:mark.start()] + ' ' + text[mark.end():])
    while True:
        cleaned = TITLE_NOISE.sub(' ', text)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -–—_·')
        if cleaned == text.strip(' -–—_·'):
            text = cleaned
            break
        text = cleaned
    return text or str(title or '').strip()

def split_video_marker(name):
    """识别 P1/P2 分段线索；明确的杜比视界 profile 不是分段。"""
    stem = Path(str(name)).stem
    match = re.search(r'(?i)(?<![a-z0-9])p([12])(?![a-z0-9])', stem)
    if not match:
        match = re.search(r'(?i)p([12])$', stem)
    if not match:
        return None
    year = re.search(r'(?<!\d)(?:19|20)\d{2}(?!\d)', stem)
    if year and match.start() < year.start():
        return None
    before = stem[:match.start()]
    if re.search(r'(?i)(?:dovi|dolby[ ._-]*vision|dv)[ ._-]*$', before):
        return None
    # Removing the complete marker yields the same key for a P1/P2 pair.
    base = (stem[:match.start()] + stem[match.end():]).casefold().strip(' ._-')
    return (base, int(match.group(1))) if base else None


def chinese_split_marker(name):
    """片名末尾的上/下仅作为配对线索，不凭单个字判断。"""
    stem = Path(str(name)).stem
    match = re.search(r'(上|下)(?=(?:[ ._-]*(?:\(? (?:19|20)\d{2} |1080p|2160p|720p|4k|WEB|$)))', stem, re.I | re.X)
    if not match:
        return None
    base = (stem[:match.start()] + stem[match.end():]).casefold().strip(' ._-')
    return (base, match.group(1)) if base else None


def require_single_episode(hint, name=''):
    """拒绝分段或合并集，避免将不完整视频按单集自动归档。"""
    if split_video_marker(name):
        raise NeedsReview('疑似 P1/P2 分段，不按完整视频自动整理。')
    if hint.get('media_type') == 'tv':
        stem = Path(str(name)).stem
        # Only inspect after a season/episode marker, so a series title such
        # as "Part 1" before S01E01 is not mistaken for a split file.
        episode_marker = re.search(r'(?i)(?:s\d{1,2}[ ._-]*e\d{1,3}|(?<![a-z0-9])ep?\s*\d{1,3}|第\s*\d{1,3}\s*[集话話])', stem)
        suffix = stem[episode_marker.end():] if episode_marker else stem
        if (re.search(r'(?i)(?<![a-z0-9])(?:part|cd|disc|disk)[ ._-]*[1-9]\d?(?![a-z0-9])', suffix) or
                re.search(r'(?:^|[ ._-])(?:上集|下集|上半集|下半集)(?=$|[ ._-])', suffix)):
            raise NeedsReview('分段视频：同一集可能由多个文件组成，不按完整单集自动整理。')
    multi_label = re.search(r'(?i)(?:第\s*\d{1,3}\s*[-~至到]\s*(?:第\s*)?\d{1,3}\s*[集话話]|(?<![a-z0-9])(?:ep?|episodes?)[ ._-]*\d{1,3}\s*[-~至到]\s*(?:(?:ep?|episodes?)[ ._-]*)?\d{1,3}(?![a-z0-9]))', str(name))
    if multi_label or (hint['media_type'] == 'tv' and hint['end_episode'] != hint['episode']):
        # 去向由整理流程按「是否配置未识别目录」决定，这里只说明拒收原因。
        raise NeedsReview('合并集：一个视频包含多集，不能按单集整理。')
    return hint


def require_work_identity(hint):
    """纯数字文件名没有年份、目录或 ID 时不许拿去检索作品：数字更可能是集号。"""
    query = str(hint.get('query') or '').strip()
    if hint.get('tmdb_id') is None and not hint.get('year') and re.fullmatch(r'\d{1,4}', query):
        raise NeedsReview('文件名只有集号，缺少作品线索，未做自动匹配。')
    return hint


def query_variants(query):
    """查询词候选：整串 → 中文段 → 拉丁段。

    文件名常把中英文标题拼在一起（如「群体.Colony」），而 TMDB 的返回标题
    通常只有其中一种语言；拿拼接后的整串去等值比较必然匹配不到。
    """
    text = str(query or '').strip()
    parts = [p for p in re.split(r'[\s._·・/|,，、\-–—]+', text) if p]
    cjk = [p for p in parts if re.search(r'[\u3400-\u9fff]', p)]
    latin = [p for p in parts if not re.search(r'[\u3400-\u9fff]', p)
             and re.fullmatch(r"[A-Za-z0-9'&:!?.\-]+", p)]
    ordered, seen = [], set()
    for value in [text, *cjk, *latin]:
        value = value.strip()
        key = value.casefold()
        if len(value) < 2 or key in seen:
            continue
        seen.add(key)
        ordered.append(value)
    return ordered


TITLE_EXACT_SCORE = 1.55


def has_cjk(value):
    return bool(re.search(r'[\u3400-\u9fff]', str(value or '')))


def strip_episode_suffix(title):
    """去掉中文发布名尾部的括号集号（(1)/(10)），但保留年份 (2026)。

    搜索前会把 `飞越官场 (1)` 剥成 `飞越官场`；保留年份是因为
    `The Wartime Post (2026)` 这类括号就是上映年，剥掉会丢证据。
    """
    def drop(match):
        value = int(match.group(1))
        return match.group(0) if 1900 <= value <= 2100 else ''
    return re.sub(r'\s*[（(]\s*(\d{1,4})\s*[)）]\s*$', drop, str(title or '')).strip()


def match_threshold(hint):
    """标题分阈值：按标题长度分档（2026-10-31 实测实测）。

    实测：2 字 0.95、3 字 0.88、4 字 0.78、5–6 字 0.62、≥7 字 0.50；
    英文等非中文标题一律 0.50。阈值比较的是「精确=1.55、其余=相似度 0..1」的标题分。
    """
    query = str(hint.get('query') or '')
    if not has_cjk(query):
        return 0.50
    length = len(re.sub(r'\s+', '', query))
    if length <= 2:
        return 0.95
    if length == 3:
        return 0.88
    if length == 4:
        return 0.78
    if length <= 6:
        return 0.62
    return 0.50


def scored_candidates(hint, results):
    """按标题相似度给候选打分；中文标题略优先，年份只作加分（对齐口径）。"""
    variants = [normal(v) for v in query_variants(hint.get('query'))]
    variants = [v for v in variants if v]
    scored = []
    for row in results:
        names = [row.get(k, '') for k in ('title', 'original_title', 'name', 'original_name')]
        names += row.get('aliases', [])
        names = [normal(n) for n in names if n]
        if not names or not variants:
            continue
        best = 0.0
        for target in variants:
            for name in names:
                if name == target:
                    ratio = TITLE_EXACT_SCORE          # 精确命中固定记 1.55
                elif len(target) <= 2:
                    ratio = 0.0
                else:
                    ratio = SequenceMatcher(None, target, name).ratio()
                if ratio > best:
                    best = ratio
        scored.append((best, row))
    return scored


def pick_best(hint, results):
    """最高分候选：标题相似度 + 年份加分 + 中文优先；不要求唯一。"""
    year = str(hint.get('year') or '')
    threshold = match_threshold(hint)
    eligible = [(score, row) for score, row in scored_candidates(hint, results) if score >= threshold]
    if not eligible:
        return None

    def key(item):
        score, row = item
        released = str(row.get('release_date') or row.get('first_air_date') or '')[:4]
        year_bucket = 3 if (year and released == year) else 0
        cjk = 1 if any(has_cjk(row.get(k)) for k in ('title', 'name')) else 0
        return (round(score, 4) + 0.15 * year_bucket, cjk, row.get('popularity') or 0)

    eligible.sort(key=key, reverse=True)
    return eligible[0][1]


def choose_match(hint, results):
    """按候选词逐个匹配，先整串、再中文、再英文；命中多个同名片仍判为无法确认。"""
    year = str(hint['year']) if hint.get('year') else ''
    for variant in query_variants(hint.get('query')):
        target = normal(variant)
        candidates = {}
        for row in results:
            names = [row.get(k, '') for k in ('title', 'original_title', 'name', 'original_name')]
            names += row.get('aliases', [])
            if target not in {normal(n) for n in names if n}:
                continue
            released = (row.get('release_date') or row.get('first_air_date') or '')[:4]
            if year and released != year:
                continue
            candidates[row['id']] = row
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1:
            raise NeedsReview('TMDB 命中多部同名片，无法唯一确认，已停止自动命名。')
    raise NeedsReview('TMDB 未找到名称、年份唯一匹配的作品，已停止自动命名。')


def title_candidates(hint, results):
    """标题精确命中的候选，先不看年份（剧集要拿季/集年份来判，不能在这里就扔掉）。

    按查询变体的优先级取第一个有命中的变体，保持与 choose_match 相同的归一化口径。
    """
    ordered, seen = [], set()
    for variant in query_variants(hint.get('query')):
        target = normal(variant)
        for row in results:
            names = [row.get(k, '') for k in ('title', 'original_title', 'name', 'original_name')]
            names += row.get('aliases', [])
            if target not in {normal(n) for n in names if n}:
                continue
            if row.get('id') in seen:
                continue
            seen.add(row.get('id'))
            ordered.append(row)
        if ordered:
            break
    return ordered


def classify(details, media_type):
    countries = [x['iso_3166_1'] for x in details.get('production_countries', []) if x.get('iso_3166_1')]
    origins = list(dict.fromkeys(details.get('origin_country', []) or countries))
    # A known work may have multiple regions or none. Preserve its identity
    # and use the existing Other bucket instead of guessing a primary region.
    regions = {'国产' if c in ('CN', 'HK', 'MO', 'TW') else '日韩' if c in ('JP', 'KR') else '欧美' if c in WESTERN else '其他' for c in origins}
    region = next(iter(regions)) if len(regions) == 1 else '其他'
    genres = {g['id'] for g in details.get('genres', [])}
    category = ('电影' if media_type == 'movie' and 16 in genres else
                '儿童节目' if 10762 in genres else '动漫' if 16 in genres else
                '纪录片' if 99 in genres else '综艺' if genres & {10764, 10767} else
                '剧集' if media_type == 'tv' else '电影')
    if media_type == 'movie' and 10402 in genres and 16 not in genres:
        # Music genre alone does not establish concert identity.
        title = ' '.join(str(details.get(k) or '') for k in ('title','name','original_title','original_name'))
        if re.search(r'(?i)演唱会|演唱會|\bconcert\b|\blive\s+tour\b', title):category = '演唱会'
        else:raise NeedsReview('音乐类电影不能可靠自动判断是否演唱会，转待处理。')
    return category, region


def technical_name(media, source_name, policy=None):
    """技术字段命名。来源不明的字段直接省略（B07），不虚构默认规格。"""
    width, height = media.get('width'), media.get('height')
    if width and height:
        width, height = max(width, height), min(width, height)
        resolution = '4320p' if width >= 7000 or height >= 4000 else '2160p' if width >= 3500 or height >= 2000 else '1440p' if width >= 2500 or height >= 1400 else '1080p' if width >= 1800 or height >= 1000 else '720p' if width >= 1200 or height >= 700 else f'{height}p'
    else:
        resolution = ''
    codec = media.get('codec')
    codec = {'hevc': 'H.265', 'h264': 'H.264', 'av1': 'AV1'}.get(codec, codec or '')
    source_label = source_of(source_name)
    # 原盘 REMUX 往往只写 BLURAY，不写 REMUX；用实测码率判定：
    rate = media.get('bit_rate')
    if rate and 'REMUX' not in source_label and source_label in ('BluRay', 'UHD BluRay'):
        limit = 45_000_000 if (media.get('height') or 0) >= 2000 else 18_000_000
        if rate >= limit:
            source_label += ' REMUX'
    fields = [resolution, platform_of(source_name), source_label, dynamic_label(media), codec,
              f'{media["bit_depth"]}-bit' if media.get('bit_depth') else '',
              f'{media["fps"]:g}fps' if media.get('fps') else '']
    # 仅使用可确认音轨命名；没有可靠音轨信息时不编造规格。
    audio = naming_track(media, policy)
    if audio:
        a = {'eac3': 'DDP', 'ac3': 'DD', 'truehd': 'TrueHD', 'dts': 'DTS', 'dts_hd_ma': 'DTS-HD MA', 'dts_hd_hra': 'DTS-HD HRA', 'pcm': 'LPCM', 'flac': 'FLAC', 'av3a': 'AV3A', 'aac': 'AAC'}.get(audio.get('codec'), audio.get('codec') or '')
        channels = {1: '1.0', 2: '2.0', 6: '5.1', 8: '7.1'}.get(audio.get('channels'), str(audio.get('channels') or ''))
        label = (a + ' ' + channels).strip()
        if audio.get('atmos'):
            label = (label + ' Atmos').strip()
        fields.append(label)
    version = re.search(r'(?i)(?<![a-z0-9])V([1-9]\d?)(?![a-z0-9])', source_name)
    if version:
        fields.append('V' + version[1])
    group = release_group(source_name)
    return '.'.join(x for x in fields if x) + ('-' + group if group else '')


def work_parts(hint, details, options=None):
    options = options or {}
    title = options.get('folder_title') or details.get('title') or details.get('name')
    year = (details.get('release_date') or details.get('first_air_date') or '')[:4]
    if not title or not re.fullmatch(r'\d{4}', year):
        raise NeedsReview('TMDB 作品名称或首映年份缺失。')
    if options.get('classify_enabled', True):
        classified_details = details
        if not options.get('concert_enabled', True):
            classified_details = {**details, 'genres': [v for v in details.get('genres', []) if v.get('id') != 10402]}
        category, region = classify(classified_details, hint['media_type'])
        if category == '儿童节目' and not options.get('children_enabled', True):
            category = '动漫' if 16 in {v['id'] for v in details.get('genres', [])} else ('剧集' if hint['media_type'] == 'tv' else '电影')
        if category == '演唱会' and not options.get('concert_enabled', True):category = '电影'
    else:
        category, region = ('剧集' if hint['media_type']=='tv' else '电影'), ''
    short_title = component(title, 100)
    series = f'{short_title} ({year}) {{tmdb-{details["id"]}}}'
    parts = [category]
    if options.get('classify_enabled', True) and options.get('region_enabled', True):parts.append(region + category)
    if options.get('year_enabled', False):parts.append(year)
    parts.append(series)
    if hint['media_type'] == 'tv':
        parts.append(f'Season {hint["season"]}')
    return parts, category, region


def episode_label(hint):
    """分集文件的集标题：第N集；合并集写成 第1集-第3集。"""
    label = f'第{hint["episode"]}集'
    if hint.get('end_episode') and hint['end_episode'] != hint['episode']:
        label += f'-第{hint["end_episode"]}集'
    return label


def naming(hint, details, episodes, media, original_name, policy=None, options=None):
    options = options or {}
    parts, category, region = work_parts(hint, details, options)
    short_title = component(options.get('file_title') or details.get('title') or details.get('name'), 100)
    year = (details.get('release_date') or details.get('first_air_date'))[:4]
    if not options.get('rename_enabled', True):return parts, original_name, category, region
    naming_policy = policy
    if policy and not (media or {}).get('audio'):
        # 没有音轨信息（探测失败退回文件名，或容器本身不带音频元数据）时音频规则无从比较，
        # 禁用以免命名里出现「未知音频」这类噪音；不按 .iso 后缀写死。
        naming_policy = {**policy, 'rules': [{**r, 'enabled': False} if r['key'] == 'audio' else r for r in policy.get('rules', [])]}
    tech = technical_name(media, original_name, naming_policy)
    if hint['media_type'] == 'tv':
        code = f'S{hint["season"]:02d}E{hint["episode"]:02d}'
        if hint['end_episode'] != hint['episode']:
            code += f'-E{hint["end_episode"]:02d}'
        # 第 4 段固定「第N集」：TMDB 集名有的有、有的没有（甚至本身就是「第 22 集」），
        # 混在一起列表很杂。集号只由 episode_label 给出，合并集写成 第1集-第3集。
        stem = f'{short_title}.{year}.{code}.{episode_label(hint)}.' + tech
    else:
        stem = f'{short_title} ({year}) - {tech}'
    if len(stem.encode()) > 220:
        raise NeedsReview('规范文件名超长，不能完整保留身份和技术字段，转待处理。')
    stem = component(stem, 220)
    return parts, (stem + Path(original_name).suffix if options.get('rename_enabled', True) else original_name), category, region


EPISODE_TAG = re.compile(r'(?i)(?<![a-z0-9])s(\d{1,2})[\s._-]*e(\d{1,3})(?:(?:[ ._-]*e|-)(\d{1,3})(?![a-z0-9]))?(?!\d)')


def episode_key(name):
    """从文件名取完整季集范围，用于跨发布组匹配字幕。"""
    match = EPISODE_TAG.search(Path(name).stem)
    if not match:
        return None
    first = int(match.group(2))
    last = int(match.group(3)) if match.group(3) else first
    if first == 0 or last < first or last - first > 30:
        return None
    title = normal(Path(name).stem[:match.start()])
    if not title:
        return None
    return title, int(match.group(1)), first, last


_SUBTITLE_TAG = re.compile(r'(?i)^(?:zh|zho|chi|cn|chs|cht|zh-hans|zh-hant|zh-cn|zh-tw|zh-hk|eng|en|jpn|ja|jap|kor|ko|kr|fre|fra|fr|ger|deu|de|spa|es|rus|ru|ita|it|por|pt|ara|ar|简体|繁体|简中|繁中|中字|双语|中英|简英|繁英|中文|英文|日文|日语|韩文|韩语|简|繁|简体中文|繁体中文|forced|force|foreign|sdh|cc|hi|default|signs|songs|commentary|特效|强制|听障)$')


def subtitle_suffix(video_name, subtitle_name):
    """保留字幕扩展名及可辨认的末尾语言/标记，兼容不同发布组。"""
    video_stem = Path(video_name).stem
    subtitle_name = Path(subtitle_name).name
    extension = Path(subtitle_name).suffix
    if not extension:
        raise NeedsReview('字幕缺少扩展名，无法安全命名。')
    if subtitle_name.startswith(video_stem):
        suffix = subtitle_name[len(video_stem):]
        if suffix.startswith('.') and suffix.endswith(extension):
            return suffix
    stem = Path(subtitle_name).stem
    parts = stem.split('.')
    tags = []
    while len(parts) > 1 and _SUBTITLE_TAG.fullmatch(parts[-1]):
        tags.insert(0, parts.pop())
    return ('.' + '.'.join(tags) if tags else '') + extension


def bind_subtitles(files, video_extensions=None, subtitle_extensions=None):
    video_extensions = VIDEOS if video_extensions is None else set(video_extensions)
    subtitle_extensions = SUBTITLES if subtitle_extensions is None else set(subtitle_extensions)
    """Index by parent/stem; do not compare every video against the entire library."""
    videos = [f for f in files if Path(f['name']).suffix.lower() in video_extensions]
    owners = {}
    by_parent = {}
    for video in videos:
        video['subtitles'] = []
        by_parent.setdefault(video['parent_id'], []).append(video)
        owners.setdefault((video['parent_id'], Path(video['name']).stem), []).append(video)
    for sub in files:
        if Path(sub['name']).suffix.lower() not in subtitle_extensions: continue
        stem = Path(sub['name']).stem
        prefixes = [stem] + [stem[:i] for i, char in enumerate(stem) if char == '.']
        matches = list({id(v): v for v in (v for prefix in prefixes for v in owners.get((sub['parent_id'], prefix), []))}.values())
        if len(matches) != 1:
            # 字幕与视频常来自不同发布组，尾部音轨/压制组会不同（例如视频 NTb、
            # 字幕 Kitsune，或视频多一个 DV），按原名永远配不上。退一步用同一
            # 目录下的季集号匹配；同集有多个版本时仍判为歧义，不做猜测。
            key = episode_key(sub['name'])
            if key is not None:
                matches = [v for v in by_parent.get(sub['parent_id'], []) if episode_key(v['name']) == key]
        if not matches:
            # Movies often have different release/quality tags on their
            # subtitles. Match only a unique same-directory movie title;
            # explicit, conflicting years are never treated as the same work.
            try:
                sh = parse_name(sub['name'])
                candidates = []
                if sh.get('media_type') == 'movie' and normal(sh.get('query') or ''):
                    for video in by_parent.get(sub['parent_id'], []):
                        try: vh = parse_name(video['name'])
                        except NeedsReview: continue
                        if (vh.get('media_type') == 'movie' and normal(vh.get('query') or '') == normal(sh['query'])
                                and not (vh.get('year') and sh.get('year') and vh['year'] != sh['year'])):
                            candidates.append(video)
                matches = candidates
            except NeedsReview:
                pass
        if len(matches) == 1:
            matches[0]['subtitles'].append(sub)
        else:
            for video in matches: video['subtitle_warning'] = True
    return videos


def media_from_filename(name):
    """从文件名解析质量标签（ffprobe 不可用时的降级方案，如分享直链探测失败）。

    B07：只承认文件名里明确写出的字段；没写的就是 None（未知），
    绝不填默认分辨率/编码/色深/帧率冒充真实规格。整份结果带
    estimated=True，表示全部字段来自文件名降级解析。
    """
    low = name.lower()
    tokens = {'4320p': (7680, 4320), '8k': (7680, 4320), '2160p': (3840, 2160),
              '4k': (3840, 2160), '1440p': (2560, 1440), '1080p': (1920, 1080),
              '1080i': (1920, 1080), '720p': (1280, 720), '480p': (854, 480)}
    stated = re.search(r'(?<![a-z0-9])(4320p|8k|2160p|4k|1440p|1080[pi]|720p|480p)(?![a-z0-9])', low)
    width, height = tokens[stated.group(1)] if stated else (None, None)
    if re.search(r'(?:h\.?265|hevc|x265)', low): codec = 'hevc'
    elif re.search(r'(?:h\.?264|avc|x264)', low): codec = 'h264'
    elif re.search(r'(?<![a-z0-9])av1(?![a-z0-9])', low): codec = 'av1'
    else: codec = None
    if re.search(r'do?vi\b|dolby.?vision|(?<![a-z])dv(?![a-z])', low):
        dr = 'Dolby Vision HDR' if re.search(r'hdr', low) else 'Dolby Vision'
    elif 'hdr10+' in low or 'hdr10plus' in low: dr = 'HDR10+'
    elif re.search(r'hdr[ ._-]*vivid', low): dr = 'HDR Vivid'
    elif 'hdr10' in low: dr = 'HDR10'
    elif 'hdr' in low: dr = 'HDR PQ'
    elif re.search(r'(?<![a-z0-9])hlg(?![a-z0-9])', low): dr = 'HLG'
    elif re.search(r'(?<![a-z0-9])sdr(?![a-z0-9])', low): dr = 'SDR'
    else: dr = None
    depth_m = re.search(r'(\d+)[-. ]?bit', low)
    fps_m = re.search(r'(\d+(?:\.\d+)?)\s*fps', low)
    if re.search(r'truehd', low): a_codec = 'truehd'
    elif re.search(r'(?<![a-z0-9])(?:av3a|audio[ ._-]+vivid)(?![a-z0-9])', low): a_codec = 'av3a'
    elif re.search(r'ddp|dd\+|e[ ._-]?ac[ ._-]?3', low): a_codec = 'eac3'
    elif re.search(r'dts[ ._-]?hd[ ._-]?ma|dts[ ._-]?ma', low): a_codec = 'dts_hd_ma'
    elif re.search(r'dts[ ._-]?hd[ ._-]?hra', low): a_codec = 'dts_hd_hra'
    elif re.search(r'dts', low): a_codec = 'dts'
    elif re.search(r'\bac3\b', low): a_codec = 'ac3'
    elif re.search(r'flac', low): a_codec = 'flac'
    elif re.search(r'(?<![a-z0-9])l?pcm(?![a-z0-9])', low): a_codec = 'pcm'
    elif re.search(r'\baac\b', low): a_codec = 'aac'
    else: a_codec = None
    if '7.1' in low: a_ch = 8
    elif '5.1' in low: a_ch = 6
    elif '2.0' in low: a_ch = 2
    elif '1.0' in low: a_ch = 1
    else: a_ch = None
    atmos = bool(re.search(r'atmos', low))
    audio = [{'codec': a_codec, 'channels': a_ch, 'atmos': atmos, 'estimated': True}] if a_codec else []
    # 只看独立出现的 P5/P7/P8：'DDP5.1' 里的 p5 不是杜比视界 Profile。
    profile = re.search(r'(?<![a-z0-9])p([578])(?![0-9])', low)
    return {'width': width, 'height': height, 'codec': codec, 'dynamic_range': dr,
            'bit_depth': int(depth_m.group(1)) if depth_m else None,
            'fps': float(fps_m.group(1)) if fps_m else None,
            'dolby_vision_profile': int(profile[1]) if (profile and dr and dr.startswith('Dolby Vision')) else None,
            'audio': audio, 'estimated': True, 'origin': 'filename'}


def format_scan_report(files):
    """Describe the same effective extension filter used by bind_subtitles.

    Excluded formats are expected skips, not recognition failures. Attachments
    stay with the existing subtitle/metadata handling and are counted separately.
    """
    excluded, attachments, formats, examples = 0, 0, {}, []
    for entry in files:
        extension = Path(entry['name']).suffix.lower()
        if extension in VIDEOS:
            continue
        if extension in SUBTITLES or extension in METADATA:
            attachments += 1
            continue
        excluded += 1
        label = extension or '无扩展名'
        formats[label] = formats.get(label, 0) + 1
        if len(examples) < 50:
            examples.append({'name': entry['name'], 'extension': extension,
                             'reason': '未列入当前整理扩展名，按规则跳过'})
    return {'scanned': len(files), 'excluded': excluded, 'attachments': attachments,
            'formats': formats, 'examples': examples, 'examples_truncated': excluded > len(examples),
            'allowed_extensions': sorted(VIDEOS)}


def format_skip_message(report):
    parts = [f'{extension} {count} 个' for extension, count in sorted(report.get('formats', {}).items())]
    if not parts:return ''
    reasons = report.get('reasons') or {}
    detail = '；'.join(f'{reason}（{count} 个）' for reason, count in reasons.items()) if reasons else '这些格式未启用整理'
    # 通知里只留「跳过了什么」，原文件保留是既定行为，不必每次重复。
    return '跳过 ' + '、'.join(parts) + ('：' + detail if detail else '')
