"""Read-only browsing of media mounts visible inside the container."""
import os
from pathlib import Path
from app.cloud.errors import CloudError


def _mount_points():
    try:
        lines = Path('/proc/self/mountinfo').read_text().splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            continue
        value = fields[4]
        for encoded, decoded in [('\\040', ' '), ('\\011', '\t'), ('\\134', '\\')]:
            value = value.replace(encoded, decoded)
        result.append(Path(value))
    return result


def _roots():
    roots = []
    for path in _mount_points():
        if path == Path('/') or path.parts[1:2] in [('proc',), ('sys',), ('dev',), ('etc',), ('run',), ('app',), ('data',)]:
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                roots.append(path.resolve(strict=True))
        except OSError:
            continue
    return sorted(set(roots), key=str)


def browse(value=''):
    roots = _roots()
    response = {'roots': [{'path': str(p), 'name': str(p), 'available': True} for p in roots], 'path': '', 'items': [], 'parent': '', 'truncated': False}
    if not value:
        return response
    raw = Path(value)
    if not raw.is_absolute() or '..' in raw.parts or '\x00' in str(raw):
        raise CloudError('请选择挂载目录内的绝对路径', 400)
    try:
        path = raw.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CloudError('目录不存在或暂不可访问', 404) from None
    containers = [root for root in roots if path == root or root in path.parents]
    if not containers:
        raise CloudError('只能浏览容器已挂载的媒体目录', 403)
    if not path.is_dir():
        raise CloudError('请选择实际目录', 400)
    root = max(containers, key=lambda p: len(p.parts))
    children = []
    try:
        with os.scandir(path) as entries:
            for index, item in enumerate(entries):
                if index >= 1000:
                    response['truncated'] = True
                    break
                if item.name.startswith('.') or item.is_symlink() or not item.is_dir(follow_symlinks=False):
                    continue
                child = Path(item.path).resolve(strict=True)
                if child == root or root in child.parents:
                    children.append({'path': str(child), 'name': item.name})
    except OSError:
        raise CloudError('目录读取失败，请检查 Docker 挂载及权限', 409) from None
    response.update(path=str(path), parent='' if path == root else str(path.parent), items=sorted(children, key=lambda item: item['name'].casefold()))
    return response
