"""代表音轨 = 正片音轨：多声道优先于立体声，编码偏好只在同类间生效。"""
import pytest

from strimkeep.audio import naming_track, representative
from strimkeep.media import NeedsReview


def test_multichannel_lossy_beats_stereo_lossless():
    tracks = [{'codec': 'pcm_s16le', 'channels': 2, 'language': 'chi'},
              {'codec': 'eac3', 'channels': 6, 'language': 'eng'},
              {'codec': 'ac3', 'channels': 6, 'language': 'eng'}]
    _, chosen = representative(tracks)
    assert (chosen['codec'], chosen['channels']) == ('eac3', 6)


def test_codec_preference_still_orders_multichannel_tracks():
    tracks = [{'codec': 'eac3', 'channels': 8}, {'codec': 'truehd', 'channels': 6}]
    _, chosen = representative(tracks, ['truehd', 'eac3'])
    assert chosen['codec'] == 'truehd'


def test_channels_break_ties_between_same_codec():
    tracks = [{'codec': 'eac3', 'channels': 6}, {'codec': 'eac3', 'channels': 8}]
    _, chosen = representative(tracks)
    assert chosen['channels'] == 8


def test_naming_follows_policy_but_never_prefers_a_stereo_track():
    media = {'audio': [{'codec': 'pcm_s16le', 'channels': 2},
                       {'codec': 'eac3', 'channels': 6}]}
    policy = {'rules': [{'key': 'audio', 'enabled': True, 'order': ['pcm', 'eac3']}]}
    assert naming_track(media, policy)['codec'] == 'eac3'


def test_strict_policy_still_rejects_unknown_tracks():
    with pytest.raises(NeedsReview):
        representative([{'codec': 'dts', 'channels': 6}], ['truehd', 'eac3'], strict=True)


def test_naming_uses_known_track_when_extra_stream_is_unknown():
    media = {'audio': [{'codec': 'aac', 'channels': 2}, {'codec': None, 'channels': 10}]}
    policy = {'rules': [{'key': 'audio', 'enabled': True, 'order': ['aac']}]}
    assert naming_track(media, policy)['codec'] == 'aac'
    assert naming_track({'audio': [{'codec': None, 'channels': 10}]}, policy) is None
    policy['rules'][0]['order'] = ['eac3']
    assert naming_track(media, policy)['codec'] == 'aac'
