#!/usr/bin/env bash
# Installs omarchy-relay for the current user: system deps via pacman,
# the package under ~/.local/share, and a launcher on PATH.
set -euo pipefail

DEPS=(python-paho-mqtt python-cryptography python-textual)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-relay"
BIN_DIR="$HOME/.local/bin"

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
exec env PYTHONPATH="$INSTALL_DIR:\${PYTHONPATH:-}" python3 -m omarchy_relay.cli "\$@"
LAUNCHER_EOF
chmod +x "$LAUNCHER"

echo "==> Installed."
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "    NOTE: $BIN_DIR is not on your PATH. Omarchy adds it by default; if yours doesn't, add:" ;
     echo "      export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
echo "==> Next steps:"
echo "    1. Point omarchy-relay at a broker you control (docker/ has a self-hosted Mosquitto setup)."
echo "    2. omarchy-relay init"
echo "    3. omarchy-relay chat        (or: omarchy-relay chat --tui)"
