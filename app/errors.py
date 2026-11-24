"""Application-level errors rendered as a stable JSON envelope."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body


def not_found(code: str, message: str, details: Any | None = None) -> ApiError:
    return ApiError(404, code, message, details)


def conflict(code: str, message: str, details: Any | None = None) -> ApiError:
    return ApiError(409, code, message, details)


def invalid(code: str, message: str, details: Any | None = None) -> ApiError:
    return ApiError(422, code, message, details)
