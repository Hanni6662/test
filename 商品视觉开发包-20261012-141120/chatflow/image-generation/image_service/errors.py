class ServiceError(Exception):
    """Only constant, non-sensitive error codes may cross the API boundary."""
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)
