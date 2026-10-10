"""Publishing boundaries must reject private data, credentials and tag drift."""
import importlib.util
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_check', ROOT/'dev/tools/release_check.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def base(tmp_path):
    (tmp_path/'app').mkdir()
    for name in ('app/version.py', 'Dockerfile', 'CHANGELOG.md',
                 'docker-compose.yml', 'dev/docker-compose.build.yml', 'dev/.env.example'):
        (tmp_path/name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, tmp_path/name)
    return tmp_path


def test_public_templates_and_checkout_allowed(tmp_path):
    root = base(tmp_path)
    (root/'.git').mkdir()
    assert checker.check(root) == []
    assert any('.git' in issue for issue in checker.check(root, archive=True))


def test_private_files_and_links_rejected(tmp_path):
    root = base(tmp_path)
    (root/'data').mkdir()
    (root/'state.db').write_bytes(b'fixture')
    (root/'compose.override.yml').write_text('private configuration')
    (root/'linked.env').symlink_to(root/'dev/.env.example')
    issues = checker.check(root, archive=True)
    for name in ('data', 'state.db', 'compose.override.yml', 'linked.env'):
        assert any(name in issue for issue in issues), issues


def test_secret_report_does_not_echo_value(tmp_path):
    root = base(tmp_path)
    value = 'private-fixture-value'
    (root/'.env.example').write_text('TMDB_KEY='+value)
    issues = checker.check(root)
    assert any('TMDB_KEY' in issue for issue in issues)
    assert all(value not in issue for issue in issues)


def test_docker_version_override_rejected(tmp_path):
    root = base(tmp_path)
    (root/'Dockerfile').write_text('ARG APP_VERSION=0.0.0\n')
    assert any('Docker version must come from app/version.py' in issue for issue in checker.check(root))
    (root/'Dockerfile').write_text('ENV APP_VERSION=0.0.0\n')
    assert any('Docker version must come from app/version.py' in issue for issue in checker.check(root))


def test_release_bump_needs_no_docker_or_compose_edits(tmp_path):
    root = base(tmp_path)
    (root/'app/version.py').write_text("__version__ = '9.8.7'\n")
    (root/'CHANGELOG.md').write_text('# 版本日志\n\n## 9.8.7（测试）\n')
    assert checker.check(root) == []


def test_stale_public_image_and_versioned_local_image_rejected(tmp_path):
    root = base(tmp_path)
    public = root/'docker-compose.yml'
    public.write_text(public.read_text().replace(':latest', ':0.0.0'))
    local = root/'dev/docker-compose.build.yml'
    local.write_text(local.read_text().replace(':local', ':0.0.0'))
    issues = checker.check(root)
    for name in ('docker-compose.yml', 'dev/docker-compose.build.yml'):
        assert f'image/source version mismatch: {name}' in issues


def test_public_compose_rejects_real_password(tmp_path):
    root = base(tmp_path)
    p = root/'docker-compose.yml'
    p.write_text(p.read_text().replace('CHANGE_ME_TO_A_STRONG_PASSWORD','private-fixture-password'))
    issues = checker.check(root)
    assert any('docker-compose.yml: WEB_PASSWORD' in issue for issue in issues)
    assert all('private-fixture-password' not in issue for issue in issues)
