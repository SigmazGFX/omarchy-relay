#!/usr/bin/env bash
# One-shot local setup: installs dependencies, stands up a self-hosted
# Mosquitto broker on THIS machine with generated credentials, installs
# omarchy-relay, writes its config, opens the firewall port, and starts the
# background listener as a systemd --user service. No manual broker setup.
#
# Run this on the first machine. To add another machine to the same
# network, run install.sh + `omarchy-relay init` there using the connection
# info this script prints at the end.
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

echo "==> Installing dependencies + Mosquitto (sudo)"
sudo pacman -S --needed python-paho-mqtt python-cryptography python-textual python-gobject gtk4 libadwaita mosquitto

MQ_USER="omarchy"
MQ_PASS="$(openssl rand -base64 18)"
NETWORK_PASSPHRASE="$(openssl rand -base64 24)"

echo "==> Configuring local Mosquitto broker (sudo)"
sudo mkdir -p /etc/mosquitto
sudo tee /etc/mosquitto/mosquitto.conf > /dev/null <<CONF
listener 1883 0.0.0.0
protocol mqtt
allow_anonymous false
password_file /etc/mosquitto/passwd
persistence true
persistence_location /var/lib/mosquitto/
CONF
sudo mosquitto_passwd -b -c /etc/mosquitto/passwd "$MQ_USER" "$MQ_PASS"
sudo systemctl enable --now mosquitto

if systemctl is-active --quiet ufw 2>/dev/null; then
  echo "==> Opening 1883/tcp in ufw (sudo)"
  sudo ufw allow 1883/tcp comment "omarchy-relay MQTT broker" || true
fi

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

echo "==> Writing config"
/usr/bin/python3 -c "
import sys; sys.path.insert(0, '$INSTALL_DIR')
from omarchy_relay.config import Config, default_device_id
Config(
    nickname='$NICKNAME',
    device_id=default_device_id(),
    network_name='$NETWORK_NAME',
    passphrase='$NETWORK_PASSPHRASE',
    broker_host='127.0.0.1',
    broker_port=1883,
    broker_tls=False,
    broker_username='$MQ_USER',
    broker_password='$MQ_PASS',
).save()
"

echo "==> Enabling background listener (systemd --user)"
mkdir -p "$HOME/.config/systemd/user"
cp "$REPO_DIR/systemd/omarchy-relay-daemon.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now omarchy-relay-daemon
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

sleep 2
LAN_IP="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"

echo
echo "======================================================================"
if systemctl --user is-active --quiet omarchy-relay-daemon; then
  echo " omarchy-relay is running as '$NICKNAME' on network '$NETWORK_NAME'."
else
  echo " Setup finished, but the daemon isn't active — check:"
  echo "   journalctl --user -xeu omarchy-relay-daemon"
fi
echo
echo " Try it:      omarchy-relay gui         (also in your app launcher, or: chat / chat --tui)"
echo " Daemon logs: journalctl --user -fu omarchy-relay-daemon"
echo
echo " To add another machine to this network: on that machine, run"
echo "   ./install.sh && omarchy-relay init"
echo " with these values (broker host is this machine's LAN/Tailscale IP):"
echo "   network name:     $NETWORK_NAME"
echo "   passphrase:       $NETWORK_PASSPHRASE"
echo "   broker host:      ${LAN_IP:-this machine LAN IP}"
echo "   broker port:      1883"
echo "   broker tls:       no"
echo "   broker username:  $MQ_USER"
echo "   broker password:  $MQ_PASS"
echo
echo " This broker is reachable from this LAN (and beyond, if you port-forward"
echo " or use Tailscale) with no TLS — fine on a trusted network, not the open"
echo " internet. See docker/README.md for TLS / overlay-network options."
echo "======================================================================"
