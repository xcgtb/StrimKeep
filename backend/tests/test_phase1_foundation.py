"""第一阶段「稳定底座」专项测试：B01/B02/B06/B07/B08 的行为契约。"""
import hashlib
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from strimkeep.cloud115 import CloudError, classify_rejection
from strimkeep.emby import EmbyDelivery
from strimkeep.media import media_from_filename, technical_name, parse_name, NeedsReview
from strimkeep.playaddr import display_slug, play_url, parse_play_path
from strimkeep.share_service import quality_rank
from strimkeep.share_service import episode_range_of


# ---------- B02 播放地址与播放身份 ----------

def test_play_url_replaces_structural_characters_but_keeps_chinese():
    # ? # % 空格 / 等破坏 URL 结构的字符必须替换；中文原样保留可读。
    slug = display_slug('测试剧集 (2024) S01E01?1080p#tag%v2 What If...?.mkv')
    for broken in ('?', '#', '%', ' ', '/'):
        assert broken not in slug
    assert '测试剧集' in slug and 'What_If' in slug
    url = play_url('https://media.example:9443/', 'a' * 32, '测试剧集 (2024) S01E01.mkv', 'tok_en-1')
    assert url == 'https://media.example:9443/play/' + 'a' * 32 + '/' + display_slug('测试剧集 (2024) S01E01.mkv')
    assert '\n' not in url and '\r' not in url


def test_parse_play_path_accepts_legacy_and_slugged_formats():
    assert parse_play_path('/play/' + 'a' * 32) == ('a' * 32, '')
    assert parse_play_path('/play/' + 'a' * 32 + '/测试电影_(2024).mkv') == ('a' * 32, '测试电影_(2024).mkv')
    assert parse_play_path('/play/share/' + 'b' * 16 + '/名字.strm')[0] == 'share'
    assert parse_play_path('/api/v1/status') == (None, None)
    assert parse_play_path('') == (None, None)


def test_emby_matches_by_playback_identity_in_both_formats():
    target = {'relative': '影视媒体库/电影/测试电影 (2024)/测试电影 (2024).strm', 'pid': 'a' * 32}
    legacy = {'Path': '/media/a.strm', 'MediaSources': [{'Path': 'http://play/play/' + 'a' * 32 + '?token=hidden'}]}
    slugged = {'Path': '/media/a.strm', 'MediaSources': [{'Path': 'http://play/play/' + 'a' * 32 + '/测试电影_(2024).mkv?token=hidden'}]}
    other = {'Path': '/media/a.strm', 'MediaSources': [{'Path': 'http://play/play/' + 'b' * 32 + '/测试电影_(2024).mkv?token=hidden'}]}
    assert EmbyDelivery.matches(legacy, target)
    assert EmbyDelivery.matches(slugged, target)
    assert not EmbyDelivery.matches(other, target)


# ---------- B01/B08 错误分类 ----------

def test_classify_rejection_never_mislabels_share_or_api_errors_as_file_deleted():
    # 登录语义：错误码优先，文字辅助，永远可恢复。
    assert classify_rejection(99, '请重新登录') == 'LOGIN_INVALID'
    assert classify_rejection(None, '请重新登录') == 'LOGIN_INVALID'
    # 「不存在」文字必须按调用场景决定语义：
    assert classify_rejection(None, '文件不存在', 'file') == 'FILE_NOT_FOUND'
    assert classify_rejection(20018, '文件不存在或已删除', 'file') == 'FILE_NOT_FOUND'
    assert classify_rejection(None, '分享不存在或已取消', 'share') == 'SHARE_NOT_FOUND'
    # 「接口不存在」「对象不存在」等未明确语义的场景，绝不能当成自有文件已删除。
    assert classify_rejection(None, '接口不存在') == 'OBJECT_NOT_FOUND'
    assert classify_rejection(20018, '对象不存在') == 'OBJECT_NOT_FOUND'
    # 115 的限流文案单独归类，程序才能自动降速冷却；它依然不是「文件已删除」。
    assert classify_rejection(None, '请求过于频繁，请稍后再试') == 'RATE_LIMITED'
    assert classify_rejection(None, '服务器开小差了') == 'CLOUD_REJECTED'


