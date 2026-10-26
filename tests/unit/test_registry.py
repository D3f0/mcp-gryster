from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError
from pygrister.api import GristApi

from mcp_gryster.errors import McpGrysterError
from mcp_gryster.grist import registry as registry_module
from mcp_gryster.grist.models import MethodSpec
from mcp_gryster.grist.registry import NON_REMOTE_METHODS, MethodRegistry, load_registry


def test_registry_classifies_representative_operations() -> None:
    registry = load_registry()

    assert (registry.get("list_records").domain, registry.get("list_records").effect) == (
        "records",
        "read",
    )
    assert (registry.get("add_records").domain, registry.get("add_records").effect) == (
        "records",
        "write",
    )
    assert (registry.get("run_sql_with_args").domain, registry.get("run_sql_with_args").effect) == (
        "sql",
        "read",
    )
    assert (
        registry.get("delete_service_account_key").domain,
        registry.get("delete_service_account_key").effect,
    ) == ("service_accounts", "write")


@pytest.mark.parametrize(
    "name", ("search_users", "search_users_raw", "search_groups", "search_groups_raw")
)
def test_scim_search_operations_are_reads(name: str) -> None:
    assert load_registry().get(name).effect == "read"


def test_registry_covers_exactly_the_pinned_remote_api() -> None:
    registry = load_registry()
    public_callables = {
        name
        for name, member in inspect.getmembers(GristApi, callable)
        if not name.startswith("_")
    }

    assert set(registry.names) == public_callables - NON_REMOTE_METHODS
    registry.assert_compatible(GristApi)


def test_registry_excludes_configuration_session_and_private_helpers() -> None:
    registry = load_registry()

    for name in ("reconfig", "open_session", "_make_user_data"):
        assert registry.inspect(name) is None
        with pytest.raises(McpGrysterError, match="not available") as caught:
            registry.get(name)
        assert caught.value.code == "METHOD_NOT_SUPPORTED"


def test_unsupported_remote_stub_is_auditable_but_not_dispatchable() -> None:
    registry = load_registry()

    spec = registry.inspect("delete_myself")
    assert spec is not None
    assert spec.supported is False
    assert spec.typed_tool is False
    with pytest.raises(McpGrysterError, match="not supported") as caught:
        registry.get("delete_myself")
    assert caught.value.code == "METHOD_NOT_SUPPORTED"


def test_registry_parameters_match_python_names_and_defaults() -> None:
    registry = load_registry()
    spec = registry.get("list_records")
    signature = inspect.signature(GristApi.list_records)
    expected = list(signature.parameters.values())[1:]

    assert list(spec.parameters) == [parameter.name for parameter in expected]
    for parameter in expected:
        registered = spec.parameters[parameter.name]
        if parameter.default is inspect.Parameter.empty:
            assert registered.required is True
        else:
            assert registered.required is False
            assert registered.default == parameter.default


def test_registry_rejects_invalid_specs_and_duplicates() -> None:
    valid = MethodSpec(
        name="example",
        domain="records",
        effect="read",
        parameters={},
        result_kind="json",
    )
    with pytest.raises(ValueError, match="duplicate"):
        MethodRegistry(pygrister_version="0.10.0", methods=(valid, valid))

    with pytest.raises(ValidationError):
        MethodSpec.model_validate(
            {
                "name": "_private",
                "domain": "unknown",
                "effect": "execute",
                "parameters": {},
                "result_kind": "stream",
            }
        )


def test_registry_model_validation_rejects_missing_parameters_and_version_drift() -> None:
    data = load_registry().model_dump(mode="json")
    list_records = next(
        method for method in data["methods"] if method["name"] == "list_records"
    )
    del list_records["parameters"]["table_id"]
    with pytest.raises(ValidationError, match="signature"):
        MethodRegistry.model_validate(data)

    data = load_registry().model_dump(mode="json")
    data["pygrister_version"] = "0.10.1"
    with pytest.raises(ValidationError, match="version"):
        MethodRegistry.model_validate(data)


def test_registry_rejects_unsupported_installed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = load_registry().model_dump(mode="json")
    monkeypatch.setattr(registry_module, "_installed_pygrister_version", lambda: "0.10.1")

    with pytest.raises(ValidationError, match="installed Pygrister version"):
        MethodRegistry.model_validate(data)


def test_compatibility_allows_unsupported_stub_to_disappear() -> None:
    without_stub = type(
        "WithoutStub",
        (),
        {
            name: member
            for name, member in GristApi.__dict__.items()
            if callable(member) and name != "delete_myself"
        },
    )

    load_registry().assert_compatible(without_stub)


def test_compatibility_reports_new_missing_and_changed_methods() -> None:
    registry = load_registry()

    class ChangedApi(GristApi):
        list_records = None

        def list_tables(self, unexpected: str) -> object:
            return unexpected

        def future_remote_operation(self) -> object:
            return None

    with pytest.raises(RuntimeError) as caught:
        registry.assert_compatible(ChangedApi)

    message = str(caught.value)
    assert "list_records" in message and "missing" in message
    assert "list_tables" in message and "signature" in message
    assert "future_remote_operation" in message and "unregistered" in message
