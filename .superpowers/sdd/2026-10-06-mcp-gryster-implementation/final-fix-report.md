# Final contract fix report

## Completed findings

1. Default policy grants read access for organizations, workspaces, documents, schema, and records while access, attachments, webhooks, SQL, identity, service accounts, and administration remain disabled. The public catalog registry entry is classified under schema.
2. Fixture preview evidence is recorded only after the configured smoke interval. Unsupported/runtime failures cancel and await the timer, revoke evidence, and do not leak `CancelledError`; stopping a pending preview cancels and clears its timer without granting evidence.
3. Public names are `widget.projects.apply_files`, `widget.fixtures.create_from_grist`, and `grist://method-registry`. Distribution resources are project-scoped and accept only canonical artifact names for the exact project, rejecting ambiguous cross-project prefixes.
4. `ValidationReport` has separate value-safe `errors`, `warnings`, and executed `checks`. `PackageResult.validation` returns the exact report used to authorize packaging for the same project revision and fixture.

## Verification

Focused changed-surface suite:

```text
uv run pytest tests/unit/test_config.py tests/unit/test_validation.py tests/unit/test_packaging.py tests/unit/test_preview.py tests/unit/test_widget_tools.py tests/unit/test_readme_contract.py -q
69 passed in 6.19s
```
