#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-relay"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-relay"
UNIT_FILE="$HOME/.config/systemd/user/omarchy-relay-daemon.service"
DESKTOP_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/omarchy-relay.desktop"

if systemctl --user is-enabled omarchy-relay-daemon >/dev/null 2>&1 \
   || systemctl --user is-active omarchy-relay-daemon >/dev/null 2>&1; then
  systemctl --user disable --now omarchy-relay-daemon >/dev/null 2>&1 || true
  echo "Stopped and disabled omarchy-relay-daemon"
fi
rm -f "$UNIT_FILE"
systemctl --user daemon-reload 2>/dev/null || true

rm -rf "$INSTALL_DIR"
rm -f "$BIN_DIR/omarchy-relay"
rm -f "$DESKTOP_FILE"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(dirname "$DESKTOP_FILE")" >/dev/null 2>&1 || true
echo "Removed $INSTALL_DIR, $BIN_DIR/omarchy-relay, and the app launcher entry"

if [ -d "$CONFIG_DIR" ]; then
  read -r -p "Also delete config (contains your network passphrase) at $CONFIG_DIR? [y/N] " reply
  if [[ "$reply" =~ ^[Yy]$ ]]; then
    rm -rf "$CONFIG_DIR"
    echo "Removed $CONFIG_DIR"
  fi
fi
