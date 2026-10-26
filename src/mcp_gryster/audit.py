"""Metadata-only structured audit events."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AuditEvent(BaseModel):
    """Non-sensitive metadata describing one tool invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    effect: Literal["none", "read", "write"]
    status: Literal["success", "error", "denied"]
    duration_ms: float = Field(ge=0)
    correlation_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuditSink(ABC):
    """Destination for safe audit metadata."""

    @abstractmethod
    def emit(self, event: AuditEvent) -> None:
        """Persist one event."""


class JsonLineAuditSink(AuditSink):
    """Append one complete JSON object per line to a local file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def emit(self, event: AuditEvent) -> None:
        payload = event.model_dump(mode="json")
        encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        with self._lock:
            descriptor = os.open(self._path, flags, 0o600)
            with os.fdopen(descriptor, "ab", closefd=True) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
