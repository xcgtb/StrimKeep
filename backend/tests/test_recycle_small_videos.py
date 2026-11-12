"""低于阈值的视频：默认跟随只跳过，打开开关才回收。"""
from strimkeep.organize_options import filter_files

SMALL = {'id': '1', 'name': '预告片.mkv', 'size': 50 * 1024 * 1024, 'parent_id': '100'}
BIG = {'id': '2', 'name': '正片.mkv', 'size': 2048 * 1024 * 1024, 'parent_id': '100'}


def test_small_video_is_kept_by_default():
    accepted, report = filter_files([SMALL], {'min_video_size_mb': 100})
    assert accepted == [], '低于阈值不整理'
    assert report['recycle'] == [], '默认不删除（对齐实测行为）'
    assert any('保留在源目录' in reason for reason in report['reasons'])


def test_small_video_is_recycled_when_enabled():
    accepted, report = filter_files([SMALL], {'min_video_size_mb': 100, 'recycle_small_videos': True})
    assert accepted == []
    assert [entry['id'] for entry in report['recycle']] == ['1']
    assert any('已回收' in reason for reason in report['reasons'])


def test_video_above_threshold_is_untouched():
    accepted, report = filter_files([BIG], {'min_video_size_mb': 100, 'recycle_small_videos': True})
    assert [entry['id'] for entry in accepted] == ['2']
    assert report['recycle'] == []
