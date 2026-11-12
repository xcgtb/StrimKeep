"""公共播放地址构造与播放身份解析（第一阶段 B02）。

STRM 内容、Emby 核对、验证样例、同步补齐全部调用这一个实现：
- 播放身份 = ``/play/{item_id}``；可读名称段只用于显示，不参与资源定位或授权；
- 兼容旧格式 ``/play/{item_id}``（无名称段）与现有可读地址；
- 空格、``/ \\ ? # % " < > |`` 及控制字符等会破坏 URL 结构的字符替换为下划线，
  其余字符（含中文）原样保留，由播放器在请求时自行编码；
- 同一播放身份经过改名、移动、重复同步、补齐输出，地址可读段可以变化，
  但 item_id 保持不变。
"""
import re

# 这些字符出现在 URL path 段会改变解析结果（?/# 起始 query/fragment、% 引入
# 百分号编码、/\ 是路径分隔、其余为不安全字符）。全部替换而不是转义，保证
# 播放器路径栏仍然可读。
_UNSAFE = re.compile(r'[\x00-\x20\x7f"<>`^{|}\\/?#%]+')
_PLAY_PATH = re.compile(r'^/play/(?P<item>[^/?#]+)(?:/(?P<slug>[^?#]*))?(?:\?.*)?$')


def display_slug(name: str) -> str:
    """把可读名称转成 URL path 段：截到标题核心（去掉技术规格尾巴），保留扩展名。

    的播放路径末尾带 ``.mkv``/``.mp4``，部分播放器与下载器靠后缀判断容器，
    这里保留同一个习惯；扩展名不参与播放身份判断。
    """
    text = str(name)
    suffix = ''
    tail = re.search(r'(\.[A-Za-z0-9]{2,5})$', text)
    if tail:
        suffix = tail.group(1).lower()
        text = text[:tail.start()]
    # 在「 - 」分隔符后跟着分辨率/编码的位置截断（Title (Year) - 2160p.WEB-DL...）
    cut = re.search(r'\s[-]\s\d{3,4}p', text)
    if cut:
        text = text[:cut.start()]
    # 兜底：按句点后跟大写字母/数字截断（Title.2160p.WEB-DL...）
    cut2 = re.search(r'\.\d{3,4}p[._]', text)
    if cut2:
        text = text[:cut2.start()]
    slug = _UNSAFE.sub('_', text).strip('._')
    return ((slug[:80].rstrip('._')) or 'video') + suffix


def play115_url(base: str, pickcode: str, fid: str, sha1: str,
                size: int | str, name: str) -> str:
    """同款 STRM 身份：/play115/{pickcode}/{fid}/{sha1}/{size}/{name}。

    身份直接写在 STRM 里，取链不查库；sha1/size 供播放器与对账使用。
    名称段整体百分号编码（含空格与中文），播放器不必再猜容器后缀。
    """
    from urllib.parse import quote
    parts = '/'.join((str(pickcode), str(fid), str(sha1), str(size), quote(str(name), safe='')))
    return (base or '').rstrip('/') + '/play115/' + parts


def identity_of(source) -> tuple | None:
    """从 115 清单条目取 play115 需要的四项身份；缺任何一项返回 None。

    返回 (pickcode, fid, sha1, size)，顺序与 play115_url 的参数一致。
    台账 source 存的就是整理/同步当时的清单条目，所以离线自愈也能用：
    换地址格式不需要为了拿 sha1/size 回云端读一次。
    """
    if not isinstance(source, dict):
        return None
    pickcode = str(source.get('pickcode') or '')
    fid = str(source.get('id') or '')
    sha1 = str(source.get('sha1') or '')
    size = source.get('size')
    if not pickcode or not fid or not sha1 or size in (None, ''):
        return None
    return pickcode, fid, sha1, size


def play115_candidates(base: str, source, name: str | None = None) -> list[bytes]:
    """play115 地址在本地可能出现的字节形态（含结尾换行）。

    名称段只用于显示，不参与身份判断；改名前后两种名称都算「我们的输出」，
    否则一次改名就会让旧 STRM 被判成别人写的文件。
    """
    ident = identity_of(source)
    if not ident:
        return []
    pickcode, fid, sha1, size = ident
    names = []
    for candidate in ((source or {}).get('name') if isinstance(source, dict) else None, name):
        text = str(candidate or '')
        if text and text not in names:
            names.append(text)
    forms = []
    for text in names:
        url = play115_url(base, pickcode, fid, sha1, size, text)
        # 仍然认作「我们的输出」，避免存量被判成外来文件。
        forms.append(url.encode())
        forms.append((url + '\n').encode())
    return forms


def ua_label(value: str, limit: int = 32) -> str:
    """UA 只留前 32 字符用于日志。

    播放失败时，「播放器没发 UA」和「UA 与取链时不一致」是首要排查点，
    115 直链与 UA 绑定，所以日志里必须带 UA。
    """
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not text:
        return '无 UA'
    return text[:limit] + ('…' if len(text) > limit else '')


def play_url(base: str, item_id: str, display_name: str | None = None,
             token: str | None = None) -> str:
    """构造无播放令牌的入口地址；token 参数仅保留旧调用兼容。"""
    url = (base or '').rstrip('/') + '/play/' + str(item_id)
    if display_name:
        url += '/' + display_slug(display_name)
    return url


def candidates(base: str, item_id: str, display_name: str | None = None,
               token: str | None = None) -> list[bytes]:
    """该播放身份在历史版本里可能被写入过的全部字节形式（含结尾换行）。

    用于核对输出归属，同时接受无令牌地址和带令牌的历史地址，
    包括旧版完整名称段。第一个元素是当前规范形式。
    """
    prefix = (base or '').rstrip('/') + '/play/' + str(item_id)
    slug = '/' + display_slug(display_name) if display_name else ''
    # New addresses have no playback token. Retain the two historical forms
    # solely for output ownership checks when old STRM files remain on disk.
    forms = [(prefix + slug).encode(), prefix.encode(),
             (prefix + slug + '\n').encode(), (prefix + '\n').encode()]
    old_slug = '/' + _UNSAFE.sub('_', str(display_name)).strip('._')[:80].rstrip('._') if display_name else ''
    if old_slug and old_slug != slug:
        forms.append((prefix + old_slug + '\n').encode())
    if token:
        forms.extend([(prefix + slug + '?token=' + str(token) + '\n').encode(),
                      (prefix + '?token=' + str(token) + '\n').encode()])
        if old_slug and old_slug != slug:
            forms.append((prefix + old_slug + '?token=' + str(token) + '\n').encode())
    return list(dict.fromkeys(forms))


def parse_play_path(path: str):
    """解析播放路径，返回 (item_id, slug)。

    同时接受旧格式 ``/play/{id}`` 和可读格式 ``/play/{id}/{slug}``；
    非播放路径返回 (None, None)。slug 永远不参与身份判断。
    """
    match = _PLAY_PATH.match(str(path or ''))
    if not match:
        return None, None
    return match.group('item'), (match.group('slug') or '')
