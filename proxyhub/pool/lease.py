class ServiceError(RuntimeError):
    def __init__(self, code, status=400, message=None):
        super().__init__(message or code)
        self.code, self.status, self.message = code, status, message or code
