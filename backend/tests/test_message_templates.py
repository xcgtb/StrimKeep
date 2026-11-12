from strimkeep.message_templates import channel_notice, channel_subject, transfer_completed, transfer_received


def test_channel_receipt_reports_resource_destination_and_real_queue_state_without_secrets():
    title, body = channel_notice('电影频道', '璀璨之城 S01\nhttps://115.com/s/secret?password=abcd\n提取码：abcd',
                                 [(12, '转存')], 'transfer', '转存 / 剧集')
    assert title == '📡 频道分享转存 · 已排队'
    assert all(part in body for part in ('璀璨之城 S01', '电影频道', '转存 / 剧集', '#12', '转存任务'))
    assert 'secret' not in body and 'abcd' not in body and '转存完成' not in title


def test_empty_or_secret_only_post_has_no_fabricated_title():
    assert channel_subject('https://115.com/s/secret?password=abcd\n提取码：abcd') == ''
    title, body = channel_notice('电影频道', '', [(None, '离线 2 个')], 'offline', '下载')
    assert '已提交' in title and 'None' not in body and '下载' in body


def test_transfer_receipt_and_completion_distinguish_queued_from_saved():
    pending = transfer_received([7, 8], '特定目录', True)
    assert '排队' in pending and '#7、#8' in pending and '特定目录' in pending
    assert '默认目录' not in pending
    title, body = transfer_completed(7, {'origin':'telegram','target_label':'特定目录',
        'receipt':{'received_entries':[{'id':'1'}], 'organize_job':9}}, '已转存')
    assert title == '📥 转存完成'
    assert all(part in body for part in ('特定目录', '1 个', '#7', '#9', '整理任务'))


def test_channel_completion_keeps_scan_warning():
    title, body = transfer_completed(7, {'origin':'channel','target_label':'转存', 'receipt':{}},
        '已转存到 转存，共 1 个文件或文件夹。定时扫描未启用，等待手动整理或开启定时扫描。')
    assert '定时扫描未启用' in body and '等待定时扫描整理' not in body


def test_share_strm_and_cloud_destinations_are_distinct_and_counts_are_real():
    title, body = channel_notice('影视频道', '影片标题', [], 'strm', count=8)
    assert '分享 STRM' in title and '分享 STRM 待整理' in body and '8 个 STRM' in body
    assert '分享数' not in body and '115 网盘 /' not in body
    title, body = channel_notice('影视频道', '影片标题', [(2, '转存')], 'transfer', '转存目录')
    # 目的地前缀统一成「📂 保存至」：转存必须写明落在网盘哪个目录，且不能再混进
    # 「分享 STRM 待整理」——那是 STRM 模式的说法。
    assert '保存至  转存目录' in body and '分享 STRM 待整理' not in body

def test_offline_submission_is_not_reported_as_completed():
    title, body = channel_notice('影视频道', '', [], 'offline', '磁力目录', count=3, resource_kind='磁力')
    assert '已提交' in title and '3 个链接' in body and '结果待确认' in body
    assert '保存至  磁力目录' in body
