"""媒体信息入库：字段映射、路径反推、批量补齐与幂等性。"""
import json
import sqlite3
from pathlib import Path

from strimkeep.emby_media_inject import (COLUMNS, Injector, inject_sidecar, sidecar_path,
                                         stream_row)

SCHEMA = """
CREATE TABLE MediaItems (Id INTEGER PRIMARY KEY, Path TEXT, Width INT, Height INT, TotalBitrate INT, Size INTEGER, RunTimeTicks INTEGER);
CREATE TABLE MediaStreams2 (%s);
CREATE TABLE Chapters3 (ItemId INT, ChapterIndex INT, StartPositionTicks INTEGER, Name TEXT,
                        ImagePath TEXT, ImageDateModified INT, MarkerType INT);
""" % ', '.join('"%s"' % column.strip() for column in COLUMNS.split(','))

SIDECAR = {
    'container': 'mkv', 'size': 1000, 'duration_ticks': 27250560000, 'bitrate': 15154535,
    'streams': [
        {'Type': 'Video', 'Codec': 'hevc', 'Width': 3840, 'Height': 1604, 'BitRate': 15154535,
         'BitDepth': 10, 'AverageFrameRate': 60, 'ColorTransfer': 'smpte2084',
         'ExtendedVideoType': 'Hdr10', 'ExtendedVideoSubType': 'Hdr10',
         'TimeBase': '1/1000', 'IsDefault': True},
        {'Type': 'Audio', 'Codec': 'eac3', 'Channels': 2, 'ChannelLayout': 'stereo',
         'SampleRate': 48000, 'BitRate': 128000, 'Language': 'chi', 'IsDefault': True},
    ],
    'chapters': [{'StartPositionTicks': 0, 'Name': '章节 1'},
                 {'StartPositionTicks': 3000000000, 'Name': '章节 2'}],
}


def test_sidecar_path_maps_emby_path_to_our_disk():
    assert sidecar_path('/media/电影/片名 (2026)/片名.strm') \
        == '/strm/电影/片名 (2026)/片名-media-info.json'
    assert sidecar_path('/media/剧集/S01E01.strm', emby_root='/media', strm_root='/mnt/strm') \
        == '/mnt/strm/剧集/S01E01-media-info.json'


def test_stream_row_maps_emby_stream_types_and_fields():
    video = stream_row(9, 0, SIDECAR['streams'][0])
    audio = stream_row(9, 1, SIDECAR['streams'][1])
    assert video[0] == 9 and video[1] == 0 and video[2] == 2      # Video
    assert audio[2] == 1                                          # Audio
    assert stream_row(9, 2, {'Type': 'Subtitle', 'Codec': 'subrip'})[2] == 3
    # 宽高与扩展 HDR 类型必须落进去，否则 Emby 详情页仍然是空的
    assert video[COLUMNS.split(',').index(' Height')] == 3840
    assert video[COLUMNS.split(',').index(' Width')] == 1604
    assert video[COLUMNS.split(',').index(' ExtendedVideoType')] == 100
    dv = stream_row(9, 0, {'Type': 'Video', 'ExtendedVideoType': 'DolbyVision'})
    assert dv[COLUMNS.split(',').index(' ExtendedVideoType')] == 130


