"""0.8.41：metrics 令牌两种 meta 写法都认；离线兜底扫描节奏进设置（页面可调）。"""
import asyncio

import pytest
from pydantic import ValidationError

from strimkeep.metrics import metrics_token
from strimkeep.offline_api import OfflineConfig, directory_config
from strimkeep.store import Store
from test_organize import setup


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', 'test-password-1234')


def test_metrics_token_accepts_env_raw_and_json_meta(tmp_path, monkeypatch):
    """meta 既可能是文档里的裸字符串，也可能是 set_meta 的 JSON 串；两种都要认。"""
    store = make_store(tmp_path)
    monkeypatch.delenv('STRIMKEEP_METRICS_TOKEN', raising=False)
    assert metrics_token(store) == ''
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO meta VALUES('metrics_token','raw-token')")
    assert metrics_token(store) == 'raw-token'
    # 用 set_meta 写入时是 JSON（"json-token"）：只读裸值会拿到带引号的串，令牌永远对不上
    store.set_meta('metrics_token', 'json-token')
    assert metrics_token(store) == 'json-token'
    monkeypatch.setenv('STRIMKEEP_METRICS_TOKEN', 'env-token')
    assert metrics_token(store) == 'env-token'
    store.close()


def test_offline_scan_minutes_live_in_offline_config():
    """页面「整理节奏」写到 offline_config.scan_minutes，默认 5，0 = 关闭，上限 1440。"""
    assert directory_config({'scan_minutes': 0})['scan_minutes'] == 0
    assert OfflineConfig().scan_minutes == 5
    with pytest.raises(ValidationError):
        OfflineConfig(scan_minutes=5000)


def test_scan_minutes_no_longer_drives_scanning(tmp_path):
    """兜底扫描已停用：scan_minutes 仍是合法配置（旧数据兼容），但不再触发扫描。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'organize_config': {'source_cids': ['1'], 'archive_cid': '2'},
                        'offline_config': {'target_cid': '900', 'magnet_cid': '900', 'scan_minutes': 5}})
        scanned = []

        async def fresh(cid):
            scanned.append(cid)
            return [{'id': '500', 'name': '电影 (2024).mkv', 'is_dir': False}]

        engine.cloud = type('C', (), {'fresh_children': staticmethod(fresh)})()
        await engine.offline_scan_tick()
        assert scanned == []
        await http.aclose()
        store.close()
    asyncio.run(run())


