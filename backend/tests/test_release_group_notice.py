"""发布组别与通知明细。

现场两条反馈：
1. 通知里组别显示 `SCDIY@HiveWeb`，但 STRM/NFO 文件名里完全没有组别
   （`...DDP 5.1.strm`），应该是 `...DDP 5.1-HiveWeb.strm`；
2. 深度删除完成通知只把路径截到 80 字，删的是哪个版本看不出来。
"""
import asyncio

from strimkeep import deep_delete
from strimkeep.cards import media_card
from pathlib import Path

from strimkeep.media import media_stem, naming, release_group
from strimkeep.store import Store

SOURCE = ('流人.2022.S04E01.第1集.2160p.Apple TV+.WEB-DL.DoVi P5.H.265.10-bit.'
          '23.976fps.DDP 5.1-SCDIY@HiveWeb.mkv')


def test_release_group_takes_the_site_after_at():
    assert release_group(SOURCE) == 'HiveWeb'
    assert release_group('Movie.2024.2160p.BluRay.x265-NTb.mkv') == 'NTb'
    # 规格尾巴不能被当成组别
    assert release_group('Movie.2024.2160p.WEB-DL.mkv') == ''
    assert release_group('Movie.2024.2160p.BluRay.DTS-HD.mkv') == ''


# 分享来的真实文件名：duxingxia_转生成猫的大叔，源文件名没有扩展名。
CAT = 'duxingxia_转生成猫的大叔 (2024) [tmdbid=248707].S01E46-1080p.WEB-DL-H264-AAC@ADWeb'


def test_release_group_reads_names_without_extension():
    # Path(...).stem 会把最后一段 `WEB-DL-H264-AAC@ADWeb` 当后缀剁掉，组别就没了。
    assert release_group(CAT) == 'ADWeb'
    assert release_group(CAT + '.mkv') == 'ADWeb'
    # `-` 后面还夹着点号时，退一步取 `@` 之后那段
    assert release_group('Show.2024.S01E01.1080p.WEB-DL.DDP5.1@ADWeb') == 'ADWeb'
    assert media_stem(CAT) == CAT


def test_share_side_target_stem_keeps_the_group():
    # share_service 用 stem + Path(name).suffix 还原原始名，再由 naming 产出目标名。
    stem = Path(CAT).stem
    original = stem + Path(CAT).suffix
    hint = {'media_type': 'tv', 'season': 1, 'episode': 46, 'end_episode': 46, 'tmdb_id': 248707}
    details = {'id': 248707, 'name': '转生成猫的大叔', 'title': '转生成猫的大叔',
               'first_air_date': '2024-10-01', 'genres': [{'id': 16}]}
    media = {'width': 1920, 'height': 1080, 'codec': 'h264'}
    _, name, _, _ = naming(hint, details, [], media, original)
    target_stem = Path(name).stem
    assert target_stem == '转生成猫的大叔.2024.S01E46.第46集.1080p.WEB-DL.H.264-ADWeb', target_stem


def test_naming_keeps_the_release_group_in_the_target_name():
    hint = {'media_type': 'tv', 'season': 4, 'episode': 1, 'end_episode': 1, 'tmdb_id': 95480}
    details = {'id': 95480, 'name': '流人', 'title': '流人', 'first_air_date': '2022-02-01',
               'genres': [{'id': 18}]}
    media = {'width': 3840, 'height': 2160, 'codec': 'hevc'}
    _, name, _, _ = naming(hint, details, [], media, SOURCE)
    assert name.endswith('-HiveWeb.mkv'), name


def test_card_group_matches_the_name_written_to_disk():
    item = ({'name': SOURCE, 'size': 1024 ** 3},
            {'hint': {'media_type': 'tv', 'season': 4, 'episode': 1},
             'details': {'id': 95480, 'name': '流人', 'first_air_date': '2022-02-01',
                         'seasons': [{'season_number': 4, 'episode_count': 6}]},
             'parts': ['剧集', '欧美剧集'], 'media': {'width': 3840, 'height': 2160,
                                                   'codec': 'hevc'}})
    _, body, _ = media_card([item])
    assert '👥 组别：HiveWeb' in body, body


def test_deep_delete_notice_keeps_full_paths_and_states(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    long_path = ('媒体库/电影/欧美电影/奥德赛 (2026) {tmdb-1368337}/'
                 '奥德赛 (2026) - 2160p.UHD BluRay REMUX.HDR.H.265.DDP 5.1-HiveWeb.mkv')
    batch = 'batch-1'
    with store.db:
        store.db.executemany(
            'INSERT INTO deep_delete_queue(batch,kind,file_id,playback_id,relative_path,'
            'share_id,source,dry_run,state,message,created,updated) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            [(batch, 'managed', 'f1', 'p1', long_path, '', 'emby', 0, 'done', '已删除云盘文件', 0, 0),
             (batch, 'managed', 'f2', 'p2', '媒体库/电影/欧美电影/别的片 (2025)/别的片.mkv',
              '', 'emby', 0, 'skipped', '文件不属于本程序管理', 0, 0)])
    engine = type('Engine', (), {})()
    engine.store = store
    calls = []

    class Notifications:
        async def publish(self, key, title, message, level, category=None):
            calls.append({'title': title, 'message': message})

    engine.notifications = Notifications()
    asyncio.run(deep_delete.notify_batch(engine, batch))
    message = calls[0]['message']
    assert long_path in message, message          # 不再被截断
    assert '✅' in message and '⏭️' in message      # 每条带状态
    assert '文件不属于本程序管理' in message          # 跳过原因也带上
    store.close()
