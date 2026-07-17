"""Issue 007 — a restart must not silently drop registered conversations.

The guardian restarts this daemon automatically after 3 failed health checks, so
losing the registry on restart is routine, not exceptional: consumers hold a
watcher_id that quietly points at nothing. These tests pin the persistence.
"""
from __future__ import annotations

import json
import os

import pytest

from watchers import (
    ConversationWatcher,
    JsonFileWatcherStore,
    NullWatcherStore,
    WatcherRegistry,
    WatcherState,
)


@pytest.fixture
def store(tmp_path):
    return JsonFileWatcherStore(tmp_path / "watchers.json")


def _watcher(alias="Claudia", **kw):
    kw.setdefault("url", "https://chatgpt.com/c/abc-123")
    kw.setdefault("project_path", "/home/op/proj")
    return ConversationWatcher(alias=alias, **kw)


# --- round trip ------------------------------------------------------------

def test_registered_watcher_survives_a_restart(store):
    registry = WatcherRegistry(store=store)
    w = registry.register(_watcher())

    # A restart is a brand-new registry over the same store.
    reborn = WatcherRegistry(store=store)
    assert reborn.restore() == 1
    restored = reborn.get(w.id)
    assert restored is not None, "the watcher_id a consumer holds must still resolve"
    assert restored.alias == "Claudia"
    assert restored.url == w.url
    assert restored.project_path == w.project_path


def test_restore_preserves_poll_configuration_and_state(store):
    registry = WatcherRegistry(store=store)
    w = registry.register(_watcher(interaction_poll_seconds=3, latency_poll_seconds=120))
    w.state = WatcherState.INTERACTION
    registry.checkpoint()

    reborn = WatcherRegistry(store=store)
    reborn.restore()
    restored = reborn.get(w.id)
    assert restored.interaction_poll_seconds == 3
    assert restored.latency_poll_seconds == 120
    assert restored.state is WatcherState.INTERACTION
    assert restored.poll_interval() == 3


def test_seen_hashes_survive_so_old_messages_are_not_redelivered(store):
    """Without the seen set, a restored watcher re-reads the whole conversation
    and routes every old message to the inbox as if it had just arrived."""
    registry = WatcherRegistry(store=store)
    w = registry.register(_watcher())
    w.seen_hashes.update({"hash-a", "hash-b"})
    w.last_seen_hash = "hash-b"
    registry.checkpoint()

    reborn = WatcherRegistry(store=store)
    reborn.restore()
    restored = reborn.get(w.id)
    assert restored.seen_hashes == {"hash-a", "hash-b"}
    assert restored.last_seen_hash == "hash-b"


def test_inbox_content_is_not_written_to_disk(store):
    """The inbox holds verbatim ChatGPT text; persisting it would put conversation
    content at rest. It is a transient queue by design."""
    registry = WatcherRegistry(store=store)
    w = registry.register(_watcher())
    w.inbox.append({"role": "assistant", "text": "dados sensíveis do beneficiário"})
    registry.checkpoint()

    on_disk = store.path.read_text()
    assert "dados sensíveis" not in on_disk
    assert "inbox" not in json.loads(on_disk)["watchers"][0]


def test_unregister_is_persisted(store):
    registry = WatcherRegistry(store=store)
    w = registry.register(_watcher())
    assert registry.unregister(w.id) is True

    reborn = WatcherRegistry(store=store)
    assert reborn.restore() == 0, "an unregistered watcher must not come back"


def test_unregister_by_project_is_persisted(store):
    registry = WatcherRegistry(store=store)
    registry.register(_watcher("A", project_path="/proj/x"))
    registry.register(_watcher("B", project_path="/proj/x"))
    keeper = registry.register(_watcher("C", project_path="/proj/y"))

    assert registry.unregister_by_project("/proj/x") == 2

    reborn = WatcherRegistry(store=store)
    reborn.restore()
    assert [w.alias for w in reborn.all()] == ["C"]
    assert reborn.get(keeper.id) is not None


# --- checkpoint behaviour --------------------------------------------------

def test_checkpoint_is_a_noop_when_nothing_changed(store):
    registry = WatcherRegistry(store=store)
    registry.register(_watcher())
    assert registry.checkpoint() is False, "an idle daemon must not rewrite the file"


def test_checkpoint_writes_after_state_drift(store):
    registry = WatcherRegistry(store=store)
    w = registry.register(_watcher())
    w.last_message_at += 60
    assert registry.checkpoint() is True
    assert registry.checkpoint() is False


# --- robustness ------------------------------------------------------------

def test_corrupt_state_file_does_not_stop_the_daemon(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ this is not json")

    registry = WatcherRegistry(store=store)
    assert registry.restore() == 0, "an empty registry is recoverable; a crash loop is not"


def test_unreadable_entry_is_skipped_without_losing_the_others(store):
    good = _watcher("Good")
    WatcherRegistry(store=store).register(good)
    raw = json.loads(store.path.read_text())
    raw["watchers"].append({"nonsense": True})
    store.path.write_text(json.dumps(raw))

    registry = WatcherRegistry(store=store)
    assert registry.restore() == 1
    assert registry.all()[0].alias == "Good"


def test_missing_state_file_starts_empty(tmp_path):
    registry = WatcherRegistry(store=JsonFileWatcherStore(tmp_path / "never-written.json"))
    assert registry.restore() == 0


def test_state_file_is_owner_only(store):
    WatcherRegistry(store=store).register(_watcher())
    assert store.path.stat().st_mode & 0o077 == 0, "watcher state must not be world-readable"


def test_write_failure_does_not_break_registration(tmp_path, monkeypatch):
    """A full disk must not stop the daemon from serving; it only loses persistence."""
    store = JsonFileWatcherStore(tmp_path / "watchers.json")

    def boom(*a, **kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    registry = WatcherRegistry(store=store)
    w = registry.register(_watcher())
    assert registry.get(w.id) is not None


def test_default_registry_persists_nothing(tmp_path):
    """The historical in-memory behaviour stays available as an explicit choice."""
    registry = WatcherRegistry()
    registry.register(_watcher())
    assert isinstance(registry._store, NullWatcherStore)
    assert WatcherRegistry().restore() == 0
