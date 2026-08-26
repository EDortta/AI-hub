"""Packaging contract for the ai-hub CLI (issue 007, part 2).

The `ai-hub` binary on PATH is a console_script installed via pipx, not a
symlink into the checkout. These tests pin the contract that makes that work:
the entry point declared in pyproject.toml must resolve to a callable, and the
package must import without the daemon's heavy dependencies (fastapi/playwright)
— the pipx venv only ships httpx + pyyaml.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

DAEMON_DIR = Path(__file__).resolve().parent.parent
PYPROJECT = DAEMON_DIR / "pyproject.toml"


def test_pyproject_declares_ai_hub_console_script():
    text = PYPROJECT.read_text(encoding="utf-8")
    # Python 3.10 has no tomllib; the shape is simple enough to pin textually.
    match = re.search(
        r'^\[project\.scripts\]\s*\n\s*ai-hub\s*=\s*"([\w.]+):(\w+)"',
        text,
        flags=re.MULTILINE,
    )
    assert match, "[project.scripts] must declare ai-hub = \"<module>:<func>\""
    module_name, func_name = match.group(1), match.group(2)

    # The declared entry point must actually resolve — a typo here would only
    # blow up at install time, on the operator's machine.
    module = importlib.import_module(module_name)
    entry = getattr(module, func_name)
    assert callable(entry)


def test_cli_imports_without_daemon_dependencies():
    """The pipx venv has httpx+pyyaml only; importing the CLI must not pull
    in the daemon stack (fastapi, uvicorn, playwright). Run in a subprocess:
    the surrounding suite imports main.py, which would pollute sys.modules."""
    import subprocess

    code = (
        "import sys\n"
        "import ai_hub.cli\n"
        "bad = {'fastapi', 'uvicorn', 'playwright'} & set(sys.modules)\n"
        "assert not bad, f'daemon deps leaked into the CLI import graph: {bad}'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(DAEMON_DIR),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_client_is_importable_as_package_module():
    from ai_hub.client import AIHubClient  # noqa: F401


def test_main_without_command_prints_help_and_fails(monkeypatch, capsys):
    from ai_hub import cli

    monkeypatch.setattr(sys, "argv", ["ai-hub"])
    assert cli.main() == 1
    out = capsys.readouterr().out
    assert "usage: ai-hub" in out


def test_pyproject_does_not_package_the_daemon():
    """Only the ai_hub package ships; the daemon stays a checkout concern."""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^packages\s*=\s*\[([^\]]*)\]', text, flags=re.MULTILINE)
    assert match, "pyproject must pin [tool.setuptools] packages explicitly"
    packages = re.findall(r'"([^"]+)"', match.group(1))
    assert packages == ["ai_hub"]


def test_sdist_hygiene_stays_pinned():
    """Council finding (2026-08-26, A2): the sdist once shipped the daemon's
    test suite (unrunnable outside the checkout) and a readme that leaked
    internal hosts into package METADATA. Pin both fixes."""
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    assert not re.search(r"^readme\s*=", pyproject, flags=re.MULTILINE), (
        "pyproject must not declare a readme: docs/INTEGRATION.md carries "
        "internal operational detail that would end up in METADATA"
    )
    manifest = (DAEMON_DIR / "MANIFEST.in").read_text(encoding="utf-8")
    for directory in ("tests", "docs", "install"):
        assert f"prune {directory}" in manifest, (
            f"MANIFEST.in must keep pruning {directory}/ from the sdist"
        )
