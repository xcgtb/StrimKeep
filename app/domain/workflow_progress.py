"""Display only persisted step/ingestion evidence; never invent stage completion."""
STAGES = (('transfer', '转存接收'), ('naming', '命名'), ('organize', '移动归档'), ('strm', 'STRM 生成'))


def summarize(rows, ingestion):
    groups = {key: {} for key, _ in STAGES}
    for stage, state, count in rows:
        if stage in groups:
            groups[stage][state] = groups[stage].get(state, 0) + count
    output = []
    for key, label in STAGES:
        counts = groups[key]
        verified = counts.get('verified', 0)
        outstanding = sum(n for state, n in counts.items() if state != 'verified')
        output.append({'key': key, 'label': label, 'verified': verified, 'outstanding': outstanding,
                       'detail': (f'已核实 {verified} 项' + (f'，{outstanding} 项待核实' if outstanding else '')) if counts else
                                 '暂无操作回执；可能尚未执行或无需此操作'})
    verified = ingestion.get('confirmed', 0)
    outstanding = sum(n for state, n in ingestion.items() if state != 'confirmed')
    output.append({'key': 'emby', 'label': 'Emby 入库', 'verified': verified, 'outstanding': outstanding,
                   'detail': f'已确认 {verified} 条，{outstanding} 条未确认' if ingestion else '暂无入库回执；STRM 生成不代表已入库'})
    return output