# ---------- B07 虚构规格 ----------

def test_media_from_filename_keeps_missing_fields_unknown():
    sparse = media_from_filename('测试电影 (2024).mkv')
    assert sparse['width'] is None and sparse['height'] is None
    assert sparse['codec'] is None and sparse['dynamic_range'] is None
    assert sparse['bit_depth'] is None and sparse['fps'] is None
    assert sparse['audio'] == [] and sparse['estimated'] is True
    # 明确写出的字段保留，并整体标记为文件名降级来源。
    stated = media_from_filename('测试剧集 S01E01.2160p.H.265.DDP5.1-HGROUP.mkv')
    assert (stated['width'], stated['height']) == (3840, 2160)
    assert stated['codec'] == 'hevc' and stated['audio'][0]['codec'] == 'eac3'
    assert stated['estimated'] is True
    # 未知字段不冒充真实规格，也不默认 HDR/SDR。
    assert media_from_filename('Movie.2024.1080p.mkv')['dynamic_range'] is None


def test_technical_name_omits_missing_fields_instead_of_faking():
    sparse = media_from_filename('测试电影 (2024).mkv')
    name = technical_name(sparse, '测试电影 (2024).mkv')
    assert name == ''  # 无任何可确认的技术字段时，不虚构任何规格段
    stated = media_from_filename('测试电影 (2024).2160p.H.265.mkv')
    assert technical_name(stated, '测试电影 (2024).2160p.H.265.mkv') == '2160p.H.265'


def test_resolution_label_is_orientation_independent():
    from strimkeep.cards import version
    for width, height in ((3840, 2160), (2160, 3840)):
        media = {'width': width, 'height': height}
        assert technical_name(media, 'example.mkv') == '2160p'
        assert version({'name': 'example.mkv'}, media) == '2160p'
    assert technical_name({'width': 4320, 'height': 7680}, 'example.mkv') == '4320p'


def test_facts_survive_sparse_media():
    from strimkeep.quality import facts, compare, KEYS, UpgradePolicy
    rules = [{'key': k, 'enabled': k == 'resolution'} for k in KEYS]
    policy = UpgradePolicy.model_validate({'mode': 'upgrade', 'rules': rules}).model_dump()
    new_facts = facts('新.2024.2160p.mkv', media_from_filename('新.2024.2160p.mkv'), 1000)
    old_facts = facts('旧.2024.mkv', media_from_filename('旧.2024.mkv'), 1000)
    # 旧文件分辨率未知：比较必须停在「证据不足」，不允许按默认值判劣质。
    with pytest.raises(NeedsReview):
        compare(new_facts, old_facts, policy)


# ---------- B06 TG 分享洗版误删 ----------

def test_wash_candidates_require_same_season_episode_range():
    assert episode_range_of('测试剧集 S01E02.1080p.mkv') == (1, 2, 2)
    assert episode_range_of('测试剧集 S01E03-E04.1080p.mkv') == (1, 3, 4)
    # 合并集与单集范围不同：不可比。
    assert episode_range_of('测试剧集 S01E03.1080p.mkv') != episode_range_of('测试剧集 S01E03-E04.1080p.mkv')
    # 无法解析季集的文件：不可比，永不参与淘汰。
    assert episode_range_of('无法解析的文件名.mkv') is None
    assert episode_range_of('测试电影 (2024).mkv') is None


def test_quality_rank_treats_missing_resolution_as_unknown():
    assert quality_rank('剧集 S01E01.2160p.DV.mkv') == (4, 3)
    # 分辨率标签缺失=未知（None），不是最低档。
    assert quality_rank('剧集 S01E01.mkv') is None
    assert quality_rank('剧集 S01E01.480p.mkv') == (1, 0)


