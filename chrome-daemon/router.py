"""Alias-based message router.

Routes ChatGPT messages to the correct watcher based on the alias prefix.
Conventions supported:
  "Beatriz, faça X"          → alias=beatriz
  "Hey, Beatriz, faça X"     → alias=beatriz  (greeting stripped)
  "Hei, Beatriz, faça X"     → alias=beatriz  (greeting stripped)
  "Hey Beatriz, faça X"      → alias=beatriz  (greeting without comma)
"""
from __future__ import annotations

import re

# Strips optional greeting "Hey[,] " or "Hei[,] " from the start
_GREET_RE = re.compile(r"^(?:hey|hei)[,\s]+", re.IGNORECASE)
_PREFIX_RE = re.compile(r"^([\w][\w\s]{0,20}?),\s*(.+)", re.DOTALL)


def extract_alias_and_text(message: str) -> tuple[str, str] | None:
    """Returns (alias, body) if message starts with '[Hey, ]Alias, ...', else None."""
    text = _GREET_RE.sub("", message.strip())
    m = _PREFIX_RE.match(text)
    if not m:
        return None
    alias = m.group(1).strip()
    body = m.group(2).strip()
    return alias, body


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
