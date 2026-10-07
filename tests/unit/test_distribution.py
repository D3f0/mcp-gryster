from __future__ import annotations

import configparser
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(output), str(ROOT)],
        check=True,
        timeout=120,
    )
    wheels = tuple(output.glob("mcp_gryster-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_wheel_metadata_entrypoint_and_package_data(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        metadata = archive.read(metadata_name).decode()
        entry_points = configparser.ConfigParser()
        entry_points.read_string(archive.read(entry_points_name).decode())

    assert "Name: mcp-gryster" in metadata
    assert "Requires-Python: >=3.12" in metadata
    assert entry_points["console_scripts"]["mcp-gryster"] == "mcp_gryster.__main__:main"
    assert {
        "mcp_gryster/grist/registry.json",
        "mcp_gryster/widget/static/harness.html",
        "mcp_gryster/widget/static/grist-bridge.js",
        "mcp_gryster/py.typed",
    } <= names

    forbidden = ("tests/", "integration/", "fixtures/", "credentials", ".mcp-gryster/")
    assert not any(any(part in name for part in forbidden) for name in names)


def test_wheel_installs_offline_in_clean_environment(wheel: Path, tmp_path: Path) -> None:
    environment = tmp_path / "venv"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], check=True)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            str(environment / "bin" / "python"),
            str(wheel),
        ],
        check=True,
        timeout=120,
    )
    result = subprocess.run(
        [str(environment / "bin" / "mcp-gryster"), "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "policy-controlled Pygrister operations" in result.stdout
