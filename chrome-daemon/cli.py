#!/usr/bin/env python3
"""ai-hub CLI — manage the AI-Hub daemon from any project directory.

Usage (run from inside the project dir):
    ai-hub register      # reads .ai-hub.yml and registers with daemon
    ai-hub unregister    # removes this project's watchers
    ai-hub status        # shows daemon status + active watchers
    ai-hub setup         # opens Chrome for manual ChatGPT login
    ai-hub logs          # tail daemon log (if journald)
    ai-hub generate-image <gpt_url> <prompt> [--orientation portrait|landscape|square]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Resolve daemon URL from env or default
import os
sys.path.insert(0, str(Path(__file__).parent))
from client import AIHubClient, DAEMON_URL


def _client() -> AIHubClient:
    return AIHubClient(project_path=str(Path.cwd()))


def cmd_register(args) -> int:
    client = _client()
    if not client.is_alive():
        print(f"ERROR: ai-hub daemon not reachable at {DAEMON_URL}. Is it running?", file=sys.stderr)
        return 1
    try:
        registered = client.register_from_config()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for r in registered:
        print(f"Registered: alias={r['alias']!r} id={r['id'][:8]} url={r['url']}")
    if not registered:
        print("No conversations found in .ai-hub.yml")
    return 0


def cmd_unregister(args) -> int:
    client = _client()
    result = client.unregister_project()
    print(f"Removed {result.get('removed', 0)} watcher(s) for project {Path.cwd()}")
    return 0


def cmd_status(args) -> int:
    client = _client()
    try:
        s = client.status()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Daemon: {'OK' if s['ok'] else 'ERROR'}")
    print(f"Chrome CDP: {'available' if s['chrome_cdp_available'] else 'NOT available'}")
    print(f"Active watchers: {s['watchers']}")
    print(f"Display: {s['display']}")
    print()

    convs = client.list_conversations()
    if not convs:
        print("No registered watchers.")
        return 0

    for w in convs:
        chatgpt = f" ↔ {w['chatgpt_alias']}" if w.get("chatgpt_alias") else ""
        print(f"  [{w['state'].upper():11s}] alias={w['alias']!r}{chatgpt}  url={w['url'][:60]}")
    return 0


def cmd_setup(args) -> int:
    client = _client()
    result = client._get("/setup")
    print(result.get("message", "Done."))
    return 0


def cmd_logs(args) -> int:
    os.execvp("journalctl", ["journalctl", "--user", "-u", "chrome-daemon", "-f"])


def cmd_generate_image(args) -> int:
    client = _client()
    print(f"Generating image via {args.gpt_url}...")
    try:
        path = client.generate_image(
            gpt_url=args.gpt_url,
            prompt=args.prompt,
            orientation=args.orientation,
        )
        print(f"Image saved: {path}")
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="ai-hub", description="AI-Hub daemon CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("register", help="Register this project with ai-hub (reads .ai-hub.yml)")
    sub.add_parser("unregister", help="Remove this project's watchers")
    sub.add_parser("status", help="Show daemon status and active watchers")
    sub.add_parser("setup", help="Open Chrome for manual ChatGPT login")
    sub.add_parser("logs", help="Tail daemon logs via journalctl")

    p_gen = sub.add_parser("generate-image", help="Generate an image")
    p_gen.add_argument("gpt_url", help="ChatGPT GPT URL")
    p_gen.add_argument("prompt", help="Image prompt")
    p_gen.add_argument("--orientation", default="portrait",
                       choices=("portrait", "landscape", "square"))

    args = parser.parse_args()

    commands = {
        "register": cmd_register,
        "unregister": cmd_unregister,
        "status": cmd_status,
        "setup": cmd_setup,
        "logs": cmd_logs,
        "generate-image": cmd_generate_image,
    }

    if args.command not in commands:
        parser.print_help()
        return 1

    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
