"""Alias-based message router.

Routes ChatGPT messages to the correct watcher based on the alias prefix.
Convention: "Claudia, faça X" → routes to the watcher with alias "claudia".
"""
from __future__ import annotations

import re

_PREFIX_RE = re.compile(r"^([\w][\w\s]{0,20}?),\s*(.+)", re.DOTALL)


def extract_alias_and_text(message: str) -> tuple[str, str] | None:
    """Returns (alias, text) if message starts with 'Alias, ...', else None."""
    m = _PREFIX_RE.match(message.strip())
    if not m:
        return None
    alias = m.group(1).strip()
    text = m.group(2).strip()
    return alias, text


def find_watcher_for_message(message: str, watchers: list) -> tuple | None:
    """Returns (watcher, text_without_prefix) or None if no alias matches."""
    parsed = extract_alias_and_text(message)
    if not parsed:
        return None
    alias, text = parsed
    alias_lower = alias.lower()
    for w in watchers:
        if w.alias and w.alias.lower() == alias_lower:
            return w, text
    # Partial match fallback
    for w in watchers:
        if w.alias and alias_lower.startswith(w.alias.lower()):
            return w, text
    return None
