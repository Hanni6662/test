from __future__ import annotations


class DomainError(Exception):
    """A safe, public domain failure handled by the application-level error handler."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        if not 400 <= status_code <= 599:
            raise ValueError("domain errors must use an HTTP error status")
        if not code or not code.isascii() or not code.replace("_", "").isalnum():
            raise ValueError("domain error codes must be safe ASCII identifiers")
        if not message or any(ord(character) < 32 for character in message):
            raise ValueError("domain error messages must be safe server-defined text")
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)