def _prepare(tmp_path):
    db_path = tmp_path / 'library.db'
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    strm_root = tmp_path / 'strm'
    target = strm_root / '电影' / '片名 (2026)' / '片名 (2026).strm'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('http://127.0.0.1:9000/play115/x/y/z/1/%E7%89%87.mkv', encoding='utf-8')
    (target.parent / '片名 (2026)-media-info.json').write_text(
        json.dumps(SIDECAR, ensure_ascii=False), encoding='utf-8')
    conn.execute('INSERT INTO MediaItems (Id, Path) VALUES (7, ?)',
                 ('/media/电影/片名 (2026)/片名 (2026).strm',))
    # 第二条没有 sidecar，应该被计为跳过
    conn.execute('INSERT INTO MediaItems (Id, Path) VALUES (8, ?)',
                 ('/media/电影/缺信息/缺信息.strm',))
    # 第三条只有一条外挂字幕（现场 105 条这种情况）：补视频/音频时必须保留字幕行
    subtitle_dir = strm_root / '剧集' / 'S01E01'
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    (subtitle_dir / 'S01E01.strm').write_text('http://127.0.0.1:9000/play115/x/y/z/1/a.mkv',
                                              encoding='utf-8')
    (subtitle_dir / 'S01E01-media-info.json').write_text(
        json.dumps(SIDECAR, ensure_ascii=False), encoding='utf-8')
    conn.execute('INSERT INTO MediaItems (Id, Path, Size, RunTimeTicks) VALUES (9, ?, 2000, 3000)',
                 ('/media/剧集/S01E01/S01E01.strm',))
    conn.execute('INSERT INTO MediaStreams2 (ItemId, StreamIndex, StreamType, Codec, IsExternal) '
                 'VALUES (9, 0, 3, ?, 1)', ('ass',))
    conn.commit()
    conn.close()
    return db_path, str(strm_root)


def test_inject_writes_streams_and_is_idempotent(tmp_path):
    db_path, strm_root = _prepare(tmp_path)
    injector = Injector(str(db_path), strm_root=strm_root)

    dry = injector.run(dry_run=True)
    assert dry['candidates'] == 3 and dry['injected'] == 2      # 7 与 9 有 sidecar，8 没有
    check = sqlite3.connect(db_path)
    assert check.execute('SELECT count(*) FROM MediaStreams2 WHERE ItemId=7').fetchone()[0] == 0

    stats = injector.run()
    assert stats['ok'] is True and stats['injected'] == 2 and stats['no_sidecar'] == 1
    assert stats['streams'] == 4 and stats['chapters'] == 4     # 两条各 2 流 2 章节
    assert Path(stats['backup']).is_file()

    rows = check.execute('SELECT StreamType, Codec FROM MediaStreams2 WHERE ItemId=7 '
                         'ORDER BY StreamIndex').fetchall()
    assert rows == [(2, 'hevc'), (1, 'eac3')]
    assert check.execute('SELECT Width, Height FROM MediaStreams2 WHERE ItemId=7 AND StreamType=2').fetchone() == (1604, 3840)
    item = check.execute('SELECT Width, Height, TotalBitrate FROM MediaItems WHERE Id=7').fetchone()
    assert item == (3840, 1604, 15154535)
    assert check.execute('SELECT Size, RunTimeTicks FROM MediaItems WHERE Id=7').fetchone() == (1000, 27250560000)

    # 只有外挂字幕的条目：补上视频/音频，同时字幕行必须还在
    kinds = check.execute('SELECT StreamType, Codec, IsExternal FROM MediaStreams2 '
                          'WHERE ItemId=9 ORDER BY StreamIndex').fetchall()
    assert kinds == [(2, 'hevc', 0), (1, 'eac3', 0), (3, 'ass', 1)]
    assert check.execute('SELECT Size, RunTimeTicks FROM MediaItems WHERE Id=9').fetchone() == (2000, 3000)

    again = injector.run()
    assert again['candidates'] == 1 and again['injected'] == 0    # 8 没 sidecar，7/9 已补过
    check.close()


def test_inject_sidecar_is_instant_and_skips_items_that_already_have_video(tmp_path):
    """追更：sidecar 写好即刻写库；已经有视频流的条目直接跳过。"""
    db_path, strm_root = _prepare(tmp_path)
    sidecar = Path(strm_root) / '电影' / '片名 (2026)' / '片名 (2026)-media-info.json'

    assert inject_sidecar(str(db_path), 7, str(sidecar), strm_root) == ('injected', 2, '')
    # 再写一次：已经有视频流，跳过
    assert inject_sidecar(str(db_path), 7, str(sidecar), strm_root) == ('skip', 0, '')
    # sidecar 不存在
    assert inject_sidecar(str(db_path), 8, str(sidecar) + '.nope', strm_root) == \
        ('missing', 0, 'sidecar_missing')

