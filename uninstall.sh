#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-relay"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-relay"

rm -rf "$INSTALL_DIR"
rm -f "$BIN_DIR/omarchy-relay"
echo "Removed $INSTALL_DIR and $BIN_DIR/omarchy-relay"

if [ -d "$CONFIG_DIR" ]; then
  read -r -p "Also delete config (contains your network passphrase) at $CONFIG_DIR? [y/N] " reply
  if [[ "$reply" =~ ^[Yy]$ ]]; then
    rm -rf "$CONFIG_DIR"
    echo "Removed $CONFIG_DIR"
  fi
fi
