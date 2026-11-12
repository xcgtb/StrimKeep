"""通知明细：STRM 后面跟同目录的 nfo / 媒体信息 json，海报不进明细。"""
from strimkeep.notifications import sidecar_paths

REL = '媒体库/金色 (2024)/Season 1/金色.S01E01.strm'
STEM = REL[:-5]


def _touch(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x')


def test_sidecar_paths_lists_nfo_and_media_info_only(tmp_path):
    for suffix in ('', '-media-info.json', '.nfo', '.jpg', '.png'):
        _touch(tmp_path, STEM + suffix)
    listed = sidecar_paths([REL], str(tmp_path))
    # 只跟 nfo：媒体信息 json 还没补齐，海报不进明细
    assert listed == [REL, STEM + '.nfo']


def test_sidecar_paths_skips_missing_and_keeps_plain_entries(tmp_path):
    _touch(tmp_path, STEM)
    # 只有 STRM，没有 nfo / json：不应凭空补出条目
    assert sidecar_paths([REL], str(tmp_path)) == [REL]
    # 非 strm 条目原样保留
    assert sidecar_paths(['媒体库/说明.txt'], str(tmp_path)) == ['媒体库/说明.txt']


def test_detail_title_counts_entries_not_lines():
    """一集一个 nfo 时，计数按条目（集数），不按正文行数。"""
    from strimkeep.notifications import strm_detail_titles, strm_entry_count, strm_tree
    paths = []
    for i in range(1, 4):
        stem = '剧集/一吻爆炸 (2025)/Season 1/一吻爆炸.S01E%02d.strm' % i
        paths.extend([stem, stem[:-5] + '.nfo'])
    chunks = strm_tree(paths)
    titles = strm_detail_titles(chunks, strm_entry_count(paths), 'STRM 与 NFO 文件明细')
    assert titles == ['📄 STRM 与 NFO 文件明细 1/1 (1-3/3)']
    assert strm_entry_count(paths) == 3


def test_detail_title_total_is_entries_even_when_split():
    """分片后总数仍然是条目数（14 集），不是 28 行。"""
    from strimkeep.notifications import strm_detail_titles, strm_entry_count, strm_tree
    paths = []
    for i in range(1, 15):
        stem = '剧集/一吻爆炸 (2025)/Season 1/一吻爆炸.S01E%02d.strm' % i
        paths.extend([stem, stem[:-5] + '.nfo'])
    chunks = strm_tree(paths)
    titles = strm_detail_titles(chunks, strm_entry_count(paths), 'STRM 与 NFO 文件明细')
    assert all('STRM 与 NFO 文件明细' in title for title in titles)
    assert titles[0].startswith('📄 STRM 与 NFO 文件明细 1/')
    assert titles[0].endswith('/14)')
    assert titles[-1].endswith('/14)')
