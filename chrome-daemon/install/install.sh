#!/usr/bin/env bash
# Install ai-hub chrome-daemon as a systemd user service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_SRC="$SCRIPT_DIR/chrome-daemon.service"
SERVICE_DST="$HOME/.config/systemd/user/chrome-daemon.service"

echo "==> Installing Python dependencies..."
pip install -r "$DAEMON_DIR/requirements.txt" --quiet

echo "==> Installing Playwright browsers..."
playwright install chromium --quiet 2>/dev/null || true

echo "==> Making main.py executable..."
chmod +x "$DAEMON_DIR/main.py"
chmod +x "$DAEMON_DIR/cli.py"

echo "==> Installing systemd user service..."
mkdir -p "$HOME/.config/systemd/user"
cp "$SERVICE_SRC" "$SERVICE_DST"
systemctl --user daemon-reload
systemctl --user enable chrome-daemon.service
systemctl --user start chrome-daemon.service

echo ""
echo "==> Installing ai-hub CLI to ~/.local/bin/ai-hub..."
mkdir -p "$HOME/.local/bin"
ln -sf "$DAEMON_DIR/cli.py" "$HOME/.local/bin/ai-hub"

echo ""
echo "Done! Service status:"
systemctl --user status chrome-daemon.service --no-pager || true
echo ""
echo "Commands:"
echo "  ai-hub status        — show daemon + watchers"
echo "  ai-hub setup         — open Chrome for ChatGPT login"
echo "  journalctl --user -u chrome-daemon -f   — follow logs"
