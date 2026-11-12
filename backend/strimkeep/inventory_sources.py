"""Hash media ownership evidence without exposing credentials.

These hashes are inputs for plan revalidation, not deletion authority.

分享侧的清理以本地文件和文件指纹为准：``share_strms`` 缺行（或已撤销）
不再阻止清理本地 STRM，只是没有分享来源证据可核对。网盘侧相反——没有
登记就无法证明要回收的是哪一个 115 文件，直接拒绝该文件的删除动作。
"""
import hashlib
import json

from .inventory_fs import is_synthetic


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _file_only(path):
    """分享侧只有本地文件时的来源证据：路径 + 「没有登记」这件事本身。"""
    return {'side': 'share', 'path': path, 'registered': False}


def source_fingerprints(db, rows, account):
    result = []
    for side, record_id, path in sorted(rows, key=lambda row: (row[0], str(row[1]), row[2])):
        if is_synthetic(record_id):
            if side != 'share':
                raise ValueError('网盘源文件未登记，无法核实要回收的 115 文件')
            result.append({'side': side, 'id': str(record_id), 'path': path,
                           'source_hash': _digest(_file_only(path))})
            continue
        if side == 'cloud':
            if not isinstance(account, str) or not account:
                raise ValueError('网盘账号身份不可核实')
            row = db.execute('SELECT playback_id,relative_path FROM organized_files WHERE file_id=?', (record_id,)).fetchone()
            output = db.execute('SELECT account_id,file_id,source,trail,playback_id,relative_path,files,active '
                                'FROM output_registry WHERE account_id=? AND file_id=?', (account, record_id)).fetchone()
            owner = db.execute('SELECT account_id,file_id,digest FROM owned_outputs WHERE relative_path=?', (path,)).fetchone()
            if not row or not output or not owner or not output['active']:
                raise ValueError('网盘文件来源或输出归属不可核实')
            if (row['relative_path'] != path or output['relative_path'] != path
                    or row['playback_id'] != output['playback_id']
                    or owner['account_id'] != account or str(owner['file_id']) != str(record_id)):
                raise ValueError('网盘文件的来源身份不一致')
            try:
                files = json.loads(output['files'])
            except (TypeError, ValueError):
                raise ValueError('网盘输出归属数据损坏') from None
            if not isinstance(files, dict) or not files.get(path) or files[path] != owner['digest']:
                raise ValueError('网盘文件的输出归属不一致')
            evidence = {'account_id': account, 'file_id': str(record_id), 'path': path,
                        'playback_id': row['playback_id'], 'source': output['source'],
                        'trail': output['trail'], 'files': output['files'], 'owner_digest': owner['digest']}
        elif side == 'share':
            row = db.execute('SELECT id,share_code,receive_code,file_id,pickcode,strm_path,token_hash,revoked '
                             'FROM share_strms WHERE id=?', (record_id,)).fetchone()
            if not row or row['revoked'] or row['strm_path'] != path or not row['share_code'] or not row['file_id']:
                raise ValueError('分享文件来源身份不可核实')
            evidence = {key: row[key] for key in ('id', 'share_code', 'receive_code', 'file_id',
                                                   'pickcode', 'strm_path', 'token_hash', 'revoked')}
        else:
            raise ValueError('库存来源类型无效')
        result.append({'side': side, 'id': str(record_id), 'path': path, 'source_hash': _digest(evidence)})
    return result
