#!/usr/bin/env python3
"""Resolve CI image version from Git tags; never use the local source fallback."""
import argparse
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
    # Follow release ancestry, not numeric maxima or editable tag dates.
    # Historical v1.12.11 must not override a later v1.5.18 release, and
    # a feature branch's tags must not replace the main release lineage.
    tags = [tag for tag in git(root, 'tag', '--merged', 'HEAD').splitlines()
            if RELEASE_TAG.fullmatch(tag)]
    if tags:
        refs = {}
        for line in git(root, 'show-ref', '--tags', '--dereference').splitlines():
            sha, ref = line.split()
            name = ref.removeprefix('refs/tags/').removesuffix('^{}')
            # The peeled annotated-tag line follows its object line.
            refs[name] = sha
        by_commit = {}
        for tag in tags:
            by_commit.setdefault(refs[tag], []).append(tag[1:])
        for commit in git(root, 'rev-list', '--first-parent', 'HEAD').splitlines():
            if commit in by_commit:
                return max(by_commit[commit], key=lambda value: tuple(map(int, value.split('.'))))
    raise ValueError('No reachable release tag (vX.Y.Z) on first-parent history. Fetch full history and tags or create the first release tag before CI image publishing.')


def head_release_tag(root):
    """One release tag must point at exactly this commit, not an ancestor."""
    candidates = git(root, 'tag', '--points-at', 'HEAD').splitlines()
    releases = [tag for tag in candidates if RELEASE_TAG.fullmatch(tag)]
    if len(releases) > 1:
        raise ValueError('Multiple release tags on HEAD; cannot choose a unique image version')
    return releases[0] if releases else ''


def publish_metadata(root):
    """Main-only pushes update latest; a simultaneous release also gets fixed tags."""
    tag = head_release_tag(root)
    version = tag[1:] if tag else resolve(root, 'branch', 'main')
    return {
        'value': version,
        'tag': tag,
        'release': 'true' if tag else 'false',
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--github-output', action='store_true', help='print build metadata to GITHUB_OUTPUT')
    parser.add_argument('--head-release-tag', action='store_true', help='print an exact HEAD release tag, or empty')
    args = parser.parse_args()
    try:
        if args.github_output:
            for key, value in publish_metadata(ROOT).items():
                print(f'{key}={value}')
        elif args.head_release_tag:
            print(head_release_tag(ROOT))
        else:
            print(resolve(ROOT, os.environ.get('GITHUB_REF_TYPE', 'branch'),
                          os.environ.get('GITHUB_REF_NAME', '')))
    except (ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
