#!/usr/bin/env python3
"""Resolve CI image version without editing tracked source files."""
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
RELEASE_TAG = re.compile(r'v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))')


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


def resolve(root, ref_type, ref_name):
    if ref_type == 'tag':
        match = RELEASE_TAG.fullmatch(ref_name)
        if not match:
            raise ValueError('Release tag must have the form v1.2.3')
        return match[1]
    # Full checkout history is required by CI. Only releases in this branch
    # determine its base version; unrelated branch tags are ignored.
    tags = git(root, 'tag', '--merged', 'HEAD', '--sort=-version:refname').splitlines()
    base = next((RELEASE_TAG.fullmatch(tag)[1] for tag in tags
                 if RELEASE_TAG.fullmatch(tag)), None)
    if base is None:
        source = (Path(root)/'app/version.py').read_text()
        match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", source)
        if not match:
            raise ValueError('Missing source fallback version')
        base = match[1]
    sha = git(root, 'rev-parse', '--short=12', 'HEAD')
    return f'{base}-dev.{sha}'


if __name__ == '__main__':
    try:
        print(resolve(ROOT, os.environ.get('GITHUB_REF_TYPE', 'branch'),
                      os.environ.get('GITHUB_REF_NAME', '')))
    except (ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
