"""整理完成后按目录补齐：目录里不是本批整理进来的文件，也要生成 STRM（对齐）。"""
import asyncio
import json

from test_organize import new_job, setup

TITLE = '某剧 (2024) {tmdb-1}'


def test_directory_fill_creates_strm_for_orphan_files(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        jid = new_job(store)
        config = json.loads(engine.job(jid)['config'])
        engine.update_job(jid, state='running')
        show = (await cloud.mkdir('200', '剧集'))['cid']
        region = (await cloud.mkdir(show, '欧美剧集'))['cid']
        work = (await cloud.mkdir(region, TITLE))['cid']
        season = (await cloud.mkdir(work, 'Season 1'))['cid']
        cloud.add_video('2001', '某剧.2024.S01E01.1080p.mkv', season)

        entries = await cloud.children(season)
        assert await engine.fill_directory(jid, season, entries, config) == 1

        # 库根默认镜像 115 归档目录名（0.8.30 起），不再固定「影视媒体库」。
        found = list((tmp_path / 'strm').rglob('某剧.2024.S01E01.1080p.strm'))
        assert found, '目录里的孤儿文件应被补出 STRM'
        target = found[0]
        assert target.parent.name == 'Season 1' and target.parent.parent.name == TITLE
        assert '/play115/' in target.read_text(encoding='utf-8')

        # 幂等：本地已有 STRM 时不再重复创建，也不重复登记
        again = await cloud.children(season)
        assert await engine.fill_directory(jid, season, again, config) == 0
        await http.aclose()
        store.close()

    asyncio.run(scenario())


def test_directory_fill_skips_revoked_playback(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        jid = new_job(store)
        config = json.loads(engine.job(jid)['config'])
        engine.update_job(jid, state='running')
        work = (await cloud.mkdir('200', '电影'))['cid']
        cloud.add_video('2100', '已撤销.2024.mkv', work)
        with store.db:
            store.db.execute("INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,created,revoked)"
                             " VALUES('p1','2100','pc2100','x','x','h',?,1)", (0,))
        entries = await cloud.children(work)
        assert await engine.fill_directory(jid, work, entries, config) == 0
        await http.aclose()
        store.close()

    asyncio.run(scenario())
