#!/usr/bin/env bash
# Installs omarchy-relay for the current user: system deps via pacman,
# the package under ~/.local/share, and a launcher on PATH.
set -euo pipefail

DEPS=(python-paho-mqtt python-cryptography python-textual python-gobject gtk4 libadwaita)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-relay"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

if ! command -v pacman >/dev/null 2>&1; then
  echo "This installer targets Arch/Omarchy (needs pacman). On another distro," >&2
  echo "create a venv and 'pip install paho-mqtt cryptography textual' instead," >&2
  echo "then run: python3 -m omarchy_relay.cli <command> from $REPO_DIR" >&2
  exit 1
fi

echo "==> Installing dependencies (requires sudo): ${DEPS[*]}"
sudo pacman -S --needed "${DEPS[@]}"

echo "==> Installing package to $INSTALL_DIR"
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -r "$REPO_DIR/omarchy_relay" "$INSTALL_DIR/"

mkdir -p "$BIN_DIR"
LAUNCHER="$BIN_DIR/omarchy-relay"
cat > "$LAUNCHER" <<LAUNCHER_EOF
#!/usr/bin/env bash
exec env PYTHONPATH="$INSTALL_DIR:\${PYTHONPATH:-}" /usr/bin/python3 -m omarchy_relay.cli "\$@"
LAUNCHER_EOF
chmod +x "$LAUNCHER"

mkdir -p "$DESKTOP_DIR"
cp "$REPO_DIR/packaging/omarchy-relay.desktop" "$DESKTOP_DIR/"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

# Bar icon (Omarchy shell only): left of the AI-agents icon, focuses the
# window if it's already running or launches it fresh otherwise. Only
# touches shell.json on an actual Omarchy system (needs the shell's own
# launch-or-focus helper), and backs the file up first since it may
# already hold the user's own bar customizations.
BAR_ICON_ID="omarchy-relay"
SHELL_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/shell.json"
SHELL_DEFAULT="/usr/share/omarchy/config/omarchy/shell.json"
if command -v omarchy-launch-or-focus >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
  echo "==> Adding an Omarchy Relay icon to the top bar"
  mkdir -p "$(dirname "$SHELL_CONFIG")"
  if [[ ! -f "$SHELL_CONFIG" ]]; then
    if [[ -f "$SHELL_DEFAULT" ]]; then
      cp "$SHELL_DEFAULT" "$SHELL_CONFIG"
    else
      echo '{"version":1,"bar":{"layout":{"right":[]}}}' > "$SHELL_CONFIG"
    fi
  fi
  cp "$SHELL_CONFIG" "$SHELL_CONFIG.bak.$(date +%s)"
  ENTRY_JSON=$(jq -n --arg id "$BAR_ICON_ID" '{
    id: $id,
    type: "command",
    text: "",
    tooltip: "Omarchy Relay — click to open or bring to front",
    onClick: "omarchy-launch-or-focus \"net.omarchy.Relay\" \"omarchy-relay gui\""
  }')
  jq --argjson entry "$ENTRY_JSON" --arg id "$BAR_ICON_ID" '
    (.bar.layout.right // []) as $r
    | if ($r | any(.id == $id)) then .
      else
        .bar.layout.right = (
          ($r | map(.id) | index("omarchy.agents")) as $idx
          | if $idx == null then $r + [$entry] else $r[0:$idx] + [$entry] + $r[$idx:] end
        )
      end
  ' "$SHELL_CONFIG" > "$SHELL_CONFIG.tmp" && mv "$SHELL_CONFIG.tmp" "$SHELL_CONFIG"
  echo "    Added (backup: $SHELL_CONFIG.bak.*; the shell hot-reloads shell.json automatically)"
else
  echo "==> Skipping bar icon (not an Omarchy shell, or jq missing) — omarchy-relay still works via the CLI/app launcher"
fi

echo "==> Installed. 'Omarchy Relay' is in your app launcher (omarchy-relay gui), or use the CLI:"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "    NOTE: $BIN_DIR is not on your PATH. Omarchy adds it by default; if yours doesn't, add:" ;
     echo "      export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
echo "==> Next steps:"
echo "    1. Point omarchy-relay at a broker you control (docker/ has a self-hosted Mosquitto setup)."
echo "    2. omarchy-relay init"
echo "    3. omarchy-relay gui         (or: chat / chat --tui in a terminal)"
