"""Browser regressions against the built UI and deterministic local API fixtures."""
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


class StaticFiles(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def completed(id):
    return {'id': id, 'kind': 'strm_sync', 'state': 'completed', 'updated': 1, 'counts': {}}


def compared(tmdb_id):
    return {'tmdb_id': tmdb_id, 'tmdb_complete': True, 'inventory_complete': True,
            'series_status': 'Ended', 'combined_missing': 0, 'combined_extra': 0,
            'seasons': [{'season': 1, 'expected': [1]}], 'failed_seasons': []}


def exercise(browser, address):
    state = {'log_head': 200, 'settings_body': None}
    requests = []
    inventory_batches, held_batches, inventory_retries = [], [], []
    errors = []
    page = browser.new_page(viewport={'width': 1360, 'height': 950})
    page.on('pageerror', lambda error: errors.append(str(error)))

    def fixture(route):
        parsed = urlparse(route.request.url)
        endpoint = parsed.path.removeprefix('/api/v1')
        query = parse_qs(parsed.query)
        if endpoint == '/auth/me':
            data = {'username': 'demo', 'csrf': 'test'}
        elif endpoint == '/console/config':
            data = {'config': {'theme': 'dark'}}
        elif endpoint == '/settings':
            if route.request.method == 'PUT':
                state['settings_body'] = route.request.post_data_json
            data = {'tmdb_key_configured': True, 'emby_url': 'http://old.local:8096',
                    'playback_base': 'http://old.local:9000', 'cloud_throttle': {}}
        elif endpoint in ('/tasks/events', '/logs/events'):
            route.abort()
            return
        elif endpoint == '/tasks':
            older = 'before' in query
            data = {'active': [], 'history': [completed(37)] if older else [completed(80), completed(79)],
                    'has_more': not older}
        elif endpoint == '/offline/submissions/attention':
            data = {key: {'total': 0, 'items': []} for key in ('active', 'review')}
        elif endpoint == '/logs':
            head = state['log_head']
            if 'after' in query:
                start = int(query['after'][0]) + 1
                ids = range(start, min(start + 200, head + 1))
            else:
                ids = range(head, max(0, head - 200), -1)
            data = {'items': [{'id': id, 'at': 1, 'kind': 'system', 'message': f'回归事件 {id}'} for id in ids]}
        elif endpoint == '/inventory/mapping/page':
            data = {'rows': [{'tmdb_id': id, 'kind': 'tv', 'title': f'测试剧集 {id}', 'status': 'pending',
                              'cloud_files': 1, 'share_files': 0} for id in range(1001, 1009)], 'total': 8}
        elif endpoint == '/inventory/tv/missing/batch':
            ids = route.request.post_data_json['tmdb_ids']
            inventory_batches.append(ids)
            if 1001 in ids:
                held_batches.append(route)
                return
            data = {'items': {str(id): compared(id) for id in ids if id != 1006},
                    'errors': {'1006': 'TMDB 请求超时'}}
        elif endpoint == '/inventory/tv/1006/missing':
            inventory_retries.append(endpoint)
            data = compared(1006)
        elif endpoint == '/explore/genres':
            data = {'genres': [{'id': 18, 'name': '剧情'}, {'id': 28, 'name': '动作'}]}
        elif endpoint == '/explore/list':
            requests.append(query)
            count = int(query['per_page'][0])
            data = {'items': [{'id': id, 'title': f'作品 {id}', 'kind': 'movie', 'poster': '', 'year': '2026',
                               'badge': '未入库'} for id in range(count)], 'page': 1,
                    'total_pages': 5, 'has_more': True, 'cursor': 'a' * 32}
        else:
            data = {'items': [], 'config': {}}
        route.fulfill(json=data)

    page.route('**/api/v1/**', fixture)
    page.route('**/health/ready', lambda route: route.fulfill(json={'version': 'test'}))
    page.goto(address + 'settings')
    page.locator('section').filter(has=page.get_by_role('heading', name='TMDB', exact=True)).locator(
        'input[type=password]').fill('fixture-key')
    page.get_by_role('button', name='保存服务连接', exact=True).click()
    expect(page.get_by_text('TMDB Key 已保存')).to_be_visible()
    assert state['settings_body'] == {'tmdb_key': 'fixture-key'}

    page.goto(address + 'tasks')
    page.get_by_role('button', name='更早的任务').click()
    loaded = page.locator('.job-card').filter(has_text='#37')
    expect(loaded).to_have_count(1)
    with page.expect_response(lambda response: response.url.endswith('/api/v1/tasks'), timeout=9000):
        pass
    page.wait_for_load_state('networkidle')
    expect(loaded).to_have_count(1)
    expect(page.get_by_role('button', name='更早的任务')).to_have_count(0)

    page.goto(address + 'logs')
    expect(page.get_by_text('已载入 200 条')).to_be_visible()
    state['log_head'] = 500
    page.get_by_role('button', name='刷新', exact=True).click()
    expect(page.get_by_text('已载入 500 条')).to_be_visible()
    page.get_by_role('textbox', name='搜索日志').fill('回归事件 250')
    expect(page.locator('.log-body').get_by_text('回归事件 250', exact=True)).to_be_visible()

    page.clock.set_fixed_time(datetime(2026, 11, 20, tzinfo=timezone.utc))
    page.goto(address + 'explore')
    expect(page.locator('.explore-card')).to_have_count(45)
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(page.locator('.explore-card')).to_have_count(30)
    expect(page.locator('.narrow-filter').get_by_text('全部类型')).to_be_visible()
    type_select = page.locator('.narrow-filter .el-select__wrapper').first
    type_select.click()
    page.get_by_role('option', name='剧情').click()
    page.get_by_role('option', name='动作').click()
    page.get_by_role('combobox', name='影视类型').press('Escape')
    expect(page.locator('.selected-filters')).to_contain_text('剧情、动作')
    expect(page.locator('.results-header [role=status]')).to_contain_text('30 部')
    assert set(requests[-1]['genre']) == {'18', '28'}
    expect(page.get_by_role('combobox', name='上映或首播年份')).to_have_attribute('readonly', '')
    page.locator('.year-filter .el-select__wrapper').click()
    expect(page.get_by_role('option', name='2026', exact=True)).to_be_visible()
    expect(page.get_by_role('option', name='2027', exact=True)).to_have_count(0)

    page.goto(address + 'inventory')
    cards = page.locator('.poster-card')
    slow = cards.filter(has_text='测试剧集 1001')
    fast = cards.filter(has_text='测试剧集 1005')
    failed = cards.filter(has_text='测试剧集 1006')
    expect(fast.get_by_text('✓ 完整')).to_be_visible()
    expect(slow.get_by_text('对照中')).to_be_visible()
    expect(failed.get_by_text('对照失败')).to_be_visible()
    failed.get_by_role('button', name='重新对照 测试剧集 1006').click()
    expect(failed.get_by_text('✓ 完整')).to_be_visible()
    expect(slow.get_by_text('对照中')).to_be_visible()
    assert sorted(inventory_batches) == [[1001, 1002, 1003, 1004], [1005, 1006, 1007, 1008]]
    assert inventory_retries == ['/inventory/tv/1006/missing']
    held_batches[0].fulfill(json={'items': {str(id): compared(id) for id in range(1001, 1005)}, 'errors': {}})
    expect(slow.get_by_text('✓ 完整')).to_be_visible()
    assert not errors, errors
    page.close()


if __name__ == '__main__':
    dist = Path(__file__).resolve().parents[1] / 'dist'
    assert (dist / 'index.html').is_file(), 'Build the frontend before running browser tests'
    with ThreadingHTTPServer(('127.0.0.1', 0), partial(StaticFiles, directory=str(dist))) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                exercise(browser, f'http://127.0.0.1:{server.server_port}/#/')
                browser.close()
        finally:
            server.shutdown()
