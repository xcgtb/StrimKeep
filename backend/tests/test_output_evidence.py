import hashlib
import json

from test_organize import setup


def test_index_refresh_and_sync_note_keep_non_derivable_provenance(tmp_path):
    store, cloud, http, engine, _ = setup(tmp_path)
    relative = '媒体库/片名.strm'
    path = engine.root / relative
    path.parent.mkdir(parents=True)
    path.write_text('https://example.test/play115/pick/1000/' + 'A' * 40 + '/1000/a.mkv')
    subtitle = path.with_suffix('.srt')
    subtitle.write_text('subtitle')
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    sub_checksum = hashlib.sha256(subtitle.read_bytes()).hexdigest()
    source = {'id': '1000', 'name': 'a.mkv', 'parent_id': '200',
              'sha1': 'A' * 40, 'size': 1000}
    plan = {'relative_path': relative, 'local_files': {
        relative: checksum, str(subtitle.relative_to(engine.root)): sub_checksum},
        'playback_id': 'legacy-id', 'token_cipher': 'encrypted'}
    engine.outputs.remember('1', source, ['200'], plan)
    engine.outputs.reindex('1')
    from strimkeep.identity import parse_file
    engine.outputs.note('1', relative, path, parse_file(path))
    row = engine.outputs.get('1', '1000')
    assert row['playback_id'] == 'legacy-id' and row['token_cipher'] == 'encrypted'
    assert json.loads(row['source'])['parent_id'] == '200'
    assert json.loads(row['trail']) == ['200']
    assert json.loads(row['files'])[str(subtitle.relative_to(engine.root))] == sub_checksum
    store.close()


def test_refresh_does_not_assign_another_accounts_file(tmp_path):
    store, cloud, http, engine, _ = setup(tmp_path)
    path = engine.root / '电影.strm'
    path.parent.mkdir(parents=True)
    path.write_text('https://example.test/play115/pick/1000/' + 'A' * 40 + '/1000/a.mkv')
    engine.outputs.reindex('1')
    assert store.db.execute('SELECT COUNT(*) FROM output_registry').fetchone()[0] == 0
    store.close()
