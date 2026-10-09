# -*- coding: utf-8 -*-
"""
Emby 路径映射测试（EmbyPathMap / emby_lib_of / emby_path_to_container）。

以前本地/分享库的判断写死成作者自己的 '/strm/115网盘/影视媒体库'、'/strm/115网盘/分享影视库'，
别的用户的 Emby 路径一律归到「其它库」，单剧删除也直接失效。现在改成读配置
emby_local_path / emby_share_path，这里覆盖：
  - 默认值下作者的目录结构分类不变
  - 自定义根目录、末尾 / 与反斜杠归一化
  - 末级目录名兜底只用于展示分类，删除映射必须严格前缀匹配
  - '..' 路径拒绝、对不上的路径返回 '' / None
  - reload_config() 能拿到新配置（config.json 与环境变量两种来源）

运行方式同其它测试：`python -m pytest dev/tests` 或 `pytest dev/tests/ -v`。
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='strimkeep_test_emby_paths_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''
os.environ['INGEST_QUIET_MINUTES'] = '0'

sys.path.insert(0, str(Path(__file__).parents[2] / 'app'))
import engine  # noqa: E402

DEF_L = '/strm/115网盘/影视媒体库'
DEF_S = '/strm/115网盘/分享影视库'


def _default_map():
    return engine.EmbyPathMap(DEF_L, DEF_S)


class TestDefaultPaths:
    def test_config_defaults_keep_author_layout(self):
        # 默认值必须保持作者原来的目录，老用户升级后行为不变
        if not os.environ.get('EMBY_LOCAL_PATH'):
            assert engine._cfg.DEFAULTS['emby_local_path'] == DEF_L
        if not os.environ.get('EMBY_SHARE_PATH'):
            assert engine._cfg.DEFAULTS['emby_share_path'] == DEF_S
        assert engine._cfg.ENV_OVERRIDE_KEYS['emby_local_path'] == 'EMBY_LOCAL_PATH'
        assert engine._cfg.ENV_OVERRIDE_KEYS['emby_share_path'] == 'EMBY_SHARE_PATH'

    def test_author_paths_classified(self):
        m = _default_map()
        assert m.lib_of(DEF_L + '/剧集/国产剧集/繁花 (2023)/Season 1/繁花 S01E01.strm') == 'local'
        assert m.lib_of(DEF_S + '/电影/沙丘 (2021)/沙丘.strm') == 'share'
        assert m.lib_of(DEF_L) == 'local'

    def test_author_paths_to_container(self):
        m = _default_map()
        assert m.to_container(DEF_L + '/剧集/A/Season 1/A S01E01.strm') == engine.L_ROOT / '剧集/A/Season 1/A S01E01.strm'
        assert m.to_container(DEF_S + '/电影/B/B.strm') == engine.S_ROOT / '电影/B/B.strm'

    def test_module_helpers_use_runtime_map(self):
        old = engine.EMBY_PATHS
        try:
            engine.EMBY_PATHS = _default_map()
            assert engine.emby_lib_of(DEF_L + '/x/y.strm') == 'local'
            assert engine._src(DEF_L + '/x/y.strm') == '本地影视库'
            assert engine._src(DEF_S + '/x/y.strm') == '分享影视库'
            assert engine._src('/other/x/y.strm') == '其它库'
            assert engine.emby_path_to_container(DEF_S + '/x/y.strm') == engine.S_ROOT / 'x/y.strm'
            assert engine.emby_path_to_container('') is None
            assert engine.emby_path_to_container(None) is None
        finally:
            engine.EMBY_PATHS = old


class TestCustomPaths:
    def test_custom_roots(self):
        m = engine.EmbyPathMap('/mnt/strm/movies-local', '/mnt/strm/movies-share')
        assert m.lib_of('/mnt/strm/movies-local/A/a.strm') == 'local'
        assert m.lib_of('/mnt/strm/movies-share/B/b.strm') == 'share'
        assert m.to_container('/mnt/strm/movies-local/A/a.strm') == engine.L_ROOT / 'A/a.strm'
        assert m.to_container('/mnt/strm/movies-share/B/b.strm') == engine.S_ROOT / 'B/b.strm'
        # 作者的默认路径在自定义配置下不再匹配
        assert m.lib_of(DEF_L + '/A/a.strm') == ''
        assert m.to_container(DEF_L + '/A/a.strm') is None

    def test_component_boundary(self):
        # /media/tv 不能把 /media/tv2 也吃进去
        m = engine.EmbyPathMap('/media/tv', '/media/share')
        assert m.lib_of('/media/tv2/A/a.strm') == ''
        assert m.to_container('/media/tv2/A/a.strm') is None
        assert m.lib_of('/media/tv/A/a.strm') == 'local'

    def test_trailing_slash_and_backslash_normalized(self):
        m = engine.EmbyPathMap('/mnt/local/', 'D:\\strm\\share\\')
        assert m.local == '/mnt/local'
        assert m.share == 'D:/strm/share'
        assert m.lib_of('/mnt/local/A/a.strm') == 'local'
        assert m.lib_of('D:\\strm\\share\\B\\b.strm') == 'share'
        assert m.to_container('D:\\strm\\share\\B\\b.strm') == engine.S_ROOT / 'B/b.strm'

    def test_nested_roots_prefer_longer(self):
        m = engine.EmbyPathMap('/strm', '/strm/share')
        assert m.lib_of('/strm/share/A/a.strm') == 'share'
        assert m.lib_of('/strm/movies/A/a.strm') == 'local'
        assert m.to_container('/strm/share/A/a.strm') == engine.S_ROOT / 'A/a.strm'

    def test_empty_config_ignored(self):
        m = engine.EmbyPathMap('', '  ')
        assert m.lib_of('/strm/A/a.strm') == ''
        assert m.lib_of('/') == ''
        assert m.to_container('/strm/A/a.strm') is None
        m2 = engine.EmbyPathMap(None, '/mnt/share')
        assert m2.lib_of('/mnt/share/A/a.strm') == 'share'
        assert m2.lib_of('/anything/a.strm') == ''


class TestBasenameFallback:
    def test_fallback_for_display_only(self):
        m = _default_map()
        # Emby 挂载点和配置不一致，但目录名还是「影视媒体库」——展示分类照旧，删除不能跟着猜
        p_l = '/mnt/emby/影视媒体库/剧集/A/Season 1/A S01E01.strm'
        p_s = '/volume1/分享影视库/电影/B/B.strm'
        assert m.lib_of(p_l) == 'local'
        assert m.lib_of(p_s) == 'share'
        assert m.to_container(p_l) is None
        assert m.to_container(p_s) is None

    def test_fallback_is_substring_like_before(self):
        """展示兜底与旧版 '影视媒体库' in path 一致（子串）；删除映射不走兜底"""
        m = _default_map()
        assert m.lib_of('/strm/115网盘/影视媒体库-4K/A/a.strm') == 'local'
        assert m.lib_of('/mnt/本地影视媒体库/A/a.strm') == 'local'
        assert m.to_container('/mnt/本地影视媒体库/A/a.strm') is None
        assert m.lib_of('/mnt/影视媒体库/A/a.strm', fallback=False) == ''

    def test_no_fallback_when_basenames_equal(self):
        m = engine.EmbyPathMap('/a/media', '/b/media')
        assert m.lib_of('/c/media/x.strm') == ''
        assert m.lib_of('/a/media/x.strm') == 'local'
        assert m.lib_of('/b/media/x.strm') == 'share'


class TestUnsafePaths:
    def test_dotdot_rejected(self):
        m = _default_map()
        assert m.to_container(DEF_L + '/../分享影视库/A/a.strm') is None
        assert m.to_container(DEF_L + '/A/../../etc/passwd') is None
        assert m.to_container(DEF_S + '/..') is None

    def test_root_itself_not_mapped(self):
        # 根目录本身不能映射成 L_ROOT / S_ROOT，否则一删就是整个库
        m = _default_map()
        assert m.to_container(DEF_L) is None
        assert m.to_container(DEF_L + '/') is None
        assert m.to_container(DEF_S + '//') is None

    def test_double_slash_stays_inside_root(self):
        m = _default_map()
        got = m.to_container(DEF_L + '//etc/passwd')
        assert got == engine.L_ROOT / 'etc/passwd'
        assert engine._inside(got, engine.L_ROOT)

    def test_non_matching(self):
        m = _default_map()
        assert m.lib_of('/somewhere/else/a.strm') == ''
        assert m.lib_of('') == ''
        assert m.to_container('/somewhere/else/a.strm') is None
        assert m.to_container('') is None


class TestReloadConfig:
    def test_reload_from_config_file(self):
        cfg_mod = engine._cfg
        orig = cfg_mod.load_config()
        saved_env = {k: os.environ.pop(k, None) for k in ('EMBY_LOCAL_PATH', 'EMBY_SHARE_PATH')}
        try:
            new = dict(orig)
            new['emby_local_path'] = '/cfg/local/'
            new['emby_share_path'] = '/cfg/share'
            cfg_mod.save_config(new)
            engine.reload_config()
            assert engine.EMBY_PATHS.local == '/cfg/local'
            assert engine.emby_lib_of('/cfg/local/A/a.strm') == 'local'
            assert engine.emby_lib_of('/cfg/share/B/b.strm') == 'share'
            assert engine.emby_path_to_container('/cfg/share/B/b.strm') == engine.S_ROOT / 'B/b.strm'
            assert engine.emby_path_to_container(DEF_L + '/A/a.strm') is None
        finally:
            for k, v in saved_env.items():
                if v is not None:
                    os.environ[k] = v
            cfg_mod.save_config(orig)
            engine.reload_config()

    def test_reload_from_env_override(self):
        cfg_mod = engine._cfg
        saved_env = {k: os.environ.get(k) for k in ('EMBY_LOCAL_PATH', 'EMBY_SHARE_PATH')}
        try:
            os.environ['EMBY_LOCAL_PATH'] = '/env/local'
            os.environ['EMBY_SHARE_PATH'] = '/env/share'
            engine.reload_config()
            assert cfg_mod.load_config()['emby_local_path'] == '/env/local'
            assert engine.emby_lib_of('/env/local/A/a.strm') == 'local'
            assert engine.emby_path_to_container('/env/share/B/b.strm') == engine.S_ROOT / 'B/b.strm'
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            engine.reload_config()


class TestEmptyFallsBackToDefault:
    def test_cleared_setting_uses_default_roots(self):
        """设置页清空路径后按默认值生效（输入框占位符显示的就是默认值），而不是什么都匹配不上"""
        m = engine.emby_path_map('', None)
        assert m.local == DEF_L and m.share == DEF_S
        m2 = engine.emby_path_map(' /mnt/l/ ', '')
        assert m2.lib_of('/mnt/l/A/a.strm') == 'local'
        assert m2.share == DEF_S
