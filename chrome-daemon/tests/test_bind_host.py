"""Issue 008 — the bind interface is configurable, and loopback is the default.

The daemon has to bind the container's eth0 once it moves into CT 4001, because a
reverse proxy on the host cannot reach the *container's* loopback. That must not
turn into "binds everywhere by default": an existing deployment that sets nothing
has to keep binding 127.0.0.1.
"""
from __future__ import annotations

import importlib
import os

import pytest


def _bind_host(monkeypatch, value=None):
    """Re-import main with AIHUB_BIND_HOST set (or unset) and read BIND_HOST."""
    if value is None:
        monkeypatch.delenv("AIHUB_BIND_HOST", raising=False)
    else:
        monkeypatch.setenv("AIHUB_BIND_HOST", value)
    # A token is required or the module logs a critical and rejects everything;
    # irrelevant here but keeps the import quiet.
    monkeypatch.setenv("AIHUB_DAEMON_TOKEN", "test-token")
    import main

    return importlib.reload(main).BIND_HOST


def test_default_is_loopback(monkeypatch):
    """The security-relevant case: unset must never mean 'listen everywhere'."""
    assert _bind_host(monkeypatch) == "127.0.0.1"


def test_explicit_opt_in_is_honoured(monkeypatch):
    assert _bind_host(monkeypatch, "0.0.0.0") == "0.0.0.0"


def test_specific_interface_is_honoured(monkeypatch):
    assert _bind_host(monkeypatch, "192.168.7.201") == "192.168.7.201"


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_value_falls_back_to_loopback(monkeypatch, value):
    """An env var set to nothing is a config mistake, not a request to bind 0.0.0.0.
    Fail towards the closed default."""
    assert _bind_host(monkeypatch, value) == "127.0.0.1"


def test_whitespace_is_stripped(monkeypatch):
    assert _bind_host(monkeypatch, "  0.0.0.0  ") == "0.0.0.0"
