# omarchy-relay

Realtime chat, direct messages, and file transfer between Omarchy machines
that don't have direct network connectivity to each other — relayed through
a shared MQTT broker, so any device that can reach the broker (home, laptop,
VPS, behind NAT, wherever) can talk to any other device in the same
"network."

## Quickstart (one command, no manual broker setup)

Three flavors, depending on how much infrastructure/trust you want:

```sh
git clone https://github.com/SigmazGFX/omarchy-relay ~/Projects/omarchy-relay
cd ~/Projects/omarchy-relay

./scripts/quickstart.sh              # self-hosted: installs + runs your own broker
# or
./scripts/quickstart-hivemq-cloud.sh # your own free HiveMQ Cloud cluster (sign up first)
# or
./scripts/quickstart-hivemq.sh       # zero infra: rides HiveMQ's free public broker
```

Every one of these prompts for a **nickname** and **network name** up
front (both have sane defaults — hostname and `home` — just press enter to
accept them). Set `NICKNAME=...` and/or `NETWORK_NAME=...` in the
environment beforehand to skip the prompts, e.g. for scripted installs.

All three install everything, configure omarchy-relay, and start it as a
background service — the only manual step every path shares is your
`sudo` password (needed to install three packages from the Arch repos).

- **`quickstart.sh`** additionally self-hosts a Mosquitto broker on this
  machine with generated credentials and opens the firewall port for it.
  Most private, but it's infrastructure you now run and keep online.
