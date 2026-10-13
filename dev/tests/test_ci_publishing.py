"""Protect the single-publish GitHub Actions topology from duplicate tag builds."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    return yaml.load((ROOT / '.github/workflows' / name).read_text(encoding='utf-8'),
                     Loader=yaml.BaseLoader)


def test_tag_is_only_automatic_publish_trigger_and_visible():
    checks = workflow('tests.yml')
    publish = workflow('docker.yml')
    assert set(checks['on']) == {'pull_request', 'workflow_call'}
    assert publish['on']['push'] == {'tags': ['v*']}
    assert 'workflow_dispatch' in publish['on']
    assert 'branches' not in publish['on']['push']
    assert 'github.ref_name' in publish['run-name']
    assert publish['jobs']['test']['uses'] == './.github/workflows/tests.yml'
    assert publish['jobs']['build']['needs'] == 'test'


def test_release_and_latest_share_one_build_job():
    publish = workflow('docker.yml')
    steps = publish['jobs']['build']['steps']
    version = next(step for step in steps if step.get('id') == 'ver')
    metadata = next(step for step in steps if step.get('id') == 'meta')
    builds = [step for step in steps if str(step.get('uses', '')).startswith('docker/build-push-action@')]
    assert '--github-output' in version['run']
    assert len(builds) == 1
    assert "steps.ver.outputs.release == 'true'" in metadata['with']['tags']
    assert 'type=raw,value=latest' in metadata['with']['tags']
    assert 'type=raw,value=${{ steps.ver.outputs.minor }}' not in metadata['with']['tags']
    assert any(step.get('name') == 'Check release version and CHANGELOG' for step in steps)
