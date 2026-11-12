"""0.8.38：/metrics 请求计数（错误率分母）、apply 只剩预演、备份脚本可用。"""
import pathlib
import sqlite3
import subprocess

from strimkeep.metrics import Metrics
from strimkeep.store import Store


class _FakeApp:
    version = '0.8.38'

    def __init__(self):
        self.state = type('S', (), {})()


def test_metrics_records_http_requests_by_route_template(tmp_path):
    """请求按「方法 + 路由模板 + 状态码」计数：/metrics 才能算出错误率。"""
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    metrics = Metrics(store)
    metrics.record_request('GET', '/api/v1/share/strm/{sid}', 200)
    metrics.record_request('GET', '/api/v1/share/strm/{sid}', 404)
    text = metrics.render(_FakeApp())
    assert 'strimkeep_http_requests_total{method="GET",route="/api/v1/share/strm/{sid}",status="200"} 1' in text
    assert 'strimkeep_http_requests_total{method="GET",route="/api/v1/share/strm/{sid}",status="404"} 1' in text
    store.close()


def test_backup_script_writes_valid_sqlite_snapshot(tmp_path):
    """备份脚本必须产出可打开的 SQLite 快照，而不是半成品。"""
    data = tmp_path / 'data'
    store = Store(data, 'admin', 'test-password-1234')
    store.set_many({'playback_base': 'http://example.test'})
    store.close()
    assert (data / 'data' / 'state.db').is_file()

    script = pathlib.Path(__file__).resolve().parents[2] / 'scripts' / 'backup.sh'
    assert script.is_file(), script
    dest = tmp_path / 'backups'
    result = subprocess.run(['sh', str(script), str(data), str(dest), '3'],
                            capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)

    snapshots = sorted(dest.glob('state-*.db'))
    assert len(snapshots) == 1
    assert not list(dest.glob('*.part'))
    with sqlite3.connect(snapshots[0]) as con:
        assert con.execute('SELECT count(*) FROM sqlite_master').fetchone()[0] > 0
        assert con.execute("SELECT count(*) FROM settings WHERE key='playback_base'").fetchone()[0] == 1
