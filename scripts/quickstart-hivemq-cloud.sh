#!/usr/bin/env bash
# One-shot setup using your own HiveMQ Cloud cluster (free "Serverless"
# tier) — private, TLS + auth, no infrastructure for you to run. You need
# a cluster and a credential first:
#
#   1. Sign up free at https://console.hivemq.cloud
#   2. Create a Serverless cluster (free tier)
#   3. In the cluster's Access Management, create a username/password credential
#   4. Copy the cluster URL from its Connection tab
#      (looks like <id>.s1.<region>.hivemq.cloud)
#
# This script installs everything else and wires those details in.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NICKNAME="${1:-$(hostname)}"
NETWORK_NAME="${OMARCHY_RELAY_NETWORK:-home}"

if ! command -v pacman >/dev/null 2>&1; then
  echo "This targets Arch/Omarchy (needs pacman)." >&2
  exit 1
fi

if [ -f "$HOME/.config/omarchy-relay/config.toml" ]; then
  echo "omarchy-relay is already configured at ~/.config/omarchy-relay/config.toml — nothing to do." >&2
  echo "(delete it first, or run ./install.sh + 'omarchy-relay init' if you want a fresh setup)" >&2
  exit 1
fi

echo "HiveMQ Cloud setup — paste in your cluster's details"
echo "(console.hivemq.cloud -> your cluster -> Connection / Access Management)"
echo
read -r -p "Cluster URL (e.g. abc123.s1.eu.hivemq.cloud): " CLUSTER_HOST
if [ -z "$CLUSTER_HOST" ]; then
  echo "Cluster URL is required." >&2
  exit 1
fi
read -r -p "Username: " MQ_USER
read -r -s -p "Password: " MQ_PASS
echo
if [ -z "$MQ_USER" ] || [ -z "$MQ_PASS" ]; then
  echo "Username and password are required." >&2
  exit 1
fi

echo "==> Installing dependencies (sudo)"
sudo pacman -S --needed python-paho-mqtt python-cryptography python-textual

NETWORK_PASSPHRASE="$(openssl rand -base64 24)"

echo "==> Installing omarchy-relay"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-relay"
BIN_DIR="$HOME/.local/bin"
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
cp -r "$REPO_DIR/omarchy_relay" "$INSTALL_DIR/"
cat > "$BIN_DIR/omarchy-relay" <<LAUNCHER
#!/usr/bin/env bash
exec env PYTHONPATH="$INSTALL_DIR:\${PYTHONPATH:-}" python3 -m omarchy_relay.cli "\$@"
LAUNCHER
chmod +x "$BIN_DIR/omarchy-relay"

echo "==> Writing config (HiveMQ Cloud, TLS)"
# Values below came from `read` (free-form user input, possibly containing
# quotes) — pass them through the environment rather than interpolating
# into the Python source, so nothing can break out of a string literal.
CLUSTER_HOST="$CLUSTER_HOST" MQ_USER="$MQ_USER" MQ_PASS="$MQ_PASS" \
NICKNAME="$NICKNAME" NETWORK_NAME="$NETWORK_NAME" NETWORK_PASSPHRASE="$NETWORK_PASSPHRASE" \
python3 -c "
import os, sys
sys.path.insert(0, '$INSTALL_DIR')
from omarchy_relay.config import Config, default_device_id
Config(
    nickname=os.environ['NICKNAME'],
    device_id=default_device_id(),
    network_name=os.environ['NETWORK_NAME'],
    passphrase=os.environ['NETWORK_PASSPHRASE'],
    broker_host=os.environ['CLUSTER_HOST'],
    broker_port=8883,
    broker_tls=True,
    broker_username=os.environ['MQ_USER'],
    broker_password=os.environ['MQ_PASS'],
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
  echo " relayed through your HiveMQ Cloud cluster ($CLUSTER_HOST)."
else
  echo " Setup finished, but the daemon isn't active yet — check:"
  echo "   journalctl --user -xeu omarchy-relay-daemon"
  echo " (double-check the cluster URL and credentials if it's failing to connect)"
fi
echo
echo " Try it:      omarchy-relay chat        (or: omarchy-relay chat --tui)"
echo " Daemon logs: journalctl --user -fu omarchy-relay-daemon"
echo
echo " To add another machine to this network, on that machine run:"
echo "   ./install.sh && omarchy-relay init   (broker option 2, HiveMQ Cloud)"
echo " with these values:"
echo "   network name:     $NETWORK_NAME"
echo "   passphrase:       $NETWORK_PASSPHRASE"
echo "   broker host:      $CLUSTER_HOST"
echo "   broker username:  $MQ_USER"
echo "   broker password:  (the one you set for this credential)"
echo " (Create a separate HiveMQ Cloud credential per device if you'd rather"
echo " not share one password — this cluster is yours to configure however.)"
echo "======================================================================"
