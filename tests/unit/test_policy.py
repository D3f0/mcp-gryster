from __future__ import annotations

import pytest

from mcp_gryster.config import DomainAccess, DomainPolicy
from mcp_gryster.errors import McpGrysterError
from mcp_gryster.grist.models import MethodSpec
from mcp_gryster.grist.policy import Policy
from mcp_gryster.grist.registry import load_registry


def _spec(effect: str) -> MethodSpec:
    return MethodSpec.model_validate(
        {
            "name": f"example_{effect}",
            "domain": "records",
            "effect": effect,
            "parameters": {},
            "result_kind": "json",
        }
    )


@pytest.mark.parametrize(
    ("access", "effect", "allowed"),
    [
        (DomainAccess.NONE, "read", False),
        (DomainAccess.NONE, "write", False),
        (DomainAccess.READ, "read", True),
        (DomainAccess.READ, "write", False),
        (DomainAccess.WRITE, "read", True),
        (DomainAccess.WRITE, "write", True),
    ],
)
def test_policy_authorization_truth_table(
    access: DomainAccess, effect: str, allowed: bool
) -> None:
    policy = Policy(DomainPolicy(records=access))

    if allowed:
        policy.authorize(_spec(effect))
    else:
        with pytest.raises(McpGrysterError, match="not permitted") as caught:
            policy.authorize(_spec(effect))
        assert caught.value.code == "PERMISSION_DENIED"


def test_policy_uses_external_schema_alias() -> None:
    policy = Policy(DomainPolicy.model_validate({"schema": "read"}))
    spec = MethodSpec(
        name="list_tables",
        domain="schema",
        effect="read",
        parameters={},
        result_kind="json",
    )

    policy.authorize(spec)


def test_registry_rejects_sql_write_entries() -> None:
    data = load_registry().model_dump(mode="json")
    for method in data["methods"]:
        if method["name"] == "run_sql":
            method["effect"] = "write"
            break

    with pytest.raises(ValueError, match="SQL"):
        type(load_registry()).model_validate(data)
