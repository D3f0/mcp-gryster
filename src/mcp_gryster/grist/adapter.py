"""Policy-checked, isolated dispatch to Pygrister."""

from __future__ import annotations

import asyncio
import inspect
import math
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints
from uuid import uuid4

from pygrister.api import GristApi

from mcp_gryster.artifacts import ArtifactStore
from mcp_gryster.audit import AuditEvent, AuditSink
from mcp_gryster.config import DomainAccess, Settings
from mcp_gryster.errors import McpGrysterError, public_error
from mcp_gryster.grist.models import GristCallResult, JsonValue, MethodSpec
from mcp_gryster.grist.policy import Policy
from mcp_gryster.grist.registry import MethodRegistry

_DOCUMENTED_CONFIG_KEYS = frozenset(
    {
        "GRIST_API_KEY",
        "GRIST_SELF_MANAGED",
        "GRIST_SELF_MANAGED_HOME",
        "GRIST_SELF_MANAGED_SINGLE_ORG",
        "GRIST_SERVER_PROTOCOL",
        "GRIST_API_SERVER",
        "GRIST_API_ROOT",
        "GRIST_TEAM_SITE",
        "GRIST_WORKSPACE_ID",
        "GRIST_DOC_ID",
    }
)

type ApiFactory = Callable[..., Any]


