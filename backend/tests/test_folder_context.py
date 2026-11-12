"""目录上下文识别：父目录当剧名、文件名序号当集号，失败一律回落到原逻辑。"""
import pytest

from strimkeep.media import NeedsReview, folder_context, infer_episode, parse_name


def test_folder_context_pulls_title_year_and_season():
    assert folder_context('法证先锋三') == {'title': '法证先锋三', 'year': None, 'season': None}
    assert folder_context('某剧 (2023)') == {'title': '某剧', 'year': 2023, 'season': None}
    assert folder_context('某剧 S02') == {'title': '某剧', 'year': None, 'season': 2}
    assert folder_context('某剧 第二季 2160p')['season'] == 2
    assert folder_context('') is None


def test_infer_episode_reads_only_unambiguous_numbers():
    assert infer_episode('28') == 28
    assert infer_episode('第12集') == 12
    assert infer_episode('EP07') == 7
    assert infer_episode('法证先锋三 (5)', '法证先锋三') == 5
    assert infer_episode('法证先锋三 05', '法证先锋三') == 5
    assert infer_episode('法证先锋三 粤语', '法证先锋三') is None


def test_bare_number_file_uses_the_folder_as_show_name():
    hint = parse_name('28.mkv', folder='法证先锋三')
    assert hint == {'query': '法证先锋三', 'year': None, 'media_type': 'tv',
                    'season': 1, 'episode': 28, 'end_episode': 28, 'tmdb_id': None,
                    'identity_source': 'ancestor_directory'}


def test_show_name_with_parenthesised_episode_number():
    hint = parse_name('法证先锋三 (5).mkv', folder='法证先锋三 (2023)')
    assert hint['query'] == '法证先锋三' and hint['year'] == 2023
    assert hint['media_type'] == 'tv' and hint['episode'] == 5


def test_season_comes_from_the_folder_when_present():
    hint = parse_name('5.mkv', folder='某剧 S02')
    assert hint['season'] == 2 and hint['episode'] == 5


def test_without_folder_the_old_behaviour_is_unchanged():
    hint = parse_name('28.mkv')
    assert hint['query'] == '28' and hint['media_type'] == 'movie'


def test_explicit_season_episode_still_wins_over_the_folder():
    hint = parse_name('某剧.S01E03.1080p.mkv', folder='另一个剧')
    assert hint['media_type'] == 'tv' and hint['season'] == 1 and hint['episode'] == 3


def test_explicit_tmdb_identity_is_not_overridden_by_the_folder():
    hint = parse_name('某片 (2024) {tmdb-123}.mkv', folder='法证先锋三')
    assert hint['tmdb_id'] == 123 and hint['media_type'] == 'movie'


def test_ambiguous_names_still_fall_through_to_review():
    # 目录名不像剧名（纯年份）时不做推断，继续保持原来的判定
    hint = parse_name('2160p.mkv', folder='2023')
    assert hint['media_type'] == 'movie'
