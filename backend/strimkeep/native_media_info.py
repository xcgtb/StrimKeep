"""Background, opt-in native Emby media probing; never writes Emby's database."""
import asyncio
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from .network import emby_request
from .store import digest


class NativeMediaInfo:
    # 首次入库的大文件要等 Emby 扫描 + ffprobe 拉 115 直链，重试窗口给足。
    MAX_ATTEMPTS = 8
    # Emby 端的媒体信息和本地 ffprobe 是两件事：本地值只服务本项目自己的展示，
    # Emby 的流必须由 Emby 自己探测出来。所以 Emby 这一半单独给耐心、单独退避，
    # 不参与上面的 state 判定（本地有值就 done，不再因为 4K 源超时被判成失败）。
    EMBY_MAX_ATTEMPTS = 6
    EMBY_BACKOFF_START = 600
    EMBY_BACKOFF_CAP = 3600
    # 同一条事件在这个窗口内只写一次（见 _event）。
    NOTICE_SECONDS = 300
    # 媒体信息缺口的定期汇总窗口：缺口不报就等于没人知道（0.9.48 那次的教训）。
    REPORT_SECONDS = 6 * 3600

    def __init__(self, delivery):
        self.delivery = delivery
        self.engine = delivery.engine
        self.store = delivery.store
        self.task = None
        self.last_sweep = 0.0
        self.last_purge = 0.0
        self.last_report = 0.0
        self._seen = {}
        # 这张表已由 store 启动 schema 统一创建；这里保留幂等 DDL，因为该类会被
        # 单独实例化（测试与后台 worker），不能假定 store 的 schema 一定先跑过。
        with self.store.db:
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS native_media_info_queue(
                job_id INTEGER NOT NULL, item_id TEXT NOT NULL, target TEXT NOT NULL,
                connection TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0, next_due REAL NOT NULL DEFAULT 0,
                emby_state TEXT NOT NULL DEFAULT 'pending',
                emby_attempts INTEGER NOT NULL DEFAULT 0, emby_next_due REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(job_id,item_id))''')
        self._ensure_emby_columns()

    def _ensure_emby_columns(self):
        """存量库补上「Emby 端确认」三列（SQLite 没有 ADD COLUMN IF NOT EXISTS）。

        0.9.48：本地探测结果直接 done 之后，全项目唯一能让 Emby 自己去探测的
        PlaybackInfo 被短路，补扫又「有 sidecar 就跳过」，现场 70 条 STRM 在 Emby
        里恒为 0 条流。把「Emby 是否已确认」独立成列，本地这一半照旧立刻收敛，
        Emby 那一半由 probe_emby() 继续追。
        """
        columns = {row[1] for row in self.store.db.execute(
            'PRAGMA table_info(native_media_info_queue)').fetchall()}
        for name, ddl in (('emby_state', "TEXT NOT NULL DEFAULT 'pending'"),
                          ('emby_attempts', 'INTEGER NOT NULL DEFAULT 0'),
                          ('emby_next_due', 'REAL NOT NULL DEFAULT 0')):
            if name in columns:
                continue
            with self.store.db:
                self.store.db.execute(
                    'ALTER TABLE native_media_info_queue ADD COLUMN %s %s' % (name, ddl))

    def write_sidecar(self, target, item, source_name=None):
        """Write only our own sidecars, after checking the current STRM identity."""
        relative = Path(target['relative'])
        root = Path(self.engine.root).resolve()
        if relative.is_absolute() or relative.suffix.lower() != '.strm' or '..' in relative.parts:
            raise ValueError('STRM 路径不安全')
        strm = root / relative
        if not strm.resolve().is_relative_to(root) or strm.is_symlink() or not strm.is_file():
            raise ValueError('STRM 文件不存在或路径不安全')
        if any(p.is_symlink() for p in strm.parents if p != root and root in p.parents):
            raise ValueError('STRM 上级目录存在链接')
        parsed = urlsplit(strm.read_text(encoding='utf-8').strip())
        path = parsed.path
        if target['pid'].startswith('share/'):
            share_id = target['pid'][len('share/'):]
            row = self.store.db.execute(
                'SELECT strm_path,revoked FROM share_strms WHERE id=?', (share_id,)).fetchone()
            if not row or row['revoked'] or row['strm_path'] != relative.as_posix():
                raise ValueError('分享 STRM 身份或路径已变化')
            # 地址形态是 /play115/share/<sid>/…，不能按网盘的 pickcode 去核。
            from .share_service import share_play_path_matches
            if not share_play_path_matches(path, target['pid'][len('share/'):]):
                raise ValueError('STRM 播放身份已变化')
        elif path == '/play/' + target['pid'] or path.startswith('/play/' + target['pid'] + '/'):
            pass
        elif path.startswith('/play115/'):
            # /play115/<pickcode>/<file_id>/<sha1>/<size>/<name>：没有播放 token，
            # 用 playbacks 里的 file_id 核对，避免把新格式一律判成「身份已变化」。
            parts = [part for part in path.split('/') if part]
            file_id = parts[2] if len(parts) >= 3 else ''
            row = self.store.db.execute('SELECT file_id FROM playbacks WHERE id=?',
                                        (target['pid'],)).fetchone()
            if not (row and file_id and str(row['file_id']) == file_id):
                raise ValueError('STRM 播放身份已变化')
        else:
            raise ValueError('STRM 播放身份已变化')
        sources = item.get('MediaSources') or []
        matching = [source for source in sources if self.delivery.matches({'Path':None,'MediaSources':[source]}, target)]
        if len(matching) == 1:
            source = matching[0]
        elif len(sources) <= 1 and self.delivery.matches(item,target):
            source = sources[0] if sources else {}
        else:
            raise ValueError('媒体版本无法唯一匹配')
        streams = source.get('MediaStreams') or item.get('MediaStreams') or []
        if not any(s.get('Type') == 'Video' for s in streams):
            raise ValueError('没有可保存的视频流')
        # 只在同一 STRM 的实测宽高恰好与 Emby 返回值完全相反时纠正；
        # 没有实测证据时，不能凭横屏/竖屏猜测。
        try:
            measured = self.probe_item(target, item)
        except sqlite3.OperationalError:
            # Older databases may not yet have the optional share media column.
            measured = None
        videos = [s for s in streams if isinstance(s, dict) and s.get('Type') == 'Video']
        measured_videos = [s for s in (measured or {}).get('MediaStreams', []) if s.get('Type') == 'Video']
        if len(videos) == len(measured_videos) == 1:
            actual = measured_videos[0]
            width, height = actual.get('Width'), actual.get('Height')
            video = videos[0]
            if width and height and width != height and (video.get('Width'), video.get('Height')) == (height, width):
                streams = [dict(s, Width=width, Height=height) if s is video else s for s in streams]
                video = next(s for s in streams if isinstance(s, dict) and s.get('Type') == 'Video')
            if actual.get('ExtendedVideoType') == 'DolbyVision' and video.get('ExtendedVideoType') in (None, 'None'):
                streams = [dict(s, ExtendedVideoType='DolbyVision') if s is video else s for s in streams]
        allowed = {'Type','Index','Codec','CodecTag','Language','DisplayLanguage','DisplayTitle','Title',
            'Profile','Level','Width','Height','AspectRatio','BitRate','BitDepth','PixelFormat',
            'AverageFrameRate','RealFrameRate','ColorSpace','ColorTransfer','ColorPrimaries','VideoRange',
            'ExtendedVideoType','ExtendedVideoSubType','ExtendedVideoSubTypeDescription','Channels',
            'ChannelLayout','SampleRate','IsDefault','IsForced','IsHearingImpaired','IsExternal',
            'IsTextSubtitleStream','SupportsExternalStream','TimeBase','RefFrames','IsInterlaced'}
        chapters = [{k:v for k,v in c.items() if k in ('Name','StartPositionTicks','EndPositionTicks')}
                    for c in (item.get('Chapters') or []) if isinstance(c,dict)]
        # Emby can return a STRM MediaSource with Size=0 and no duration even
        # when the registered source and ffprobe have both facts.  Only use
        # measured values tied to this exact STRM as a fallback.
        measured = measured or {}
        data = {'version':1, 'playback_id':target['pid'],
                'container':source.get('Container') or item.get('Container'),
                'size':source.get('Size') or item.get('Size') or measured.get('Size'),
                'duration_ticks':source.get('RunTimeTicks') or item.get('RunTimeTicks') or measured.get('RunTimeTicks'),
                'bitrate':source.get('Bitrate') or item.get('Bitrate') or measured.get('Bitrate'), 'chapters':chapters,
                'streams':[{k:v for k,v in s.items() if k in allowed} for s in streams if isinstance(s,dict)]}
        if source_name:
            data['source'] = source_name
        content = (json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
        path = strm.with_name(strm.stem+'-media-info.json')
        if path.is_symlink():raise ValueError('媒体信息文件为链接')
        if path.exists():
            # 0.9.54：归属只看文件内容（契约：身份写在文件里），不再查 media_info_outputs。
            # 自家 sidecar 必然带 version=1 与写入时的 playback_id；别人写的、或被改成
            # 非法 JSON 的一律拒绝覆盖（用户自己改坏的 sidecar 不会被我们悄悄吃掉）。
            try:
                current = json.loads(path.read_text(encoding='utf-8') or '{}')
            except (OSError, ValueError):
                current = None
            if not (isinstance(current, dict) and current.get('version') == 1
                    and current.get('playback_id') == target['pid']):
                raise ValueError('媒体信息文件已存在且归属或内容无法确认')
        from .fsutil import UnsafePath, atomic_write, write_once
        try:
            if path.exists():
                atomic_write(path, content, root)
            elif not write_once(path, content, root):
                raise ValueError('媒体信息文件在写入期间被其他进程创建')
        except UnsafePath as exc:
            raise ValueError('媒体信息文件路径不安全') from exc
        # 追更：sidecar 一写好就立刻写进 Emby，不必等定时任务（定时只当兜底）。
        self._push_to_emby(item, path)

    def _push_to_emby(self, item, sidecar_path):
        """把刚写好的 sidecar 立即写进 Emby 的 library.db；任何异常都不影响提取流程。

        结果（成功/失败原因）同时写进失败清单：失败时能直接看出是哪一部、为什么。
        """
        try:
            from .emby_media_inject import emby_strm_mount, inject_sidecar, library_db
            from .media_info_log import record as record_failure
            item_id = str(item.get('Id') or '')
            if not item_id.isdigit():
                return
            db_path = library_db(self.store)
            if not db_path:
                return          # 用户没挂 Emby 数据目录：只生成 sidecar，不猜路径
            state, _streams, reason = inject_sidecar(db_path, int(item_id), str(sidecar_path),
                                                     str(self.engine.root), emby_strm_mount(self.store))
            if state == 'injected':
                self._event('媒体信息已即时写入 Emby（条目 %s）。' % item_id)
                record_failure(self.store, item_id, str(sidecar_path), '')
            elif reason:
                record_failure(self.store, item_id, str(sidecar_path), reason)
        except Exception:
            pass

    def save_sidecar_safely(self, target, item, source_name=None):
        """Do not let a user-owned sidecar block Emby's native extraction."""
        try:
            self.write_sidecar(target, item, source_name)
            return True
        except ValueError as exc:
            if str(exc) != '媒体信息文件已存在且归属或内容无法确认':
                raise
            self._event('媒体信息 JSON 已存在且不属于本项目，已保留；Emby 媒体信息仍继续核查。')
            return False

    # ---- Emby 端媒体信息：和本地 ffprobe 分开记账 ----
    def _confirm_emby(self, relative):
        """Emby 已经有视频流了：这一半收工，后续补扫也不再管它。"""
        with self.store.db:
            cursor = self.store.db.execute(
                "UPDATE native_media_info_queue SET emby_state='confirmed' "
                "WHERE json_extract(target,'$.relative')=? AND emby_state!='confirmed'", (relative,))
        return bool(cursor.rowcount)

    def _defer_emby(self, row, reason):
        """Emby 这一半没成：按 10 分钟起翻倍、封顶 1 小时退避，次数用完就认输。"""
        attempts = int(row['emby_attempts'] or 0) + 1
        state = 'gave_up' if attempts >= self.EMBY_MAX_ATTEMPTS else 'pending'
        delay = min(self.EMBY_BACKOFF_CAP, self.EMBY_BACKOFF_START * 2 ** (attempts - 1))
        with self.store.db:
            self.store.db.execute(
                'UPDATE native_media_info_queue SET emby_state=?,emby_attempts=?,emby_next_due=? '
                'WHERE job_id=? AND item_id=?',
                (state, attempts, time.time() + delay, row['job_id'], row['item_id']))
        if attempts == 1 or state == 'gave_up':
            self._event(f'任务 {row["job_id"]}：Emby 端媒体信息未取得（第 {attempts}/{self.EMBY_MAX_ATTEMPTS} 次，'
                        f'{str(reason)[:60]}），'
                        + ('已停止追踪，本地媒体信息不受影响。' if state == 'gave_up' else '稍后重试。'))

    @staticmethod
    def _video_streams(item):
        streams = item.get('MediaStreams') or [stream for source in (item.get('MediaSources') or [])
                                               for stream in source.get('MediaStreams', [])]
        return [stream for stream in streams if isinstance(stream, dict)]

    async def _emby_item(self, url, headers, item_id, target):
        """按 Id 取单个条目；路径或播放身份不符时返回 None（重试也不会变对）。"""
        response = await emby_request(self.delivery.http, 'GET', url + '/Items', headers=headers,
            params={'Ids': item_id, 'Fields': 'Path,MediaSources,MediaStreams,Chapters'}, timeout=15)
        response.raise_for_status()
        items = response.json().get('Items') or []
        if len(items) != 1 or not self.delivery.matches(items[0], target):
            return None
        return items[0]

    def _give_up_emby(self, row, message):
        with self.store.db:
            self.store.db.execute(
                "UPDATE native_media_info_queue SET emby_state='gave_up' WHERE job_id=? AND item_id=?",
                (row['job_id'], row['item_id']))
        self._event(f'任务 {row["job_id"]}：{message}')

    def enqueue(self, job_id, item, target, connection):
        if not (self.store.get('sync_config', {}) or {}).get('emby_media_info', False):
            return
        item_id = str(item.get('Id') or '')
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,80}', item_id):
            return
        with self.store.db:
            cursor = self.store.db.execute(
                "INSERT INTO native_media_info_queue(job_id,item_id,target,connection) VALUES(?,?,?,?) "
                "ON CONFLICT(job_id,item_id) DO UPDATE SET target=excluded.target,connection=excluded.connection,"
                "state='queued',attempts=0,next_due=0,"
                "emby_state=CASE WHEN native_media_info_queue.emby_state='confirmed' THEN 'confirmed'"
                " ELSE 'pending' END,"
                "emby_attempts=CASE WHEN native_media_info_queue.emby_state='confirmed'"
                " THEN native_media_info_queue.emby_attempts ELSE 0 END,"
                "emby_next_due=CASE WHEN native_media_info_queue.emby_state='confirmed'"
                " THEN native_media_info_queue.emby_next_due ELSE 0 END "
                "WHERE native_media_info_queue.state='done' AND excluded.job_id=0",
                (job_id, item_id, json.dumps(target, ensure_ascii=False), connection))
        return bool(cursor.rowcount)

    def probe_item(self, target, item):
        """本项目自己探测过的媒体信息，不把文件名推测当作探测值。

        分享库看 share_strms.media，网盘库看 organized_files.metadata —— 整理阶段
        已经 ffprobe 过每个视频，媒体信息不该再等 Emby 去拉 115 直链（4K 源经常超时）。
        """
        pid = str(target.get('pid') or '')
        media = None
        source_size = None
        if pid.startswith('share/'):
            try:
                row = self.store.db.execute(
                    'SELECT media,size FROM share_strms WHERE id=? AND strm_path=? AND revoked=0',
                    (pid[6:], target['relative'])).fetchone()
            except sqlite3.OperationalError:
                row = self.store.db.execute(
                    'SELECT media FROM share_strms WHERE id=? AND strm_path=? AND revoked=0',
                    (pid[6:], target['relative'])).fetchone()
            try:
                media = json.loads(row['media'] or '{}') if row else {}
                source_size = row['size'] if row and 'size' in row.keys() else None
            except (ValueError, TypeError):
                return None
        else:
            for row in self.store.db.execute(
                    'SELECT file_id,metadata FROM organized_files WHERE relative_path=?',
                    (target['relative'],)).fetchall():
                try:
                    candidate = (json.loads(row['metadata'] or '{}') or {}).get('media')
                except (ValueError, TypeError, AttributeError):
                    continue
                if isinstance(candidate, dict) and candidate:
                    media = candidate
                    # The archive registry is the ownership evidence for the
                    # original cloud file; do not use a filename-derived size.
                    try:
                        account = self.engine.index.account({})
                        registered = self.store.db.execute(
                            'SELECT source FROM output_registry WHERE account_id=? AND file_id=? AND relative_path=? AND active=1',
                            (account, row['file_id'], target['relative'])).fetchone()
                        source = json.loads(registered['source'] or '{}') if registered else {}
                        if str(source.get('id') or '') == str(row['file_id']):
                            source_size = source.get('size')
                    except Exception:
                        source_size = None
                    break
        if media is None:
            return None
        if (not isinstance(media, dict) or media.get('estimated') or not media.get('probe_revision')
                or not isinstance(media.get('codec'), str) or not media['codec']
                or not isinstance(media.get('width'), (int, float)) or media['width'] <= 0):
            return None
        video = {'Type': 'Video', 'Codec': media['codec'], 'Width': media['width']}
        if media.get('dynamic_range') in ('Dolby Vision', 'Dolby Vision HDR'):
            video['ExtendedVideoType'] = 'DolbyVision'
        for source, dest in (('height', 'Height'), ('bit_depth', 'BitDepth'),
                             ('fps', 'AverageFrameRate'), ('video_level', 'Level')):
            if isinstance(media.get(source), (int, float)):
                video[dest] = media[source]
        for source, dest in (('video_profile', 'Profile'), ('pixel_format', 'PixelFormat'),
                             ('display_aspect_ratio', 'AspectRatio'), ('color_primaries', 'ColorPrimaries'),
                             ('color_space', 'ColorSpace'), ('color_transfer', 'ColorTransfer'),
                             ('video_codec_tag', 'CodecTag'), ('video_title', 'Title')):
            if isinstance(media.get(source), str) and media[source] and media[source].lower() not in ('unknown', 'n/a'):
                video[dest] = media[source]
        raw_video_rate = media.get('video_bit_rate')
        if str(raw_video_rate or '').isdigit() and int(raw_video_rate) > 0:
            video['BitRate'] = int(raw_video_rate)
        elif isinstance(media.get('bit_rate'), (int, float)) and media['bit_rate'] > 0:
            # Older cache records contain only total bitrate; preserve their
            # established behaviour until the source can be probed again.
            video['BitRate'] = media['bit_rate']
        if media.get('field_order') in ('progressive', 'tt', 'bb', 'tb', 'bt'):
            video['IsInterlaced'] = media['field_order'] != 'progressive'
        streams = [video]
        for audio in media.get('audio') or []:
            if not isinstance(audio, dict) or not isinstance(audio.get('codec'), str):
                continue
            entry = {'Type': 'Audio', 'Codec': audio['codec']}
            for source, dest in (('channels', 'Channels'), ('language', 'Language'),
                                 ('profile', 'Profile'), ('channel_layout', 'ChannelLayout')):
                if isinstance(audio.get(source), (int, float, str)):
                    entry[dest] = audio[source]
            for source, dest in (('sample_rate', 'SampleRate'), ('bit_rate', 'BitRate'), ('bit_depth', 'BitDepth')):
                value = audio.get(source)
                if str(value or '').isdigit() and int(value) > 0:
                    entry[dest] = int(value)
            if isinstance(audio.get('title'), str) and audio['title']:
                entry['Title'] = audio['title']
            streams.append(entry)
        from decimal import Decimal, InvalidOperation
        duration_ticks = None
        try:
            duration = Decimal(str(media.get('duration') or '0'))
            if duration.is_finite() and 0 < duration < Decimal('315360000'):
                duration_ticks = int(duration * 10000000)
        except (InvalidOperation, ValueError, TypeError):
            pass
        size = int(source_size) if type(source_size) in (int, float) and 0 < source_size <= 2**53 and source_size == int(source_size) else None
        return {**item, 'MediaStreams': streams, 'MediaSources': [],
                'Size': size, 'RunTimeTicks': duration_ticks, 'Bitrate': media.get('bit_rate')}

    async def tick(self):
        if not (self.store.get('sync_config', {}) or {}).get('emby_media_info', False):
            return
        row = self.store.db.execute("SELECT * FROM native_media_info_queue WHERE state='queued' AND next_due<=? ORDER BY job_id,item_id LIMIT 1", (time.time(),)).fetchone()
        if not row:
            return
        connection = self.delivery.connection()
        if not connection or digest('|'.join(connection)) != row['connection']:
            self._finish(row, 'failed', 'Emby 连接已变化，未执行媒体信息提取。')
            return
        url, key = connection
        headers = {'X-Emby-Token':key}
        try:
            response = await emby_request(self.delivery.http, 'GET', url + '/Items', headers=headers,
                params={'Ids':row['item_id'], 'Fields':'Path,MediaSources,MediaStreams,Chapters'}, timeout=15)
            response.raise_for_status()
            items = response.json().get('Items') or []
            target = json.loads(row['target'])
            if len(items) != 1 or not self.delivery.matches(items[0], target):
                self._finish(row, 'failed', '条目路径或播放身份已变化，未提取媒体信息。')
                return
            streams = items[0].get('MediaStreams') or [s for source in items[0].get('MediaSources',[]) for s in source.get('MediaStreams',[])]
            if any(s.get('Type') == 'Video' for s in streams):
                saved = self.save_sidecar_safely(target, items[0])
                self._confirm_emby(target['relative'])
                self._finish(row, 'done', 'Emby 已有媒体信息，' + ('已保存媒体信息 JSON。' if saved else '已有媒体信息文件保留未覆盖。'))
                return
            # 本地优先：整理阶段已经 ffprobe 过这个视频，本项目这一半直接落盘收工，
            # 不再等 Emby 拉 115 直链（4K 源常常撑不过重试窗口）。
            # 但 Emby 自己的媒体信息只能由 Emby 探测出来，所以这一行仍然留着
            # emby_state='pending'，交给 probe_emby() 继续追——0.9.1 在这里直接收工，
            # Emby 侧就一直空着（0.9.48 修）。
            probed = self.probe_item(target, items[0])
            if probed:
                saved = self.save_sidecar_safely(target, probed, source_name='ffprobe')
                self._finish(row, 'done', '本地探测已有媒体信息，'
                             + ('已保存媒体信息 JSON。' if saved else '已有媒体信息文件保留未覆盖。'))
                return
            response = await emby_request(self.delivery.http, 'GET', url + '/Items/' + row['item_id'] + '/PlaybackInfo', headers=headers, timeout=60)
            response.raise_for_status()
            sources = response.json().get('MediaSources') or []
            streams = [s for source in sources for s in source.get('MediaStreams',[])]
            if not any(s.get('Type') == 'Video' for s in streams):
                raise ValueError('未取得视频流')
            check = await emby_request(self.delivery.http, 'GET', url + '/Items', headers=headers,
                params={'Ids':row['item_id'], 'Fields':'Path,MediaSources,MediaStreams,Chapters'}, timeout=15)
            check.raise_for_status()
            items = check.json().get('Items') or []
            if len(items) != 1 or not self.delivery.matches(items[0], target):
                raise ValueError('媒体信息回读未确认')
            saved = items[0].get('MediaStreams') or [s for source in items[0].get('MediaSources',[]) for s in source.get('MediaStreams',[])]
            if not any(s.get('Type') == 'Video' for s in saved):
                raise ValueError('媒体信息尚未保存')
            saved = self.save_sidecar_safely(target, items[0])
            self._confirm_emby(target['relative'])
            self._finish(row, 'done', 'Emby 媒体信息已提取，' + ('已保存媒体信息 JSON。' if saved else '已有媒体信息文件保留未覆盖。'))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempts = row['attempts'] + 1
            stale = isinstance(exc, ValueError) and any(text in str(exc) for text in (
                'STRM 播放身份已变化', '分享 STRM 身份或路径已变化', 'STRM 文件不存在或路径不安全'))
            if stale:
                # 这条 STRM 已被洗版替换或撤销，重试也不会变对。
                self._finish(row, 'failed', 'STRM 已变化，跳过媒体信息：' + str(exc)[:60])
                return
            # 3 次（约 90 秒）对首次入库的大文件太短：Emby 要先扫描再用 ffprobe 拉 115 直链，
            # 4K 源常常超过这个窗口。放宽到 8 次、间隔 30s 起翻倍封顶 10 分钟，
            # 总计约 25 分钟的耐心，仍不合格才判失败。
            with self.store.db:
                self.store.db.execute('UPDATE native_media_info_queue SET attempts=?,next_due=?,state=? WHERE job_id=? AND item_id=?',
                    (attempts,time.time()+min(600,30*2**(attempts-1)),'failed' if attempts>=self.MAX_ATTEMPTS else 'queued',row['job_id'],row['item_id']))
            # 8 次退避重试每次都写一条，同样的内容会刷 8 条：只留第一次和最后一次。
            if attempts == 1 or attempts >= self.MAX_ATTEMPTS:
                self._event(f'任务 {row["job_id"]}：原生媒体信息提取未确认（第 {attempts}/{self.MAX_ATTEMPTS} 次，'
                            f'{type(exc).__name__}: {str(exc)[:80]}），'
                            + ('已停止重试。' if attempts>=self.MAX_ATTEMPTS else '稍后重试。'))

    def _event(self, message):
        """同一条事件在 NOTICE_SECONDS 内只写一次。

        一个任务可能包含几百个待探测条目：每个条目每次重试都会写一条**一模一样**
        的事件（实测单个任务写出约 4500 条，占掉事件表 2 万上限的 23%），
        把日志中心里真正有用的信息挤掉。这里按消息全文去重，不同消息不受影响。
        """
        now = time.time()
        if now - self._seen.get(message, 0.0) < self.NOTICE_SECONDS:
            return
        if len(self._seen) >= 500:
            self._seen.clear()
        self._seen[message] = now
        try:
            self.store.event('emby', message)
        except Exception:
            # 留痕失败不能反过来影响探测流程。
            pass

    def _finish(self, row, state, message):
        with self.store.db:
            self.store.db.execute('UPDATE native_media_info_queue SET state=? WHERE job_id=? AND item_id=?',(state,row['job_id'],row['item_id']))
        self._event(f'任务 {row["job_id"]}：{message}')

    async def probe_emby(self, limit=1):
        """追 Emby 端的媒体信息：本地 ffprobe 只是「本项目展示」的一半。

        唯一的触发点是 PlaybackInfo。条目刚入库时 Emby 还没有解析出视频流，
        `POST /Library/Media/Updated` 只会让它重新读元数据，不会替我们探测 STRM
        背后的 115 直链。旧代码靠 8 次、约 25 分钟的重试窗口等 Emby 补齐；0.9.1
        把这个窗口砍掉后，现场 70 条 STRM 在 Emby 里一直是 0 条流。

        这里用独立的 emby_state / emby_attempts / emby_next_due 记账：每轮只推一条，
        拿不到就按 10 分钟起翻倍、封顶 1 小时退避，最多 EMBY_MAX_ATTEMPTS 次。
        """
        if not (self.store.get('sync_config', {}) or {}).get('emby_media_info', False):
            return 0
        rows = self.store.db.execute(
            "SELECT * FROM native_media_info_queue WHERE state='done' AND emby_state='pending' "
            "AND emby_next_due<=? ORDER BY emby_next_due,rowid LIMIT ?",
            (time.time(), limit)).fetchall()
        pushed = 0
        for row in rows:
            connection = self.delivery.connection()
            if not connection or digest('|'.join(connection)) != row['connection']:
                self._give_up_emby(row, 'Emby 连接已变化，不再追 Emby 端媒体信息。')
                continue
            url, key = connection
            try:
                target = json.loads(row['target'])
            except ValueError:
                self._give_up_emby(row, 'Emby 目标记录损坏，不再追 Emby 端媒体信息。')
                continue
            if not isinstance(target, dict) or not target.get('relative'):
                self._give_up_emby(row, 'Emby 目标记录不完整，不再追 Emby 端媒体信息。')
                continue
            headers = {'X-Emby-Token': key}
            pushed += 1
            try:
                item = await self._emby_item(url, headers, row['item_id'], target)
                if item is None:
                    self._give_up_emby(row, 'Emby 条目路径或播放身份已变化，不再追媒体信息。')
                    continue
                if any(stream.get('Type') == 'Video' for stream in self._video_streams(item)):
                    # Emby 自己已经探好了（或上一次踢的 PlaybackInfo 已经生效）。
                    self.save_sidecar_safely(target, item)
                    self._confirm_emby(target['relative'])
                    continue
                # 踢一次：全项目唯一能让 Emby 去解析 STRM 背后直链的入口。
                response = await emby_request(self.delivery.http, 'GET',
                    url + '/Items/' + row['item_id'] + '/PlaybackInfo', headers=headers, timeout=60)
                response.raise_for_status()
                item = await self._emby_item(url, headers, row['item_id'], target)
                if item is not None and any(stream.get('Type') == 'Video'
                                            for stream in self._video_streams(item)):
                    self.save_sidecar_safely(target, item)
                    self._confirm_emby(target['relative'])
                    continue
                self._defer_emby(row, 'Emby 尚未解析出视频流')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._defer_emby(row, '%s: %s' % (type(exc).__name__, exc))
        return pushed

    def report(self, now=None):
        """定期把「Emby 端还差多少媒体信息」说出来。

        0.9.1~0.9.47 这段最贵的不是探测逻辑，而是**没人知道**：70 条 STRM 在 Emby
        里空了 24 小时，所有报表和通知都说「媒体信息已保存」。这里按 REPORT_SECONDS
        汇总一次，有缺口才写，缺口消失后窗口内自然安静。
        """
        now = time.time() if now is None else now
        if now - self.last_report < self.REPORT_SECONDS:
            return None
        self.last_report = now
        try:
            row = self.store.db.execute(
                "SELECT sum(emby_state='pending') p, sum(emby_state='gave_up') g, "
                "count(*) n FROM native_media_info_queue").fetchone()
        except Exception:
            return None
        pending = int(row['p'] or 0)
        gave_up = int(row['g'] or 0)
        total = int(row['n'] or 0)
        if not pending and not gave_up:
            return None
        self.store.event('emby', f'媒体信息核对：Emby 端仍有 {pending} 条待补齐、{gave_up} 条已停止追踪'
                                 f'（受管 STRM 共 {total} 条）；本地媒体信息不受影响。')
        return pending, gave_up

    async def loop(self):
        while not self.engine.closing:
            try:
                await self.tick()
                await self.probe_emby()
                self.report()
                pending = self.store.db.execute(
                    "SELECT count(*) FROM native_media_info_queue WHERE state IN ('queued','running')"
                ).fetchone()[0]
                if time.time() - self.last_purge >= 3600:
                    self.last_purge = time.time()
                    await asyncio.to_thread(self.purge_orphan_sidecars)
                if not pending and time.time() - self.last_sweep >= 300:
                    await self.sweep_missing()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.store.event('emby','媒体信息后台队列暂时不可用，稍后重试。')
            await asyncio.sleep(2)

    def purge_orphan_sidecars(self):
        """删掉 STRM 已经不存在的媒体信息 JSON。

        洗版替换、清理、撤销都会让 STRM 消失，以前只删 STRM 不删 sidecar，
        现场积了 58 个孤儿；同名新文件还会把它们误当成「已有媒体信息」。
        0.9.54 起只剩一条判定：**带本项目字段（version=1 + playback_id）且 STRM 已不在**
        就清掉——归属看内容，不再依赖 media_info_outputs 登记表。别人的 JSON 不碰。
        """
        root = Path(self.engine.root)
        removed = 0
        # 看起来就是本项目写的（带 version / playback_id），而 STRM 没了：清掉。
        # 留着不只是垃圾——同名新文件会被它挡住，媒体信息永远写不进去。
        for sidecar in root.rglob('*-media-info.json'):
            if not sidecar.is_file():
                continue
            if sidecar.with_name(sidecar.name.replace('-media-info.json', '.strm')).is_file():
                continue
            try:
                data = json.loads(sidecar.read_text(encoding='utf-8') or '{}')
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get('version') != 1 or 'playback_id' not in data:
                continue
            try:
                sidecar.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            self.store.event('emby', f'媒体信息 JSON 清理：{removed} 个 STRM 已不存在的 sidecar 已删除。')
        return removed

    async def sweep_missing(self):
        """全库补扫：受管 STRM 缺 sidecar 的补入队（对齐的全库主动生成）。

        每 5 分钟最多跑一次、单次最多 20 条；只收「playbacks 里有身份、Emby 里
        找得到条目」的文件，其余（待整理/无身份）留给正常入库流程。
        """
        if not (self.store.get('sync_config', {}) or {}).get('emby_media_info', False):
            return 0
        connection = self.delivery.connection()
        if not connection:
            return 0
        self.last_sweep = time.time()
        url, key = connection
        # 只枚举电影和单集，分页完整读取；截断或格式错误时停止本轮，
        # 避免固定取前 3000 条导致新入库的分享永远无法补扫。
        items = []
        page_size = 500
        for start in range(0, 100000, page_size):
            response = await emby_request(self.delivery.http, 'GET', url + '/Items',
                headers={'X-Emby-Token': key},
                params={'Recursive': 'true', 'IncludeItemTypes': 'Movie,Episode', 'StartIndex': start,
                        'Limit': page_size, 'Fields': 'Path,MediaSources,MediaStreams'}, timeout=30)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
                raise ValueError('Emby 媒体信息补扫结果不完整')
            page = data['Items']
            if not all(isinstance(item, dict) for item in page):
                raise ValueError('Emby 媒体信息补扫条目无效')
            items.extend(page)
            total = data.get('TotalRecordCount')
            if isinstance(total, int) and len(items) >= total:
                break
            if len(page) < page_size:
                if isinstance(total, int) and len(items) < total:
                    raise ValueError('Emby 媒体信息补扫结果被截断')
                break
        else:
            raise ValueError('Emby 媒体信息补扫条目超过上限')
        fingerprint = digest('|'.join(connection))
        queued = await asyncio.to_thread(self._sweep_files, items, fingerprint)
        if queued:
            self.store.event('emby', f'媒体信息补扫：为 {queued} 条缺失 sidecar 的受管 STRM 入队。')
        return queued

    @staticmethod
    def _entry_path(item):
        return str(item.get('Path') or '').replace('\\', '/').rstrip('/')

    @staticmethod
    def _path_matches(item, relative):
        """条目路径是否就是这条本地 STRM（重复条目收敛用）。"""
        paths = [item.get('Path')] + [source.get('Path') for source in (item.get('MediaSources') or [])
                                      if isinstance(source, dict)]
        for path in paths:
            if isinstance(path, str) and path.replace('\\', '/').rstrip('/').endswith('/' + relative):
                return True
        return False

    def _sweep_files(self, items, fingerprint, limit=20):
        """线程内执行：按播放身份建索引，再逐个本地 STRM 判断是否需要补扫。

        0.8.21：这里是原来的「每个 STRM × 每个 Emby 条目」双层循环，而且整段跑在
        事件循环上——现场一次补扫把循环堵了 16 秒（看门狗栈：loop → sweep_missing
        → matches）。索引建一次之后单文件是 O(1) 查表；文件遍历与数据库都放在工作
        线程里执行（`Store.db` 会返回该线程自己的连接）。
        """
        by_pid = {}
        plain = []
        for item in items:
            identifier = str(item.get('Id') or '')
            ids = self.delivery.play_ids(item)
            if not ids:
                # 没有播放入口的条目：旧语义靠「路径后缀」匹配，不能因为建索引就丢掉。
                plain.append(item)
                continue
            for pid in ids:
                by_pid.setdefault(pid, {})[identifier] = item
        root = Path(self.engine.root)
        queued = 0
        # Emby 这一半的记账：一次查完，避免对全库 STRM 逐个查库/UPDATE。
        emby_states = {row['rel']: row['emby_state'] for row in self.store.db.execute(
            "SELECT json_extract(target,'$.relative') AS rel, emby_state "
            "FROM native_media_info_queue").fetchall() if row['rel']}
        # 同名文件在另一个库已经有视频流：Emby 把这两条当同一部片（刷新时一起刷），
        # 本条自己永远不会被单独探测。现场 7 条就卡在这——继续催它只是白跑 6 次退避。
        video_by_name = {}
        for item in items:
            streams = item.get('MediaStreams') or [stream for source in (item.get('MediaSources') or [])
                                                   for stream in source.get('MediaStreams', [])]
            if not any(isinstance(stream, dict) and stream.get('Type') == 'Video' for stream in streams):
                continue
            name = self._entry_path(item).rsplit('/', 1)[-1]
            if name:
                video_by_name.setdefault(name, set()).add(str(item.get('Id') or ''))
        for strm in root.rglob('*.strm'):
            if queued >= limit:
                break
            has_sidecar = strm.with_name(strm.stem + '-media-info.json').is_file()
            rel = str(strm.relative_to(root))
            playbacks = self.store.db.execute(
                'SELECT id FROM playbacks WHERE relative_path=? AND revoked=0 LIMIT 2', (rel,)).fetchall()
            shares = self.store.db.execute(
                'SELECT id FROM share_strms WHERE strm_path=? AND revoked=0 LIMIT 2', (rel,)).fetchall()
            if len(playbacks) + len(shares) != 1:
                continue
            pid = playbacks[0]['id'] if playbacks else 'share/' + shares[0]['id']
            candidates = dict(by_pid.get(pid) or {})
            # 与老实现等价：带播放入口的条目只认 pid；没有入口的条目按路径后缀匹配。
            for item in plain:
                if self.delivery.matches(item, {'relative': rel, 'pid': pid}):
                    candidates[str(item.get('Id') or '')] = item
            if not candidates:
                continue
            if len(candidates) > 1:
                # 同一播放入口在 Emby 里可能对应多个条目（另一条 STRM 产线重复入库）。
                # 先按条目路径收敛；只剩一个就用它。
                same_path = {key: value for key, value in candidates.items()
                             if self._path_matches(value, rel)}
                if len(same_path) == 1:
                    candidates = same_path
                elif same_path and len({self._entry_path(value) for value in same_path.values()}) == 1:
                    # 路径完全相同的真重复：稳定取 Id 最小的那个，不再整条跳过。
                    chosen = min(same_path)
                    self.store.event('emby', f'媒体信息补扫：{rel} 在 Emby 有 {len(same_path)} 个同路径条目，选用 {chosen}。')
                    candidates = {chosen: same_path[chosen]}
                else:
                    # 路径各不相同：可能来自另一个库，不猜。
                    continue
            item = next(iter(candidates.values()))
            streams = item.get('MediaStreams') or [stream for source in item.get('MediaSources', [])
                                                   for stream in source.get('MediaStreams', [])]
            if has_sidecar:
                # sidecar 在：剩下的只是 Emby 那一半。
                file_name = rel.rsplit('/', 1)[-1]
                if any(isinstance(stream, dict) and stream.get('Type') == 'Video'
                       for stream in streams):
                    # 本地有 sidecar、Emby 也有视频流：两边都齐了，顺手把存量行标成已确认
                    # （含以前 gave_up 的：Emby 后来自己探出来了也算齐）。
                    if rel in emby_states and emby_states[rel] != 'confirmed':
                        self._confirm_emby(rel)
                elif video_by_name.get(file_name, set()) - {str(item.get('Id') or '')}:
                    # 另一个库里有同名条目且已有视频流：Emby 把这两条当同一部片（刷新时
                    # 一起刷），本条自己永远得不到探测。继续追只是白跑退避，标已确认并留痕。
                    if self._confirm_emby(rel):
                        self.store.event('emby', '媒体信息：%s 在另一个库的同名条目已有视频流，'
                                                 '本条不再追 Emby 端探测。' % file_name)
                elif rel not in emby_states:
                    # 一条记账行都没有（被清理或早于本功能入库）：补一条，让本地/Emby
                    # 两半都重走一次——没有记账行就没人去追 Emby 那一半。
                    if self.enqueue(0, item, {'relative': rel, 'pid': pid}, fingerprint):
                        queued += 1
                # 有记账行的一律交给 probe_emby()：退避归它管，补扫不去重置，
                # 也不新增队列行（0.9.16 之前重新入队才堆出 234 组重复）。
            elif self.store.db.execute(
                    "SELECT 1 FROM native_media_info_queue WHERE json_extract(target,'$.relative')=? "
                    "AND state IN ('queued','running')", (rel,)).fetchone():
                continue
            elif self.enqueue(0, item, {'relative': rel, 'pid': pid}, fingerprint):
                queued += 1
        return queued
