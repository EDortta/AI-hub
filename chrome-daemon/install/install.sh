#!/usr/bin/env bash
# Install ai-hub chrome-daemon as a systemd user service + the ai-hub CLI (pipx).
#
# Usage:
#   install.sh              # full install: daemon deps + systemd unit + CLI
#   install.sh --cli-only   # only (re)install the ai-hub CLI via pipx — use this
#                           # after a git pull that changed ai_hub/, without
#                           # touching the running daemon service
#   install.sh --skip-cli   # only daemon deps + systemd unit; pipx not required
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_SRC="$SCRIPT_DIR/chrome-daemon.service"
SERVICE_DST="$HOME/.config/systemd/user/chrome-daemon.service"

if [ $# -gt 1 ]; then
    echo "Too many arguments: expected at most one of --cli-only | --skip-cli" >&2
    exit 2
fi
INSTALL_DAEMON=1
INSTALL_CLI=1
case "${1:-}" in
    --cli-only) INSTALL_DAEMON=0 ;;
    --skip-cli) INSTALL_CLI=0 ;;
    "") ;;
    *) echo "Unknown option: $1 (expected --cli-only or --skip-cli)" >&2; exit 2 ;;
esac

# Fail early: when the CLI is part of this run, require pipx before touching
# anything else on the system.
if [ "$INSTALL_CLI" = 1 ] && ! command -v pipx >/dev/null 2>&1; then
    echo "ERROR: pipx not found. Install it first:" >&2
    echo "  sudo apt install pipx                    # Debian/Ubuntu (Debian 12+: pip fora de venv é bloqueado por PEP 668)" >&2
    echo "  python3 -m pip install --user pipx && python3 -m pipx ensurepath   # fora de Debian/PEP 668" >&2
    echo "Or run with --skip-cli to install only the daemon." >&2
    exit 1
fi

if [ "$INSTALL_DAEMON" = 1 ]; then
    echo "==> Installing Python dependencies..."
    # --break-system-packages: Debian 12+ marks the system Python as externally
    # managed (PEP 668) and rejects plain `pip install` outside a venv.
    pip install --break-system-packages -r "$DAEMON_DIR/requirements.txt" --quiet \
        || pip install -r "$DAEMON_DIR/requirements.txt" --quiet

    echo "==> Installing Playwright browsers..."
    playwright install chromium --quiet 2>/dev/null || true

    echo "==> Making main.py executable..."
    chmod +x "$DAEMON_DIR/main.py"

    echo "==> Installing systemd user service..."
    mkdir -p "$HOME/.config/systemd/user"
    # ExecStart is rewritten from DAEMON_DIR so the unit follows the repo wherever it
    # is checked out, instead of baking in the path this file happened to ship with.
    sed "s|^ExecStart=.*|ExecStart=$DAEMON_DIR/main.py|" "$SERVICE_SRC" > "$SERVICE_DST"
    systemctl --user daemon-reload
    systemctl --user enable chrome-daemon.service
    systemctl --user start chrome-daemon.service
fi

if [ "$INSTALL_CLI" = 1 ]; then
    echo ""
    echo "==> Installing ai-hub CLI via pipx..."
    # The CLI used to be a symlink into this checkout (~/.local/bin/ai-hub ->
    # chrome-daemon/cli.py). Remove the legacy symlink so it cannot shadow the
    # pipx-installed binary, then install the real console_script (issue 007).
    if [ -L "$HOME/.local/bin/ai-hub" ]; then
        echo "    removing legacy symlink $HOME/.local/bin/ai-hub"
        rm -f "$HOME/.local/bin/ai-hub"
    fi
    # Stale in-tree build artifacts (pip builds in-tree; .gitignore hides them)
    # would otherwise leak removed/renamed modules into the wheel forever.
    rm -rf "$DAEMON_DIR/build" "$DAEMON_DIR/ai_hub.egg-info"
    # uninstall first: `pipx install --force` reuses an existing `ai-hub` venv
    # (leftover deps/scripts from a same-named package survive); uninstall
    # guarantees a clean slate. --force still guards pipx's name-collision quirks.
    pipx uninstall ai-hub >/dev/null 2>&1 || true
    pipx install --force "$DAEMON_DIR"
elif [ -L "$HOME/.local/bin/ai-hub" ] && [ ! -e "$HOME/.local/bin/ai-hub" ]; then
    # --skip-cli on a host where the pull already deleted chrome-daemon/cli.py:
    # the legacy symlink is dangling and every `ai-hub` call fails with ENOENT.
    echo ""
    echo "WARNING: ~/.local/bin/ai-hub is a dangling symlink (the CLI it pointed" >&2
    echo "to no longer exists). Removing it. Run 'install.sh --cli-only' to" >&2
    echo "install the packaged CLI (requires pipx)." >&2
    rm -f "$HOME/.local/bin/ai-hub"
fi

echo ""
if [ "$INSTALL_DAEMON" = 1 ]; then
    echo "Done! Service status:"
    systemctl --user status chrome-daemon.service --no-pager || true
    echo ""
fi
if command -v ai-hub >/dev/null 2>&1 || [ "$INSTALL_CLI" = 1 ]; then
    echo "Commands:"
    echo "  ai-hub status        — show daemon + watchers"
    echo "  ai-hub setup         — open Chrome for ChatGPT login"
else
    echo "NOTE: the ai-hub CLI is not installed on this host (ran with --skip-cli)."
    echo "      Run 'install.sh --cli-only' when pipx is available."
fi
echo "  journalctl --user -u chrome-daemon -f   — follow logs"
# NOTE: this script never restarts a daemon that is already running
# (systemctl start is a no-op then). Restarting is a separate, operator-gated
# step: systemctl --user restart chrome-daemon
