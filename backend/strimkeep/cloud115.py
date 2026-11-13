"""Independent playback access; background 115 operations remain serialized."""
import asyncio
import functools
import io
import hashlib
import json
import random
import secrets
import time
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx
import qrcode
from httpx_request import request as sdk_request
from p115client import P115Client, check_response
from p115client.exception import P115AuthenticationError, P115Error, P115FileNotFoundError
from p115client.tool.attr import normalize_attr, update_resp_ancestors
from p115client.util import unescape_115_charref

try:  # pickcode -> file_id 的本地换算，纯计算不发请求（p115client 公开能力）
    from p115client import to_id
except ImportError:  # 老版本 SDK 只在类上暴露这个静态方法
    to_id = P115Client.to_id

from .login_clients import CLIENT_LABELS

playback_request = ContextVar('115_playback_request', default=False)

# Emby/Plex/Jellyfin 的探测与转码会在一轮扫描里反复请求同一集，
# 这类 UA 给更长的直链 TTL；真实播放仍用短 TTL，撤销能尽快生效。
_PROBE_UA_MARKERS = ('lavf/', 'ffprobe', 'ffmpeg', 'emby', 'plex', 'jellyfin')


def _probe_user_agent(value: str) -> bool:
    text = str(value or '').casefold()
    return any(marker in text for marker in _PROBE_UA_MARKERS)


class CloudError(Exception):
    def __init__(self, message: str, code: str = 'CLOUD_UNAVAILABLE', status: int = 502):
        super().__init__(message)
        self.code, self.status = code, status


# 任务级信号：登录失效与限流必须上抛。下面那些「先吞掉错误、再按目录核对结果」的地方
# 一旦把它们也吞了，真实原因会被核对失败盖成「结果不一致」，还会继续对着 115 撞墙。
FATAL_CODES = ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED')


def fatal(exc):
    return isinstance(exc, CloudError) and exc.code in FATAL_CODES


# 错误分类（B01/B08）：
# - LOGIN_REQUIRED / LOGIN_INVALID：登录未配置 / 失效，等待恢复，不产生删除。
# - FILE_NOT_FOUND：仅在文件语义接口上成立，表示「这个自有文件已不存在」。
# - SHARE_NOT_FOUND：仅在分享语义接口上成立，表示「分享或其内容已失效」。
# - OBJECT_NOT_FOUND：其他接口出现「不存在/已删除」文字时的保守归类——
#   「接口不存在」「分享不存在」等不能被当成自有文件已删除。
# - CLOUD_REJECTED：明确的业务拒绝（含限流），请求发生了但被拒绝。
# - CLOUD_UNAVAILABLE：请求没有成功返回（超时/网络），操作是否发生未知。
# - SCHEMA_CHANGED / FILE_CHANGED / INCOMPLETE_LIST 等：结果不明，按未知处理。
# 115 没有公开的频率上限，但社区共识很一致：读接口 1 QPS 量级、写接口（移动/重命名/
# 删除）3 秒起步，超了会进风控冷却（30 分钟到 2 小时不等）。这里只认「明确的限流文字」，
# 不把普通业务拒绝（如「错误的链接」）误判成限流。
RATE_LIMIT_HINTS = ('频繁', '过快', '操作太', '稍后再', '请求过多', 'too many', 'rate limit', 'rate-limit')

# 115 对「同一条分享重复转存到同一目录」的拒绝文案。它不是网络故障，也不是限流。
ALREADY_SAVED_HINTS = ('已转存', '已保存', '已接收', '重复转存', '重复保存')


def classify_rejection(errno, error_msg: str, scope: str = 'generic') -> str:
    """把 115 state:false 响应映射为稳定错误码。文字只作辅助，范围决定语义。"""
    if errno == 99 or '重新登录' in error_msg:
        return 'LOGIN_INVALID'
    if scope == 'share' and any(hint in error_msg for hint in ALREADY_SAVED_HINTS):
        return 'ALREADY_SAVED'
    lowered = error_msg.lower()
    if any(hint in lowered for hint in RATE_LIMIT_HINTS):
        return 'RATE_LIMITED'
    if errno == 20018 or '不存在' in error_msg or '已删除' in error_msg:
        return {'file': 'FILE_NOT_FOUND', 'share': 'SHARE_NOT_FOUND'}.get(scope, 'OBJECT_NOT_FOUND')
    return 'CLOUD_REJECTED'


_NOT_FOUND_STATUS = {'FILE_NOT_FOUND': 404, 'SHARE_NOT_FOUND': 404, 'OBJECT_NOT_FOUND': 409}


