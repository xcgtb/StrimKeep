"""STRM 文件明细的树状渲染：同目录只出现一次表头，跨片重复表头。"""
from strimkeep.notifications import _units, strm_detail_titles, strm_tree

SEASON = '影视媒体库/剧集/欧美剧集/末日地堡 (2023) {tmdb-125988}/Season 1'


def test_tree_drops_library_root_and_marks_last_file():
    chunks = strm_tree([f'{SEASON}/末日地堡.2023.S01E0{n}.{title}.2160p.SDR.H.265.10-bit.23.976fps.DDP 5.1.strm'
                        for n, title in ((1, '自由日'), (2, '汉娜'))])
    assert len(chunks) == 1
    lines = chunks[0].split('\n')
    assert lines[0] == '剧集 / 欧美剧集 / 末日地堡 (2023) {tmdb-125988} / Season 1'
    assert lines[1].startswith('├── 末日地堡.2023.S01E01.自由日.')
    assert lines[2].startswith('└── 末日地堡.2023.S01E02.汉娜.')
    assert len(lines) == 3


def test_tree_keeps_directories_apart_with_a_blank_line():
    chunks = strm_tree([f'{SEASON}/末日地堡.2023.S01E01.自由日.strm',
                        '影视媒体库/电影/国产电影/流浪地球 (2019) {tmdb-535167}/流浪地球.2019.2160p.strm'])
    assert len(chunks) == 1
    lines = chunks[0].split('\n')
    assert lines == ['剧集 / 欧美剧集 / 末日地堡 (2023) {tmdb-125988} / Season 1',
                     '└── 末日地堡.2023.S01E01.自由日.strm',
                     '',
                     '电影 / 国产电影 / 流浪地球 (2019) {tmdb-535167}',
                     '└── 流浪地球.2019.2160p.strm']


def test_tree_repeats_heading_when_one_directory_spans_chunks():
    paths = [f'影视媒体库/剧集/日韩剧集/迪迦奥特曼 (1996) {{tmdb-2253}}/Season 1/'
             f'迪迦奥特曼.1996.S01E{n:02d}.第{n}集.2160p.WEB-DL.SDR.H.265.10-bit.30fps.AAC 2.0.strm'
             for n in range(1, 53)]
    chunks = strm_tree(paths)
    assert len(chunks) == 3, [len(c.split('\n')) for c in chunks]
    heading = '剧集 / 日韩剧集 / 迪迦奥特曼 (1996) {tmdb-2253} / Season 1'
    assert all(chunk.split('\n')[0] == heading for chunk in chunks)
    assert sum(chunk.count('├──') + chunk.count('└──') for chunk in chunks) == 52
    assert all(_units(chunk) <= 3400 for chunk in chunks)
    assert not any(chunk.endswith('\n') for chunk in chunks)


def test_tree_splits_an_absurdly_long_name_without_breaking_the_cap():
    long_name = '长' * 4000 + '.strm'
    chunks = strm_tree([f'{SEASON}/{long_name}'])
    assert len(chunks) >= 2
    assert all(_units(chunk) <= 3400 for chunk in chunks)
    assert ''.join(chunks).replace('\n', '').endswith('.strm')


def test_detail_titles_carry_the_file_range_of_each_chunk():
    paths = [f'影视媒体库/剧集/日韩剧集/迪迦奥特曼 (1996) {{tmdb-2253}}/Season 1/迪迦奥特曼.1996.S01E{n:02d}.第{n}集.2160p.strm'
             for n in range(1, 53)]
    chunks = strm_tree(paths)
    titles = strm_detail_titles(chunks, len(paths))
    assert titles[0] == '📄 STRM 文件明细 1/3 (1-19/52)'
    assert titles[-1] == '📄 STRM 文件明细 3/3 (39-52/52)'


def test_detail_paths_list_only_washed_episodes_in_a_wash_batch():
    """洗版批次：没变化（既不是新增也不是替换）的集一条都不列。"""
    from strimkeep.notifications import strm_detail_paths
    items=[(1, {'relative_path': f'{SEASON}/末日地堡.2023.S01E01.自由日.strm'}),
           (2, {'relative_path': f'{SEASON}/末日地堡.2023.S01E02.汉娜.strm', 'strm_created': True}),
           (3, {'relative_path': f'{SEASON}/末日地堡.2023.S01E03.第三集.strm', 'share_wash_replaced': True})]
    created, replaced = strm_detail_paths(items)
    assert created == [f'{SEASON}/末日地堡.2023.S01E02.汉娜.strm']
    assert replaced == [f'{SEASON}/末日地堡.2023.S01E03.第三集.strm']


