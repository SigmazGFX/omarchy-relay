#!/usr/bin/env bash
# One-shot setup using HiveMQ's free public test broker: no self-hosted
# broker, no firewall changes, no exposed port. The only sudo step left is
# installing three packages from the Arch repos.
#
# HiveMQ's public broker (mqttdashboard.com) has NO authentication and is
# shared with the entire internet. omarchy-relay's own encryption still
# protects message/file CONTENT (only devices with your passphrase can read
# it), but there's no privacy guarantee for metadata, no auth stopping
# someone from spamming your topic, and no uptime/persistence promise. Fine
# for casual use and testing; for anything you actually care about, use
# scripts/quickstart.sh (self-hosted) or a private HiveMQ Cloud cluster
# instead (see README's Broker options).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v pacman >/dev/null 2>&1; then
  echo "This targets Arch/Omarchy (needs pacman)." >&2
  exit 1
fi

if [ -f "$HOME/.config/omarchy-relay/config.toml" ]; then
  echo "omarchy-relay is already configured at ~/.config/omarchy-relay/config.toml — nothing to do." >&2
  echo "(delete it first, or run ./install.sh + 'omarchy-relay init' if you want a fresh setup)" >&2
  exit 1
fi

# NICKNAME/NETWORK_NAME can be pre-set in the environment to skip these
# prompts (useful for scripting); otherwise ask.
DEFAULT_NICKNAME="$(hostname)"
if [ -z "${NICKNAME:-}" ]; then
  read -r -p "Nickname [$DEFAULT_NICKNAME]: " NICKNAME
fi
NICKNAME="${NICKNAME:-$DEFAULT_NICKNAME}"

if [ -z "${NETWORK_NAME:-}" ]; then
  read -r -p "Network name (a room/group of devices) [home]: " NETWORK_NAME
fi
NETWORK_NAME="${NETWORK_NAME:-home}"

echo "==> Installing dependencies (sudo)"
sudo pacman -S --needed python-paho-mqtt python-cryptography python-textual python-gobject gtk4 libadwaita

# Use a hard-to-guess network name by default too, since this broker is
# public — anyone who guesses your network name still can't decrypt your
# traffic, but a random name means they'd have to guess it in the first
# place to even see that it exists.
NETWORK_PASSPHRASE="$(openssl rand -base64 24)"

echo "==> Installing omarchy-relay"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-relay"
BIN_DIR="$HOME/.local/bin"
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
cp -r "$REPO_DIR/omarchy_relay" "$INSTALL_DIR/"
cat > "$BIN_DIR/omarchy-relay" <<LAUNCHER
#!/usr/bin/env bash
exec env PYTHONPATH="$INSTALL_DIR:\${PYTHONPATH:-}" /usr/bin/python3 -m omarchy_relay.cli "\$@"
LAUNCHER
chmod +x "$BIN_DIR/omarchy-relay"

DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$DESKTOP_DIR"
cp "$REPO_DIR/packaging/omarchy-relay.desktop" "$DESKTOP_DIR/"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

echo "==> Writing config (HiveMQ public broker, TLS, no auth)"
/usr/bin/python3 -c "
import sys; sys.path.insert(0, '$INSTALL_DIR')
from omarchy_relay.config import Config, default_device_id
Config(
    nickname='$NICKNAME',
    device_id=default_device_id(),
    network_name='$NETWORK_NAME',
    passphrase='$NETWORK_PASSPHRASE',
    broker_host='mqttdashboard.com',
    broker_port=8883,
    broker_tls=True,
    broker_username='',
    broker_password='',
).save()
"

echo "==> Enabling background listener (systemd --user)"
mkdir -p "$HOME/.config/systemd/user"
cp "$REPO_DIR/systemd/omarchy-relay-daemon.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now omarchy-relay-daemon
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

sleep 2
echo
echo "======================================================================"
if systemctl --user is-active --quiet omarchy-relay-daemon; then
  echo " omarchy-relay is running as '$NICKNAME' on network '$NETWORK_NAME',"
  echo " relayed through HiveMQ's public test broker (mqttdashboard.com)."
else
  echo " Setup finished, but the daemon isn't active — check:"
  echo "   journalctl --user -xeu omarchy-relay-daemon"
fi
echo
echo " Reminder: this broker has no authentication and is shared with the"
echo " whole internet. Casual/testing use only — see the warning at the top"
echo " of this script for details."
echo
echo " Try it:      omarchy-relay gui         (also in your app launcher, or: chat / chat --tui)"
echo " Daemon logs: journalctl --user -fu omarchy-relay-daemon"
echo
echo " To add another machine to this network, on that machine run:"
echo "   ./install.sh && omarchy-relay init   (choose broker option 2)"
echo " or install.sh + set this config by hand, using:"
echo "   network name:     $NETWORK_NAME"
echo "   passphrase:       $NETWORK_PASSPHRASE"
echo "   broker host:      mqttdashboard.com"
echo "   broker port:      8883"
echo "   broker tls:       yes"
echo "   broker username:  (leave blank)"
echo "   broker password:  (leave blank)"
echo "======================================================================"
