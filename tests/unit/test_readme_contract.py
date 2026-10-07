from pathlib import Path


README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")


def test_readme_keeps_pygrister_as_primary_identity() -> None:
    assert README.index("Pygrister") < README.index("custom-widget authoring")
    assert "Pygrister 0.10.0" in README


def test_readme_contains_uvx_mcp_configuration() -> None:
    assert '"command": "uvx"' in README
    assert '"args": ["mcp-gryster"]' in README
    assert '"GRIST_API_KEY"' in README


def test_readme_documents_release_boundaries() -> None:
    required = (
        "GRIST_TEAM_SITE",
        "GRIST_SELF_MANAGED",
        "MCP_GRYSTER_POLICY",
        "organizations",
        "service_accounts",
        "grist.call",
        "127.0.0.1",
        "Grist 1.7.19",
        "Python 3.12",
        "fixtures/",
    )
    for text in required:
        assert text in README
