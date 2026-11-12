"""版本号只有一个来源：backend/strimkeep/__init__.py。

历史上镜像 tag 在 docker-compose.yml 和 scripts/deploy_config.py 里各写一份，
其中一份还被测试钉死在 0.6.5；后端和前端升到 0.7.0 后，容器一直跑 0.6.5 的标签，
页面显示 0.7.0，回滚和排查都分不清在跑哪一版。这里把落点全部钉住。
"""
import importlib.util
import json
import re
from pathlib import Path

from strimkeep import __version__


ROOT = Path(__file__).resolve().parents[2]
IMAGE = re.compile(r'(?m)^\s*image:\s*strimkeep(?:-dev)?:(\S+)\s*$')


def test_compose_image_tag_matches_backend_version():
    tags = IMAGE.findall((ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
    assert tags == [__version__], f'compose 镜像 tag {tags} 与后端 {__version__} 不一致'


def test_frontend_package_version_matches_backend_version():
    package = json.loads((ROOT / 'frontend/package.json').read_text(encoding='utf-8'))
    assert package['version'] == __version__
    lock = json.loads((ROOT / 'frontend/package-lock.json').read_text(encoding='utf-8'))
    assert lock['version'] == __version__
    assert lock['packages']['']['version'] == __version__


def test_readme_heading_matches_backend_version():
    heading = re.search(r'(?m)^# StrimKeep ([\w.\-]+)$',
                        (ROOT / 'README.md').read_text(encoding='utf-8'))
    assert heading and heading.group(1) == __version__


def test_deploy_config_renders_current_version_not_a_constant():
    path = ROOT / 'scripts/deploy_config.py'
    spec = importlib.util.spec_from_file_location('deploy_version_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = module.convert({'name': 'existing', 'services': {'strimkeep': {'environment': {}}}})
    assert f'image: strimkeep:{__version__}' in text
    assert '${' not in text


def test_version_script_reports_no_drift():
    path = ROOT / 'scripts/version.py'
    spec = importlib.util.spec_from_file_location('version_script_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    version, problems = module.check()
    assert version == __version__
    assert problems == [], problems
