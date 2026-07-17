"""Test bootstrap.

`chrome-daemon/` has a hyphen, so it is not importable as a package; the daemon's
modules import each other flat (`from chrome_manager import ...`). Tests therefore
put the daemon directory itself on sys.path and import the modules the same way
the daemon does — no rename, no shim.
"""
from __future__ import annotations

import sys
from pathlib import Path

DAEMON_DIR = Path(__file__).resolve().parent.parent
if str(DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(DAEMON_DIR))

import pytest


@pytest.fixture(autouse=True)
def _reset_chrome_op_counter():
    """Keep the module-level in-flight counter from leaking across tests."""
    import chrome_manager

    chrome_manager._chrome_op_inflight = 0
    yield
    chrome_manager._chrome_op_inflight = 0
