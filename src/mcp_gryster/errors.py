"""Stable public errors and exception sanitization."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

type ErrorCode = Literal[
    "INVALID_ARGUMENT",
    "METHOD_NOT_SUPPORTED",
    "PERMISSION_DENIED",
    "REVISION_CONFLICT",
    "PROJECT_NOT_FOUND",
    "PATH_NOT_ALLOWED",
    "LIMIT_EXCEEDED",
    "GRIST_REQUEST_FAILED",
    "VALIDATION_FAILED",
    "PREVIEW_FAILED",
    "PACKAGE_FAILED",
]

_logger = logging.getLogger(__name__)
_AUTHORIZATION = re.compile(
    r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s,;]+"
)
_KEY_VALUE_SECRET = re.compile(
    r"(?i)((?:api[-_ ]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+"
)
_JSON_BODY = re.compile(r"(?is)(body\s*[=:]\s*)(\{.*\}|\[.*\])")
_ABSOLUTE_PATH = re.compile(r"(?<![\w.])/(?:[^\s,;]+/)*[^\s,;]*")
_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])(?:[a-z]:\\)(?:[^\s,;]+\\)*[^\s,;]*")


class _ErrorData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ErrorCode
    message: str
    details: dict[str, Any] | None = None
    correlation_id: str | None = Field(default=None, min_length=1)


class McpGrysterError(Exception):
    """Safe, throwable error with Pydantic-validated structured fields."""

    __slots__ = ("_data",)

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> None:
        data = _ErrorData(
            code=code,
            message=message,
            details=details,
            correlation_id=correlation_id,
        )
        object.__setattr__(self, "_data", data)
        super().__init__(message)

    @property
    def code(self) -> ErrorCode:
        return self._data.code

    @property
    def message(self) -> str:
        return self._data.message

    @property
    def details(self) -> dict[str, Any] | None:
        return self._data.details

    @property
    def correlation_id(self) -> str | None:
        return self._data.correlation_id

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"code", "message", "details", "correlation_id"}:
            setattr(self._data, name, value)
            return
        object.__setattr__(self, name, value)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """Return the structured public payload."""
        return self._data.model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        """Serialize the structured public payload as JSON."""
        return self._data.model_dump_json(**kwargs)

    def __str__(self) -> str:
        return self.message


_LOCAL_ERRORS: tuple[tuple[type[BaseException], ErrorCode, str], ...] = (
    (NotImplementedError, "METHOD_NOT_SUPPORTED", "The requested method is not supported"),
    (PermissionError, "PERMISSION_DENIED", "Permission denied"),
    (FileNotFoundError, "PROJECT_NOT_FOUND", "The requested project was not found"),
    (ValueError, "INVALID_ARGUMENT", "An argument was invalid"),
)


def public_error(exc: BaseException) -> McpGrysterError:
    """Convert an exception to a stable error without exposing its input or internals."""
    if isinstance(exc, McpGrysterError):
        return exc

    correlation_id = uuid4().hex
    code, message = _classify(exc)
    safe_debug = _redact_debug_text(f"{type(exc).__name__}: {exc}")
    _logger.debug("Failure correlation_id=%s error=%s", correlation_id, safe_debug)
    return McpGrysterError(code, message, correlation_id=correlation_id)


def _classify(exc: BaseException) -> tuple[ErrorCode, str]:
    for exception_type, code, message in _LOCAL_ERRORS:
        if isinstance(exc, exception_type):
            return code, message
    if _is_remote_failure(exc):
        return "GRIST_REQUEST_FAILED", "The Grist request failed"
    return "GRIST_REQUEST_FAILED", "The Grist request failed"


def _is_remote_failure(exc: BaseException) -> bool:
    module = type(exc).__module__
    return module == "requests" or module.startswith(("requests.", "pygrister"))


def _redact_debug_text(value: str) -> str:
    """Remove credentials, bodies, and local absolute paths from diagnostic text."""
    redacted = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
    redacted = _KEY_VALUE_SECRET.sub(r"\1[REDACTED]", redacted)
    redacted = _JSON_BODY.sub(r"\1[REDACTED]", redacted)
    redacted = _WINDOWS_PATH.sub("[PATH]", redacted)
    redacted = _ABSOLUTE_PATH.sub("[PATH]", redacted)
    home = str(Path.home())
    if home:
        redacted = redacted.replace(home, "[HOME]")
    return redacted
