class CloudError(Exception):
    def __init__(self, message, status=400, uncertain=False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain
