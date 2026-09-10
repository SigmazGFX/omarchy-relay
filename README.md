# omarchy-relay

Realtime chat, direct messages, and file transfer between Omarchy machines
that don't have direct network connectivity to each other — relayed through
a shared MQTT broker, so any device that can reach the broker (home, laptop,
VPS, behind NAT, wherever) can talk to any other device in the same
"network."

## How it works

- Every device connects outbound to one MQTT broker (yours — see `docker/`
  to self-host one, or use a managed provider). No inbound ports, no direct
  peer-to-peer connectivity required.
- Devices are grouped into a **network**: a name + a shared passphrase.
  All message/file payloads are encrypted with a key derived from that
  passphrase (PBKDF2 → Fernet/AES). Anyone with the passphrase can read
  traffic on that network; anyone without it (including the broker
  operator, if it's not you) sees only ciphertext.
- **Presence** uses MQTT retained messages + Last Will: a device publishes
  its `{nickname, timestamp}` retained on connect, and the broker clears it
  automatically (Last Will) if the device disconnects ungracefully — so
  `peers` reflects who's actually online, not a stale list.
- **Chat** is a broadcast topic within the network; **DMs** are routed to a
  per-device topic, but note DMs are *routed*, not cryptographically
  private from other members — see [Security model](#security-model).
- **Files** are chunked, base64-encoded, encrypted per-chunk, and sent as
  a stream of MQTT messages with an explicit index. The receiver
  reassembles by seeking to `index * chunk_size` (tolerant of MQTT's
  at-least-once delivery: duplicates and out-of-order arrival are both
  handled), then verifies the whole file's SHA-256 before it's kept.

## Install

Targets Arch/Omarchy — dependencies are pulled from the official repos, no
pip/venv needed:

```sh
git clone <this repo> ~/Projects/omarchy-relay   # or wherever
cd ~/Projects/omarchy-relay
./install.sh
```

This installs `python-paho-mqtt`, `python-cryptography`, `python-textual`
via `pacman`, copies the package to `~/.local/share/omarchy-relay`, and
puts a launcher at `~/.local/bin/omarchy-relay`.

On another distro: create a venv, `pip install paho-mqtt cryptography
textual`, and run `python3 -m omarchy_relay.cli` from inside the repo.

## Set up a broker

You need an MQTT broker every device in your network can reach. There is
**no built-in default** — `omarchy-relay` refuses to start without
`broker.host` set, deliberately, so nobody accidentally broadcasts onto a
public test broker. See [`docker/README.md`](docker/README.md) for a
self-hosted Mosquitto setup with three options for making it reachable
(private overlay network, TLS, or a managed provider).

## Configure

```sh
omarchy-relay init
```

Walks you through nickname, network name + passphrase (share these with
whoever else should be in the network — out of band, not over the relay
itself), and broker connection details. Writes
`~/.config/omarchy-relay/config.toml`, mode `600`.

Multiple devices join the same network by using the same `network.name` +
`network.passphrase` and pointing at the same broker.

## Use it

```sh
omarchy-relay chat              # line-oriented interactive chat
omarchy-relay chat --tui        # full-screen Textual UI
omarchy-relay peers             # list who's online right now
omarchy-relay send FILE [--to nickname]   # send a file (default: broadcast)
omarchy-relay msg "text" [--to nickname]  # one-off message, no interactive session
omarchy-relay daemon            # headless: prints chat, auto-saves incoming files
```

Inside `chat`: `/peers`, `/msg <nick> <text>`, `/send <path> [nick]`,
`/help`, `/quit`.

Received files land in `transfer.downloads_dir` (default
`~/Downloads/omarchy-relay`), with a desktop notification via
`notify-send` if it's installed.

### Run the daemon in the background

```sh
mkdir -p ~/.config/systemd/user
cp systemd/omarchy-relay-daemon.service ~/.config/systemd/user/
systemctl --user enable --now omarchy-relay-daemon
journalctl --user -fu omarchy-relay-daemon   # watch chat/file activity
```

Useful on a headless machine, or any device you want reachable for file
transfers without keeping a chat window open.

## Security model

- **What's protected:** message and file *content* is encrypted end-to-end
  across the broker with a key derived from your network passphrase — the
  broker (and anyone who can read its traffic but doesn't know the
  passphrase) sees only ciphertext.
- **What's not:** this is *group* encryption, not per-peer end-to-end
  encryption. Every device that knows the network passphrase can decrypt
  everything on that network — broadcasts and "DMs" alike. A DM is routed
  to a specific device by topic, but any other member of the same network
  could technically decrypt it too if they were subscribed to that topic.
  If you want a channel that's actually private between two specific
  devices, give those two devices their own network name/passphrase pair
  that nobody else has.
- **Metadata:** the broker (and its operator) can always see connection
  timing, topic names, and message sizes, even though it can't read
  content. Self-hosting your own broker (see `docker/`) avoids handing
  that metadata to a third party.
- Config files are written `chmod 600` since they hold the passphrase and
  any broker credentials in plaintext.

## Limits worth knowing

- `transfer.max_file_size` defaults to 25 MB. Base64 + encryption overhead
  means a file this size becomes ~45 MB of actual MQTT traffic — raise the
  limit in your config if you need bigger, but expect broker/bandwidth
  cost to scale with it.
- `transfer.chunk_size` defaults to 64 KB of plaintext per chunk (~87 KB on
  the wire after encoding). Lower it if your broker enforces a smaller
  max packet size.

## Repo layout

```
omarchy_relay/     the package (config, crypto, mqttclient, transfer,
                    presence, chat, cli, tui)
install.sh          installs deps (pacman) + the omarchy-relay launcher
uninstall.sh
systemd/            user service unit for `omarchy-relay daemon`
docker/             self-hosted Mosquitto broker (compose + docs)
```
