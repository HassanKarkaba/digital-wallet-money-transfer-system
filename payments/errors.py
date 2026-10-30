class DomainError(Exception):
    def __init__(self, message, code='INVALID', status=422):
        self.message = message
        self.code = code
        self.status = status
        super().__init__(message)
