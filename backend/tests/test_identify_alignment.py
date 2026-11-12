"""识别评分对齐：阈值按标题长度分档、精确命中 1.55、TV 年份硬校验、电影补上一年。"""
import asyncio

import pytest

from strimkeep.media import NeedsReview, match_threshold, scored_candidates
from strimkeep.organizer import Organizer


@pytest.mark.parametrize('query,expected', [
    ('火凤', 0.95),
    ('火凤凰', 0.88),
    ('飞越官场', 0.78),
    ('陀枪师姐五', 0.62),
    ('陀枪师姐第五部', 0.50),
    ('陀枪师姐第五部续', 0.50),
    ('Inception', 0.50),
    ('Some Unknown Work', 0.50),
])
def test_title_threshold_by_length(query, expected):
    assert match_threshold({'query': query}) == expected


def test_exact_title_scores_one_point_five_five():
    scored = scored_candidates({'query': '让子弹飞'}, [{'id': 1, 'title': '让子弹飞'}])
    assert scored[0][0] == pytest.approx(1.55)


def test_partial_title_keeps_similarity_scale():
    score = scored_candidates({'query': '火凤凰'}, [{'id': 1, 'title': '欲火凤凰'}])[0][0]
    assert 0 < score < 1


class FakeTMDB:
    def __init__(self, search=None, details=None):
        self.search = search or {}
        self.details = details or {}
        self.calls = []

    async def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path.startswith('search/'):
            key = path.split('/')[1]
            year = str((params or {}).get('year') or '')
            table = self.search.get(key, {})
            if isinstance(table, dict):
                return {'results': table.get(year, [])}
            return {'results': table}
        if path.endswith('/alternative_titles'):
            return {'results': []}
        ident = int(path.split('/')[1])
        return self.details.get(ident, {'id': ident})


def engine_with(fake):
    engine = Organizer.__new__(Organizer)
    engine.tmdb = fake
    return engine


def tv_hint(**over):
    hint = {'query': 'Twin', 'year': None, 'media_type': 'tv', 'season': 1, 'episode': 1,
            'end_episode': 1, 'tmdb_id': None, 'identity_source': 'filename'}
    hint.update(over)
    return hint


def test_tv_without_year_picks_highest_rank():
    """无年份时按「标题×100 + 热度」排序取最高，不要求唯一。"""
    async def run():
        fake = FakeTMDB(
            search={'tv': [
                {'id': 1, 'name': 'Twin', 'first_air_date': '2024-01-01', 'popularity': 10},
                {'id': 2, 'name': 'Twin', 'first_air_date': '2024-06-01', 'popularity': 99},
            ]},
            details={1: {'id': 1, 'first_air_date': '2024-01-01', 'seasons': []},
                     2: {'id': 2, 'first_air_date': '2024-06-01', 'seasons': []}},
        )
        details = await engine_with(fake).identify(tv_hint())
        assert details['id'] == 2
    asyncio.run(run())


def test_movie_retries_previous_release_year():
    """对电影会再试「上映年前一年」，跨年首映的片子靠这一步命中。"""
    async def run():
        item = {'id': 5, 'title': 'A Film', 'release_date': '2023-12-31', 'popularity': 1}
        fake = FakeTMDB(search={'movie': {'2024': [], '2023': [item]}},
                        details={5: {'id': 5, 'title': 'A Film', 'release_date': '2023-12-31'}})
        hint = {'query': 'A Film', 'year': 2024, 'media_type': 'movie', 'tmdb_id': None}
        details = await engine_with(fake).identify(hint)
        assert details['id'] == 5
        years = [str(p.get('year')) for path, p in fake.calls if path == 'search/movie']
        assert years[:2] == ['2024', '2023']
    asyncio.run(run())
