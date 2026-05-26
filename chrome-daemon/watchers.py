"""Conversation watcher registry and polling loop.

Each registered conversation is polled at different rates depending on state:
- INTERACTION: recent activity → poll every interaction_poll_seconds
- LATENCY: silence → poll every latency_poll_seconds

New user messages are routed by alias and dispatched to the project's callback_url.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from chrome_manager import ChromeManager, playwright_executor
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
# Registry
# ---------------------------------------------------------------------------

class WatcherRegistry:
    def __init__(self):
        self._watchers: dict[str, ConversationWatcher] = {}

    def register(self, w: ConversationWatcher) -> ConversationWatcher:
        self._watchers[w.id] = w
        log.info("Registered watcher %s alias=%r url=%s", w.id[:8], w.alias, w.url)
        return w

    def unregister(self, watcher_id: str) -> bool:
        if watcher_id in self._watchers:
            del self._watchers[watcher_id]
            return True
        return False

    def unregister_by_project(self, project_path: str) -> int:
        ids = [wid for wid, w in self._watchers.items() if w.project_path == project_path]
        for wid in ids:
            del self._watchers[wid]
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
    """Single poll cycle for one watcher. Uses sync playwright via playwright_executor."""
    loop = asyncio.get_event_loop()
    try:
        messages = await loop.run_in_executor(
            playwright_executor,
            lambda: _sync_fetch_messages(watcher.url),
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


async def run_polling_loop(registry: WatcherRegistry, chrome_manager_factory) -> None:
    """Main loop: polls all registered watchers at their respective intervals."""
    next_poll: dict[str, float] = {}
    active_tasks: dict[str, asyncio.Task] = {}

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

        await asyncio.sleep(1)
