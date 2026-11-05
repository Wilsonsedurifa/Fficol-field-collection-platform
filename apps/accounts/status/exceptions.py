"""Exceptions for account status transition workflows."""


class AccountStatusWorkflowError(Exception):
    """Workflow error with machine-readable code and HTTP status code."""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
