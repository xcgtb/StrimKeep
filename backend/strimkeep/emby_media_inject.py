"""把媒体信息写进 Emby 的 library.db（复刻神医助手「媒体信息持久化」的入库动作）。

为什么走这条路：Emby 的公开 API 没有写 ``MediaStreams`` 的接口，而它自己也**不会**重算
STRM 的媒体信息（2026-11-10 实测：Refresh(Default/FullRefresh)、Library/Media/Updated、
touch + 全库扫描，四种方式之后 ``MediaStreams`` 都还是 0）。神医助手能做到是因为它是服务端
插件，直接写库；本模块做的是同一件事，只是从外部写：

- 读本项目已经生成好的 sidecar（``<名字>-media-info.json``，字段本身就是 Emby 的
  MediaStream 形状）；
- 按 ``MediaItems.Path`` 反推 sidecar（``/media/…`` → STRM 根，``.strm`` → ``-media-info.json``）；
- 写 ``MediaStreams2`` / ``Chapters3``，并把 ``MediaItems`` 的宽高、总码率补上。

实测（2026-11-10，Emby 4.10.2）：写完 5 秒内 ``/emby/Items/{id}`` 就返回
``MediaStreams=4``，**不需要重启 Emby**。写入前会整库备份，且只补「一条流都没有」的条目，
重复执行幂等（按 ItemId 先删后插）。
"""
import json
import shutil
import sqlite3
import time

# Emby 的 MediaStreamType：1=Audio 2=Video 3=Subtitle 4=EmbeddedImage（按线上库统计核对过）
TYPE_CODE = {'Video': 2, 'Audio': 1, 'Subtitle': 3, 'EmbeddedImage': 4, 'Attachment': 4}
# Emby 的 ExtendedVideoType 枚举（我们 sidecar 里存的是字符串）
EXTENDED = {'None': 0, 'Hdr10': 100, 'DolbyVision': 130}
EXTENDED_SUBTYPE = {'None': 0, 'Hdr10': 100, 'DoviProfile76': 13076}
COLUMNS = ('ItemId, StreamIndex, StreamType, Codec, Language, ChannelLayout, Profile, AspectRatio, '
           'Path, IsInterlaced, BitRate, Channels, SampleRate, IsDefault, IsForced, IsHearingImpaired, '
           'IsExternal, Height, Width, AverageFrameRate, RealFrameRate, Level, PixelFormat, BitDepth, '
           'IsAnamorphic, RefFrames, Rotation, CodecTag, Comment, NalLengthSize, Title, TimeBase, '
           'ColorPrimaries, ColorSpace, ColorTransfer, Extradata, AttachmentSize, MimeType, '
           'ExtendedVideoType, ExtendedVideoSubtype')
PLACEHOLDERS = ', '.join('?' * len(COLUMNS.split(',')))
DEFAULT_DB = ''          # 不设默认：每个用户的 Emby 数据目录挂载点都不同，必须由使用者配置


def library_db(store):
    """Emby 的 library.db 路径：设置项 → 环境变量 → 空（未配置就明确报错，不猜路径）。"""
    import os
    return str(store.get('emby_library_db')
               or os.environ.get('STRIMKEEP_EMBY_LIBRARY_DB')
               or DEFAULT_DB)


def emby_strm_mount(store):
    """Emby 侧看到 STRM 的挂载根（Emby 里的 ``/media``、``/strimkeep`` 之类）。

    顺序：设置项 ``emby_strm_mount`` → 环境变量 → 控制台的 ``emby_strm_path`` → ``/media``。
    每个用户的挂载点不同，所以这里只是"兜底"，不假设就是某台机器的布局。
    """
    import os
    mount = str(store.get('emby_strm_mount') or os.environ.get('STRIMKEEP_EMBY_STRM_MOUNT') or '').strip()
    if mount:
        return mount.rstrip('/')
    console = store.get('console_config') or {}
    return str(console.get('emby_strm_path') or '/media').rstrip('/')



