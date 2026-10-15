"""Bounded ffprobe reads of verified 115 sources; credentials stay in Python."""

import hashlib
import ipaddress
import json
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
from app.cloud import store
from app.cloud.errors import CloudError

MAX_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 8 * 1024 * 1024
TIMEOUT = 30
UA = "StrimKeep-MediaProbe"
_GATE = threading.BoundedSemaphore(2)


def available():
    return bool(shutil.which("ffprobe"))


def summarize(data):
    streams = data.get("streams", [])
    if not isinstance(streams, list) or len(streams) > 128:
        raise CloudError("ffprobe 返回的媒体流信息异常", 502)
    videos = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise CloudError("ffprobe 未识别到视频流，保留源文件", 409)
    video = next((s for s in videos if s.get("disposition", {}).get("default")), videos[0])
    audios = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    audio = next((s for s in audios if s.get("disposition", {}).get("default")), audios[0] if audios else {})
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if not 0 < width <= 32768 or not 0 < height <= 32768:
        raise CloudError("ffprobe 未能确认视频分辨率", 502)
    # Cropped widescreen sources retain their familiar display tier (e.g. 3840x1600).
    tier = next((p for p, w in ((4320, 7680), (2160, 3840), (1440, 2560), (1080, 1920), (720, 1280)) if width >= w or height >= p), height)
    scan = "i" if video.get("field_order") in ("tt", "bb", "tb", "bt") else "p"
    codec = {"hevc": "H265", "h264": "H264", "av1": "AV1", "vp9": "VP9", "mpeg2video": "MPEG2"}.get(video.get("codec_name"), str(video.get("codec_name") or "").upper())
    audio_codec = {"eac3": "DDP", "ac3": "DD", "truehd": "TrueHD", "dts": "DTS", "aac": "AAC", "flac": "FLAC", "opus": "Opus"}.get(audio.get("codec_name"), str(audio.get("codec_name") or "").upper())
    fps = ""
    for field in ("avg_frame_rate", "r_frame_rate"):
        try:
            rate = Fraction(str(video.get(field) or "0/0"))
            if 0 < rate <= 1000:
                fps = f"{float(rate):.3f}".rstrip("0").rstrip(".") + "fps"
                break
        except (ValueError, ZeroDivisionError):
            pass
    depth = str(video.get("bits_per_raw_sample") or "")
    if depth in ("", "0"):
        match = re.search(r"(?:p|gbrp)(\d{2})(?:le|be)?$", str(video.get("pix_fmt") or ""))
        depth = match[1] if match else ("8" if video.get("pix_fmt") in ("yuv420p", "yuv422p", "yuv444p", "nv12", "rgb24", "bgr24") else "")
    dynamic_range = ""
    side = video.get("side_data_list") or []
    if any(isinstance(s, dict) and ("dovi" in str(s.get("side_data_type", "")).lower() or "dolby vision" in str(s.get("side_data_type", "")).lower()) for s in side):
        dynamic_range = "DV"
    elif any(isinstance(s, dict) and "2094-40" in str(s.get("side_data_type", "")) for s in side):
        dynamic_range = "HDR10+"
    elif video.get("color_transfer") == "smpte2084":
        dynamic_range = "HDR10"
    elif video.get("color_transfer") == "arib-std-b67":
        dynamic_range = "HLG"
    elif video.get("color_transfer") in ("bt709", "smpte170m", "bt470bg", "gamma22", "gamma28"):
        dynamic_range = "SDR"
    duration = str((data.get("format") or {}).get("duration") or "")
    try:
        duration = f"{float(duration):.3f}" if 0 < float(duration) < 604800 else ""
    except (ValueError, OverflowError):
        duration = ""
    channels = str(audio.get("channels") or "")
    layout = str(audio.get("channel_layout") or "")
    layout = {"mono": "1.0", "stereo": "2.0", "5.1(side)": "5.1", "7.1(wide)": "7.1", "7.1(wide-side)": "7.1"}.get(layout, layout)
    if not re.fullmatch(r"\d{1,2}\.\d{1,2}", layout):
        layout = ""
    return {"resolution": f"{tier}{scan}", "width": str(width), "height": str(height), "codec": codec, "video_codec": codec, "audio_codec": audio_codec, "audio_channels": channels, "audio_layout": layout, "dynamic_range": dynamic_range, "fps": fps, "bitdepth": depth+"bit" if depth in ("8", "9", "10", "12", "14", "16") else "", "duration": duration}


