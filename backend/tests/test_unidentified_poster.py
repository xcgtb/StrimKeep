from strimkeep.inventory_unidentified_poster import read_unidentified_poster

def test_poster_reads_only_real_known_image(tmp_path):
    work=tmp_path/'share/Show'; season=work/'Season 1'; season.mkdir(parents=True)
    item={'cloud':[], 'share':[{'path':'share/Show/Season 1/S01E01.strm'}]}
    poster=work/'poster.png'; poster.write_bytes(b'\x89PNG\r\n\x1a\n'+b'test')
    data,mime=read_unidentified_poster(tmp_path,item)
    assert mime=='image/png' and data.startswith(b'\x89PNG')
    poster.write_bytes(b'not-an-image'); assert read_unidentified_poster(tmp_path,item) is None

def test_poster_rejects_symlinks(tmp_path):
    work=tmp_path/'share/Show'; work.mkdir(parents=True)
    outside=tmp_path/'outside.jpg'; outside.write_bytes(b'\xff\xd8\xffdata')
    (work/'poster.jpg').symlink_to(outside)
    item={'cloud':[], 'share':[{'path':'share/Show/S01E01.strm'}]}
    assert read_unidentified_poster(tmp_path,item) is None
