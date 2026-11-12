from strimkeep.library_mapping import page
from test_inventory_mapping import Store

def test_unidentified_grouped_filterable_not_executable():
    issues=[{'side':'cloud','id':'1','path':'云库/剧集/Show (2020)/Season 1/S01E01.strm','reason':'identity_unresolved'},
            {'side':'share','id':'2','path':'分享影视库/剧集/Show (2020)/Season 1/S01E02.strm','reason':'identity_unresolved'},
            {'side':'share','id':'3','path':'分享影视库/剧集/Other (2021)/Season 1/S01E01.strm','reason':'missing_or_unsafe'}]
    snap={'items':[],'issues':issues,'complete':False}
    result=page(snap,Store(),media='tv',status='unmatched')
    assert result['total']==1
    card=result['rows'][0]
    assert card['unidentified'] and card['in_cloud'] and card['in_share'] and card['tmdb_id']==0
    assert (card['cloud_files'],card['share_files'])==(1,1)
    assert page(snap,Store(),media='tv',status='missing')['total']==0
