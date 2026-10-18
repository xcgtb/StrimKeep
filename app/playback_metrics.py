"""Persistent, conservative playback observations from Emby's authenticated /Sessions.

Only adjacent, advancing, unpaused observations count. Seeks, collection gaps,
restarts and missing stop events never turn elapsed wall time into watched time.
"""
import hashlib
import math
import re
import threading
import time
from datetime import datetime, timezone
from app import engine
from app.cloud import store

_STOP = threading.Event()
_WAKE = threading.Event()
_LOCK = threading.RLock()
_THREAD = None
INTERVAL = 15
MAX_GAP = 45
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS playback_collection (
 scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, started REAL NOT NULL,
 last_poll REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', gaps INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS playback_observations (
 scope TEXT NOT NULL, session TEXT NOT NULL, user_id TEXT NOT NULL, item_id TEXT NOT NULL,
 segment TEXT NOT NULL, position REAL NOT NULL, paused INTEGER NOT NULL, rate REAL NOT NULL,
 observed REAL NOT NULL, PRIMARY KEY(scope,session));
CREATE TABLE IF NOT EXISTS playback_segments (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
 item_id TEXT NOT NULL, title TEXT NOT NULL, started REAL NOT NULL, last_seen REAL NOT NULL,
 seconds REAL NOT NULL DEFAULT 0, state TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS playback_segments_scope ON playback_segments(scope,last_seen DESC);
CREATE TABLE IF NOT EXISTS playback_daily (
 scope TEXT NOT NULL, day TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
 item_id TEXT NOT NULL, title TEXT NOT NULL, seconds REAL NOT NULL, plays INTEGER NOT NULL,
 PRIMARY KEY(scope,day,user_id,item_id));
CREATE TABLE IF NOT EXISTS playback_events (
 id INTEGER PRIMARY KEY, scope TEXT NOT NULL, segment TEXT NOT NULL, event TEXT NOT NULL,
 ts REAL NOT NULL, seconds REAL NOT NULL);
CREATE INDEX IF NOT EXISTS playback_events_scope ON playback_events(scope,id DESC);
'''


def _schema(con):
    for sql in SCHEMA.split(';'):
        if sql.strip():
            con.execute(sql)


def scope():
    return hashlib.sha256((str(engine.EMBY_HOST).rstrip('/')+'\0'+str(engine.EMBY_KEY)).encode()).hexdigest()


def configured():
    return bool(engine.EMBY_HOST and engine.EMBY_KEY)


def _state(con, sid):
    _schema(con)
    con.execute('INSERT OR IGNORE INTO playback_collection(scope,started) VALUES(?,?)', (sid,time.time()))
    return con.execute('SELECT enabled,started,last_poll,error,gaps FROM playback_collection WHERE scope=?',(sid,)).fetchone()


def configure(enabled):
    if type(enabled) is not bool:
        raise ValueError('enabled 必须是布尔值')
    if enabled and not configured():
        raise ValueError('请先保存 Emby 服务地址与密钥')
    sid = scope()
    with _LOCK, store.db() as con:
        state = _state(con,sid)
        if bool(state[0]) != enabled:
            con.execute('UPDATE playback_collection SET enabled=?,error=? WHERE scope=?',(int(enabled),'',sid))
            con.execute('DELETE FROM playback_observations WHERE scope=?',(sid,))
            con.execute("UPDATE playback_segments SET state='interrupted' WHERE scope=? AND state='active'",(sid,))
            if enabled:
                if not state[2]:
                    con.execute('UPDATE playback_collection SET started=? WHERE scope=?',(time.time(),sid))
                con.execute('UPDATE playback_collection SET gaps=gaps+1 WHERE scope=? AND last_poll>0',(sid,))
    _WAKE.set()
    return summary()


def _number(value, default=None):
    if type(value) not in (float,int) or not math.isfinite(value):
        return default
    return float(value)


def _normalize(rows):
    if not isinstance(rows,list) or len(rows)>500:
        raise ValueError('invalid Emby sessions')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row,dict):
            raise ValueError('invalid Emby session')
        item, play = row.get('NowPlayingItem'), row.get('PlayState')
        if not item:
            continue
        if not isinstance(item,dict) or not isinstance(play,dict):
            raise ValueError('invalid playback state')
        if item.get('Type') not in ('Movie','Episode','Video'):
            continue
        session, user, iid = (str(row.get('Id','')),str(row.get('UserId','')),str(item.get('Id','')))
        if not user:
            continue  # Anonymous/DLNA sessions cannot establish distinct viewers.
        position = _number(play.get('PositionTicks'))
        paused = play.get('IsPaused')
        if not all(_ID.fullmatch(v) for v in (session,user,iid)) or position is None or position<0 or type(paused) is not bool or session in seen:
            raise ValueError('incomplete playback identity')
        rate = _number(play.get('PlaybackRate'),1)
        if not .25<=rate<=4:
            raise ValueError('invalid playback rate')
        seen.add(session)
        title = str(item.get('SeriesName') or item.get('Name') or '未命名影片')[:200]
        rank_id = str(item.get('SeriesId') or iid)
        if not _ID.fullmatch(rank_id):
            raise ValueError('invalid series identity')
        result.append({'session':session,'user':user,'user_name':str(row.get('UserName') or 'Emby 用户')[:100],
                       'item':iid, 'rank_id':rank_id,'title':title,'position':position/10000000,
                       'paused':paused,'rate':rate})
    return result


def _event(con,sid,segment,event,now,seconds=0):
    con.execute('INSERT INTO playback_events(scope,segment,event,ts,seconds) VALUES(?,?,?,?,?)',(sid,segment,event,now,seconds))


def observe(sid, rows, now=None):
    """Commit one validated server sample atomically; invalid/old samples change nothing."""
    now = time.time() if now is None else now
    values = _normalize(rows)
    with _LOCK, store.db() as con:
        enabled, _, previous_poll, _, _ = _state(con,sid)
        if sid!=scope() or not enabled or now<=previous_poll:
            return False
        if previous_poll and now-previous_poll>MAX_GAP:
            con.execute('UPDATE playback_collection SET gaps=gaps+1 WHERE scope=?',(sid,))
        active = {r[0]:r for r in con.execute('SELECT session,user_id,item_id,segment,position,paused,rate,observed FROM playback_observations WHERE scope=?',(sid,))}
        for value in values:
            session = value['session']
            old = active.pop(session,None)
            same = old and old[1:3]==(value['user'],value['item']) and 0<now-old[7]<=MAX_GAP
            if not same:
                if old:
                    con.execute("UPDATE playback_segments SET state='interrupted' WHERE id=?",(old[3],))
                    _event(con,sid,old[3],'gap' if old[1:3]==(value['user'],value['item']) else 'stop',now)
                segment = store.ident()
                con.execute('INSERT INTO playback_segments VALUES(?,?,?,?,?,?,?,?,0,?)',
                            (segment,sid,value['user'],value['user_name'],value['rank_id'],value['title'],now,now,'active'))
                _event(con,sid,segment,'start',now)
            else:
                segment = old[3]
                elapsed, advance = now-old[7], value['position']-old[4]
                # Two unpaused observations at the same speed, plausibly continuous.
                seconds = min(elapsed, advance/value['rate']) if not old[5] and not value['paused'] and old[6]==value['rate'] and 0<advance<=elapsed*value['rate']+3 else 0
                if seconds>0:
                    was_zero = con.execute('SELECT seconds=0 FROM playback_segments WHERE id=?',(segment,)).fetchone()[0]
                    con.execute('UPDATE playback_segments SET seconds=seconds+? WHERE id=?',(seconds,segment))
                    # Attribute measured intervals to UTC days, including midnight splits.
                    end, left, first = now, seconds, bool(was_zero)
                    while left>0:
                        day = datetime.fromtimestamp(end-.000001,timezone.utc).strftime('%Y-%m-%d')
                        midnight = datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
                        part = min(left,end-midnight)
                        if part<=0:
                            break
                        con.execute('INSERT INTO playback_daily VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope,day,user_id,item_id) DO UPDATE SET seconds=seconds+excluded.seconds,plays=plays+excluded.plays,user_name=excluded.user_name,title=excluded.title',
                                    (sid,day,value['user'],value['user_name'],value['rank_id'],value['title'],part,int(first)))
                        first, left, end = False, left-part, end-part
                    _event(con,sid,segment,'progress',now,seconds)
                elif bool(old[5]) != value['paused']:
                    _event(con,sid,segment,'pause' if value['paused'] else 'resume',now)
                elif advance<0 or advance>elapsed*value['rate']+3:
                    _event(con,sid,segment,'seek',now)
                con.execute('UPDATE playback_segments SET last_seen=? WHERE id=?',(now,segment))
            con.execute('INSERT OR REPLACE INTO playback_observations VALUES(?,?,?,?,?,?,?,?,?)',
                        (sid,session,value['user'],value['item'],segment,value['position'],int(value['paused']),value['rate'],now))
        for old in active.values():
            con.execute("UPDATE playback_segments SET state='stopped' WHERE id=?",(old[3],))
            con.execute('DELETE FROM playback_observations WHERE scope=? AND session=?',(sid,old[0]))
            _event(con,sid,old[3],'stop',now)
        con.execute("UPDATE playback_collection SET last_poll=?,error='' WHERE scope=?",(now,sid))
        con.execute('DELETE FROM playback_events WHERE id <= (SELECT max(id)-10000 FROM playback_events)')
        con.execute('DELETE FROM playback_segments WHERE last_seen<?',(now-90*86400,))
        con.execute('DELETE FROM playback_daily WHERE day<?',(datetime.fromtimestamp(now-365*86400,timezone.utc).strftime('%Y-%m-%d'),))
    return True


def poll():
    sid = scope()
    with store.db() as con:
        enabled = _state(con,sid)[0]
    if not enabled or not configured():
        return
    try:
        rows = engine.emby_request('/Sessions',timeout=8,retries=0)
        observe(sid,rows)
    except Exception:
        with store.db() as con:
            _state(con,sid)
            con.execute('UPDATE playback_collection SET error=? WHERE scope=?',('播放会话读取失败，请检查 Emby 连接和访问权限；中断期间不累计时长',sid))


def summary(days=7):
    days = max(1,min(90,int(days)))
    now, sid = time.time(), scope()
    cutoff = datetime.fromtimestamp(now-(days-1)*86400,timezone.utc).strftime('%Y-%m-%d')
    with store.db() as con:
        enabled, started, last_poll, error, gaps = _state(con,sid)
        totals = con.execute('SELECT coalesce(sum(seconds),0),coalesce(sum(plays),0),count(distinct user_id) FROM playback_daily WHERE scope=? AND day>=?',(sid,cutoff)).fetchone()
        trend = con.execute('SELECT day,sum(seconds),sum(plays),count(distinct user_id) FROM playback_daily WHERE scope=? AND day>=? GROUP BY day ORDER BY day',(sid,cutoff)).fetchall()
        rankings = con.execute('SELECT item_id,max(title),sum(seconds),sum(plays),count(distinct user_id) FROM playback_daily WHERE scope=? AND day>=? GROUP BY item_id ORDER BY sum(seconds) DESC,item_id LIMIT 10',(sid,cutoff)).fetchall()
        users = con.execute('SELECT user_id,max(user_name),sum(seconds),sum(plays) FROM playback_daily WHERE scope=? AND day>=? GROUP BY user_id ORDER BY sum(seconds) DESC,user_id LIMIT 10',(sid,cutoff)).fetchall()
        active = con.execute('SELECT s.title,s.user_name,s.seconds,o.paused FROM playback_observations o JOIN playback_segments s ON s.id=o.segment WHERE o.scope=? AND o.observed>? ORDER BY s.last_seen DESC LIMIT 20',(sid,now-MAX_GAP)).fetchall() if enabled else []
    by_day = {r[0]:r for r in trend}
    complete_trend = []
    for offset in reversed(range(days)):
        day = datetime.fromtimestamp(now-offset*86400,timezone.utc).strftime('%Y-%m-%d')
        r = by_day.get(day,(day,0,0,0))
        complete_trend.append(dict(zip(('day','seconds','plays','viewers'),r)))
    return {'enabled':bool(enabled),'configured':configured(), 'days':days,'timezone':'UTC',
            'collected_since':started,'last_poll':last_poll or None,'error':error,'gaps':gaps,
            'stale':bool(enabled and (not last_poll or now-last_poll>MAX_GAP)),
            'seconds':round(totals[0],1),'plays':totals[1],'viewers':totals[2], 'trend':complete_trend,
            'rankings':[dict(zip(('item_id','title','seconds','plays','viewers'),r)) for r in rankings],
            'users':[dict(zip(('user_id','name','seconds','plays'),r)) for r in users],
            'active':[dict(zip(('title','user','seconds','paused'),r)) for r in active],
            'source':'Emby 播放会话 · 每15秒观测',
            'coverage':'仅统计带用户身份且连续观测到的有效播放段；暂停、拖动、重启和采集空档不补算。历史数据从启用后积累，保留365天的按日汇总。'}


def _loop():
    while not _STOP.is_set():
        try:
            poll()
        except Exception:
            pass  # Collector failures never stop the media job worker.
        _WAKE.wait(INTERVAL)
        _WAKE.clear()


def start():
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        with store.db() as con:
            _schema(con)
            con.execute('DELETE FROM playback_observations')
            con.execute("UPDATE playback_segments SET state='interrupted' WHERE state='active'")
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop,name='playback-metrics',daemon=True)
        _THREAD.start()


def stop():
    global _THREAD
    _STOP.set()
    _WAKE.set()
    if _THREAD:
        _THREAD.join(10)
    _THREAD = None