class Cloud115:
    # 请求节奏可以按账号调：读慢一点只是慢，写太密会进风控。
    # burst/pause_ms 仅兼容旧配置；固定次数暂停已移除，不参与请求节奏。
    THROTTLE_DEFAULTS = {'read_ms': 350, 'write_ms': 3000, 'burst': 15, 'pause_ms': 30000}
    THROTTLE_LIMITS = {'read_ms': (0, 5000), 'write_ms': (0, 15000), 'burst': (1, 200), 'pause_ms': (0, 600000)}
    COOLDOWN_SECONDS = (60, 300)
    # 405 是阿里云 WAF 按时间窗的封锁，不是逐请求限流：阶梯冷却对它无效，
    # 每次重试只会把窗口往后推。命中后整段时间一个请求都不发。
    WAF_SECONDS = 1800
    # 批量写（一次调用覆盖 N 个文件）之后只留短间隔；单文件写仍按 write_ms。
    BATCH_GAP = 1.0
    # 847 目录 / 16185 文件 = 1 次目录表 + 4 次文件表 + 4 次批量详情，file_id 由 pickcode 本地换算。
    # 这两页大小就是 115 的硬上限（per_page>5000 不再生效，file_id 批量同理）。
    DOWNLOAD_PAGE = 5000
    META_BATCH = 5000
    # 防御性上限：单棵子树最多翻 400 页（≈200 万文件），防止接口异常时死循环。
    MAX_DOWNLOAD_PAGES = 400
    # 115 的 proapi 浏览器端接口必须带 app_ver，缺了直接返回 405（不是限流，是参数校验）。
    APP_VER = '38.0.2'
    # App 通道（android/2.0/ufile/files）与网页接口是两套配额：网页接口被 405 挡住时，
    # 期内直接走 App 通道，不再拿网页接口试错。
    ANDROID_LIST = 'https://proapi.115.com/android/2.0/ufile/files'
    WEBAPI_PROBE_SECONDS = 600
    # 播放取链的并发上限。115 惩罚的是「无间隔轰炸」，间隔由 PLAY_SPACING 把住；
    # 4 路刚好覆盖播放器开播的并发突发（探测、正片、断线重连、下一集预取）。
    PLAY_CONCURRENCY = 4
    PLAY_SPACING = .05
    # 直链缓存的有效期。0.9.59 之前是写死的 15 秒（探测 UA 60 秒），可 115 返回的
    # 直链里 `t` 就是过期时间戳，现场实测有效期 92~297 小时——等于把一个能用四天的
    # 地址每 15 秒重取一次，既慢又白烧配额。现在按直链自己的有效期算：
    # 留 5 分钟安全边距，读不到 `t` 时退回原来的 15 秒。
    PLAY_LINK_TTL_MARGIN = 300.0
    PLAY_LINK_TTL_FLOOR = 15.0
    PLAY_LINK_TTL_CAP = 6 * 3600.0
    # 每条缓存只是一个 URL 字符串，容量给足，别为了省内存把热条目挤掉。
    PLAY_LINK_CACHE = 2048
    SHARE_PLAY_LINK_TTL_CAP = 60.0

    def __init__(self, store):
        self.store = store
        self.gate = asyncio.Lock()
        # 播放取链不能用全局互斥：播放器开播会同时发几个请求，以前一把 Lock 把间隔和
        # 请求一起扣住，实测 6 个并发要 1.37 秒（≈ 6 ×（50ms 间隔 + 单次往返）），
        # 开播就卡在这一段。改成有限并发后请求可以同时飞，发车时刻仍由 PLAY_SPACING 把住。
        self.play_gate = asyncio.Semaphore(self.PLAY_CONCURRENCY)
        self.play_timetable = asyncio.Lock()
        # 播放取链要快：连不上 5 秒放弃、读 10 秒；池排队 5 秒。
        self.play_http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
            follow_redirects=True, trust_env=False)
        self.play_next_request = 0.0
        self.play_urls = {}
        # 同 (账号, pickcode) 的并发取链合并用
        self.play_locks = {}
        # 直链缓存命中计数（/metrics 里是 strimkeep_play_cache）。
        self.play_hits = 0
        self.play_misses = 0
        # 常规 115 API：连不上 5 秒、读/写 15 秒、池排队 5 秒。
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0),
            follow_redirects=True, trust_env=False)
        self.qr = {}
        self.qr_revision = 0
        self.next_request = 0.0
        self.clock = time.monotonic
        self.sleep = asyncio.sleep
        self.throttle_scale = 1.0     # 限流后 >1，逐渐回落到 1
        self.limit_streak = 0
        self.success_streak = 0
        self.rate_limit_until = 0.0
        self.waf_until = 0.0
        self.webapi_until = 0.0   # 网页接口 405 的窗口；期内目录读取改走 App 通道
        self._throttle_cache = None
        self._throttle_at = 0.0
        # 目录清单缓存与请求账本都按「115 账号 + 任务」隔离：三条泳道（整理/转存/
        # 同步）并发跑，账号也可能切换，混在一份进程级缓存里会互相踩。
        self._scope_context = ContextVar('cloud115_scope_' + str(id(self)), default=('', ''))
        self._children_memo = {}
        # 文件详情缓存：(账号, 文件 ID) → (读取时间, 详情)。
        # 洗版准备会对「库内已存在的每个版本」逐个复核，同一任务里反复读同一文件只会
        # 拿到同一份事实；我们自己的改名/移动/回收会主动失效，另有 TTL 兜底外部改动。
        self._file_memo = {}
        self._ledgers = {}
        # 回收站快照：一份「刚核对过的清单」可当下一个文件的回收前状态。
        self._recycle_snapshot = None
        self._recycle_last_total = None
        # 0.8.22：把上次进程留下的冷却/封锁状态接回来（以前只在内存里，重启即清零，
        # 于是重启后第一次请求立刻撞上还没结束的封锁窗口）。
        self._restore_runtime()

    def _restore_runtime(self):
        """从 meta 恢复限流状态：用**墙钟**存剩余秒数，重启后换算成 monotonic 截止点。

        monotonic 不跨进程，所以持久化的是 wall clock 时间戳；读取时减掉当前墙钟，
        再折算到本进程的 clock() 上。读到坏数据就忽略，绝不因此启动失败。
        """
        state = self.store.meta_json('cloud_runtime', {}) or {}
        try:
            cooldown = float(state.get('cooldown_until') or 0) - time.time()
            waf = float(state.get('waf_until') or 0) - time.time()
            scale = float(state.get('throttle_scale') or 1.0)
            streak = int(state.get('limit_streak') or 0)
        except (TypeError, ValueError):
            return
        if cooldown > 0:
            self.rate_limit_until = self.clock() + cooldown
        if waf > 0:
            self.waf_until = self.clock() + waf
        if scale > 1.0:
            self.throttle_scale = min(8.0, scale)
            self.limit_streak = max(0, streak)

    def _persist_runtime(self):
        """把当前限流状态写进 meta（只在真正变化时调用，不参与热路径）。"""
        now = self.clock()
        wall = time.time()
        self.store.set_meta('cloud_runtime', {
            'throttle_scale': round(self.throttle_scale, 3),
            'limit_streak': self.limit_streak,
            'cooldown_until': wall + max(0.0, self.rate_limit_until - now),
            'waf_until': wall + max(0.0, self.waf_until - now),
            'at': wall})

    def throttle(self):
        """读 store 里的 cloud_throttle，30 秒缓存一次，别让每个请求都查库。"""
        now = self.clock()
        if self._throttle_cache is not None and now - self._throttle_at < 30:
            return self._throttle_cache
        raw = self.store.get('cloud_throttle') or {}
        values = {}
        for key, default in self.THROTTLE_DEFAULTS.items():
            low, high = self.THROTTLE_LIMITS[key]
            try:
                number = int(raw.get(key, default))
            except (TypeError, ValueError):
                number = default
            values[key] = max(low, min(high, number))
        self._throttle_cache, self._throttle_at = values, now
        return values

    @staticmethod
    def _fresh_ledger():
        return {'read': 0, 'write': 0, 'blocked': 0, 'scope': {}, 'ops': {},
                'started': time.monotonic()}

    @property
    def ledger(self):
        """当前作用域的请求账本（只做计数，不影响任何行为）。"""
        return self._ledgers.setdefault(self._scope, self._fresh_ledger())

    @property
    def _scope(self):
        return self._scope_context.get()

    @_scope.setter
    def _scope(self, value):
        self._scope_context.set(value)

    def reset_ledger(self):
        self._ledgers[self._scope] = self._fresh_ledger()

    def use_scope(self, account=None, job=None):
        """切到某个「账号 + 任务」的缓存与账本作用域；同一作用域重复调用无副作用。"""
        key = (str(account or ''), str(job or ''))
        if key != self._scope:
            self._scope = key
        return key

    def scope_key(self, cid):
        return (self._scope[0], str(cid))

    def count_op(self, name, files=1):
        """按业务动作计数（改名/移动/删除/详情），与 HTTP 次数区分开。"""
        entry = self.ledger['ops'].setdefault(name, [0, 0])
        entry[0] += 1
        entry[1] += max(1, int(files))

    def ledger_summary(self):
        l = self.ledger
        ops = '、'.join(f'{k} {v[0]}次/{v[1]}个' for k, v in l['ops'].items()) or '无'
        scope = '、'.join(f'{k} {v}' for k, v in sorted(l['scope'].items())) or '无'
        text = (f"读 {l['read']} 次 · 写 {l['write']} 次（业务动作：{ops}）"
                f" · 请求分类：{scope}"
                f" · 合计 {l['read'] + l['write']} 次 · 限流 {l['blocked']} 次"
                f" · 耗时 {time.monotonic() - l['started']:.1f}s")
        return {'read': l['read'], 'write': l['write'], 'blocked': l['blocked'],
                'scope': dict(l['scope']),
                'ops': {k: v[0] for k, v in l['ops'].items()},
                'files': {k: v[1] for k, v in l['ops'].items()},
                'seconds': round(time.monotonic() - l['started'], 1), 'text': text}

    def interval(self, kind):
        settings = self.throttle()
        base = settings['write_ms'] if kind == 'write' else settings['read_ms']
        return base / 1000.0 * self.throttle_scale

    def check_waf(self):
        """WAF 封锁期：连播放取链也不发，否则播放器重连就是在给封锁续期。"""
        blocked = self.waf_until - self.clock()
        if blocked > 0:
            raise CloudError('115 网页接口处于风控封锁期，还需 %.0f 秒；封锁期内不会发起任何请求。'
                             % blocked, 'RATE_LIMITED', 503)

    def check_cooldown(self):
        """冷却期内直接失败，不排队：让任务体面停下，而不是继续撞墙。"""
        self.check_waf()
        remaining = self.rate_limit_until - self.clock()
        if remaining > 0:
            raise CloudError('115 正在限流，已冷却 %.0f 秒后再重试。' % remaining, 'RATE_LIMITED', 503)

    def penalize(self):
        """被限流：间隔翻倍（上限 8 倍），并按 60/120/240/300 秒冷却。"""
        self.ledger['blocked'] += 1
        self.limit_streak += 1
        self.throttle_scale = min(8.0, self.throttle_scale * 2)
        low, high = self.COOLDOWN_SECONDS
        backoff = min(high, low * (2 ** (self.limit_streak - 1)))
        # 0.8.22：±15% 抖动。否则同一时刻被限流的多个任务会以完全相同的节奏重试，
        # 冷却结束时再次同时撞上去（AWS《Exponential Backoff And Jitter》）。
        backoff = min(high, backoff * random.uniform(0.85, 1.15))
        self.rate_limit_until = max(self.rate_limit_until, self.clock() + backoff)
        self.store.event('cloud', '115 请求被限流：间隔提升到 %.2f 倍，冷却 %d 秒。' % (self.throttle_scale, backoff))
        self._persist_runtime()

    def block_waf(self, status):
        """405 命中：整段封锁。不猜「再试一次会不会好」——那正是封锁被续期的原因。"""
        self.ledger['blocked'] += 1
        self.waf_until = max(self.waf_until, self.clock() + self.WAF_SECONDS)
        self.store.event('cloud', '115 网页接口被 WAF 拦截（HTTP %d），%d 分钟内不再发起任何请求。'
                         % (status, self.WAF_SECONDS // 60))
        self._persist_runtime()

    def reward(self):
        """连续成功就把之前抬高的间隔慢慢收回来，不做断崖式恢复。"""
        self.success_streak += 1
        if self.success_streak >= 20:
            self.success_streak = 0
            self.limit_streak = 0
            self.throttle_scale = max(1.0, self.throttle_scale * 0.9)
            if self.throttle_scale <= 1.0:
                self._persist_runtime()

    async def close(self):
        await self.http.aclose()
        await self.play_http.aclose()
        self.play_urls.clear()

    def client(self):
        cookie = self.store.get('cookie')
        if not cookie:
            raise CloudError('请先扫码登录或保存 115 Cookie。', 'LOGIN_REQUIRED', 409)
        # Never construct a no-cookie client: that would launch SDK console login.
        return P115Client(cookie)

    async def sdk_request(self, url, *, cookies=None, headers=None, **kwargs):
        # httpx_request 0.1.9.1 builds its Request before applying `cookies`.
        # Put the SDK's domain/path-scoped jar in the outgoing header first.
        # Never depend on httpx's in-memory jar: a fresh process has no jar,
        # and a changed account must not inherit the previous account's cookies.
        outgoing = httpx.Headers(headers or {})
        if 'cookie' not in outgoing:
            probe = httpx.Request('GET', url)
            if cookies is not None:
                httpx.Cookies(cookies).set_cookie_header(probe)
            outgoing['Cookie'] = probe.headers.get('cookie', '')
        return await sdk_request(url, cookies=cookies, headers=outgoing, **kwargs)

    @staticmethod
    def link_expiry(url: str) -> float:
        """读直链里的 `t`（过期时间戳，秒）。读不到返回 0。"""
        for pair in urlsplit(url).query.split('&'):
            name, _, value = pair.partition('=')
            if name == 't' and value.isdigit():
                stamp = float(value)
                # 毫秒级也认，免得哪天换了单位就当没有。
                return stamp / 1000.0 if stamp > 1e11 else stamp
        return 0.0

    def link_ttl(self, url: str) -> float:
        """这条直链还能缓存多久：按它自己的有效期算，留安全边距再封顶。

        115 给的直链能用几天，缓存没必要跟着「播放器重连的一小会儿」走。
        封顶 6 小时是为了兼顾「换账号/文件被撤」这类我们看不见的失效。
        """
        expiry = self.link_expiry(url)
        if not expiry:
            return self.PLAY_LINK_TTL_FLOOR
        remaining = expiry - time.time() - self.PLAY_LINK_TTL_MARGIN
        return max(self.PLAY_LINK_TTL_FLOOR, min(self.PLAY_LINK_TTL_CAP, remaining))

    async def reserve_play_slot(self):
        """领一个播放取链的发车时刻，并把下一次推后 PLAY_SPACING。

        间隔必须是原子的：并发读同一个 play_next_request 会让 4 个请求同时出发，
        等于把限流关掉。这里只把「抢时刻」放进临界区，请求本身在锁外并发跑。
        """
        async with self.play_timetable:
            next_request = self.play_next_request
            self.play_next_request = self.clock() + self.PLAY_SPACING
            wait = next_request - self.clock()
        if wait > 0:
            await self.sleep(wait)

    async def call(self, method, *args, _scope: str = 'generic', _kind: str = 'read', _batch: int = 0,
                   _channel: str = '', _parents: tuple | None = None, _files: tuple = (), **kwargs):
        playing = playback_request.get()
        if _kind == 'write':
            if _files:
                self.forget_files(_files)
            # 写之前就清：万一响应丢失（结果未知），后续读也不能用旧清单。
            # 给了 _parents 就只清这几个目录：以前每次写都清整棵树，同一批整理里
            # 分类/地区/作品目录被反复重读（现场：36 个文件却 656 次目录读取）。
            if _parents is None:
                self._children_memo.clear()
            else:
                self.forget_children(_parents)
        if not playing:
            self.ledger['read' if _kind == 'read' else 'write'] += 1
            self.ledger['scope'][_scope] = self.ledger['scope'].get(_scope, 0) + 1
            self.check_cooldown()
        else:
            self.check_waf()
        gate, session = (self.play_gate, self.play_http) if playing else (self.gate, self.http)
        async with gate:
            if playing:
                # 发车时刻在这里预留，请求本身不再被全局锁串起来。
                await self.reserve_play_slot()
            else:
                # Cooldown may have been entered while this request waited.
                self.check_cooldown()
                await self.sleep(max(0.0, self.next_request - self.clock()))
            try:
                # SDK's parser contract is handled by its maintained httpx transport.
                async with asyncio.timeout(15 if playing else 30):
                    result = await method(*args, async_=True, request=self.sdk_request,
                                          session=session, timeout=10 if playing else 15, **kwargs)
                if isinstance(result, dict) and result.get('state') is False:
                    # 115 返回 state:false 时 error_msg 就是业务错误原文（如「错误的链接」），
                    # 提前取出来抛 CloudError，比让 check_response 抛一整坨 repr 干净。
                    error_msg = result.get('error_msg') or result.get('error') or ''
                    if error_msg:
                        code = classify_rejection(result.get('errno'), str(error_msg), _scope)
                        # 登录类错误必须保留 LOGIN_INVALID，下游「去重新登录」的引导
                        # 依赖这个码，不能折进通用 CLOUD_REJECTED。
                        raise CloudError(str(error_msg), code, _NOT_FOUND_STATUS.get(code, 422))
                value = check_response(result) if isinstance(result, dict) else result
                if not playing:
                    self.reward()
                return value
            except (asyncio.CancelledError, CloudError) as exc:
                if isinstance(exc, CloudError) and exc.code == 'RATE_LIMITED':
                    self.penalize()
                raise
            except P115AuthenticationError:
                raise CloudError('115 登录失效或需验证，请重新检查登录。', 'LOGIN_INVALID', 401) from None
            except P115FileNotFoundError:
                # SDK 对部分端点自己抛「文件不存在」；同样按调用场景决定语义，
                # 分享接口的「不存在」不能被当成自有文件已删除。
                code = {'file': 'FILE_NOT_FOUND', 'share': 'SHARE_NOT_FOUND'}.get(_scope, 'OBJECT_NOT_FOUND')
                raise CloudError('对象不存在或已删除。', code, _NOT_FOUND_STATUS[code]) from None
            except P115Error as exc:
                # 兜底：115 返回了 state:false 但没带 error_msg 的情况。
                raise CloudError(str(exc)[:200] or '115 返回了错误。', 'CLOUD_REJECTED', 422) from None
            except Exception as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                    raise CloudError('115 登录失效或请求被拒绝，请重新检查登录。', 'LOGIN_INVALID', 401) from None
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 405:
                    # 115 对 webapi 的超量读返回 405（阿里云 WAF 页），不是 429。
                    # 封锁按时间窗计算，阶梯冷却对它无效，所以整段停手。
                    status = exc.response.status_code
                    if _channel == 'webapi':
                        # 只标记这条路暂时不可用，交给调用方换通道，不冻结整个任务。
                        self.webapi_until = self.clock() + self.WEBAPI_PROBE_SECONDS
                        raise CloudError('115 网页接口被限流（HTTP 405），已改用 App 通道读取。',
                                         'WEBAPI_BLOCKED', 503) from None
                    self.block_waf(status)
                    raise CloudError('115 网页接口被 WAF 拦截（HTTP %d），已停止请求 %d 分钟；'
                                     '封锁按时间窗计算，重试会延长封锁，等它自然解开。'
                                     % (status, self.WAF_SECONDS // 60), 'RATE_LIMITED', 503) from None
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
                    self.penalize()
                    raise CloudError('115 请求被限流（HTTP %d），已进入冷却；稍后重试。'
                                     % exc.response.status_code, 'RATE_LIMITED', 503) from None
                raise CloudError('115 请求未完成，请检查登录、网络或接口限制后重试。') from None
            finally:
                if not playing:
                    if _batch:
                        # 一次调用已经覆盖 _batch 个文件，不必再按单文件 3 秒等。
                        self.next_request = self.clock() + self.BATCH_GAP
                    else:
                        self.next_request = self.clock() + self.interval(_kind)

    async def status(self):
        ok = await self.call(self.client().login_status)
        if not ok:
            raise CloudError('115 登录已失效，请重新登录。', 'LOGIN_INVALID', 401)
        return {'connected': True}

    async def account(self):
        """115 账号与容量（只读）：昵称、UID、VIP、已用/总容量。"""
        client = self.client()
        info = await self.call(client.user_info)
        space = await self.call(client.user_space_info)
        info = info.get('data') if isinstance(info, dict) else None
        space = space.get('data') if isinstance(space, dict) else None
        if not isinstance(info, dict) or not isinstance(space, dict):
            raise CloudError('115 账号信息格式发生变化。', 'SCHEMA_CHANGED')

        def number(value):
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

        total = (space.get('all_total') or {}).get('size')
        used = (space.get('all_use') or {}).get('size')
        return {'uid': str(info.get('user_id') or ''),
                'nickname': str(info.get('user_name') or ''),
                'vip_level': number(info.get('is_vip')),
                'avatar': str(info.get('user_face') or ''),
                'space_total': number(total), 'space_used': number(used),
                'updated': time.time()}

    async def offline_add(self, urls, cid=None):
        """提交离线任务：共用写入节流，但保留 proapi 自身的传输参数契约。

        只提交一次：调用超时、连接失败或取消时结果可能未知，交给上层核查，
        不能自动重发。proapi 不接受 call() 注入的传输参数，适配器仅消费这些参数。
        """
        from p115client.client import P115OpenClient
        payload = {'urls': '\n'.join(urls)}
        if cid:
            payload['wp_path_id'] = int(cid) if str(cid).isdigit() else cid

        async def submit(*, async_, request, session, timeout):
            result = await P115OpenClient.clouddownload_task_add_urls(self.client(), payload, async_=True)
            if isinstance(result, dict) and result.get('state') is False:
                message = result.get('message') or result.get('error_msg') or '提交失败'
                # 只将明确的登录/限流拒绝升级；其他离线业务拒绝保留原有语义。
                code = classify_rejection(result.get('errno'), str(message), 'offline')
                if code not in ('RATE_LIMITED', 'LOGIN_INVALID'):
                    code = 'CLOUD_REJECTED'
                raise CloudError(str(message), code, 422)
            return result

        # 离线任务永远是后台写，即使调用方继承了播放请求的 context。
        token = playback_request.set(False)
        try:
            return await self.call(submit, _scope='offline', _kind='write', _parents=())
        finally:
            playback_request.reset(token)

    async def offline_tasks(self, page=1, size=20):
        """云下载任务列表与运行中数量。"""
        client = self.client()
        rows = await self.call(client.clouddownload_task_list, {'page': page, 'page_size': size, 'stat': 0})
        try:
            running = await self.call(client.clouddownload_task_cnt, {'flag': 0})
        except CloudError:
            running = None
        return rows, running

    async def list_files(self, cid: int, offset: int, limit: int):
        """目录读取：网页接口优先，被 405 挡住时改走 App 通道。

        App 通道（android/2.0/ufile/files）与网页接口配额独立，字段更全
        （fid/fn/fs/sha1/pc/pid/fc），换过去连 sha1 都不用再补查。
        """
        if self.clock() < self.webapi_until:
            return await self.list_files_android(cid, offset, limit)
        try:
            return await self._list_files_web(cid, offset, limit)
        except CloudError as exc:
            if exc.code != 'WEBAPI_BLOCKED':
                raise
            return await self.list_files_android(cid, offset, limit)

    async def _list_files_web(self, cid: int, offset: int, limit: int):
        data = await self.call(self.client().fs_files, {
            'cid': cid, 'offset': offset, 'limit': limit, 'show_dir': 1,
            'cur': 1, 'count_folders': 1, 'record_open_time': 0, 'o': 'file_name', 'asc': 1},
            _channel='webapi')
        if not isinstance(data.get('data'), list):
            raise CloudError('115 未返回目录条目，稍后重试。', 'SCHEMA_CHANGED')
        try:
            total = max(0, int(data.get('count', 0)))
        except (TypeError, ValueError):
            total = 0  # Advisory count; actual rows still determine pagination.
        items = []
        for raw in data.get('data', []):
            try:
                a = normalize_attr(raw, simple=True)
                # Check actual child membership, rather than a stale breadcrumb
                # or count (which can exclude folders on some 115 responses).
                if str(a['parent_id']) != str(cid):
                    raise CloudError('115 返回了其他目录的条目，正在读取的目录暂不可用。', 'DIRECTORY_CHANGED', 409)
                items.append({'id': str(a['id']), 'name': a['name'], 'is_dir': a['is_dir'],
                              'size': a.get('size', 0), 'pickcode': a.get('pickcode', ''),
                              'sha1': a.get('sha1', '')})
            except (KeyError, ValueError, TypeError):
                raise CloudError('115 目录返回格式发生变化，已停止处理。', 'SCHEMA_CHANGED') from None
        # Read through the empty terminal page. Neither count nor echoed offset
        # is an enumeration boundary; both differ between 115 endpoints.
        raw = data.get('count')
        raw_count = int(raw) if (not isinstance(raw, bool) and isinstance(raw, (int, str))
                                 and str(raw).isdigit()) else None
        return {'items': items, 'total': max(total, offset + len(items)),
                'offset': offset, 'limit': limit, 'next_offset': offset + len(items),
                'has_more': bool(items), 'path': data.get('path', []), 'raw_count': raw_count}

    async def list_files_android(self, cid: int, offset: int, limit: int):
        """App 通道读取目录，返回结构与 list_files 一致。

        字段对照：fid=id、fn=name、fs=size、sha1=sha1、pc=pickcode、pid=parent_id，
        fc="0" 表示目录、其余表示文件；count 是子项总数（与网页接口的 count 同义）。
        """
        client = self.client()
        result = await self.call(client.request, self.ANDROID_LIST, 'GET',
                                 {'aid': 1, 'cid': str(cid), 'offset': offset,
                                  'limit': limit, 'show_dir': 1, 'app_ver': self.APP_VER},
                                 _scope='list')
        rows = result.get('data') if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise CloudError('115 未返回目录条目，稍后重试。', 'SCHEMA_CHANGED')
        items = []
        for raw in rows:
            if not isinstance(raw, dict):
                raise CloudError('115 目录返回格式发生变化，已停止处理。', 'SCHEMA_CHANGED')
            fid, name, pid = raw.get('fid'), raw.get('fn'), raw.get('pid')
            if fid is None or not isinstance(name, str) or pid is None:
                raise CloudError('115 目录返回格式发生变化，已停止处理。', 'SCHEMA_CHANGED')
            if str(pid) != str(cid):
                raise CloudError('115 返回了其他目录的条目，正在读取的目录暂不可用。', 'DIRECTORY_CHANGED', 409)
            try:
                size = int(raw.get('fs') or 0)
            except (TypeError, ValueError):
                size = 0
            items.append({'id': str(fid), 'name': name, 'is_dir': str(raw.get('fc')) == '0',
                          'size': size, 'pickcode': str(raw.get('pc') or ''),
                          'sha1': str(raw.get('sha1') or '')})
        raw_count = result.get('count')
        known = not isinstance(raw_count, bool) and isinstance(raw_count, (int, str)) and str(raw_count).isdigit()
        raw_count = int(raw_count) if known else None
        total = max(0, raw_count or 0)
        has_more = (offset + len(items) < total) if (known and items) else bool(items)
        return {'items': items, 'total': max(total, offset + len(items)),
                'offset': offset, 'limit': limit, 'next_offset': offset + len(items),
                'has_more': has_more, 'path': result.get('path', []), 'raw_count': raw_count}

    FILE_MEMO_SECONDS = 300

    async def cached_file(self, file_id):
        """带缓存的文件详情：只给「洗版候选复核」这类重复只读场景用。

        写入后的核对（organizer.change_file）必须走 file()：改名/移动刚发生，
        读缓存会看到旧名字。缓存按文件 ID 记账，我们自己的写会主动失效。
        """
        key = str(file_id)
        hit = self._file_memo.get(key)
        if hit and self.clock() - hit[0] < self.FILE_MEMO_SECONDS:
            return dict(hit[1])
        detail = await self.file(file_id)
        self._file_memo[key] = (self.clock(), dict(detail))
        return detail

    async def file(self, file_id: str):
        self.count_op('详情')
        client = self.client()
        data = await self.call(client.fs_file_skim, file_id, _scope='file')
        rows = data.get('data', [])
        if not isinstance(rows, list) or not rows:
            raise CloudError('源文件不存在或无法访问。', 'FILE_NOT_FOUND', 404)
        a = rows[0]
        if not isinstance(a, dict):
            raise CloudError('115 文件信息格式无效。', 'SCHEMA_CHANGED')
        if str(a.get('file_id')) != file_id or a.get('file_category') in (0, '0'):
            raise CloudError('源文件身份不匹配或选中了目录。', 'INVALID_FILE', 409)
        name = a.get('file_name', '')
        pc = a.get('pick_code') or a.get('pickcode')
        if not isinstance(name, str) or not name or not isinstance(pc, str) or not pc or 'file_size' not in a:
            raise CloudError('115 文件信息不完整，已停止处理。', 'SCHEMA_CHANGED')
        # fs_file_skim deliberately omits parent_id on normal responses. The SDK's
        # own get_attr(skim=True, ensure_parent_id=True) uses fs_supervision for it.
        # Use the actual pickcode rather than triggering SDK synchronous discovery.
        details = await self.call(client.fs_supervision, str(pc), _scope='file')
        info = details.get('data')
        if not isinstance(info, dict) or any(info.get(k) is None for k in
                ('file_id', 'file_name', 'parent_id', 'file_size', 'file_sha1', 'pick_code')):
            raise CloudError('115 文件详情缺少身份、大小或父目录信息，已停止修改。', 'SCHEMA_CHANGED')
        if any(not isinstance(info[k], str) for k in ('file_name', 'file_sha1', 'pick_code')):
            raise CloudError('115 文件详情字段格式无效，已停止修改。', 'SCHEMA_CHANGED')
        try:
            fid, parent, size = str(int(info['file_id'])), str(int(info['parent_id'])), int(info['file_size'])
            skim_size = int(a['file_size'])
        except (TypeError, ValueError, OverflowError):
            raise CloudError('115 文件详情字段格式无效，已停止修改。', 'SCHEMA_CHANGED') from None
        if not info['file_name'] or not info['file_sha1'] or not info['pick_code'] or size < 0 or int(parent) < 0:
            raise CloudError('115 文件详情不是可核实的普通文件，已停止修改。', 'INVALID_FILE', 409)
        # Check the two reads agree. Never borrow the manifest's expected parent:
        # a real external move/rename must still pause this task.
        if (fid != file_id or info['pick_code'] != str(pc) or size != skim_size or
                unescape_115_charref(info['file_name']) != unescape_115_charref(name) or
                (a.get('sha1') and a['sha1'].upper() != info['file_sha1'].upper())):
            raise CloudError('两次文件信息读取结果不一致，请核查文件是否正在变化。', 'FILE_CHANGED', 409)
        return {'id': fid, 'name': unescape_115_charref(info['file_name']), 'pickcode': str(pc),
                'size': size, 'parent_id': parent, 'sha1': info['file_sha1']}

    async def fresh_children(self, cid):
        """扫描来源目录时直接读取网盘，不能复用先前任务的目录缓存。"""
        key = self.scope_key(cid)
        self._children_memo.pop(key, None)
        try:
            return (await self._children(cid))[0]
        finally:
            # 不把扫描结果留给后续不同任务复用。
            self._children_memo.pop(key, None)

    async def children(self, cid):
        """目录清单缓存：扫描入口使用 fresh_children 获取当前状态。"""
        key = self.scope_key(cid)
        if key in self._children_memo:
            return self._children_memo[key]
        rows = (await self._children(cid))[0]
        self._children_memo[key] = rows
        return rows

    def forget_children(self, cids):
        """只丢掉这些目录的缓存（写操作后按实际影响面失效）。"""
        for cid in cids or ():
            self._children_memo.pop(self.scope_key(cid), None)

    def forget_files(self, file_ids):
        """丢掉这些文件的详情缓存：我们自己改名/移动/回收之后它们就不再有效。

        只按文件 ID 记账：115 的文件 ID 全局唯一，掺进「账号 + 任务」作用域会让
        缓存写入与失效落在不同的键上（现场：改名后校验读回旧名字）。
        """
        for file_id in file_ids or ():
            self._file_memo.pop(str(file_id), None)

    def affected_parents(self, file_ids):
        """从目录缓存里找出「装着这些文件」的目录 ID。

        写操作只能影响文件所在目录与明确的目标目录；分类、地区这些上层目录读了
        还能继续用。缓存里查不到时返回空，调用方按未知范围处理。
        """
        wanted = {str(fid) for fid in file_ids or () if fid not in (None, '')}
        if not wanted:
            return ()
        found = []
        for key, rows in self._children_memo.items():
            if any(str(entry.get('id')) in wanted for entry in rows):
                found.append(key[1])
        return tuple(found)

    def write_parents(self, file_ids, *extra):
        """写操作的失效范围：文件所在目录 + 明确的目标目录（去重）。

        缓存里找不到这些文件时返回 None —— 调用方按「未知范围」处理，退回整棵清空。
        安全优先：宁可多读几次，也不能拿一份不知道会不会过期的清单去判「文件还在」。
        """
        found = self.affected_parents(file_ids)
        if not found and any(fid not in (None, '') for fid in file_ids or ()):
            return None
        cids = list(found)
        cids.extend(str(cid) for cid in extra if cid not in (None, ''))
        return tuple(dict.fromkeys(cids))

    async def _children(self, cid, page_size=1000):
        # 大目录要多翻 5 倍请求，正好撞 WAF 的读预算。
        """完整读取目录，并顺带带回平台自报的子项数量（字段缺失时为 None）。"""
        expected = None
        for attempt in range(3):
            items, seen, offset = [], set(), 0
            while True:
                try:
                    result = await self.list_files(int(cid), offset, page_size)
                except CloudError as exc:
                    # 平台偶尔会把别的目录条目混进来（分页/WAF 抖动）。这是只读操作，
                    # 重读一次即可；不重试就会让「清空目录」整批放弃，把残留图片留下。
                    if exc.code != 'DIRECTORY_CHANGED' or attempt == 2:
                        raise
                    break
                if result.get('raw_count') is not None:
                    expected = result['raw_count']
                page = result['items']
                ids = [entry['id'] for entry in page]
                if len(ids) != len(set(ids)) or seen.intersection(ids):
                    break  # Concurrent directory changes: restart the read only.
                seen.update(ids)
                items.extend({**entry, 'parent_id': str(cid)} for entry in page)
                if not page or not result['has_more']:
                    return items, expected
                offset += len(page)
            if attempt < 2:
                await asyncio.sleep(.35 * (attempt + 1))
        raise CloudError('目录分页重复，自动重读后仍未恢复，稍后重试。', 'INCOMPLETE_LIST')

    async def directory_count(self, cid):
        """平台自报的子项数量；字段缺失或格式异常时返回 None（表示未知）。"""
        result = await self.list_files(int(cid), 0, 1)
        return result.get('raw_count')

    async def children_verified(self, cid):
        """带总量对账的完整目录读取，供「将要授权删除」的清单使用。

        计数直接取自分页响应，不额外发请求。分页只以「读到空页」结束，一旦平台
        提前截断，扫描会看起来完整而实际缺项，随后按「缺项」清理就会误删本地输出。
        因此计数不一致时重读一次；仍不一致就拒绝发布清单（不清理）。
        """
        items, expected = await self._children(cid, 1150)
        if expected is None or expected == len(items):
            return items
        again, expected = await self._children(cid, 1150)
        if expected is None or expected == len(again):
            return again
        raise CloudError(f'目录 {cid} 清单不完整：读到 {len(again)} 项，平台计数 {expected}，'
                         '本轮不发布清单也不清理，请稍后重试。', 'INCOMPLETE_LIST', 409)

    async def root_pickcode(self, root):
        """来源目录的 pickcode。用 /files/get_info 取一次，这里保持同一形态。"""
        client = self.client()
        result = await self.call(client.fs_file, str(root), _scope='strm')
        rows = result.get('data') if isinstance(result, dict) else None
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise CloudError('115 未返回来源目录信息，稍后重试。', 'SCHEMA_CHANGED')
        row = rows[0]
        for key in ('pc', 'pick_code', 'pickcode'):
            value = row.get(key)
            if isinstance(value, str) and value:
                return value
        # get_info 不带 pickcode 时退回本地换算（不产生请求）。
        convert = getattr(client, 'to_pickcode', None)
        if callable(convert):
            value = convert(str(root))
            if isinstance(value, str) and value:
                return value
        raise CloudError('115 未返回来源目录的 pickcode。', 'SCHEMA_CHANGED')

    async def _download_rows(self, method, pickcode, label, key):
        """把整棵子树的扁平清单读完（每页 5000）。

        完整性按页校验：`count` 与实到条数必须一致，主键不得重复。
        任何一项对不上就抛 INCOMPLETE_LIST —— 这份清单会授权后续的本地清理，
        不能把「没读到」当成「云端没有」。
        """
        rows, seen, page = [], set(), 1
        while page <= self.MAX_DOWNLOAD_PAGES:
            payload = {'pickcode': pickcode, 'page': page, 'per_page': self.DOWNLOAD_PAGE,
                       'app_ver': self.APP_VER}
            result = await self.call(method, payload, _scope='strm')
            data = result.get('data') if isinstance(result, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get('list'), list):
                raise CloudError('115 未返回%s清单，稍后重试。' % label, 'SCHEMA_CHANGED')
            batch = data['list']
            expected = data.get('count')
            if isinstance(expected, bool) or not isinstance(expected, int):
                expected = None
            if expected is not None and expected != len(batch):
                raise CloudError('115 %s清单第 %d 页不完整：实到 %d 条，平台计数 %d。'
                                 % (label, page, len(batch), expected), 'INCOMPLETE_LIST', 409)
            if not batch:
                if data.get('has_next_page'):
                    raise CloudError('115 %s清单第 %d 页为空但仍声称有下一页。'
                                     % (label, page), 'INCOMPLETE_LIST', 409)
                break
            for row in batch:
                if not isinstance(row, dict):
                    raise CloudError('115 %s清单格式发生变化。' % label, 'SCHEMA_CHANGED')
                value = row.get(key)
                if not isinstance(value, str) or not value:
                    raise CloudError('115 %s清单缺少关键字段，已停止处理。' % label, 'SCHEMA_CHANGED')
                if value in seen:
                    raise CloudError('115 %s清单第 %d 页出现重复条目，稍后重试。'
                                     % (label, page), 'INCOMPLETE_LIST', 409)
                seen.add(value)
            rows.extend(batch)
            if not data.get('has_next_page'):
                return rows
            page += 1
        raise CloudError('115 %s清单超过 %d 页，已停止处理。' % (label, self.MAX_DOWNLOAD_PAGES),
                         'INCOMPLETE_LIST', 409)

    async def download_tree(self, root):
        """一次读完整棵子树，返回 (dirs, entries)。

        与全量 STRM 完全同构：
          1. `/files/get_info` 取根目录 pickcode（每棵树 1 次）；
          2. `app/chrome/downfolders` 每页 5000 拿完整目录表；
          3. `app/chrome/downfiles` 每页 5000 拿完整文件表（只有 pid/pc/fs）；
          4. pickcode 本地换算 file_id，再用 `/files/file` 批量补
             file_name / sha1 / file_size（每批 5000，无逐文件请求）。
        """
        root = str(root)
        client = self.client()
        pickcode = await self.root_pickcode(root)
        folders = await self._download_rows(client.download_folders_app, pickcode, '目录', 'fid')
        flat = await self._download_rows(client.download_files_app, pickcode, '文件', 'pc')
        dirs, seen_dirs = [], set()
        for row in folders:
            fid, pid = str(row.get('fid') or ''), str(row.get('pid') or '')
            if not fid or not pid:
                raise CloudError('115 目录清单缺少标识或上级。', 'SCHEMA_CHANGED')
            if fid == root:
                continue  # 根自身由调用方提供路径，不重复计入
            if fid in seen_dirs:
                raise CloudError('115 目录清单出现重复标识。', 'INCOMPLETE_LIST', 409)
            seen_dirs.add(fid)
            dirs.append({'id': fid, 'name': str(row.get('fn') or ''), 'parent_id': pid})
        targets, seen_files = [], set()
        for row in flat:
            pc = row.get('pc')
            fid = str(to_id(pc))
            if not fid.isdigit() or fid in seen_files:
                raise CloudError('115 文件清单出现重复或无效标识。', 'INCOMPLETE_LIST', 409)
            seen_files.add(fid)
            targets.append((fid, pc, str(row.get('pid') or ''), row.get('fs')))
        entries = []
        for start in range(0, len(targets), self.META_BATCH):
            chunk = targets[start:start + self.META_BATCH]
            # id 多时必须是 POST（SDK 文档明确要求）：GET 会把上万个 id 塞进查询串，
            # 2000 条以上直接被端上拒掉，是最容易踩的一步。
            result = await self.call(functools.partial(client.fs_file_skim, method='POST'),
                                     [item[0] for item in chunk], _scope='strm')
            rows = result.get('data') if isinstance(result, dict) else None
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list):
                raise CloudError('115 批量文件详情格式发生变化，已停止处理。', 'SCHEMA_CHANGED')
            detail = {}
            for row in rows:
                if isinstance(row, dict) and row.get('file_id') is not None:
                    detail[str(row['file_id'])] = row
            for fid, pc, pid, size in chunk:
                info = detail.get(fid)
                if info is None:
                    raise CloudError('115 批量文件详情缺少条目（%s），稍后重试。' % fid,
                                     'INCOMPLETE_LIST', 409)
                name = info.get('file_name')
                sha1 = info.get('sha1') or info.get('file_sha1')
                told = info.get('pick_code') or info.get('pickcode')
                try:
                    real = int(info.get('file_size', size))
                except (TypeError, ValueError):
                    raise CloudError('115 批量文件详情的文件大小无效。', 'SCHEMA_CHANGED') from None
                if (not isinstance(name, str) or not name or not isinstance(sha1, str) or not sha1
                        or not isinstance(told, str) or told != pc or real < 0):
                    raise CloudError('115 批量文件详情与清单不一致（%s），已停止处理。' % fid,
                                     'FILE_CHANGED', 409)
                entries.append({'id': fid, 'name': unescape_115_charref(name), 'is_dir': False,
                                'size': real, 'pickcode': pc, 'sha1': sha1.upper(),
                                'parent_id': pid})
        return dirs, entries

    async def directory(self, cid):
        return [entry['id'] for entry in await self.path_info(cid)]

    async def path_info(self, cid):
        cid = str(cid)
        if cid == '0':
            return [{'id': '0', 'name': ''}]
        data = await self.list_files(int(cid), 0, 32)
        path = data.get('path')
        if not isinstance(path, list) or not path or str(path[-1].get('cid', path[-1].get('id'))) != cid:
            # SDK get_info uses category/get when files has no usable path.
            # `paths` contains ancestors, excluding the object itself.
            info = await self.call(self.client().fs_category_get, {'cid': cid, 'status': 1})
            try:
                info = update_resp_ancestors(info, ...)
                if str(info['id']) != cid or not info['is_dir']:
                    raise ValueError('not the requested directory')
                path = info['ancestors']
            except (KeyError, TypeError, ValueError, IndexError):
                raise CloudError('目录不存在或暂时不可读取，请稍后重试。', 'DIRECTORY_UNAVAILABLE', 409) from None
        result = []
        for entry in path:
            key = str(entry.get('cid', entry.get('id', '')))
            name = entry.get('name', entry.get('n'))
            if not key.isdigit() or (key != '0' and (not isinstance(name, str) or not name)):
                raise CloudError('目录路径名称或身份缺失。', 'SCHEMA_CHANGED', 409)
            result.append({'id': key, 'name': unescape_115_charref(name or '')})
        if result[0]['id'] != '0':
            result.insert(0, {'id': '0', 'name': ''})
        return result

    async def mkdir(self, parent_id, name):
        return await self.call(self.client().fs_mkdir, name, pid=parent_id, _kind='write',
                               _parents=(str(parent_id),))

    async def rename(self, file_id, name):
        self.count_op('改名')
        # fs_rename(dict) expects already encoded field names; use tuple contract.
        return await self.call(self.client().fs_rename, (file_id, name), _kind='write',
                               _parents=self.write_parents([file_id]), _files=(file_id,))

    async def rename_many(self, items):
        """按文件 ID 批量改名；分批串行提交，结果由调用方逐项核查。"""
        self.count_op('改名', len(items))
        pairs = [(str(fid), name) for fid, name in items]
        if not pairs or len({fid for fid, _ in pairs}) != len(pairs):
            raise ValueError('重命名需要互不相同的文件 ID。')
        last = None
        client = self.client()
        parents = self.write_parents([fid for fid, _ in pairs])
        for start in range(0, len(pairs), 50):
            # 不标记 _batch：每次请求仍按用户设置的普通写入间隔执行。
            last = await self.call(client.fs_rename, pairs[start:start + 50],
                                   _scope='file', _kind='write', _parents=parents,
                                   _files=tuple(fid for fid, _ in pairs[start:start + 50]))
        return last

    async def move(self, file_id, parent_id):
        self.count_op('移动')
        # SDK documents that the server defaults to destructive replacement.
        # Always request keep_both; organizer also checks names and file identity.
        return await self.call(self.client().fs_move, {'fid': file_id, 'pid': parent_id,
            'conflict_policy': json.dumps({str(file_id): {'action': 'keep_both'}})}, _kind='write',
            _parents=self.write_parents([file_id], parent_id), _files=(file_id,))

    async def move_many(self, file_ids, parent_id):
        """对齐：一次 /open/ufile/move，file_ids 逗号分隔 + to。"""
        self.count_op('移动', len(file_ids))
        ids = [str(fid) for fid in file_ids]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError('批量移动需要互不相同的文件 ID。')
        return await self.call(self.client().fs_move_open,
                               {'file_ids': ','.join(ids), 'to_cid': str(parent_id)},
                               _scope='file', _kind='write', _batch=len(ids),
                               _parents=self.write_parents(ids, parent_id), _files=tuple(ids))

    async def sign(self):
        """115 每日签到。

        `user_points_sign` 是查询（是否已签、连续天数），`user_points_sign_post` 才是签到；
        不要拿 `user_sign`（那是个性签名设置，返回的是 HTML 页面）。
        """
        info = await self.call(self.client().user_points_sign, _scope='maintenance')
        data = info.get('data') if isinstance(info, dict) else None
        if isinstance(data, dict) and data.get('is_sign_today'):
            return {'signed': False, 'message': '今天已经签到过了。',
                    'days': data.get('sign_day_count')}
        result = await self.call(self.client().user_points_sign_post,
                                 _scope='maintenance', _kind='write')
        payload = result.get('data') if isinstance(result, dict) else None
        days = payload.get('sign_day_count') if isinstance(payload, dict) else None
        return {'signed': True, 'message': '签到完成。', 'days': days}

    async def recycle_info(self):
        """回收站概览：只读第一页拿总条数，不拉全量清单。"""
        data = await self.call(self.client().recyclebin_list,
                                 {'aid': 7, 'limit': 1, 'offset': 0}, _scope='maintenance')
        count = data.get('count') if isinstance(data, dict) else None
        try:
            count = self.exact_number(count) if count is not None else None
        except (TypeError, ValueError):
            count = None
        return {'count': count}

    async def recycle_clear(self, password='000000'):
        """清空回收站（不可恢复）。

        115 的清空需要安全密钥；用户在账号安全里关掉它时传任意 6 位数字即可。
        """
        return await self.call(self.client().recyclebin_clean, password,
                                 _scope='maintenance', _kind='write', _parents=())

    async def recycle_files(self, limit_pages=None):
        """Strict complete listing used as positive evidence, never as missing-file inference.

        limit_pages 给定时只读前 N 页（100/页）就返回：删完文件想确认结果时，新记录
        一定在最前面，没必要每次把几千条回收站翻一遍。调用方用 `recycle_recent()`
        能拿到「是否已读全」，读不完整时再退回全量，正确性不受影响。
        """
        offset, total, rows, seen = 0, None, [], set()
        while True:
            data = await self.call(self.client().recyclebin_list, {'offset': offset, 'limit': 100, 'aid': 7})
            if 'count' not in data or 'offset' not in data:
                raise CloudError('回收站缺少完整分页字段，停止淘汰。', 'RECYCLE_UNVERIFIED', 409)
            try:
                count = self.exact_number(data['count'])
                returned = self.exact_number(data['offset'])
            except (ValueError, TypeError):
                raise CloudError('回收站分页格式无效，停止淘汰。', 'RECYCLE_UNVERIFIED', 409) from None
            if count < 0 or returned != offset or (total is not None and count != total):
                raise CloudError('回收站分页发生变化，停止淘汰。', 'RECYCLE_UNVERIFIED', 409)
            total = count
            # The Web API also returns entries at numeric top-level keys, with
            # pagination metadata alongside them (no `data`, including when empty).
            raw = data.get('data')
            if raw is None and 'data' not in data:
                meta = {'state','count','offset','limit','page_size','rb_pass','error','errno','errNo','code','order','o','is_asc','asc'}
                if any(not str(k).isdigit() and k not in meta for k in data):
                    raise CloudError('回收站返回未知条目字段，停止淘汰。', 'RECYCLE_UNVERIFIED', 409)
                raw = {k: v for k, v in data.items() if str(k).isdigit()}
            if isinstance(raw, list):
                page = [(None, e) for e in raw]
            elif isinstance(raw, dict) and all(str(k).isdigit() for k in raw):
                page = list(raw.items())
            else:
                raise CloudError('回收站条目结构无效，停止淘汰。', 'RECYCLE_UNVERIFIED', 409)
            for key, entry in page:
                if not isinstance(entry, dict):
                    raise CloudError('回收站条目格式不明，停止淘汰。', 'RECYCLE_UNVERIFIED', 409)
                # /rb `id` is the RECYCLE RECORD ID, not the original file ID.
                # Keep an original ID only when the response explicitly has one.
                rid = str(entry.get('rid', entry.get('id', key if key is not None else '')))
                fid = str(entry.get('file_id', entry.get('fid', '')))
                name, size = entry.get('file_name'), entry.get('file_size')
                if not rid.isdigit() or (fid and not fid.isdigit()) or not isinstance(name, str) or not name or size is None or rid in seen:
                    raise CloudError('回收站缺少唯一 ID、名称或大小，停止淘汰。', 'RECYCLE_UNVERIFIED', 409)
                try:
                    size = self.exact_number(size)
                except (ValueError, TypeError):
                    raise CloudError('回收站大小字段不明，停止淘汰。', 'RECYCLE_UNVERIFIED', 409) from None
                seen.add(rid)
                record = {'id': fid, 'rid': rid, 'name': unescape_115_charref(name), 'size': size}
                if 'cid' in entry or 'parent_id' in entry:
                    parent = str(entry.get('parent_id', entry.get('cid')))
                    if not parent.isdigit():
                        raise CloudError('回收站原目录 ID 无效，停止淘汰。', 'RECYCLE_UNVERIFIED', 409)
                    record['parent_id'] = parent
                # Web /rb: 1 = file, 2 = directory (confirmed on the NAS).
                # This is not fs_file_skim's file_category enumeration.
                if str(entry.get('type')) in ('1', '2'):
                    record['is_dir'] = str(entry['type']) == '2'
                if 'dtime' in entry:
                    record['deleted_at'] = entry['dtime']
                rows.append(record)
            if len(rows) > total or (not page and len(rows) < total):
                raise CloudError('回收站分页不完整，停止淘汰。', 'RECYCLE_UNVERIFIED', 409)
            if len(rows) == total:
                self._recycle_last_total = total
                self._recycle_snapshot = (self.clock(), rows)
                return rows
            if limit_pages and len(rows) >= max(1, int(limit_pages)) * 100:
                self._recycle_last_total = total
                return rows
            offset = len(rows)

    async def recycle_recent(self, pages=3):
        """最近若干页 + 是否已读全：确认回收结果时用它，读不全由调用方退回全量。"""
        rows = await self.recycle_files(limit_pages=pages)
        total = self._recycle_last_total
        return rows, (total is not None and len(rows) >= total)

    def recycle_snapshot(self, max_age=30.0):
        """上一次完整回收站清单（默认 30 秒内有效）。

        删完一个文件后的那份清单就是下一个文件的「回收前状态」，直接复用它，
        每个文件能少一次全量翻页。
        """
        snapshot = getattr(self, '_recycle_snapshot', None)
        if snapshot and self.clock() - snapshot[0] <= max_age:
            return snapshot[1]
        return None

    @staticmethod
    def exact_number(value):
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isascii() or not str(value).isdigit():
            raise ValueError('not an exact nonnegative integer')
        return int(value)

    async def live_file_exists(self, file_id):
        """Check live namespace, not skim metadata that survives recycling.

        /files/file still returns the original ID/name/size after recycling and
        omits aid. /files/get_info returns 20018 for those same retired IDs.
        A missing object is distinct from login/network/unknown-schema failures.
        """
        try:
            data = await self.call(self.client().fs_file, str(file_id), _scope='file')
        except CloudError as exc:
            if exc.code == 'FILE_NOT_FOUND':
                return False
            raise
        rows = data.get('data') if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise CloudError('文件存续详情不完整，停止核查。', 'SCHEMA_CHANGED', 409)
        row = rows[0]
        # get_info uses fid for files, cid for directories. cid on a file row
        # denotes its parent, not its identity. Never substitute a missing ID.
        ids = [row[k] for k in ('file_id', 'fid') if k in row]
        if not ids and 'cid' in row:
            ids = [row['cid']]
        areas = [row[k] for k in ('area_id', 'aid') if k in row]
        try:
            ids = {self.exact_number(value) for value in ids}
            areas = {self.exact_number(value) for value in areas}
            expected = self.exact_number(file_id)
        except (TypeError, ValueError):
            raise CloudError('文件存续身份或状态字段无效，停止核查。', 'SCHEMA_CHANGED', 409) from None
        if ids != {expected}:
            raise CloudError('文件存续检查身份不一致。', 'SCHEMA_CHANGED', 409)
        if len(areas) != 1 or not areas <= {1, 7, 12, 120}:
            raise CloudError('文件存续详情缺少明确状态，停止核查。', 'SCHEMA_CHANGED', 409)
        return bool(areas & {1, 12})

    async def recycle_empty_directory(self, directory_id, parent_id):
        # Dedicated entry point: callers bind a scoped, persisted empty-directory
        # intent. Recheck emptiness immediately before the single request.
        if await self.directory(directory_id) != [*(await self.directory(parent_id)), str(directory_id)]:
            raise CloudError('空目录位置变化，已保留。', 'DIRECTORY_CHANGED', 409)
        if await self.children(directory_id):
            raise CloudError('目录已出现内容，已保留。', 'DIRECTORY_CHANGED', 409)
        return await self.recycle(directory_id, parent_id)

    async def recycle(self, file_id, parent_id):
        self.count_op('删除')
        if not str(file_id).isdigit() or not str(parent_id).isdigit():
            raise CloudError('回收文件 ID 无效。', 'RECYCLE_UNVERIFIED', 409)
        # One explicit ID. Empty directories use the separate guarded entry point.
        # Never purge or clean the recycle bin.
        return await self.call(self.client().fs_delete, {'fid': str(file_id), 'pid': str(parent_id), 'ignore_warn': 0},
                               _scope='file', _kind='write',
                               _parents=self.write_parents([file_id], parent_id), _files=(file_id,))

    async def recycle_many(self, file_ids, parent_id):
        self.count_op('删除', len(file_ids))
        """一次请求回收同目录下的多个文件（对齐「批量删除 N 个文件」）。

        SDK 契约：``/rb/delete`` 的 ``fid`` 支持逗号分隔、不限制文件数，
        但明确要求不要并发调用——所以这里仍走写通道的单通道节流。
        """
        ids = [str(fid) for fid in file_ids]
        if not ids:
            return None
        if len(ids) == 1:
            return await self.recycle(ids[0], parent_id)
        if len(set(ids)) != len(ids) or not all(i.isdigit() for i in ids) or not str(parent_id).isdigit():
            raise CloudError('批量回收的文件 ID 无效或重复。', 'RECYCLE_UNVERIFIED', 409)
        # 真机验证：115 的 /rb/delete 不认逗号 fid（回 errno 231011 且不删），必须用 fid[i]。
        parents = self.write_parents(ids, parent_id)

        async def send(targets):
            return await self.call(self.client().fs_delete_open,
                                   {'file_ids': ','.join(targets)},
                                   _scope='file', _kind='write', _batch=len(targets),
                                   _parents=parents, _files=tuple(targets))

        try:
            return await send(ids)
        except CloudError as exc:
            # 刚改名/移动后立刻删会误报「文件已删除」；按目录实况判断，还在就等 0.6s 重试一次。
            if '已删除' not in str(exc):
                raise
            still = {str(e['id']) for e in await self.children(parent_id)} & set(ids)
            if not still:
                return {'state': True, 'verified': 'gone'}
            await self.sleep(.6)
            return await send(sorted(still))

    async def recycle_revert(self, rid):
        """按回收记录 ID 精确恢复（G10）：不按名称猜测，rid 来自已核实的回收关联。"""
        if not str(rid).isdigit():
            raise CloudError('恢复记录 ID 无效。', 'RECYCLE_UNVERIFIED', 409)
        return await self.call(self.client().recyclebin_revert, {'rid': [int(rid)]}, _kind='write')

    async def share_items(self, secret, cid=0):
        items=[];offset=0;count=None;seen=set()
        while True:
            result=await self.call(self.client().share_snap,{**secret,'cid':cid,'offset':offset,'limit':100},_scope='share')
            data=result.get('data')
            if not isinstance(data,dict) or not isinstance(data.get('list'),list) or 'count' not in data:
                raise CloudError('分享目录返回结构不完整。','SCHEMA_CHANGED',409)
            try: total=self.exact_number(data['count'])
            except ValueError:raise CloudError('分享计数字段无效。','SCHEMA_CHANGED',409) from None
            if count is not None and total!=count:raise CloudError('分享内容在读取期间发生变化。','INCOMPLETE_LIST',409)
            count=total
            if 'offset' in data and self.exact_number(data['offset'])!=offset:raise CloudError('分享分页位置不匹配。','INCOMPLETE_LIST',409)
            page=data['list']
            for raw in page:
                try:
                    a=normalize_attr(raw,simple=True)
                    if not str(a['id']).isascii() or not str(a['id']).isdigit() or not a['name'] or not isinstance(a['name'],str) or type(a.get('size',0)) is not int or a.get('size',0)<0:raise ValueError()
                    key=(a['name'],bool(a['is_dir']))
                    if str(a['id']) in seen or any((x['name'],x['is_dir'])==key for x in items):raise ValueError()
                    seen.add(str(a['id']))
                    # 115 的快照里带 sha，带出来才能命中内容索引；
                    # 非 40 位十六进制一律当没有，别把脏值写进缓存键。
                    sha=str(a.get('sha1') or '').upper()
                    if sha and (len(sha)!=40 or any(ch not in '0123456789ABCDEF' for ch in sha)):sha=''
                    entry={'id':str(a['id']),'name':a['name'],'is_dir':bool(a['is_dir']),
                           'size':a.get('size',0)}
                    # 有 sha 才带这个键：目录本来没有，下游还有按整条等值比较的地方。
                    if sha:entry['sha1']=sha
                    items.append(entry)
                except (KeyError,TypeError,ValueError):raise CloudError('分享条目身份或名称不唯一。','SCHEMA_CHANGED',409) from None
            if len(items)>count or (not page and len(items)<count):raise CloudError('分享分页不完整。','INCOMPLETE_LIST',409)
            if len(items)==count:return items
            offset=len(items)

    async def share_receive(self,secret,items,dest):
        return await self.call(self.client().share_receive,{**secret,'file_id':','.join(x['id'] for x in items),'cid':dest},_scope='share',_kind='write',_parents=(str(dest),))

    async def share_url_download(self, file_id, share_code, receive_code, user_agent):
        """获取分享直链；播放请求按账号、分享、文件和 UA 短暂缓存并合并并发。"""
        key = None
        if playback_request.get():
            cookie = self.store.get('cookie')
            if not cookie:
                self.client()  # Preserve the normal login-required error.
            secret = hashlib.sha256((str(share_code) + '\0' + str(receive_code)).encode()).hexdigest()
            key = ('share', hashlib.sha256(cookie.encode()).hexdigest(), str(file_id), secret, user_agent)
            cached = self.play_urls.get(key)
            if cached and time.monotonic() < cached[0]:
                self.play_hits += 1
                return cached[1], dict(cached[2])
            self.play_misses += 1
        if key is None:
            return await self._share_download_url(file_id, share_code, receive_code, user_agent)
        lock = self.play_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self.play_urls.get(key)
            if cached and time.monotonic() < cached[0]:
                return cached[1], dict(cached[2])
            url, headers = await self._share_download_url(file_id, share_code, receive_code, user_agent)
            now = time.monotonic()
            self.play_urls = {k:v for k,v in self.play_urls.items() if now < v[0]}
            if len(self.play_locks) > 512:
                self.play_locks = {k:v for k,v in self.play_locks.items() if v.locked()}
            if len(self.play_urls) >= self.PLAY_LINK_CACHE:
                self.play_urls.pop(next(iter(self.play_urls)))
            # 短于网盘直链缓存，分享被撤销时尽快重新检查。
            ttl = min(self.SHARE_PLAY_LINK_TTL_CAP, self.link_ttl(url))
            self.play_urls[key] = (now + ttl, url, headers)
            return url, dict(headers)

    async def _share_download_url(self, file_id, share_code, receive_code, user_agent):
        value = await self.call(self.client().share_download_url,
                                {'file_id': str(file_id), 'share_code': share_code, 'receive_code': receive_code},
                                app='android', user_agent=user_agent, _scope='share')
        url = str(value)
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or '\r' in url or '\n' in url:
            raise CloudError('115 分享播放地址无效。', 'INVALID_URL')
        headers = dict(getattr(value, 'headers', {}) or {})
        return url, headers

    async def url(self, pickcode: str, user_agent: str):
        # 进程内缓存，有效期取自直链自身（见 link_ttl）。
        #
        # **UA 必须留在缓存键里**：0.9.59 一度改成「账号 + 文件」，认为 CDN 地址不绑 UA
        # （依据是 115driver 对**分享**下载的注释）。现场证伪了：同一条直链用
        # Infuse/7.8 取 206，用 Emby/4.8、Lavf/57.28.100、空 UA 取全是 403。
        # 后果是 Emby 的探测（Lavf）拿到的链接被 Emby 播放复用 → CDN 403 → Emby 回 500。
        # 所以键恢复成 (账号, 文件, UA)：宁可多取一次链，也不能把别人的链接递出去。
        key = None
        if playback_request.get():
            cookie = self.store.get('cookie')
            if not cookie:
                self.client()  # Preserve the normal login-required error.
            key = (hashlib.sha256(cookie.encode()).hexdigest(), pickcode, user_agent)
            cached = self.play_urls.get(key)
            if cached and time.monotonic() < cached[0]:
                self.play_hits += 1
                return cached[1], dict(cached[2])
            self.play_misses += 1
        if key is None:
            value = await self._download_url(pickcode, user_agent)
        else:
            # 同一 (账号, pickcode, UA) 并发只向 115 取一次链：Emby 扫描一季时
            # 探测与播放会同时撞上来，没有这一步就是成倍的重复请求。
            lock = self.play_locks.setdefault(key, asyncio.Lock())
            async with lock:
                cached = self.play_urls.get(key)
                if cached and time.monotonic() < cached[0]:
                    return cached[1], dict(cached[2])
                value = await self._download_url(pickcode, user_agent)
        url = str(value)
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or '\r' in url or '\n' in url:
            raise CloudError('115 返回的播放地址无效。', 'INVALID_URL')
        headers = dict(getattr(value, 'headers', {}) or {})
        if key is not None:
            now = time.monotonic()
            self.play_urls = {k:v for k,v in self.play_urls.items() if now < v[0]}
            if len(self.play_locks) > 512:
                self.play_locks = {k:v for k,v in self.play_locks.items() if v.locked()}
            if len(self.play_urls) >= self.PLAY_LINK_CACHE:
                self.play_urls.pop(next(iter(self.play_urls)))
            self.play_urls[key] = (now + self.link_ttl(url), url, headers)
        return url, headers

    async def _download_url(self, pickcode: str, user_agent: str):
        """向 115 取一次播放直链；缓存与并发合并都在 url() 里，这里只发请求。"""
        return await self.call(self.client().download_url, pickcode,
                               user_agent=user_agent, app='android')

    async def qr_start(self, app: str):
        if app not in CLIENT_LABELS:
            raise CloudError('请选择支持的登录客户端。', 'INVALID_LOGIN_CLIENT', 422)
        # Token creation and cookie exchange use the SAME explicit client.
        # Replacing a challenge also invalidates a poll that is already in flight.
        self.qr_revision += 1
        revision = self.qr_revision
        self.qr.clear()
        data = (await self.call(P115Client.login_qrcode_token, app=app))['data']
        if revision != self.qr_revision:
            raise CloudError('扫码请求已被替换，请使用最新二维码。', 'QR_REPLACED', 409)
        if not isinstance(data, dict) or not all(k in data for k in ('uid', 'time', 'sign', 'qrcode')):
            raise CloudError('115 二维码返回格式发生变化。', 'SCHEMA_CHANGED')
        key = secrets.token_urlsafe(24)
        self.qr[key] = {'payload': {k: data[k] for k in ('uid', 'time', 'sign')},
                        'text': data['qrcode'], 'expires': time.monotonic() + 180, 'app': app}
        buf = io.BytesIO()
        qrcode.make(data['qrcode']).save(buf, format='PNG')
        import base64
        return {'id': key, 'image': 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode(),
                'expires_in': 180, 'app': app, 'client_label': CLIENT_LABELS[app]}

    def qr_cancel(self, key: str):
        self.qr.pop(key, None)
        return {'status': 'cancelled'}

    async def qr_poll(self, key: str):
        entry = self.qr.get(key)
        if not entry or entry['expires'] < time.monotonic():
            self.qr.pop(key, None)
            return {'status': 'expired'}
        try:
            result = await self.call(P115Client.login_qrcode_scan_status, entry['payload'])
        except CloudError:
            if self.qr.get(key) is not entry:
                return {'status': 'cancelled'}
            # Long polling timeouts are not equivalent to an invalid QR code.
            return {'status': 'waiting', 'message': '等待扫码，暂未取得状态。'}
        if self.qr.get(key) is not entry:
            return {'status': 'cancelled'}
        status = int(result.get('data', {}).get('status', -99))
        if status == 2:
            result = await self.call(P115Client.login_qrcode_scan_result,
                                     entry['payload']['uid'], app=entry['app'])
            if self.qr.get(key) is not entry:
                return {'status': 'cancelled'}
            if entry['expires'] < time.monotonic():
                self.qr.pop(key, None)
                return {'status': 'expired'}
            cookie = result.get('data', {}).get('cookie')
            if not isinstance(cookie, dict) or not all(cookie.get(k) for k in ('UID', 'CID', 'SEID')):
                raise CloudError('扫码结果未包含可用 Cookie。', 'SCHEMA_CHANGED')
            self.store.set_many({'cookie': '; '.join(f'{k}={v}' for k, v in cookie.items()),
                                 '115_qr_client': entry['app']})
            self.qr.pop(key, None)
            label = CLIENT_LABELS[entry['app']]
            self.store.event('115', f'扫码登录完成，客户端：{label}；Cookie 已加密保存。')
            return {'status': 'connected', 'app': entry['app'], 'client_label': label}
        if status in (-1, -2):
            self.qr.pop(key, None)
        return {'status': {0: 'waiting', 1: 'scanned', -1: 'expired', -2: 'cancelled'}.get(status, 'waiting')}
