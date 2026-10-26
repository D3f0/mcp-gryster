"""Per-domain authorization for registered Grist operations."""

from __future__ import annotations

from mcp_gryster.config import DomainAccess, DomainPolicy
from mcp_gryster.errors import McpGrysterError
from mcp_gryster.grist.models import MethodSpec


class Policy:
    """Authorize read/write effects against the configured domain ceiling."""

    def __init__(self, domains: DomainPolicy) -> None:
        self._domains = domains

    def authorize(self, spec: MethodSpec) -> None:
        field_name = "schema_access" if spec.domain == "schema" else spec.domain
        access = getattr(self._domains, field_name)
        permitted = access is DomainAccess.WRITE or (
            access is DomainAccess.READ and spec.effect == "read"
        )
        if not permitted:
            raise McpGrysterError(
                "PERMISSION_DENIED",
                "The requested Grist operation is not permitted by policy",
                details={"domain": spec.domain, "effect": spec.effect},
            )
