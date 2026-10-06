"""Load and verify the checked-in Pygrister 0.10.0 operation registry."""

from __future__ import annotations

import inspect
import json
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, model_validator

from mcp_gryster.errors import McpGrysterError
from mcp_gryster.grist.models import MethodSpec

PINNED_PYGRISTER_VERSION = "0.10.0"
NON_REMOTE_METHODS = frozenset(
    {
        "close_session",
        "inspect",
        "make_server",
        "open_session",
        "reconfig",
        "update_config",
    }
)


class MethodRegistry(BaseModel):
    """Immutable registry with dispatch and compatibility checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SUPPORTED_VERSION: ClassVar[str] = PINNED_PYGRISTER_VERSION

    pygrister_version: str
    methods: tuple[MethodSpec, ...]

    @model_validator(mode="after")
    def validate_registry(self) -> MethodRegistry:
        names = [method.name for method in self.methods]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate method names: {', '.join(duplicates)}")
        if self.pygrister_version != self.SUPPORTED_VERSION:
            raise ValueError(
                f"unsupported Pygrister registry version {self.pygrister_version}; "
                f"expected {self.SUPPORTED_VERSION}"
            )
        installed = _installed_pygrister_version()
        if installed != self.SUPPORTED_VERSION:
            raise ValueError(
                f"unsupported installed Pygrister version {installed}; "
                f"expected {self.SUPPORTED_VERSION}"
            )
        from pygrister.api import GristApi  # type: ignore[import-untyped]

        signature_problems = [
            method.name
            for method in self.methods
            if callable(member := getattr(GristApi, method.name, None))
            and _parameters(member) != method.parameters
        ]
        if signature_problems:
            raise ValueError(
                "registry parameters do not match pinned Pygrister signature: "
                + ", ".join(signature_problems)
            )
        sql_writes = [
            method.name
            for method in self.methods
            if method.domain == "sql" and method.effect != "read"
        ]
        if sql_writes:
            raise ValueError(f"SQL methods must be read-only: {', '.join(sql_writes)}")
        unsupported_typed = [
            method.name for method in self.methods if not method.supported and method.typed_tool
        ]
        if unsupported_typed:
            raise ValueError(
                "unsupported methods cannot be typed tools: " + ", ".join(unsupported_typed)
            )
        return self

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(method.name for method in self.methods)

    def inspect(self, name: str) -> MethodSpec | None:
        """Return an entry for introspection, including unsupported operations."""
        return next((method for method in self.methods if method.name == name), None)

    def get(self, name: str) -> MethodSpec:
        """Return a supported dispatch entry or a safe public error."""
        spec = self.inspect(name)
        if spec is None:
            raise McpGrysterError(
                "METHOD_NOT_SUPPORTED", "The requested Grist method is not available"
            )
        if not spec.supported:
            raise McpGrysterError(
                "METHOD_NOT_SUPPORTED", "The requested Grist method is not supported"
            )
        return spec

    def assert_compatible(self, api_type: type[Any]) -> None:
        """Reject installed API drift before exposing any registry operation."""
        installed = _installed_pygrister_version()
        problems: list[str] = []
        if installed != self.pygrister_version:
            problems.append(
                f"installed Pygrister version {installed!r} does not match "
                f"registry {self.pygrister_version!r}"
            )

        public_callables = {
            name
            for name, member in inspect.getmembers(api_type, callable)
            if not name.startswith("_")
        }
        registered = set(self.names)
        supported = {method.name for method in self.methods if method.supported}
        missing = sorted(supported - public_callables)
        added = sorted(public_callables - NON_REMOTE_METHODS - registered)
        if missing:
            problems.append("missing registered methods: " + ", ".join(missing))
        if added:
            problems.append("unregistered methods: " + ", ".join(added))

        for spec in self.methods:
            member = getattr(api_type, spec.name, None)
            if not callable(member):
                continue
            actual = _parameters(member)
            if actual != spec.parameters:
                problems.append(f"signature changed for {spec.name}")

        if problems:
            raise RuntimeError("Pygrister API is incompatible: " + "; ".join(problems))


def _installed_pygrister_version() -> str:
    try:
        return version("Pygrister")
    except PackageNotFoundError as exc:
        raise RuntimeError("Pygrister is not installed") from exc


def _parameters(method: Any) -> dict[str, Any]:
    from mcp_gryster.grist.models import ParameterSpec

    parameters: dict[str, ParameterSpec] = {}
    for parameter in inspect.signature(method).parameters.values():
        if parameter.name in {"self", "cls"}:
            continue
        required = parameter.default is inspect.Parameter.empty
        parameters[parameter.name] = ParameterSpec(
            required=required,
            default=None if required else parameter.default,
        )
    return parameters


def load_registry() -> MethodRegistry:
    """Load and validate the packaged immutable registry."""
    registry_path = resources.files("mcp_gryster.grist").joinpath("registry.json")
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    return MethodRegistry.model_validate(data)
