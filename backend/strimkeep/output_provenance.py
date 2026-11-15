"""Recover source evidence from existing organized records when the disk index lacks it."""
import json
import re


def source_from_organized(db, account, fid, relative, output):
    """Return corroborated source and playback id; never infer missing identity."""
    try:
        index_source = json.loads(output['source'] or '{}')
        row = db.execute('''SELECT o.file_id,o.playback_id,o.relative_path,i.original,i.plan,j.config
            FROM organized_files o JOIN organize_items i ON i.id=o.item_id
            JOIN organize_jobs j ON j.id=o.job_id WHERE o.file_id=?''', (str(fid),)).fetchone()
        if not row or row['relative_path'] != relative or output['relative_path'] != relative:
            return None
        original, plan, config = (json.loads(row[key] or '{}') for key in ('original','plan','config'))
        playback = str(row['playback_id'] or '')
        mapping = db.execute('SELECT file_id,revoked FROM playbacks WHERE id=?',(playback,)).fetchone()
        if (not playback or not mapping or mapping['revoked'] or str(mapping['file_id']) != str(fid)
                or str(config.get('account_id') or '') != str(account)
                or str(original.get('id') or '') != str(fid)
                or str(plan.get('playback_id') or '') != playback
                or plan.get('relative_path') != relative):
            return None
        sha = str(original.get('sha1') or '').upper()
        size = original.get('size')
        name, parent = plan.get('target_name'), str(plan.get('dest') or '')
        if (not re.fullmatch(r'[0-9A-F]{40}', sha) or type(size) is not int or size <= 0
                or str(index_source.get('sha1') or '').upper() != sha
                or index_source.get('size') != size or not isinstance(name, str) or not name
                or not parent.isdigit()):
            return None
        source = {**original, 'id':str(fid), 'name':name, 'parent_id':parent,
                  'sha1':sha, 'size':size}
        if index_source.get('pickcode'):
            source['pickcode'] = index_source['pickcode']
        return source, playback
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
