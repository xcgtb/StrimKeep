"""Git staging must preserve binary assets and normalize frontend source only."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('asset', [
    'icons/apple-touch-icon.png', 'icons/favicon.ico',
    'images/overview-hero.webp', 'images/sky-city.webp',
])
def test_staging_and_checkout_keep_binary_bytes(tmp_path, asset):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    shutil.copyfile(ROOT/'.gitattributes', tmp_path/'.gitattributes')
    name = 'frontend/public/'+asset
    target = tmp_path/name
    target.parent.mkdir(parents=True)
    original = (ROOT/name).read_bytes()
    target.write_bytes(original)
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True)
    assert subprocess.check_output(['git', '-C', str(tmp_path), 'show', ':'+name]) == original
    target.unlink()
    subprocess.run(['git', '-C', str(tmp_path), 'checkout-index', '-a'], check=True)
    assert target.read_bytes() == original


def test_frontend_source_still_normalizes_crlf(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    shutil.copyfile(ROOT/'.gitattributes', tmp_path/'.gitattributes')
    target = tmp_path/'frontend/Example.vue'
    target.parent.mkdir()
    target.write_bytes(b'<template>\r\n  example\r\n</template>\r\n')
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True)
    assert b'\r\n' not in subprocess.check_output(['git', '-C', str(tmp_path), 'show', ':frontend/Example.vue'])
