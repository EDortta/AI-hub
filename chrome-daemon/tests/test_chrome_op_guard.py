"""Issue 001 — the watchdog must not kill Chrome while an operation is in flight.

These tests cover the guard itself and, more importantly, the reaper's refusal to
kill: the original incident was a *renderer child* of the managed Chrome being
SIGKILLed mid-generation, so both the in-flight refusal and the process-tree
sparing are asserted here.
"""
from __future__ import annotations

import threading

import pytest

import chrome_manager
import watchers


# --- counter primitives ----------------------------------------------------

def test_counter_starts_clear():
    assert chrome_manager.chrome_op_in_flight() is False


def test_start_marks_in_flight():
    chrome_manager.mark_chrome_op_start()
    assert chrome_manager.chrome_op_in_flight() is True


def test_end_clears_in_flight():
    chrome_manager.mark_chrome_op_start()
    chrome_manager.mark_chrome_op_end()
    assert chrome_manager.chrome_op_in_flight() is False


def test_nested_operations_stay_in_flight_until_the_last_one_ends():
    chrome_manager.mark_chrome_op_start()
    chrome_manager.mark_chrome_op_start()
    chrome_manager.mark_chrome_op_end()
    assert chrome_manager.chrome_op_in_flight() is True, "outer op still running"
    chrome_manager.mark_chrome_op_end()
    assert chrome_manager.chrome_op_in_flight() is False


def test_end_without_start_does_not_underflow():
    """An unbalanced end must not drive the counter negative.

    A negative counter would read as "not in flight" forever after and silently
    disable the guard — the failure mode this test exists to prevent.
    """
    chrome_manager.mark_chrome_op_end()
    chrome_manager.mark_chrome_op_end()
    assert chrome_manager._chrome_op_inflight == 0
    chrome_manager.mark_chrome_op_start()
    assert chrome_manager.chrome_op_in_flight() is True


def test_guard_marks_and_clears():
    with chrome_manager.chrome_op_guard():
        assert chrome_manager.chrome_op_in_flight() is True
    assert chrome_manager.chrome_op_in_flight() is False


def test_guard_clears_even_when_the_body_raises():
    with pytest.raises(ValueError):
        with chrome_manager.chrome_op_guard():
            raise ValueError("generation blew up")
    assert chrome_manager.chrome_op_in_flight() is False, "a failed op must not pin the guard"


def test_counter_is_thread_safe():
    """Endpoints run in the event loop; Playwright work runs in a thread pool."""
    def worker():
        for _ in range(200):
            chrome_manager.mark_chrome_op_start()
            chrome_manager.mark_chrome_op_end()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert chrome_manager._chrome_op_inflight == 0


# --- the reaper honours the guard ------------------------------------------

def test_kill_stale_chrome_refuses_while_an_operation_is_in_flight(monkeypatch):
    """The guard lives next to the SIGKILL, so no caller can forget it."""
    def explode():  # pragma: no cover - must never run
        raise AssertionError("_chrome_snapshot must not be reached while in flight")

    monkeypatch.setattr(watchers, "_chrome_snapshot", explode)
    chrome_manager.mark_chrome_op_start()
    assert watchers._kill_stale_chrome() == 0


def test_kill_stale_chrome_scans_when_idle(monkeypatch):
    monkeypatch.setattr(watchers, "_chrome_snapshot", lambda: [])
    monkeypatch.setattr(watchers, "_managed_chrome_pids", lambda: set())
    assert watchers._kill_stale_chrome() == 0


# --- the reaper spares the managed Chrome's whole tree ---------------------

class _FakeProc:
    """Stands in for psutil.Process entries returned by _chrome_snapshot."""

    def __init__(self, pid: int, age_s: float, display: str = ""):
        import time as _t

        self.pid = pid
        self.info = {"pid": pid, "name": "chrome", "create_time": _t.time() - age_s}
        self._display = display

    def environ(self):
        return {"DISPLAY": self._display}


def _stale_enough(pid: int) -> tuple:
    """A process that trips every kill condition: old, hot and hidden."""
    return (_FakeProc(pid, age_s=watchers._CHROME_PROC_AGE_MIN + 100), 40.0)


def test_managed_chrome_children_are_spared(monkeypatch):
    """Regression for issue 001: the killed pid was a renderer *child*.

    Sparing only the parent pid left every renderer of the managed browser
    eligible for the reaper — and a renderer under load is precisely old, hot and
    hidden. Killing it closes the Playwright driver ("Connection closed while
    reading from the driver").
    """
    parent_pid, renderer_pid, foreign_pid = 1000, 1001, 2000
    monkeypatch.setattr(
        watchers, "_chrome_snapshot",
        lambda: [_stale_enough(parent_pid), _stale_enough(renderer_pid), _stale_enough(foreign_pid)],
    )
    monkeypatch.setattr(watchers, "_managed_chrome_pids", lambda: {parent_pid, renderer_pid})

    killed_pids = []
    monkeypatch.setattr(watchers.os, "kill", lambda pid, sig: killed_pids.append(pid))

    assert watchers._kill_stale_chrome() == 1
    assert killed_pids == [foreign_pid], "only the unmanaged Chrome may be reaped"


def test_young_and_cold_chrome_processes_are_never_killed(monkeypatch):
    young = (_FakeProc(3000, age_s=10), 90.0)
    cold = (_FakeProc(3001, age_s=watchers._CHROME_PROC_AGE_MIN + 100), 0.5)
    visible = (_FakeProc(3002, age_s=watchers._CHROME_PROC_AGE_MIN + 100, display=":0"), 90.0)
    monkeypatch.setattr(watchers, "_chrome_snapshot", lambda: [young, cold, visible])
    monkeypatch.setattr(watchers, "_managed_chrome_pids", lambda: set())
    monkeypatch.setattr(watchers.os, "kill", lambda pid, sig: pytest.fail(f"killed pid={pid}"))

    assert watchers._kill_stale_chrome() == 0


def test_managed_pids_include_descendants(monkeypatch):
    """_managed_chrome_pids walks the tree, not just the launched pid."""
    class _Popen:
        pid = 500

        def poll(self):
            return None

    class _Child:
        def __init__(self, pid):
            self.pid = pid

    class _Parent:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            assert recursive is True, "renderers are grandchildren too"
            return [_Child(501), _Child(502)]

    monkeypatch.setattr(chrome_manager, "_chrome_process", _Popen())
    import psutil

    monkeypatch.setattr(psutil, "Process", _Parent)
    assert watchers._managed_chrome_pids() == {500, 501, 502}


def test_managed_pids_empty_when_no_chrome_is_tracked(monkeypatch):
    monkeypatch.setattr(chrome_manager, "_chrome_process", None)
    assert watchers._managed_chrome_pids() == set()
