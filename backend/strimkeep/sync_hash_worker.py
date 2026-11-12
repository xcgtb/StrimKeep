"""Read-only subprocess for bounded local output checksums.

The parent owns SQLite, cloud calls, rate limits and publication locks.
No credentials or media URLs are sent to this worker.
"""
import asyncio
import hashlib
import json
import os
import stat
import sys

MAX_SIZE = 20 * 1024 * 1024


def checksum(path):
    try:
        with open(path, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SIZE:
                return None
            digest = hashlib.sha256()
            remaining = MAX_SIZE + 1
            while remaining:
                data = source.read(min(1024 * 1024, remaining))
                if not data: break
                digest.update(data)
                remaining -= len(data)
            if not remaining:
                return None
            after = os.fstat(source.fileno())
            current = os.stat(path)
            identity = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns)
            if identity(info) != identity(after) or identity(info) != identity(current):
                return None
            return digest.hexdigest()
    except (OSError, ValueError):
        return None


def serve():
    for raw in sys.stdin.buffer:
        try:
            request = json.loads(raw)
            path = request['path']
            if not isinstance(path, str) or len(path) > 8192:
                raise ValueError('invalid path')
            result = checksum(path)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            result = None
        sys.stdout.write(json.dumps({'sha256':result}, separators=(',', ':')) + '\n')
        sys.stdout.flush()


class HashWorkerError(Exception):
    pass


class HashWorker:
    def __init__(self):
        self.process = None
        self.lock = asyncio.Lock()

    async def _start(self):
        # Deliberately omit the server environment, including administrator
        # credentials, proxy settings and other unrelated secrets.
        env = {'PATH': os.environ.get('PATH', ''), 'PYTHONUNBUFFERED': '1'}
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'strimkeep.sync_hash_worker',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=16384)

    async def close(self):
        process, self.process = self.process, None
        if process is None: return
        if process.stdin: process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            if process.returncode is None:
                try: process.kill()
                except ProcessLookupError: pass
            await process.wait()

    async def checksum(self, path):
        async with self.lock:
            # Read-only checks are safe to repeat if a child exits mid-request.
            for attempt in range(2):
                try:
                    if self.process is None or self.process.returncode is not None:
                        await self.close()
                        await self._start()
                    self.process.stdin.write((json.dumps({'path':str(path)}, ensure_ascii=True) + '\n').encode())
                    await asyncio.wait_for(self.process.stdin.drain(), 15)
                    raw = await asyncio.wait_for(self.process.stdout.readline(), 15)
                    if not raw: raise EOFError()
                    result = json.loads(raw)
                    value = result['sha256']
                    if value is not None and (not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value)):
                        raise ValueError('invalid digest')
                    return value
                except asyncio.CancelledError:
                    await self.close()
                    raise
                except (OSError, EOFError, ValueError, TypeError, KeyError, TimeoutError, json.JSONDecodeError):
                    await self.close()
                    if attempt: raise HashWorkerError('本地文件核对子进程异常，已停止本次同步。') from None


if __name__ == '__main__':
    serve()
