"""A real HTTP media file is read by the installed FFprobe, without 115 credentials."""
import asyncio
import functools
import http.server
import shutil
import subprocess
import threading

import pytest

from strimkeep.cloud115 import CloudError
from strimkeep.probe import probe


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='需要 FFmpeg/FFprobe')
def test_ffprobe_reads_real_http_video(tmp_path):
    path = tmp_path / 'video.mkv'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=320x240:r=24',
                    '-t', '0.4', '-c:v', 'ffv1', '-y', str(path)], check=True)
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = asyncio.run(probe(f'http://127.0.0.1:{server.server_port}/video.mkv', {'User-Agent': 'StrimKeep/Test'}))
        assert result['width'] == 320 and result['height'] == 240
        assert result['codec'] == 'ffv1' and result['fps'] == 24
        with pytest.raises(CloudError, match='无法读取'):
            asyncio.run(probe(f'http://127.0.0.1:{server.server_port}/missing.mkv', {}))
    finally:
        server.shutdown(); server.server_close(); thread.join()
