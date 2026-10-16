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
                 'requirements.txt', 'requirements-cloud115.txt',
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
    assert any('Docker APP_VERSION must be supplied by CI' in issue for issue in checker.check(root))
    (root/'Dockerfile').write_text('ENV APP_VERSION=0.0.0\n')
    assert any('Docker APP_VERSION must be supplied by CI' in issue for issue in checker.check(root))


def test_release_bump_needs_no_docker_or_compose_edits(tmp_path):
    root = base(tmp_path)
    change = root/'CHANGELOG.md'
    change.write_text(change.read_text().replace('## 1.0.3（', '## 9.8.7（', 1))
    assert checker.check(root) == []


def test_source_fallback_must_not_look_like_release(tmp_path):
    root = base(tmp_path)
    (root/'app/version.py').write_text("__version__ = '1.0.3'\n")
    assert any('source version must be non-release fallback' in x for x in checker.check(root))


def test_changelog_requires_unreleased_section(tmp_path):
    root = base(tmp_path)
    changelog = root/'CHANGELOG.md'
    changelog.write_text(changelog.read_text().replace('## 未发布\n\n', '', 1))
    assert any('CHANGELOG must begin with' in x for x in checker.check(root))


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


def test_docker_must_include_referenced_runtime_requirements(tmp_path):
    root = base(tmp_path)
    docker = root/'Dockerfile'
    docker.write_text(docker.read_text().replace(
        'COPY requirements.txt requirements-cloud115.txt ./', 'COPY requirements.txt ./'))
    assert 'Docker install stage missing requirements file: requirements-cloud115.txt' in checker.check(root)


def test_nested_requirements_dependency_must_be_copied(tmp_path):
    root = base(tmp_path)
    (root/'requirements-cloud115.txt').write_text('-r another-runtime.lock\n')
    (root/'another-runtime.lock').write_text('fixture-package==1\n')
    assert 'Docker install stage missing requirements file: another-runtime.lock' in checker.check(root)


def test_dependabot_can_discover_every_runtime_include(tmp_path):
    root = base(tmp_path)
    pins = root/'requirements-cloud115.txt'
    pins.rename(root/'requirements-cloud115.lock')
    for name in ('requirements.txt', 'Dockerfile'):
        p = root/name
        p.write_text(p.read_text().replace('requirements-cloud115.txt', 'requirements-cloud115.lock'))
    assert 'Dependabot requirements include must use .txt: requirements-cloud115.lock' in checker.check(root)