class GristAdapter:
    """The sole checked seam between MCP features and Pygrister."""

    def __init__(
        self,
        registry: MethodRegistry,
        policy: Policy,
        artifacts: ArtifactStore,
        settings: Settings,
        audit: AuditSink,
        *,
        api_factory: ApiFactory = GristApi,
        max_pages: int = 100,
        max_items: int = 10_000,
    ) -> None:
        if max_pages <= 0 or max_items <= 0:
            raise ValueError("pagination limits must be positive")
        self._registry = registry
        self._policy = policy
        self._artifacts = artifacts
        self._settings = settings
        self._audit = audit
        self._api_factory = api_factory
        self._max_pages = max_pages
        self._max_items = max_items

    async def call(
        self, method: str, arguments: dict[str, JsonValue]
    ) -> GristCallResult:
        """Authorize, validate, execute, and normalize one registered operation."""
        started = monotonic()
        spec = self._registry.inspect(method)
        audit_domain = spec.domain if spec is not None else "unknown"
        audit_effect = spec.effect if spec is not None else "none"
        audit_status: Literal["success", "error", "denied"] = "error"
        correlation_id: str | None = None
        try:
            dispatch_spec = self._registry.get(method)
            self._policy.authorize(dispatch_spec)
            signature, annotations = self._call_contract(dispatch_spec)
            call_arguments = self._bind_arguments(
                dispatch_spec, signature, annotations, arguments
            )
            client = self._api_factory(config=self._client_config())
            operation = getattr(client, dispatch_spec.name)
            result = await asyncio.to_thread(
                self._execute,
                operation,
                dispatch_spec,
                call_arguments,
            )
            audit_status = "success"
            return result
        except Exception as exc:
            error = public_error(exc)
            correlation_id = error.correlation_id
            if error.code == "PERMISSION_DENIED":
                audit_status = "denied"
            if error is exc:
                raise
            raise error from exc
        finally:
            self._audit.emit(
                AuditEvent(
                    tool=f"grist.{method}",
                    domain=audit_domain,
                    effect=audit_effect,
                    status=audit_status,
                    duration_ms=(monotonic() - started) * 1000,
                    correlation_id=correlation_id,
                )
            )

    def _call_contract(
        self, spec: MethodSpec
    ) -> tuple[inspect.Signature, dict[str, Any]]:
        try:
            member = inspect.getattr_static(self._api_factory, spec.name)
            signature = inspect.signature(member)
            annotations = get_type_hints(member)
        except (AttributeError, TypeError) as exc:
            raise McpGrysterError(
                "METHOD_NOT_SUPPORTED", "The requested Grist method is not supported"
            ) from exc
        except NameError:
            annotations = dict(getattr(member, "__annotations__", {}))

        parameters = list(signature.parameters.values())
        if parameters and parameters[0].name in {"self", "cls"}:
            signature = signature.replace(parameters=parameters[1:])
        if set(signature.parameters) != set(spec.parameters):
            raise McpGrysterError(
                "METHOD_NOT_SUPPORTED", "The requested Grist method is not supported"
            )
        return signature, annotations

    def _bind_arguments(
        self,
        spec: MethodSpec,
        signature: inspect.Signature,
        annotations: dict[str, Any],
        arguments: dict[str, JsonValue],
    ) -> dict[str, Any]:
        try:
            bound = signature.bind(**arguments)
        except TypeError as exc:
            raise McpGrysterError(
                "INVALID_ARGUMENT", "Arguments do not match the Grist method"
            ) from exc
        bound.apply_defaults()

        validated: dict[str, Any] = {}
        for name, value in bound.arguments.items():
            annotation = annotations.get(name, signature.parameters[name].annotation)
            try:
                validated[name] = self._validate_value(name, value, annotation)
            except McpGrysterError:
                raise
            except (TypeError, ValueError) as exc:
                raise McpGrysterError(
                    "INVALID_ARGUMENT", "An argument has an invalid type"
                ) from exc
        if spec.result_kind == "file":
            for name, annotation in annotations.items():
                if name in validated and validated[name] is None and _allows_path(annotation):
                    validated[name] = self._workspace_path(f"{spec.name}-{uuid4().hex}.bin")
        return validated

    def _validate_value(self, name: str, value: Any, annotation: Any) -> Any:
        if annotation in {inspect.Parameter.empty, Any}:
            self._validate_json_value(value)
            return value
        if isinstance(annotation, str):
            raise McpGrysterError(
                "METHOD_NOT_SUPPORTED", "The requested Grist method is not supported"
            )

        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin in {types.UnionType, Union}:
            for option in args:
                try:
                    return self._validate_value(name, value, option)
                except McpGrysterError as exc:
                    if exc.code != "INVALID_ARGUMENT":
                        raise
            raise McpGrysterError("INVALID_ARGUMENT", "An argument has an invalid type")
        if annotation is type(None):
            if value is None:
                return None
            raise McpGrysterError("INVALID_ARGUMENT", "An argument has an invalid type")
        if annotation is Path:
            if not isinstance(value, str):
                raise McpGrysterError("INVALID_ARGUMENT", "An argument has an invalid type")
            return self._workspace_path(value)
        if origin is list or annotation is list:
            if not isinstance(value, list):
                raise McpGrysterError("INVALID_ARGUMENT", "An argument has an invalid type")
            item_type = args[0] if args else Any
            return [self._validate_value(name, item, item_type) for item in value]
        if origin is dict or annotation is dict:
            if not isinstance(value, dict):
                raise McpGrysterError("INVALID_ARGUMENT", "An argument has an invalid type")
            key_type, item_type = args if args else (str, Any)
            if key_type is not str or any(not isinstance(key, str) for key in value):
                raise McpGrysterError("INVALID_ARGUMENT", "An argument has an invalid type")
            return {
                key: self._validate_value(name, item, item_type)
                for key, item in value.items()
            }
        if annotation is int:
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif annotation is float:
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
        else:
            valid = isinstance(value, annotation)
        if not valid:
            raise McpGrysterError("INVALID_ARGUMENT", "An argument has an invalid type")
        return value

    def _validate_json_value(self, value: Any) -> None:
        if value is None or isinstance(value, (bool, int, str)):
            return
        if isinstance(value, float):
            if math.isfinite(value):
                return
            raise McpGrysterError("INVALID_ARGUMENT", "An argument has an invalid type")
        if isinstance(value, list):
            for item in value:
                self._validate_json_value(item)
            return
        if isinstance(value, dict) and all(isinstance(key, str) for key in value):
            for item in value.values():
                self._validate_json_value(item)
            return
        raise McpGrysterError("INVALID_ARGUMENT", "An argument has an invalid type")

    def _workspace_path(self, value: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            raise McpGrysterError("PATH_NOT_ALLOWED", "File path is not allowed")
        resolved = (self._settings.workspace_root / candidate).resolve()
        if not resolved.is_relative_to(self._settings.workspace_root.resolve()):
            raise McpGrysterError("PATH_NOT_ALLOWED", "File path is not allowed")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def _client_config(self) -> dict[str, str]:
        config = {
            key: value
            for key, value in self._settings.grist_config.items()
            if key in _DOCUMENTED_CONFIG_KEYS
        }
        writable = any(
            access is DomainAccess.WRITE
            for access in self._settings.policy.model_dump().values()
        )
        config["GRIST_RAISE_ERROR"] = "Y"
        config["GRIST_SAFEMODE"] = "N" if writable else "Y"
        return config

    def _execute(
        self,
        operation: Callable[..., Any],
        spec: MethodSpec,
        arguments: dict[str, Any],
    ) -> GristCallResult:
        raw = operation(**arguments)
        if spec.result_kind == "paginated":
            status_code, data = self._consume_pages(raw)
        else:
            status_code, data = self._split_response(raw)
        self._ensure_success(status_code)

        if spec.result_kind == "file":
            data = self._persist_output_file(arguments, data)
        elif spec.result_kind == "binary" or isinstance(data, (bytes, bytearray, memoryview)):
            data = self._persist_bytes(spec.name, bytes(data))
        return GristCallResult(status_code=status_code, data=self._normalize(data))

    def _consume_pages(self, value: Any) -> tuple[int, list[Any]]:
        if not isinstance(value, Iterator):
            try:
                pages = iter(value)
            except TypeError as exc:
                raise McpGrysterError(
                    "GRIST_REQUEST_FAILED", "The Grist request failed"
                ) from exc
        else:
            pages = value

        items: list[Any] = []
        status_code = 200
        for _ in range(self._max_pages):
            if len(items) >= self._max_items:
                break
            try:
                page = next(pages)
            except StopIteration:
                break
            status_code, page_data = self._split_response(page)
            self._ensure_success(status_code)
            if not isinstance(page_data, Sequence) or isinstance(
                page_data, (str, bytes, bytearray)
            ):
                raise McpGrysterError(
                    "GRIST_REQUEST_FAILED", "The Grist request failed"
                )
            remaining = self._max_items - len(items)
            items.extend(page_data[:remaining])
        return status_code, items

    @staticmethod
    def _split_response(value: Any) -> tuple[int, Any]:
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], int)
            and not isinstance(value[0], bool)
        ):
            return value[0], value[1]
        return 200, value

    @staticmethod
    def _ensure_success(status_code: int) -> None:
        if not 200 <= status_code < 300:
            raise McpGrysterError(
                "GRIST_REQUEST_FAILED",
                "The Grist request failed",
                details={"status_code": status_code},
            )

    def _persist_output_file(
        self, arguments: Mapping[str, Any], response: Any
    ) -> dict[str, Any]:
        paths = [value for value in arguments.values() if isinstance(value, Path)]
        if len(paths) != 1 or not paths[0].is_file():
            if isinstance(response, (bytes, bytearray, memoryview)):
                return self._persist_bytes("download", bytes(response))
            raise McpGrysterError("GRIST_REQUEST_FAILED", "The Grist request failed")
        output = paths[0]
        try:
            artifact = self._artifacts.put_bytes("grist", output.name, output.read_bytes())
        finally:
            output.unlink(missing_ok=True)
        return self._artifact_data(artifact.name, artifact.size_bytes)

    def _persist_bytes(self, stem: str, data: bytes) -> dict[str, Any]:
        name = f"{stem}-{uuid4().hex}.bin"
        artifact = self._artifacts.put_bytes("grist", name, data)
        return self._artifact_data(artifact.name, artifact.size_bytes)

    @staticmethod
    def _artifact_data(name: str, size_bytes: int) -> dict[str, Any]:
        return {"artifact": {"kind": "grist", "name": name, "size_bytes": size_bytes}}

    def _normalize(self, value: Any) -> JsonValue:
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            raise McpGrysterError("GRIST_REQUEST_FAILED", "The Grist request failed")
        if isinstance(value, Enum):
            return self._normalize(value.value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Path):
            return value.name
        if isinstance(value, Mapping):
            if any(not isinstance(key, str) for key in value):
                raise McpGrysterError("GRIST_REQUEST_FAILED", "The Grist request failed")
            return {key: self._normalize(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [self._normalize(item) for item in value]
        if hasattr(value, "model_dump"):
            return self._normalize(value.model_dump(mode="json"))
        raise McpGrysterError("GRIST_REQUEST_FAILED", "The Grist request failed")


def _allows_path(annotation: Any) -> bool:
    """Return whether an annotation accepts a filesystem path."""
    if annotation is Path:
        return True
    return get_origin(annotation) in {types.UnionType, Union} and Path in get_args(annotation)
