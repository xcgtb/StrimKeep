"""动态范围口径：8-bit 无传输特性按 SDR，旧缓存里的「未知」需要重探一次。"""
from strimkeep.cache import MediaCache
from strimkeep.probe import PROBE_REVISION, summarize

BASE = {'width': 3840, 'height': 1608, 'codec_name': 'hevc', 'avg_frame_rate': '25/1'}


def video(**extra):
    return {'streams': [{'codec_type': 'video', **BASE, **extra}],
            'format': {'duration': '3000'}}


def test_eight_bit_without_transfer_is_sdr():
    media = summarize(video(pix_fmt='yuv420p'))
    assert media['dynamic_range'] == 'SDR' and media['bit_depth'] == 8
    assert media['probe_revision'] == PROBE_REVISION


def test_dolby_vision_side_data_still_wins():
    media = summarize(video(pix_fmt='yuv420p10le',
                            side_data_list=[{'side_data_type': 'DOVI configuration record', 'dv_profile': 5}]))
    assert media['dynamic_range'] == 'Dolby Vision' and media['dolby_vision_profile'] == 5


def test_ten_bit_without_transfer_stays_unknown():
    media = summarize(video(pix_fmt='yuv420p10le'))
    assert media['dynamic_range'] == '未知'


def test_bt709_transfer_is_sdr_without_bit_depth():
    media = summarize(video(pix_fmt='yuv420p10le', color_transfer='bt709'))
    assert media['dynamic_range'] == 'SDR'


def test_stale_unknown_cache_entry_is_not_reused():
    stale = {'width': 3840, 'height': 1608, 'codec': 'hevc', 'dynamic_range': '未知'}
    assert MediaCache.sufficient(stale) is False
    fresh = {**stale, 'probe_revision': PROBE_REVISION}
    assert MediaCache.sufficient(fresh) is True


def test_string_sample_depth_is_normalized_and_used_for_sdr():
    media = summarize(video(bits_per_raw_sample='8', pix_fmt='unknown'))
    assert media['bit_depth'] == 8 and media['dynamic_range'] == 'SDR'


def test_zero_or_invalid_sample_depth_falls_back_to_pixel_format():
    for raw in ('0', 'N/A', 0):
        media = summarize(video(bits_per_raw_sample=raw, pix_fmt='yuv420p10le'))
        assert media['bit_depth'] == 10 and media['dynamic_range'] == '未知'


def test_legacy_string_bit_depth_cache_is_not_reused():
    media = {'width': 1920, 'height': 1080, 'codec': 'h264', 'dynamic_range': 'SDR', 'bit_depth': '8'}
    assert not MediaCache.sufficient(media)
