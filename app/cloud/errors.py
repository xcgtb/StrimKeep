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
          'prepare': '准备与配置核验', 'generate_share_strm': '生成分享 STRM', 'forward': '发送转发消息'}

def diagnostic(error, stage='prepare'):
    stage = getattr(error, 'stage', '') or stage
    return {'stage': stage, 'stage_label': STAGES.get(stage, stage),
            'category': getattr(error, 'category', '') or 'operation_failed',
            'code': getattr(error, 'code', ''), 'uncertain': bool(getattr(error, 'uncertain', False)),
            'reason': str(error)}