def test_share_upgrade_requires_evidence_on_both_sides():
    # U05 统一后的证据契约：质量证据缺失=未知（None），未知不可比、不授权淘汰。
    # 洗版决策由 quality.compare 承担；这里验证证据函数本身的行为。
    assert quality_rank('新 S01E01.2160p.DV.mkv') == (4, 3)
    assert quality_rank('新 S01E01.1080p.mkv') == (3, 0)
    # 任一侧分辨率证据缺失：不可比（None），不会授权替换。
    assert quality_rank('新 S01E01.无标签.mkv') is None


# ---------- B08 分享存活检测 ----------

class ShareCheckCloud:
    def __init__(self, store, failures=None):
        self.store = store
        self.failures = failures or {}

    async def close(self):
        pass

    async def status(self):
        return {'connected': True}

    async def share_url_download(self, file_id, share_code, receive_code, ua):
        error = self.failures.get(file_id)
        if error:
            raise error
        return 'https://cdn.example/file', {}


def _seed_share_rows(client, rows):
    store = client.app.state.store
    strm_root = client.app.state.strms
    with store.db:
        for sid, fid, name in rows:
            rel = '分享STRM待整理/' + name + '.strm'
            path = strm_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('https://media.example/play115/share/' + sid + '?token=x\n', encoding='utf-8')
            store.db.execute(
                'INSERT OR REPLACE INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created) '
                "VALUES(?,?,?,?,?,?,?,?,0,?)",
                (sid, 'code123', 'recv', fid, '', name, rel,
                 hashlib.sha256(b'x').hexdigest(), time.time()))


def test_share_check_alive_skips_unknown_errors_and_only_retires_proven_shares(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    failures = {
        '2001': CloudError('请求过于频繁，请稍后再试。', 'CLOUD_REJECTED', 422),
        '2002': CloudError('115 登录失效。', 'LOGIN_INVALID', 401),
        '2003': CloudError('接口不存在。', 'OBJECT_NOT_FOUND', 409),
        '2004': CloudError('分享不存在或已取消。', 'SHARE_NOT_FOUND', 404),
    }
    from strimkeep.main import create_app
    app = create_app(tmp_path / 'data', tmp_path / 'strm', lambda store: ShareCheckCloud(store, failures))
    with TestClient(app) as c:
        response = c.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        assert response.status_code == 200
        c.headers['X-CSRF-Token'] = response.json()['csrf']
        _seed_share_rows(c, [('s1', '2001', '限流'), ('s2', '2002', '登录'),
                             ('s3', '2003', '接口'), ('s4', '2004', '失效'), ('s5', '2005', '正常')])
        result = c.post('/api/v1/share/check').json()
        # 只有明确分享失效证据的条目被撤销；限流/登录/接口变化/未知全部跳过。
        assert result['dead_count'] == 1 and result['skipped'] == 3 and result['alive'] == 1
        report = c.get('/api/v1/share/check').json()['history'][0]
        assert report['total'] == 5 and report['alive'] == 1 and report['dead'] == 1 and report['skipped'] == 3
        assert {item['id'] for item in report['details']} == {'s1', 's2', 's3', 's4'}
        assert next(item for item in report['details'] if item['id'] == 's2')['reason'] == '115 登录失效'
        assert 'recv' not in str(report) and 'code123' not in str(report)
        store = c.app.state.store
        states = {r['id']: r['revoked'] for r in store.db.execute('SELECT id,revoked FROM share_strms')}
        assert states == {'s1': 0, 's2': 0, 's3': 0, 's4': 1, 's5': 0}
        # 失效条目的 STRM 已清掉，其余保留。
        assert not (c.app.state.strms / '分享STRM待整理' / '失效.strm').exists()
        assert (c.app.state.strms / '分享STRM待整理' / '限流.strm').exists()
