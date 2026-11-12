from pathlib import Path, PurePosixPath
import os, re, stat


def read_unidentified_poster(root, item):
    root=Path(root)
    if root.is_symlink() or not root.is_dir():
        return None
    for file in item.get('cloud', []) + item.get('share', []):
        path=PurePosixPath(str(file.get('path') or ''))
        if path.is_absolute() or len(path.parts)<3 or '..' in path.parts or '\\' in str(path):
            continue
        parent=path.parent
        if re.fullmatch(r'(?i)season[ ._-]*\d+', parent.name):
            parent=parent.parent
        current=root
        for part in parent.parts:
            current=current/part
            if current.is_symlink(): break
        else:
            try:
                if not current.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
                    continue
                for name in ('poster.jpg','poster.jpeg','poster.png','poster.webp','folder.jpg','folder.png'):
                    candidate=current/name
                    try:
                        fd=os.open(candidate,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                    except OSError:
                        continue
                    try:
                        info=os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_size>10*1024*1024: continue
                        with os.fdopen(fd,'rb',closefd=False) as stream:
                            data=stream.read(10*1024*1024+1)
                        if len(data)>10*1024*1024: continue
                        mime = 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/webp' if data[:4]==b'RIFF' and data[8:12]==b'WEBP' else None
                        if mime: return data,mime
                    finally:
                        os.close(fd)
            except (OSError,ValueError):
                continue
    return None