- **`quickstart-hivemq-cloud.sh`** uses your own free
  [HiveMQ Cloud](https://console.hivemq.cloud) cluster instead — private
  (TLS + your own credentials), but no broker for you to host. Requires a
  free sign-up first (the script tells you exactly what to click); it then
  prompts for the cluster URL + a username/password you create there and
  wires them in.
- **`quickstart-hivemq.sh`** skips all of that and points at
  `mqttdashboard.com` instead — no signup, no broker to run, no port to
  open. It's a public, unauthenticated broker shared with the whole
  internet: your message/file *content* is still encrypted (see
  [Security model](#security-model)), but there's no privacy guarantee for
  metadata and no uptime promise. Fine for trying this out or casual use;
  not for anything you'd mind losing or having timing/size metadata seen.

Every path prints the network name/passphrase/broker details at the end so
you can add other machines to the same network. See [Install](#install)
below for the manual/customizable path instead.

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
  `peers` reflects who's actually online, not a stale list. Going offline
  is debounced 5 seconds before a peer actually disappears from any
  online list — covers a brief reconnect (network blip, client restart)
  without it flickering off and back on; if they don't return in time,
  they're removed and "went offline" fires then, not the instant their
  presence first cleared.
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

This installs `python-paho-mqtt`, `python-cryptography`, `python-textual`,
`python-gobject`, `gtk4`, and `libadwaita` via `pacman`, copies the package
to `~/.local/share/omarchy-relay`, puts a launcher at
`~/.local/bin/omarchy-relay`, and adds "Omarchy Relay" to your app
launcher.

On another distro: create a venv, `pip install paho-mqtt cryptography
textual PyGObject`, install GTK4 + libadwaita through your package
manager, and run `python3 -m omarchy_relay.cli` from inside the repo.

## Set up a broker

You need an MQTT broker every device in your network can reach. There is
**no built-in default** — `omarchy-relay` refuses to start without
`broker.host` set, deliberately, so nobody accidentally broadcasts onto a
public broker without choosing to.

Options, roughly in order of "more private" to "less setup":

1. **Self-hosted Mosquitto** — `scripts/quickstart.sh`, or manually via
   [`docker/README.md`](docker/README.md), which also covers making it
   reachable from off-box (private overlay network, TLS, or a managed
   provider).
2. **HiveMQ Cloud (free "Serverless" tier)** — a *private* managed broker,
   TLS and your own credentials, no infrastructure to run yourself:
   1. Sign up free at [console.hivemq.cloud](https://console.hivemq.cloud).
   2. Create a Serverless cluster (the free tier).
   3. In the cluster's **Access Management**, create a username/password
      credential.
   4. Copy the cluster URL from its **Connection** tab — it looks like
      `<id>.s1.<region>.hivemq.cloud`.

   Then either run `./scripts/quickstart-hivemq-cloud.sh` (prompts for
   those three values and does everything else), or run `omarchy-relay
   init` and pick broker option 2 to enter them by hand. Port (`8883`) and
   TLS are set automatically either way — HiveMQ Cloud requires both.
3. **HiveMQ's public test broker** — `scripts/quickstart-hivemq.sh`, or
   `omarchy-relay init` broker option 3. Zero setup, zero signup, but
   public and unauthenticated — see the quickstart section above for the
   tradeoffs.

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
omarchy-relay gui                # native GTK4/libadwaita chat window
omarchy-relay chat               # line-oriented interactive chat
omarchy-relay chat --tui         # full-screen Textual UI
omarchy-relay peers              # list who's online right now
omarchy-relay send FILE [--to nickname]   # send a file (default: broadcast)
omarchy-relay msg "text" [--to nickname]  # one-off message, no interactive session
omarchy-relay daemon             # headless: prints chat, auto-saves incoming files
```

`gui` is also in your application launcher as **Omarchy Relay**. It has a
peer sidebar with online status, a message log, a file-attach button, and
a Settings screen (gear icon) for editing nickname/network
name/passphrase/broker host/port/TLS/credentials without touching the
config file by hand. Settings also has a "Show online/offline messages"
switch if you'd rather the chat log stayed quiet about peers connecting
and disconnecting — the sidebar still stays accurate either way, this
only mutes the log lines. Changing network/broker fields reconnects
immediately; toggling the presence switch doesn't (no need to bounce the
connection for a display preference).

Inside `chat`: `/peers`, `/msg <nick> <text>`, `/send <path> [nick]`,
`/help`, `/quit`.

Received files land in `transfer.downloads_dir` (default
`~/Downloads/omarchy-relay`) — in `gui`, that log entry has an **Open
Folder** button. All interfaces (`gui`, `chat`, `chat --tui`, `daemon`)
pop a desktop notification via `notify-send` for incoming chat messages,
DMs, and received files (if `notify-send` is installed), plus a quiet
"ding" via `canberra-gtk-play` using your system's own sound theme (if
`libcanberra` is installed — it usually already is). Neither fires for
your own messages echoing back to you.

### Run the daemon in the background

```sh
mkdir -p ~/.config/systemd/user
cp systemd/omarchy-relay-daemon.service ~/.config/systemd/user/
systemctl --user enable --now omarchy-relay-daemon
journalctl --user -fu omarchy-relay-daemon   # watch chat/file activity
```

Useful on a headless machine, or any device you want reachable for file
transfers without keeping a chat window open.

## Remote actions

A peer can ask another machine to run a **named** action over the relay —
e.g. checking status, restarting a service, triggering a script that's
already there. **Off by default.** Currently serviced by `chat` and
`daemon` (not yet by `gui`/`chat --tui`).

**The security model, in one sentence: the receiving machine's own config
always decides what runs on it — a sender can pick a name, never supply
code.** Concretely:

- Each machine keeps a **named-command table** in its own config: a name
  (`status`) mapped to a fixed shell string (`systemctl status foo`) that
  *you* wrote. A remote request can only reference a name — the string
  that actually executes is never derived from anything the sender sent.
- Each machine also keeps a **per-peer trust table**, keyed by device id:
  `"none"` (the default for every peer you haven't listed) or
  `"commands"` (may trigger a name from your table). There's no
  "everyone in the network" fallback — an unlisted peer is always denied.
- The whole feature has a **global off switch**
  (`remote_actions.enabled`), separate from the per-peer table, so it's a
  deliberate two-step opt-in: turn it on, *and* trust specific peers.
- Requests are deduplicated by id, so an MQTT redelivery can't cause a
  command to run twice.

```sh
omarchy-relay trust enable                        # turn the feature on for this machine
omarchy-relay commands set status "systemctl status omarchy-relay-daemon --no-pager"
omarchy-relay trust set <their-device-id> commands # find device ids via: omarchy-relay peers
omarchy-relay trust list                           # review what's granted
omarchy-relay trust set <their-device-id> none      # revoke

omarchy-relay action <their-nickname> status        # from the other machine: run it, print the result
```

Inside `chat`: `/action <nick> <name>`.

Output is captured and returned (stdout/stderr, capped at 8KB each, plus
the exit code) — there's no arbitrary-command tier, by design, so there's
nothing here equivalent to a remote shell.

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
                    presence, remote_actions, chat, cli, tui, gui)
install.sh          installs deps (pacman) + the omarchy-relay launcher
uninstall.sh
scripts/            one-shot quickstart installers (see Quickstart above)
systemd/            user service unit for `omarchy-relay daemon`
packaging/           .desktop file for the app launcher
docker/             self-hosted Mosquitto broker (compose + docs)
```
