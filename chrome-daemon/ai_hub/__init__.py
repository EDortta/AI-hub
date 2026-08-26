"""ai-hub — CLI and Python client for the AI-Hub chrome-daemon.

The daemon itself (main.py & friends) is not part of this package; it keeps
running from the repository checkout under systemd. This package exists so the
`ai-hub` binary on PATH is a real console_script installed via pipx, not a
symlink into a checkout (hub issue 007, part 2).
"""
from __future__ import annotations

__version__ = "0.1.0"
