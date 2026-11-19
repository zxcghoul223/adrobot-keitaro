class AppError(Exception):
    def __init__(self, status: int, message: str, *, uncertain: bool = False):
        self.status = status
        self.message = message
        self.uncertain = uncertain
        super().__init__(message)
