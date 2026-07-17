"""Conversation watcher registry and polling loop.

Each registered conversation is polled at different rates depending on state:
- INTERACTION: recent activity → poll every interaction_poll_seconds
- LATENCY: silence → poll every latency_poll_seconds

New user messages are routed by alias and dispatched to the project's callback_url.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from chrome_manager import (
    CDP_URL,
    CHROME_PROFILE,
    XVFB_DISPLAY,
    ChromeManager,
    chrome_op_in_flight,
    ensure_xvfb,
    is_cdp_available,
    launch_chrome,
    playwright_executor,
    run_playwright_async,
)
from router import find_watcher_for_message

log = logging.getLogger("ai-hub.watchers")

# ---------------------------------------------------------------------------
# JavaScript injected into ChatGPT page to extract conversation turns
# ---------------------------------------------------------------------------
EXTRACT_JS = """
() => {
    const results = [];
    const turns = document.querySelectorAll('[data-testid^="conversation-turn"]');
    for (const turn of turns) {
        const turnId = turn.getAttribute('data-testid') || '';
        const roleEl = turn.querySelector('[data-message-author-role]');
        const role = roleEl ? roleEl.getAttribute('data-message-author-role') : 'unknown';
        const content = turn.querySelector('.markdown.prose')
                     || turn.querySelector('.markdown')
                     || turn.querySelector('.whitespace-pre-wrap')
                     || turn.querySelector('[data-message-author-role]');
        const node = (content || turn);
        const tInner = (node.innerText || '').trim();
        const tContent = (node.textContent || '').trim();
        const text = (tContent.length > tInner.length ? tContent : tInner).trim();
        if (text) results.push({ role, text, turn_id: turnId });
    }
    if (results.length > 0) return results;
    const articles = document.querySelectorAll('article');
    for (const a of articles) {
        const text = a.innerText.trim();
        if (text) results.push({ role: 'unknown', text, turn_id: '' });
    }
    return results;
}
"""

EXPAND_JS = """
() => {
    const MAX_SAMPLE = 10;
    let clicked = 0;
    const sample = [];
    const matchAny = (s, patterns) => {
        if (!s) return false;
        const v = String(s).toLowerCase();
        return patterns.some(p => v.includes(p));
    };
    const isLikelyMenuMore = (text, label) => {
        const t = (text || '').toLowerCase().trim();
        const l = (label || '').toLowerCase().trim();
        if (!t && !l) return true;
        if (t === '...' || t === '…') return true;
        if (matchAny(t, ['more options', 'opções', 'menu', 'options'])) return true;
        if (matchAny(l, ['more options', 'opções', 'menu', 'options'])) return true;
        return false;
    };
    const patterns = ['show more','read more','expand','continue',
                      'mostrar mais','ver mais','ler mais','continuar','mais'];
    const turns = document.querySelectorAll('[data-testid^="conversation-turn"]');
    for (const turn of turns) {
        const candidates = turn.querySelectorAll('button, [role="button"], a');
        for (const el of candidates) {
            const label = (el.getAttribute && el.getAttribute('aria-label')) || '';
            const ariaExpanded = (el.getAttribute && el.getAttribute('aria-expanded')) || '';
            const text = (el.innerText || '').trim();
            if (isLikelyMenuMore(text, label)) continue;
            if (!text && !label) continue;
            const textLower = text.toLowerCase();
            const labelLower = String(label).toLowerCase();
            const explicitMatch = matchAny(labelLower, patterns) || matchAny(textLower, patterns);
            const shortTextOk = text.length > 0 && text.length <= 30;
            const collapsed = ariaExpanded === 'false';
            if (!(explicitMatch && (shortTextOk || collapsed))) continue;
            try { el.click(); clicked++; if (sample.length < MAX_SAMPLE) sample.push(text || label); } catch(e){}
        }
    }
    return { clicked, sample };
}
"""

_UI_ARTIFACTS = re.compile(r"^(Edit\s+|Copy\s+|Regenerate\s+)+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class WatcherState(str, Enum):
    INTERACTION = "interaction"
    LATENCY = "latency"


INBOX_MAX = 50


@dataclass
class ConversationWatcher:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    url: str = ""
    alias: str = ""
    chatgpt_alias: str = ""
    purpose: str = ""
    interaction_poll_seconds: int = 5
    latency_poll_seconds: int = 60
    callback_url: str = ""
    project_path: str = ""

    state: WatcherState = WatcherState.LATENCY
    last_message_at: float = field(default_factory=time.time)
    last_seen_hash: str | None = None
    registered_at: float = field(default_factory=time.time)

    # Inbox: messages addressed to this alias (user or assistant), capped at INBOX_MAX
    inbox: list = field(default_factory=list)
    # Hashes of all messages already routed to inbox (prevents duplicates)
    seen_hashes: set = field(default_factory=set)

    def poll_interval(self) -> int:
        if self.state == WatcherState.INTERACTION:
            return self.interaction_poll_seconds
        return self.latency_poll_seconds

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "alias": self.alias,
            "chatgpt_alias": self.chatgpt_alias,
            "purpose": self.purpose,
            "state": self.state.value,
            "interaction_poll_seconds": self.interaction_poll_seconds,
            "latency_poll_seconds": self.latency_poll_seconds,
            "callback_url": self.callback_url,
            "project_path": self.project_path,
            "last_message_at": self.last_message_at,
            "registered_at": self.registered_at,
            "inbox_count": len(self.inbox),
        }


# ---------------------------------------------------------------------------
# Persistence (issue 007)
# ---------------------------------------------------------------------------

WATCHERS_STATE_PATH = Path.home() / ".local" / "share" / "ai-hub" / "watchers.json"

# What survives a restart. Deliberately excludes `inbox`: the inbox holds verbatim
# ChatGPT message text, and persisting it would turn a transient queue into
# conversation content at rest. `seen_hashes` (one-way md5, no content) IS
# persisted — without it a restored watcher re-reads its whole conversation and
# routes every old message to the inbox as if it had just arrived.
_PERSISTED_FIELDS = (
    "id", "url", "alias", "chatgpt_alias", "purpose",
    "interaction_poll_seconds", "latency_poll_seconds",
    "callback_url", "project_path",
    "last_message_at", "last_seen_hash", "registered_at",
)


def _watcher_to_state(w: "ConversationWatcher") -> dict:
    data = {name: getattr(w, name) for name in _PERSISTED_FIELDS}
    data["state"] = w.state.value
    data["seen_hashes"] = sorted(w.seen_hashes)
    return data


def _watcher_from_state(data: dict) -> "ConversationWatcher":
    if not isinstance(data, dict):
        raise TypeError(f"watcher entry must be an object, got {type(data).__name__}")
    # Every ConversationWatcher field has a default, so an entry missing `id`/`url`
    # would silently construct a watcher with a fresh random id and no page to poll
    # — a ghost that never resolves for the consumer holding the real id. Reject it.
    for required in ("id", "url"):
        if not data.get(required):
            raise KeyError(required)

    kwargs = {name: data[name] for name in _PERSISTED_FIELDS if name in data}
    w = ConversationWatcher(**kwargs)
    try:
        w.state = WatcherState(data.get("state", WatcherState.LATENCY.value))
    except ValueError:
        w.state = WatcherState.LATENCY
    w.seen_hashes = set(data.get("seen_hashes") or [])
    return w


class WatcherStore:
    """Where a registry's watchers survive a restart.

    An interface, so the registry never learns what a file is: tests use the
    in-memory implementation, the daemon uses JSON on disk, and a future backend
    (sqlite, redis) is a new class rather than an edit to the registry.
    """

    def load(self) -> "list[ConversationWatcher]":
        raise NotImplementedError

    def save(self, watchers: "list[ConversationWatcher]") -> None:
        raise NotImplementedError


class NullWatcherStore(WatcherStore):
    """Persists nothing — the historical behaviour, kept as an explicit choice."""

    def load(self) -> "list[ConversationWatcher]":
        return []

    def save(self, watchers: "list[ConversationWatcher]") -> None:
        return None


class JsonFileWatcherStore(WatcherStore):
    """Watchers as a JSON file, written atomically and owner-readable only."""

    def __init__(self, path: Path = WATCHERS_STATE_PATH):
        self.path = Path(path)

    def load(self) -> "list[ConversationWatcher]":
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text())
            entries = raw.get("watchers", []) if isinstance(raw, dict) else raw
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt state file must not stop the daemon from booting: an empty
            # registry is recoverable (re-register), a crash loop is not.
            log.error("Watcher state at %s is unreadable (%s) — starting empty.", self.path, exc)
            return []

        watchers = []
        for entry in entries:
            try:
                watchers.append(_watcher_from_state(entry))
            except (TypeError, KeyError) as exc:
                log.error("Skipping unreadable watcher entry (%s): %r", exc, entry)
        return watchers

    def save(self, watchers: "list[ConversationWatcher]") -> None:
        payload = {"version": 1, "watchers": [_watcher_to_state(w) for w in watchers]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            os.chmod(tmp, 0o600)
            # Atomic: a crash mid-write leaves the previous good file, never a
            # truncated one that would read as "no watchers" on the next boot.
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("Could not persist watcher state to %s: %s", self.path, exc)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class WatcherRegistry:
    """The set of conversations this daemon watches.

    Membership changes are written through to `store` immediately; the poll loop
    calls `checkpoint()` to persist state drift (last_message_at, seen_hashes)
    only when something actually changed.
    """

    def __init__(self, store: WatcherStore | None = None):
        self._watchers: dict[str, ConversationWatcher] = {}
        self._store = store or NullWatcherStore()
        self._last_saved: str | None = None

    def restore(self) -> int:
        """Load persisted watchers. Returns how many came back."""
        restored = self._store.load()
        for w in restored:
            self._watchers[w.id] = w
        if restored:
            log.info(
                "Restored %d watcher(s) from store: %s",
                len(restored), ", ".join(repr(w.alias) for w in restored),
            )
        self._last_saved = self._fingerprint()
        return len(restored)

    def _fingerprint(self) -> str:
        return json.dumps([_watcher_to_state(w) for w in self.all()], sort_keys=True)

    def _persist(self) -> None:
        self._store.save(self.all())
        self._last_saved = self._fingerprint()

    def checkpoint(self) -> bool:
        """Persist only if the state changed since the last write. Returns True if written."""
        current = self._fingerprint()
        if current == self._last_saved:
            return False
        self._store.save(self.all())
        self._last_saved = current
        return True

    def register(self, w: ConversationWatcher) -> ConversationWatcher:
        self._watchers[w.id] = w
        log.info("Registered watcher %s alias=%r url=%s", w.id[:8], w.alias, w.url)
        self._persist()
        return w

    def unregister(self, watcher_id: str) -> bool:
        if watcher_id in self._watchers:
            del self._watchers[watcher_id]
            self._persist()
            return True
        return False

    def unregister_by_project(self, project_path: str) -> int:
        ids = [wid for wid, w in self._watchers.items() if w.project_path == project_path]
        for wid in ids:
            del self._watchers[wid]
        if ids:
            self._persist()
        return len(ids)

    def all(self) -> list[ConversationWatcher]:
        return list(self._watchers.values())

    def get(self, watcher_id: str) -> ConversationWatcher | None:
        return self._watchers.get(watcher_id)


# ---------------------------------------------------------------------------
# Message extraction (sync, called from asyncio via run_in_executor)
# ---------------------------------------------------------------------------

def _msg_hash(text: str) -> str:
    return hashlib.md5(text.strip()[:300].encode()).hexdigest()


def _expand_and_extract(page) -> list[dict[str, Any]]:
    try:
        result = page.evaluate(EXPAND_JS) or {}
        clicked = int(result.get("clicked") or 0)
        if clicked > 0:
            page.wait_for_timeout(600)
    except Exception:
        pass

    for _ in range(4):
        page.keyboard.press("PageDown")
        page.wait_for_timeout(250)

    try:
        result = page.evaluate(EXPAND_JS) or {}
        clicked = int(result.get("clicked") or 0)
        if clicked > 0:
            page.wait_for_timeout(600)
    except Exception:
        pass

    try:
        msgs = page.evaluate(EXTRACT_JS) or []
        cleaned = []
        for m in msgs:
            text = (m.get("text") or "").strip()
            if not text:
                continue
            text = _UI_ARTIFACTS.sub("", text).strip()
            if text:
                cleaned.append({**m, "text": text})
        return cleaned
    except Exception as e:
        log.warning("Error extracting messages: %s", e)
        return []


# ---------------------------------------------------------------------------
# Async variant of expand+extract (uses async playwright page)
# ---------------------------------------------------------------------------

async def _async_expand_and_extract(page) -> list[dict[str, Any]]:
    try:
        result = await page.evaluate(EXPAND_JS) or {}
        clicked = int(result.get("clicked") or 0)
        if clicked > 0:
            await page.wait_for_timeout(600)
    except Exception:
        pass

    for _ in range(4):
        await page.keyboard.press("PageDown")
        await page.wait_for_timeout(250)

    try:
        result = await page.evaluate(EXPAND_JS) or {}
        clicked = int(result.get("clicked") or 0)
        if clicked > 0:
            await page.wait_for_timeout(600)
    except Exception:
        pass

    try:
        msgs = await page.evaluate(EXTRACT_JS) or []
        cleaned = []
        for m in msgs:
            text = (m.get("text") or "").strip()
            if not text:
                continue
            text = _UI_ARTIFACTS.sub("", text).strip()
            if text:
                cleaned.append({**m, "text": text})
        return cleaned
    except Exception as e:
        log.warning("Error extracting messages (async): %s", e)
        return []


# ---------------------------------------------------------------------------
# Async polling loop
# ---------------------------------------------------------------------------

async def _dispatch_callback(callback_url: str, payload: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(callback_url, json=payload)
    except Exception as e:
        log.warning("Callback to %s failed: %s", callback_url, e)


def _sync_fetch_messages(url: str) -> list[dict]:
    """Fetch conversation messages synchronously. Runs in playwright_executor."""
    with ChromeManager() as mgr:
        page = mgr.get_or_open_page(url)
        return _expand_and_extract(page)


async def poll_watcher(watcher: ConversationWatcher, chrome_manager_factory) -> None:
    """Single poll cycle for one watcher. Uses sync playwright in a fresh thread."""
    loop = asyncio.get_event_loop()
    cdp_ok = await loop.run_in_executor(None, is_cdp_available)
    if not cdp_ok:
        return  # Chrome is down; watchdog handles recovery — don't leak playwright FDs

    try:
        messages = await run_playwright_async(
            lambda: _sync_fetch_messages(watcher.url),
            timeout=120,
        )
    except Exception as e:
        log.warning("Poll error for watcher %s: %s", watcher.id[:8], e)
        return

    # Find new user messages
    new_msgs = []
    found_last = watcher.last_seen_hash is None
    for msg in messages:
        if msg.get("role") != "user":
            continue
        h = _msg_hash(msg["text"])
        if not found_last:
            if h == watcher.last_seen_hash:
                found_last = True
            continue
        new_msgs.append(msg)

    if not new_msgs:
        # Check transition INTERACTION → LATENCY
        idle_s = time.time() - watcher.last_message_at
        if watcher.state == WatcherState.INTERACTION and idle_s > watcher.latency_poll_seconds:
            watcher.state = WatcherState.LATENCY
            log.info("Watcher %s → LATENCY (idle %.0fs)", watcher.id[:8], idle_s)
    else:
        # Update state to INTERACTION and dispatch each new user message
        watcher.state = WatcherState.INTERACTION
        watcher.last_message_at = time.time()

        for msg in new_msgs:
            watcher.last_seen_hash = _msg_hash(msg["text"])
            text = msg["text"]

            payload = {
                "watcher_id": watcher.id,
                "alias": watcher.alias,
                "url": watcher.url,
                "role": msg.get("role", "user"),
                "text": text,
                "turn_id": msg.get("turn_id", ""),
            }

            log.info("New message for alias=%r: %.60s…", watcher.alias, text)

            if watcher.callback_url:
                await _dispatch_callback(watcher.callback_url, payload)

    # Populate inbox: scan ALL messages (user + assistant) for alias-addressed content
    if watcher.alias:
        from router import find_watcher_for_message
        for msg in messages:
            h = _msg_hash(msg["text"])
            if h in watcher.seen_hashes:
                continue
            watcher.seen_hashes.add(h)
            parsed = find_watcher_for_message(msg["text"], [watcher])
            if parsed:
                _, body = parsed
                entry = {
                    "role": msg.get("role", "unknown"),
                    "text": msg["text"],
                    "body": body,
                    "turn_id": msg.get("turn_id", ""),
                    "ts": time.time(),
                }
                watcher.inbox.append(entry)
                if len(watcher.inbox) > INBOX_MAX:
                    watcher.inbox = watcher.inbox[-INBOX_MAX:]
                log.info("Inbox[%s] ← [%s] %.60s…", watcher.alias, entry["role"], body)


_CHECKPOINT_INTERVAL = 30     # seconds between watcher-state checkpoints (no-op if unchanged)
_CHROME_DOWN_THRESHOLD = 3    # consecutive failures before restart attempt
_CHROME_RELAUNCH_MAX = 5      # max relaunch attempts before giving up and dying
_CHROME_CHECK_INTERVAL = 30   # seconds between CDP health checks
_CHROME_CPU_LIMIT = 60.0      # % — combined CPU threshold to trigger stale-Chrome inspection
_CHROME_PROC_CPU_MIN = 10.0   # % — individual process must exceed this to be considered runaway
_CHROME_PROC_AGE_MIN = 300    # seconds — process must be older than this to be killed (5 min)


def _chrome_snapshot() -> "list[tuple]":
    """Return [(psutil.Process, cpu_percent)] for all Chrome processes.

    Uses a two-sample measurement (0.5 s apart) to get a meaningful per-process
    CPU reading instead of the always-zero first-call value.
    """
    try:
        import psutil
    except ImportError:
        return []
    attrs = ["pid", "name", "create_time"]
    procs = [p for p in psutil.process_iter(attrs) if "chrome" in (p.info["name"] or "").lower()]
    if not procs:
        return []
    for p in procs:
        try:
            p.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(0.5)
    result = []
    for p in procs:
        try:
            result.append((p, p.cpu_percent(interval=None)))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def _chrome_cpu_total() -> float:
    """Return the combined CPU % of all running Chrome processes."""
    return sum(cpu for _, cpu in _chrome_snapshot())


def _managed_chrome_pids() -> "set[int]":
    """PIDs of the daemon's own Chrome: the launched process AND its descendants.

    Sparing only the parent PID is not enough. Chrome's renderers are separate
    child processes, and a renderer is exactly what burns CPU while a page works
    — it is the child, not the parent, that trips the age+CPU heuristic. Issue
    001 recorded the watchdog SIGKILLing one mid-generation
    ("killing stale Chrome pid=3568219 age=376s cpu=37.9% display=''"), which
    closed the Playwright driver and produced the 500. Killing a renderer of the
    managed browser is never the intent of a *stale-process* reaper, so the whole
    tree is protected.
    """
    try:
        import psutil
        import chrome_manager as _cm
    except ImportError:
        return set()

    proc = getattr(_cm, "_chrome_process", None)
    if proc is None or proc.poll() is not None:
        return set()

    pids = {proc.pid}
    try:
        parent = psutil.Process(proc.pid)
        pids.update(child.pid for child in parent.children(recursive=True))
    except Exception:
        # Parent vanished or is unreadable — still spare the PID we know about.
        pass
    return pids


def _kill_stale_chrome() -> int:
    """Kill only Chrome processes that are hidden, old, and individually CPU-heavy.

    A process is killed only when ALL three conditions hold:
      1. Hidden — DISPLAY is not ':0' (running on Xvfb or no display, not the user's screen).
      2. Old    — running for more than _CHROME_PROC_AGE_MIN seconds (5 min).
      3. Hot    — individual CPU% > _CHROME_PROC_CPU_MIN (actually consuming resources).

    The daemon's own Chrome process *tree* (_managed_chrome_pids) is always spared
    regardless of the above criteria.

    Refuses to kill anything at all while a long Chrome operation is in flight.
    The guard lives here, next to the SIGKILL, rather than only at the watchdog
    call site: any future caller of this reaper inherits the protection instead of
    having to remember it (issue 001).
    """
    try:
        import psutil
        import chrome_manager as _cm
    except ImportError:
        return 0

    if _cm.chrome_op_in_flight():
        log.debug("Chrome watchdog: operation in flight — refusing to kill any Chrome process.")
        return 0

    protected_pids = _managed_chrome_pids()

    now = time.time()
    killed = 0
    for p, cpu in _chrome_snapshot():
        try:
            if p.pid in protected_pids:
                log.debug("Chrome watchdog: sparing managed Chrome pid=%d (cpu=%.1f%%)", p.pid, cpu)
                continue

            age = now - p.info["create_time"]
            if age < _CHROME_PROC_AGE_MIN:
                log.debug("Chrome watchdog: skipping young Chrome pid=%d age=%.0fs", p.pid, age)
                continue

            if cpu < _CHROME_PROC_CPU_MIN:
                log.debug("Chrome watchdog: skipping low-CPU Chrome pid=%d cpu=%.1f%%", p.pid, cpu)
                continue

            try:
                display = p.environ().get("DISPLAY", "")
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                display = ""
            if display == ":0":
                log.debug("Chrome watchdog: skipping visible Chrome pid=%d (DISPLAY=:0)", p.pid)
                continue

            log.warning(
                "Chrome watchdog: killing stale Chrome pid=%d age=%.0fs cpu=%.1f%% display=%r",
                p.pid, age, cpu, display,
            )
            os.kill(p.pid, signal.SIGKILL)
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError, PermissionError):
            pass
    return killed


async def run_chrome_watchdog(
    cdp_url: str = CDP_URL,
    profile_dir=CHROME_PROFILE,
    xvfb_display: str = XVFB_DISPLAY,
) -> None:
    """Periodically checks CDP availability and relaunches Chrome when it's down.

    After _CHROME_RELAUNCH_MAX failed relaunches the daemon kills itself so
    systemd can restart it cleanly — prevents file-descriptor exhaustion from
    repeated subprocess.Popen calls when Chrome is permanently down.
    """
    import os
    import signal

    consecutive_failures = 0
    relaunch_attempts = 0
    loop = asyncio.get_event_loop()

    while True:
        await asyncio.sleep(_CHROME_CHECK_INTERVAL)

        # CPU guard: if combined Chrome CPU exceeds the limit, kill stale hidden processes.
        # The daemon's own Chrome and any visible (:0) Chrome are always preserved.
        #
        # Skip the whole scan while a long operation (image generation, publish) is
        # in flight: high CPU then means "busy", not "stale". Killing Chrome — or a
        # renderer child — mid-generation closes the Playwright driver and produces a
        # 500. See AI-hub issue 001. _kill_stale_chrome re-checks the same guard;
        # skipping here also avoids the 0.5s CPU sampling that would be discarded.
        if chrome_op_in_flight():
            log.debug("Chrome watchdog: operation in flight — skipping stale-process scan.")
            cpu = 0.0
        else:
            cpu = await loop.run_in_executor(None, _chrome_cpu_total)
        if cpu > _CHROME_CPU_LIMIT:
            log.warning(
                "Chrome watchdog: combined Chrome CPU %.1f%% > %.0f%% — scanning for stale processes.",
                cpu, _CHROME_CPU_LIMIT,
            )
            killed = await loop.run_in_executor(None, _kill_stale_chrome)
            if killed:
                log.warning("Chrome watchdog: killed %d stale Chrome process(es).", killed)
            else:
                log.info("Chrome watchdog: no stale processes found (all are managed, visible, young, or low-CPU).")
        else:
            if cpu > 0:
                log.debug("Chrome watchdog: combined Chrome CPU %.1f%%", cpu)

        available = await loop.run_in_executor(None, lambda: is_cdp_available(cdp_url))
        if available:
            if consecutive_failures > 0:
                log.info("Chrome watchdog: CDP back online.")
            consecutive_failures = 0
            relaunch_attempts = 0
            continue

        consecutive_failures += 1
        log.warning(
            "Chrome watchdog: CDP unavailable (failure %d/%d).",
            consecutive_failures,
            _CHROME_DOWN_THRESHOLD,
        )

        if consecutive_failures < _CHROME_DOWN_THRESHOLD:
            continue

        if relaunch_attempts >= _CHROME_RELAUNCH_MAX:
            log.error(
                "Chrome watchdog: %d relaunch attempts failed — killing daemon so systemd can restart cleanly.",
                relaunch_attempts,
            )
            os.kill(os.getpid(), signal.SIGTERM)
            return

        relaunch_attempts += 1
        log.warning("Chrome watchdog: attempting relaunch #%d...", relaunch_attempts)
        try:
            display = await loop.run_in_executor(None, lambda: ensure_xvfb(xvfb_display))
            await loop.run_in_executor(
                None,
                lambda: launch_chrome(profile_dir=profile_dir, display=display, cdp_url=cdp_url),
            )
            log.info("Chrome watchdog: relaunch triggered — waiting 15s to verify.")
            await asyncio.sleep(15)
            ok = await loop.run_in_executor(None, lambda: is_cdp_available(cdp_url))
            if ok:
                log.info("Chrome watchdog: Chrome is back up.")
                consecutive_failures = 0
                relaunch_attempts = 0
            else:
                log.error("Chrome watchdog: Chrome still not available after relaunch #%d.", relaunch_attempts)
        except Exception as exc:
            log.error("Chrome watchdog: relaunch #%d failed: %s", relaunch_attempts, exc)


async def run_polling_loop(registry: WatcherRegistry, chrome_manager_factory) -> None:
    """Main loop: polls all registered watchers at their respective intervals."""
    next_poll: dict[str, float] = {}
    active_tasks: dict[str, asyncio.Task] = {}
    next_checkpoint = time.time() + _CHECKPOINT_INTERVAL

    while True:
        now = time.time()

        # Clean up completed tasks
        done = [wid for wid, t in active_tasks.items() if t.done()]
        for wid in done:
            del active_tasks[wid]

        for watcher in registry.all():
            wid = watcher.id
            if wid in active_tasks:  # previous poll still in progress — skip
                continue
            due = next_poll.get(wid, 0)
            if now >= due:
                task = asyncio.create_task(poll_watcher(watcher, chrome_manager_factory))
                active_tasks[wid] = task
                next_poll[wid] = now + watcher.poll_interval()

        # Persist state drift (seen_hashes, last_message_at) so a restart does not
        # re-deliver an entire conversation as new (issue 007). No-op when nothing
        # changed, so an idle daemon does not rewrite the file every 30s.
        if now >= next_checkpoint:
            registry.checkpoint()
            next_checkpoint = now + _CHECKPOINT_INTERVAL

        await asyncio.sleep(1)
