import asyncio
import json
from fractions import Fraction

from .cloud115 import CloudError

# 事实口径变化时递增：旧缓存里的「未知」会因此重新探测一次。
PROBE_REVISION = 3


def summarize(data: dict) -> dict:
    streams = data.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video' and
                  not s.get('disposition', {}).get('attached_pic')), None)
    if not video or not video.get('width') or not video.get('height') or not video.get('codec_name'):
        raise CloudError('未取得有效的视频流、尺寸和编码，探测未通过。', 'PROBE_INCOMPLETE', 422)
    try:
        fps = round(float(Fraction(video.get('avg_frame_rate') or '0')), 3) or None
    except (ValueError, ZeroDivisionError):
        fps = None
    raw_depth = video.get('bits_per_raw_sample')
    # FFprobe commonly emits this field as a string; zero means unknown.
    try:
        depth = int(raw_depth) if raw_depth is not None else None
    except (TypeError, ValueError, OverflowError):
        depth = None
    if not depth or depth < 0:
        depth = None
    if not depth:
        import re
        match = re.search(r'(?:p|gbrp)(10|12|14|16)(?:le|be)?$', video.get('pix_fmt', ''))
        depth = int(match[1]) if match else None
        if depth is None and video.get('pix_fmt') in ('yuv420p', 'yuv422p', 'yuv444p', 'yuvj420p', 'yuvj422p', 'yuvj444p', 'nv12', 'nv21', 'rgb24', 'bgr24', 'gray'):
            depth = 8
    # 整体码率：判断"是不是原盘 REMUX"最直接的证据（文件名常不写 REMUX）。
    rate = None
    raw_rate = (data.get('format') or {}).get('bit_rate')
    if isinstance(raw_rate, str) and raw_rate.isdigit():
        rate = int(raw_rate)
    elif isinstance(raw_rate, (int, float)) and raw_rate > 0:
        rate = int(raw_rate)
    side = video.get('side_data_list', [])
    dovi = next((s for s in side if 'dovi' in str(s.get('side_data_type', '')).lower()), None)
    transfer = video.get('color_transfer', '')
    if dovi:
        dynamic_range = 'Dolby Vision HDR' if transfer == 'smpte2084' else 'Dolby Vision'
    elif any('2094' in str(s.get('side_data_type', '')) or 'hdr10+' in str(s.get('side_data_type', '')).lower() for s in side):
        dynamic_range = 'HDR10+'
    elif transfer == 'smpte2084':
        dynamic_range = 'HDR PQ'
    elif transfer == 'arib-std-b67':
        dynamic_range = 'HLG'
    elif transfer in ('bt709', 'smpte170m', 'gamma22', 'gamma28', 'iec61966-2-1'):
        dynamic_range = 'SDR'
    elif isinstance(depth, int) and depth <= 8:
        # 8-bit 视频不可能承载 HDR10/杜比视界；没有传输特性时按 SDR 归类，
        # 否则洗版比较会把「未知」当成无法比较，把整批新片挡在门外。
        dynamic_range = 'SDR'
    else:
        dynamic_range = '未知'
    return {'width': video['width'], 'height': video['height'], 'codec': video['codec_name'],
            'fps': fps, 'fps_raw': video.get('avg_frame_rate'), 'dynamic_range': dynamic_range, 'bit_depth': depth,
            'video_profile': video.get('profile'), 'video_level': video.get('level'),
            'pixel_format': video.get('pix_fmt'), 'display_aspect_ratio': video.get('display_aspect_ratio'),
            'color_primaries': video.get('color_primaries'), 'color_space': video.get('color_space'),
            'color_transfer': video.get('color_transfer'), 'field_order': video.get('field_order'),
            'video_codec_tag': video.get('codec_tag_string'), 'video_bit_rate': video.get('bit_rate'),
            'video_title': (video.get('tags') or {}).get('title'),
            'dolby_vision_profile': dovi.get('dv_profile') if dovi else None,
            'duration': data.get('format', {}).get('duration'),
            'bit_rate': rate, 'probe_revision': PROBE_REVISION,
            'audio': [{'codec': s.get('codec_name'), 'channels': s.get('channels'),
                       'language': s.get('tags', {}).get('language'), 'profile': s.get('profile'),
                       'title': s.get('tags', {}).get('title'),
                       'atmos': 'atmos' in str(s.get('profile') or '').casefold()
                                or 'atmos' in str(s.get('tags', {}).get('title') or '').casefold(),
                       'sample_rate': s.get('sample_rate'), 'bit_rate': s.get('bit_rate'),
                       'bit_depth': s.get('bits_per_raw_sample'), 'channel_layout': s.get('channel_layout')}
                      for s in streams if s.get('codec_type') == 'audio']}


async def probe(url: str, headers: dict, timeout: int = 45):
    allowed = {k.lower(): str(v) for k, v in headers.items()
               if k.lower() in ('user-agent', 'referer', 'cookie', 'authorization')}
    if any('\r' in v or '\n' in v for v in allowed.values()):
        raise CloudError('媒体请求头无效。', 'INVALID_HEADERS')
    command = ['ffprobe', '-v', 'error', '-protocol_whitelist', 'http,https,tcp,tls',
               '-rw_timeout', '15000000', '-probesize', '8000000', '-analyzeduration', '8000000']
    if allowed:
        command += ['-headers', ''.join(f'{k}: {v}\r\n' for k, v in allowed.items())]
    command += ['-show_streams', '-show_format', '-of', 'json', url]
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(timeout):
            output = bytearray()
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > 1024 * 1024:
                    raise CloudError('探测输出超出限制。', 'PROBE_TOO_LARGE')
            await process.wait()
        if process.returncode:
            raise CloudError('FFprobe 无法读取此文件，请检查直链、格式和网络。', 'PROBE_FAILED', 422)
        try:
            return summarize(json.loads(output))
        except (json.JSONDecodeError, TypeError, AttributeError):
            raise CloudError('FFprobe 输出无法解析。', 'PROBE_FAILED', 422) from None
    except TimeoutError:
        raise CloudError('远程探测超过总时限，已终止进程。', 'PROBE_TIMEOUT', 504) from None
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