def test_detail_paths_keep_every_new_strm_and_ignore_unchanged_and_filtered():
    from strimkeep.notifications import strm_detail_paths
    items=[(1, {'relative_path': f'{SEASON}/末日地堡.2023.S01E01.自由日.strm', 'strm_created': True}),
           (2, {'relative_path': f'{SEASON}/末日地堡.2023.S01E02.汉娜.strm', 'strm_created': True}),
           (3, {'relative_path': f'{SEASON}/末日地堡.2023.S01E03.第三集.strm'}),
           (4, {'relative_path': f'{SEASON}/末日地堡.2023.S01E04.第四集.strm', 'strm_created': True,
                'strm_filtered': '低于最小体积'})]
    created, replaced = strm_detail_paths(items)
    assert created == [f'{SEASON}/末日地堡.2023.S01E01.自由日.strm', f'{SEASON}/末日地堡.2023.S01E02.汉娜.strm']
    assert replaced == []


def test_detail_paths_treat_a_wash_decision_as_a_replacement():
    from strimkeep.notifications import strm_detail_paths
    items=[(1, {'relative_path': f'{SEASON}/末日地堡.2023.S01E01.自由日.strm', 'strm_created': True}),
           (2, {'relative_path': f'{SEASON}/末日地堡.2023.S01E02.汉娜.strm', 'wash': {'decision': 'replace'}})]
    created, replaced = strm_detail_paths(items)
    assert created == [f'{SEASON}/末日地堡.2023.S01E01.自由日.strm']
    assert replaced == [f'{SEASON}/末日地堡.2023.S01E02.汉娜.strm']


def test_batch_summary_accounts_for_updated_strm_not_just_created():
    """现场 1795：写入 30 个、其中新增 5 个，汇总必须把另外 25 个更新算出来。"""
    from strimkeep.notifications import strm_summary_lines
    items = []
    for n in range(1, 31):
        plan = {'relative_path': f'{SEASON}/末日地堡.2023.S01E{n:02d}.第{n}集.strm'}
        if n <= 5:
            plan['strm_created'] = True
        items.append((n, plan))
    lines, created, replaced, written = strm_summary_lines(items)
    assert written == 30 and len(created) == 5 and replaced == []
    assert '写入 STRM：30 个' in lines
    assert '新增 STRM：5 个' in lines
    assert '更新 STRM：25 个（地址/元数据变化，明细略）' in lines


def test_batch_summary_reports_a_pure_update_batch():
    """全部是同路径更新时也要发汇总，不能因为「新增 0」就静默。"""
    from strimkeep.notifications import strm_summary_lines
    items = [(n, {'relative_path': f'{SEASON}/末日地堡.2023.S01E{n:02d}.第{n}集.strm'})
             for n in range(1, 31)]
    lines, created, replaced, written = strm_summary_lines(items)
    assert written == 30 and created == [] and replaced == []
    assert lines[0] == '写入 STRM：30 个'
    assert '更新 STRM：30 个（地址/元数据变化，明细略）' in lines


def test_batch_summary_counts_explicit_rewrites():
    """分享重复整理同一路径：计划里标了 strm_rewritten，汇总必须说「重写」而不是「更新」。"""
    from strimkeep.notifications import strm_summary_lines
    items = [(n, {'relative_path': f'{SEASON}/末日地堡.2023.S01E{n:02d}.第{n}集.strm',
                  'strm_rewritten': True})
             for n in range(1, 31)]
    lines, created, replaced, written = strm_summary_lines(items)
    assert written == 30 and created == [] and replaced == []
    assert '重写 STRM：30 个（同路径覆盖，明细略）' in lines
    assert not any(line.startswith('更新 STRM') for line in lines)


def test_batch_summary_excludes_filtered_outputs_from_written_count():
    from strimkeep.notifications import strm_summary_lines
    items = [(1, {'relative_path': f'{SEASON}/末日地堡.2023.S01E01.自由日.strm',
                  'strm_filtered': '低于最小体积'}),
             (2, {'relative_path': f'{SEASON}/末日地堡.2023.S01E02.汉娜.strm',
                  'strm_created': True})]
    lines, created, _, written = strm_summary_lines(items)
    assert written == 1 and len(created) == 1
    assert '写入 STRM：1 个' in lines

