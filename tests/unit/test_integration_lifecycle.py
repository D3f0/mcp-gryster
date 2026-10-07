from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.integration import conftest


def test_published_base_url_accepts_docker_allocated_loopback_port() -> None:
    completed = SimpleNamespace(returncode=0, stdout="127.0.0.1:49153\n")

    assert conftest._published_base_url(completed) == "http://127.0.0.1:49153"


@pytest.mark.parametrize(
    "completed",
    [
        SimpleNamespace(returncode=1, stdout="service is not running\n"),
        SimpleNamespace(returncode=0, stdout="0.0.0.0:49153\n"),
        SimpleNamespace(returncode=0, stdout="127.0.0.1:not-a-port\n"),
        SimpleNamespace(returncode=0, stdout="127.0.0.1:1\n127.0.0.1:2\n"),
    ],
)
def test_published_base_url_rejects_missing_or_non_loopback_mapping(
    completed: Any,
) -> None:
    with pytest.raises(RuntimeError, match="usable Grist port"):
        conftest._published_base_url(completed)


def test_node_failed_recognizes_failed_setup_call_or_teardown() -> None:
    node = SimpleNamespace(
        rep_setup=SimpleNamespace(failed=False),
        rep_call=SimpleNamespace(failed=True),
        rep_teardown=SimpleNamespace(failed=False),
    )

    assert conftest._node_failed(node) is True
    assert conftest._node_failed(SimpleNamespace()) is False


def test_stop_compose_removes_volumes_without_success_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, ...]] = []

    def run_compose(project: str, *arguments: str, check: bool = True) -> Any:
        calls.append((project, *arguments, f"check={check}"))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(conftest, "_run_compose", run_compose)

    conftest._stop_compose("test-project", (), preserve_failure=False)

    assert calls == [
        (
            "test-project",
            "down",
            "--volumes",
            "--remove-orphans",
            "check=False",
        )
    ]
    assert capsys.readouterr().err == ""


def test_stop_compose_does_not_replace_an_existing_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, ...]] = []

    def run_compose(project: str, *arguments: str, check: bool = True) -> Any:
        calls.append((project, *arguments, f"check={check}"))
        if arguments[0] == "down":
            return SimpleNamespace(returncode=1, stdout="down failed")
        return SimpleNamespace(returncode=0, stdout="diagnostic")

    monkeypatch.setattr(conftest, "_run_compose", run_compose)

    conftest._stop_compose("test-project", (), preserve_failure=True)

    assert calls[0][1:4] == ("down", "--volumes", "--remove-orphans")
    assert "Docker Compose teardown failed" in capsys.readouterr().err