def inject_sidecar(db_path, item_id, sidecar, strm_root='/strm', emby_root='/media'):
    """单条即时写入（追更用）：已经有视频流就跳过。

    返回 ``(状态, 流数, 原因)``：状态为 ``injected`` / ``skip`` / ``missing`` /
    ``unreadable`` / ``no_streams`` / ``failed``；原因只在没写进去时非空，
    这样"哪一条没写进去、为什么"可以直接落到失败清单里，而不是只剩一个汇总数字。
    """
    import os
    conn = sqlite3.connect(db_path, timeout=20)
    conn.execute('PRAGMA busy_timeout=15000')
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute('SELECT 1 FROM MediaStreams2 WHERE ItemId=? AND StreamType=2 LIMIT 1',
                        (item_id,)).fetchone():
            return 'skip', 0, ''
        if not os.path.exists(sidecar):
            return 'missing', 0, 'sidecar_missing'
        try:
            with open(sidecar, encoding='utf-8') as handle:
                json.load(handle)
        except (OSError, ValueError):
            return 'unreadable', 0, 'sidecar_unreadable'
        result = Injector(db_path, strm_root=strm_root, emby_root=emby_root).inject_one(
            conn, item_id, sidecar)
        if not result['ok']:
            conn.rollback()
            return 'failed', 0, result['reason']
        conn.commit()
        return 'injected', result['streams'], ''
    except sqlite3.Error:
        conn.rollback()
        return 'failed', 0, 'write_failed'
    finally:
        conn.close()



def stream_row(item_id, index, track):
    """sidecar 里的一条流 → ``MediaStreams2`` 的一行。"""
    kind = track.get('Type') or ('Video' if track.get('Width') else 'Audio')
    return (item_id, index, TYPE_CODE.get(kind, 0), track.get('Codec'), track.get('Language'),
            track.get('ChannelLayout'), track.get('Profile'), track.get('AspectRatio'),
            track.get('Path'), 1 if track.get('IsInterlaced') else 0, track.get('BitRate'),
            track.get('Channels'), track.get('SampleRate'), 1 if track.get('IsDefault') else 0,
            1 if track.get('IsForced') else 0, 1 if track.get('IsHearingImpaired') else 0,
            # Emby 4.10 reads these two database fields in the opposite order.
            1 if track.get('IsExternal') else 0, track.get('Width'), track.get('Height'),
            track.get('AverageFrameRate'), track.get('RealFrameRate'), track.get('Level'),
            track.get('PixelFormat'), track.get('BitDepth'),
            1 if track.get('IsAnamorphic') else 0, track.get('RefFrames'), track.get('Rotation'),
            track.get('CodecTag'), track.get('Comment'), track.get('NalLengthSize'),
            track.get('Title') or track.get('DisplayTitle'), track.get('TimeBase'),
            track.get('ColorPrimaries'), track.get('ColorSpace'), track.get('ColorTransfer'),
            track.get('Extradata'), track.get('AttachmentSize'), track.get('MimeType'),
            EXTENDED.get(str(track.get('ExtendedVideoType')), 0),
            EXTENDED_SUBTYPE.get(str(track.get('ExtendedVideoSubType')), 0))