def probe_url(link, size):
    """Relay ranged chunks on loopback; reject redirects and cap bytes/time globally."""
    from app.cloud.playback import safe_url
    if size <= 0:
        raise CloudError("源文件大小无效，无法提取媒体信息", 409)
    url = safe_url(link["url"])
    addresses = socket.getaddrinfo(urlsplit(url).hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise CloudError("115 CDN 地址异常，无法提取媒体信息", 502)
    headers = {str(k): str(v) for k, v in link.get("headers", {}).items() if k.lower() in ("user-agent", "cookie", "referer")}
    headers.update({"User-Agent": UA, "Accept-Encoding": "identity"})
    token = "/"+secrets.token_hex(24)
    state = {"remaining": MAX_BYTES, "deadline": time.monotonic()+TIMEOUT, "requests": 0}
    lock = threading.Lock()

    class Relay(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_HEAD(self):
            if self.path != token:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

        def do_GET(self):
            if self.path != token:
                self.send_error(404)
                return
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", "bytes=0-"))
            if not match:
                self.send_error(416)
                return
            start = int(match[1])
            with lock:
                state["requests"] += 1
                if start >= size or state["remaining"] <= 0 or state["requests"] > 24 or time.monotonic() >= state["deadline"]:
                    self.send_error(416)
                    return
                end = min(int(match[2]) if match[2] else size-1, size-1, start+CHUNK_BYTES-1, start+state["remaining"]-1)
            if end < start:
                self.send_error(416)
                return
            request_headers = {**headers, "Range": f"bytes={start}-{end}"}
            sent = False
            try:
                self.connection.settimeout(10)
                with httpx.Client(follow_redirects=False, trust_env=False, timeout=10) as client:
                    with client.stream("GET", url, headers=request_headers) as response:
                        expected = f"bytes {start}-{end}/{size}"
                        valid = response.status_code == 206 and response.headers.get("content-range") == expected
                        full = response.status_code == 200 and start == 0 and end == size-1
                        if not (valid or full) or response.headers.get("content-encoding", "identity") != "identity":
                            self.send_error(502)
                            return
                        self.send_response(206)
                        self.send_header("Content-Length", str(end-start+1))
                        self.send_header("Content-Range", expected)
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        sent = True
                        left = end-start+1
                        for chunk in response.iter_raw(65536):
                            with lock:
                                if time.monotonic() >= state["deadline"] or len(chunk) > left or len(chunk) > state["remaining"]:
                                    break
                                state["remaining"] -= len(chunk)
                            self.wfile.write(chunk)
                            left -= len(chunk)
                            if not left:
                                break
            except Exception:
                if not sent:
                    try:
                        self.send_error(502)
                    except OSError:
                        pass
            finally:
                self.close_connection = True

    executable = shutil.which("ffprobe")
    if not executable:
        raise CloudError("未安装 ffprobe，请更新包含 ffmpeg 的镜像", 409)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    entries = "stream=codec_type,codec_name,width,height,pix_fmt,bits_per_raw_sample,avg_frame_rate,r_frame_rate,color_transfer,channels,channel_layout,field_order:stream_disposition=default,attached_pic:stream_side_data:format=duration"
    command = [executable, "-v", "error", "-protocol_whitelist", "http,tcp", "-format_whitelist", "mov,matroska,webm,avi,mpegts,flv,asf,mpeg,ogg", "-rw_timeout", "10000000", "-probesize", "8000000", "-analyzeduration", "5000000", "-show_entries", entries, "-of", "json", f"http://127.0.0.1:{server.server_port}{token}"]
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(command, stdout=output, stderr=subprocess.DEVNULL, timeout=TIMEOUT, check=False)
            output.seek(0)
            raw = output.read(1024*1024+1)
        if completed.returncode or len(raw) > 1024*1024:
            raise CloudError("ffprobe 提取失败或超过读取限制，保留源文件", 502)
        return summarize(json.loads(raw))
    except CloudError:
        raise
    except Exception:
        raise CloudError("ffprobe 提取超时或响应异常，保留源文件", 502) from None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def extract(account_id, provider, row):
    from app.cloud import events
    events.emit('probe','ffprobe 媒体信息提取：'+row['name'])
    current = provider.info(row["id"])
    if current["is_dir"] or any(current[k] != row[k] for k in ("size", "sha1")):
        raise CloudError("源文件指纹变化，无法提取媒体信息", 409)
    fingerprint = hashlib.sha256(store.encode([current["size"], current["sha1"], 1]).encode()).hexdigest()
    with store.db() as con:
        cached = con.execute("SELECT payload FROM cloud_media_probes WHERE account_id=? AND file_id=? AND fingerprint=?", (account_id, row["id"], fingerprint)).fetchone()
    if cached and current["sha1"]:
        events.emit('probe','复用 ffprobe 媒体信息缓存：'+row['name'])
        return {"status": "cached", "fields": json.loads(cached[0])}
    if not _GATE.acquire(timeout=1):
        raise CloudError("媒体信息提取繁忙，请稍后重新预览", 409)
    try:
        fields = probe_url(provider.download(current["id"], current["pickcode"], UA), current["size"])
    except CloudError:
        raise
    except Exception:
        raise CloudError("媒体信息提取失败，请检查源文件与网络", 502) from None
    finally:
        _GATE.release()
    after = provider.info(row["id"])
    if any(after[k] != current[k] for k in ("size", "sha1")):
        raise CloudError("提取过程中源文件指纹变化，请重新预览", 409)
    with store.db() as con:
        con.execute("INSERT OR REPLACE INTO cloud_media_probes VALUES(?,?,?,?,?)", (account_id, row["id"], fingerprint, store.encode(fields), time.time()))
    events.emit('probe','ffprobe 提取完成：'+row['name'])
    return {"status": "success", "fields": fields}
