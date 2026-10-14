class CloudError(Exception):
    def __init__(self, message, status=400, uncertain=False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


class UnrecognizedMedia(CloudError):
    """A semantic matching failure; transport/configuration failures use CloudError."""
    def __init__(self, message):
        super().__init__(message, 409)