def sidecar_path(strm_path, emby_root='/media', strm_root='/strm'):
    """Emby 里的 STRM 路径 → 我们磁盘上的 sidecar 路径。

    两个根都做 ``str()`` 兜底：调用方传 ``Path`` 是很自然的事，
    之前直接 ``.rstrip('/')`` 会在 ``Path`` 上炸掉（测试就先炸了）。
    """
    relative = str(strm_path or '')
    emby_root, strm_root = str(emby_root or ''), str(strm_root or '')
    if relative.startswith(emby_root + '/'):
        relative = relative[len(emby_root):]
    path = strm_root.rstrip('/') + '/' + relative.lstrip('/')
    for suffix in ('.strm', '.STRM'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
    return path + '-media-info.json'


class Injector:
    """一次批量写入：备份 → 逐条补齐 → 汇总。"""

    def __init__(self, db_path, strm_root='/strm', emby_root='/media', keep_backups=3, recorder=None):
        self.db_path = db_path
        self.strm_root = strm_root
        self.emby_root = emby_root
        self.keep_backups = keep_backups
        self.recorder = recorder      # 逐条回报：成功传 ''，失败传原因码

    # ---------- 库 ----------

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.execute('PRAGMA busy_timeout=15000')
        conn.row_factory = sqlite3.Row     # 按列名取值（保留外挂字幕行时要读原始字段）
        return conn

    def schema_ok(self, conn):
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        return {'MediaStreams2', 'Chapters3', 'MediaItems'} <= names

    def backup(self):
        """整库备份；保留最近几次，避免把磁盘写满。"""
        import glob
        import os
        stamp = time.strftime('%Y%m%d-%H%M%S')
        target = '%s.bak-%s' % (self.db_path, stamp)
        shutil.copy2(self.db_path, target)
        old = sorted(glob.glob(self.db_path + '.bak-*'))
        for path in old[:-self.keep_backups]:
            try:
                os.remove(path)
            except OSError:
                pass
        return target

    def targets(self, conn, limit=0):
        """缺视频流（StreamType=2）的 STRM 条目：``[(item_id, path)]``。

        只补「一条流都没有」会漏掉「只登记了外挂字幕」的条目（现场 105 条就是这种：
        只有一条 ``3:ass``/``3:sup``，没有视频和音频信息），所以判据是**没有视频流**。
        """
        rows = conn.execute(
            "SELECT Id, Path FROM MediaItems WHERE Path LIKE '%.strm' AND Id NOT IN "
            "(SELECT ItemId FROM MediaStreams2 WHERE StreamType=2) ORDER BY Id").fetchall()
        return rows[:limit] if limit else rows

    # ---------- 写 ----------

    def inject_one(self, conn, item_id, sidecar):
        """一条条目：先删后插。返回 ``{'ok','streams','chapters','reason'}``。

        ``reason`` 只在失败时非空（``sidecar_missing`` / ``sidecar_unreadable`` /
        ``no_streams``）：只给调用方一个 ``None`` 的话，"没 sidecar"和"sidecar 坏了"
        就分不出来，失败清单也就没法说明原因。
        """
        import json
        import os
        if not os.path.exists(sidecar):
            return {'ok': False, 'streams': 0, 'chapters': 0, 'reason': 'sidecar_missing'}
        try:
            with open(sidecar, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {'ok': False, 'streams': 0, 'chapters': 0, 'reason': 'sidecar_unreadable'}
        tracks = data.get('streams') or []
        if not tracks:
            return {'ok': False, 'streams': 0, 'chapters': 0, 'reason': 'no_streams'}
        # 外挂字幕/音轨指向磁盘上的真实文件，sidecar 里没有它们；先留住，写完再补回去，
        # 否则「只登记了外挂字幕」的条目会因为补视频信息而丢掉字幕。
        existing = conn.execute(
            'SELECT * FROM MediaStreams2 WHERE ItemId=? ORDER BY StreamIndex', (item_id,)).fetchall()
        kept = [row for row in existing if row['IsExternal']]
        conn.execute('DELETE FROM MediaStreams2 WHERE ItemId=?', (item_id,))
        conn.execute('DELETE FROM Chapters3 WHERE ItemId=?', (item_id,))
        for index, track in enumerate(tracks):
            conn.execute('INSERT INTO MediaStreams2 (%s) VALUES (%s)' % (COLUMNS, PLACEHOLDERS),
                         stream_row(item_id, index, track))
        for offset, row in enumerate(kept):
            values = []
            for column in COLUMNS.split(','):
                name = column.strip()
                values.append(item_id if name == 'ItemId'
                              else len(tracks) + offset if name == 'StreamIndex'
                              else row[name])
            conn.execute('INSERT INTO MediaStreams2 (%s) VALUES (%s)' % (COLUMNS, PLACEHOLDERS), values)
        chapters = data.get('chapters') or []
        for index, chapter in enumerate(chapters):
            conn.execute('INSERT INTO Chapters3 (ItemId, ChapterIndex, StartPositionTicks, Name, '
                         'ImagePath, ImageDateModified, MarkerType) VALUES (?,?,?,?,?,?,?)',
                         (item_id, index, int(chapter.get('StartPositionTicks') or 0),
                          chapter.get('Name') or ('章节 %d' % (index + 1)), None, None, 0))
        video = next((track for track in tracks if track.get('Type') == 'Video'), None)
        if video:
            conn.execute('UPDATE MediaItems SET Width=COALESCE(Width, ?), Height=COALESCE(Height, ?), '
                         'TotalBitrate=COALESCE(TotalBitrate, ?) WHERE Id=?',
                         (video.get('Width'), video.get('Height'), data.get('bitrate'), item_id))
        # Older Emby versions may not expose both fields.  Fill only missing
        # or zero values; do not overwrite a media server's existing facts.
        columns = {row[1] for row in conn.execute('PRAGMA table_info(MediaItems)')}
        for column, field in (('Size', 'size'), ('RunTimeTicks', 'duration_ticks')):
            value = data.get(field)
            if column in columns and type(value) is int and 0 < value < 2**63:
                conn.execute(f'UPDATE MediaItems SET {column}=? WHERE Id=? AND ({column} IS NULL OR {column}=0)',
                             (value, item_id))
        return {'ok': True, 'streams': len(tracks), 'chapters': len(chapters), 'reason': ''}

    def run(self, limit=0, dry_run=False, backup=True):
        """批量补齐；返回统计。``dry_run`` 只统计有多少条能补。

        ``recorder(item_id, path, reason)`` 存在时，逐条回报结果（成功传 ``''``）：
        批量补的时候"哪一条没补上、为什么"就落在失败清单里，而不是只剩 ``failed: 7``。
        """
        conn = self.connect()
        try:
            if not self.schema_ok(conn):
                return {'ok': False, 'message': '这个库不是 Emby 4.x 的 library.db（缺 MediaStreams2）。'}
            pending = self.targets(conn, limit)
            stats = {'ok': True, 'candidates': len(pending), 'injected': 0, 'streams': 0,
                     'chapters': 0, 'no_sidecar': 0, 'failed': 0, 'backup': '', 'failures': []}
            if dry_run:
                ready = sum(1 for item_id, path in pending
                            if self.inject_one_ready(item_id, path))
                stats['injected'] = ready
                return stats
            if pending and backup:
                stats['backup'] = self.backup()
            for item_id, path in pending:
                try:
                    result = self.inject_one(conn, item_id,
                                             sidecar_path(path, self.emby_root, self.strm_root))
                except sqlite3.Error:
                    conn.rollback()
                    result = {'ok': False, 'reason': 'write_failed'}
                if not result['ok']:
                    if result['reason'] == 'sidecar_missing':
                        stats['no_sidecar'] += 1
                    else:
                        stats['failed'] += 1
                    if len(stats['failures']) < 50:      # 接口回包别被上千条失败撑爆
                        stats['failures'].append({'item_id': item_id, 'path': path,
                                                  'reason': result['reason']})
                    self._record(item_id, path, result['reason'])
                    continue
                conn.commit()
                stats['injected'] += 1
                stats['streams'] += result['streams']
                stats['chapters'] += result['chapters']
                self._record(item_id, path, '')
            return stats
        finally:
            conn.close()

    def _record(self, item_id, path, reason):
        if not self.recorder:
            return
        try:
            self.recorder(item_id, path, reason)
        except Exception:
            pass

    def inject_one_ready(self, item_id, path):
        import os
        return os.path.exists(sidecar_path(path, self.emby_root, self.strm_root))

