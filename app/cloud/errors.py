class CloudError(Exception):
    def __init__(self, message, status=400, uncertain=False, *, code="", category="", stage=""):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain
        self.code = code
        self.category = category
        self.stage = stage


class UnrecognizedMedia(CloudError):
    """A semantic matching failure; transport/configuration failures use CloudError."""
    def __init__(self, message):
        super().__init__(message, 409)


STAGES = {'read_share': '读取分享', 'submit_transfer': '提交转存', 'verify_transfer': '核验转存结果',
          'prepare': '准备与配置核验', 'generate_share_strm': '生成分享 STRM', 'forward': '发送转发消息',
          'probe': '媒体信息', 'tmdb_episode': 'TMDB 季集核验', 'account': '核验账号',
          'list': '读取目录', 'info': '读取文件信息', 'mkdir': '创建目录', 'rename': '重命名',
          'move': '移动归档', 'delete': '删除文件', 'download': '获取下载地址', 'provider': '115 接口',
          'recognize': '影视识别', 'organize': '整理归档'}

def diagnostic(error, stage='prepare'):
    stage = getattr(error, 'stage', '') or stage
    return {'stage': stage, 'stage_label': STAGES.get(stage, stage),
            'category': getattr(error, 'category', '') or 'operation_failed',
            'code': getattr(error, 'code', ''), 'uncertain': bool(getattr(error, 'uncertain', False)),
            'reason': str(error)}
