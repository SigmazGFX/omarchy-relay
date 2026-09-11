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
- **Typing status** has its own encrypted broadcast topic, separate from
  chat. While the message box has text that keeps changing, `gui` sends a
  "typing" event at most every 3 seconds (QoS 0 — they're disposable), and
  "stopped" once the box empties or the message goes out. Receivers drop
  the indicator if no refresh arrives within 6 seconds. Older clients never
  subscribe to the topic, so they never see these events.
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
`python-gobject`, `gtk4`, `libadwaita`, `gstreamer`, `gst-plugins-base`,
`gst-plugin-pipewire`, `grim`, `slurp`, and `figlet` via `pacman`, copies the package to
`~/.local/share/omarchy-relay`, puts a launcher at
`~/.local/bin/omarchy-relay`, and adds "Omarchy Relay" to your app
launcher.

On another distro: create a venv, `pip install paho-mqtt cryptography
textual PyGObject`, install GTK4 + libadwaita + GStreamer (core,
`gst-plugins-base`, and a PipeWire or ALSA source/sink plugin) plus `grim`,
`slurp`, and `figlet` through your package manager, and run `python3 -m omarchy_relay.cli` from inside the
repo.

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
peer sidebar with online status, a message log, a file-attach button,
clipboard image paste (Ctrl+V into the message box with an image on the
clipboard — a screenshot, a browser image copy — sends it immediately;
received images render inline as a thumbnail instead of a generic file
row, on both ends — double-click one to see it full size), and a Settings screen (gear icon) for editing
nickname/network
name/passphrase/broker host/port/TLS/credentials without touching the
config file by hand. Settings also has a "Show online/offline messages"
switch if you'd rather the chat log stayed quiet about peers connecting
and disconnecting — the sidebar still stays accurate either way, this
only mutes the log lines. Changing network/broker fields reconnects
immediately; toggling the presence switch doesn't (no need to bounce the
connection for a display preference).

**Snip**: the snip button in the chat header picks part of your screen
and sends it as an image, just like pasting one. On Omarchy it's the same
picker as the Print Screen key — the screen freezes while you drag out an
area, a click takes a whole window, and Escape cancels; elsewhere it's
plain `slurp`. Needs `grim` and `slurp`.

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

**Voice messages**: the microphone button next to Send records a voice
note (Ogg Opus, via GStreamer/PipeWire) — click once to start, again to
stop and send. Received (and your own sent) voice messages render as a
play/pause bubble with duration; only one plays at a time. Sent voice
files land in `transfer.downloads_dir` alongside received ones.

**`/sparkles`**: any message containing this word sets off a five-second
pixie-dust burst on every client that renders it, sender included.

**Typing status**: while someone is typing in `gui`, three bouncing dots
show in a bubble at the bottom of the chat (with their name) and under
their name in the sidebar, and the chat header says so ("Alice is
typing…"). `chat` and `chat --tui` neither send nor show it.

**ASCII drawings**: `/ascii <what to draw>` sends a ready-made drawing —
`/ascii cat`, `/ascii rocket`, `/ascii birthday cake`, and dozens more
(`/ascii` on its own lists them, from `omarchy_relay/ascii_art.py`).
Anything without a drawing comes out in big `figlet` letters. A
multi-line drawing pasted right after `/ascii ` is sent as it is. Drawings
show in a monospaced box that keeps their spacing and scrolls sideways if
wider than the chat, work with replies and reactions, and are kept in
message history; `chat`, `chat --tui`, and older clients show them as
plain text.

**Replies**: the reply arrow under a message (next to the react button)
quotes it above the message box; what you send next carries that quote at
its top. Click a quote to jump to the original, and Esc or × cancels the
reply. A reply to a direct message goes back to that person only. Replies
are kept in message history; `chat` and `chat --tui` mark them "(reply to
Alice)", and older clients show them as ordinary messages.

**Edit and delete**: the ⋯ button under a message (after the reply arrow)
has **Edit** and **Delete for everyone** on your own messages, and **Delete
for me** on anyone's. Edit works on text messages (not drawings); the new
text replaces the old wherever the message went, marked "edited" by its
time. A message deleted for everyone leaves "This message was deleted" in
its place, in history too, with its text gone; one deleted for you just
disappears from your chat and history. Quotes of it in replies keep what
they quoted. Only the device that sent a message can edit or delete it for
everyone — though, as with DMs, that device is claimed rather than proven,
so anyone holding the network passphrase could pose as it. `chat`, `chat
--tui`, and older clients show an edit as a new "(edited) …" message and a
delete as "(deleted a message)". Files, images, and voice messages have
the same react, reply, and ⋯ buttons (without Edit); deleting one leaves
the saved file where it is in your downloads folder.

**Coffee, cocktails, and dancers**: `/coffee`, `/cocktail`, and `/dancer`
send one to everyone, and `/coffee <nickname> [note]` (likewise
`/cocktail` and `/dancer`) sends one to one person — the commands are the
only way to send them. Each lands as a card (reactions work on it like any
message) with an animation over the window — a steaming cup, a clinking
glass, a dancer — and is kept in message history. Older clients, `chat`,
and `chat --tui` show it as an ordinary message ("☕ sent you a cup of
coffee", "🍸 bought everyone a cocktail").

**What's New**: the main menu's **What's New** lists what changed in each
release, and it opens by itself once after an update. Release notes live
in `omarchy_relay/release_notes.py`.

**Lock window size**: Settings → Window keeps the chat window at a fixed
width × height (defaulting to its current size) — no resizing, maximizing,
or fullscreen, including from compositor keybindings. On Hyprland a
tiled window is resized by the layout regardless of what it asks for, so
locking also floats the window (via `hyprctl`) and centers it; unlocking
tiles it again.

**Closing the window keeps you connected**: it hides into the background,
so you stay online and still get notifications and dings. The bar icon or
app launcher brings the same window back. To actually go offline, use
**Quit** in the sidebar's main menu, or Ctrl+Q.

**Message history**: recent messages are kept locally (SQLite, under
`~/.local/share/omarchy-relay/history.db`, scoped per network) and shown
on startup before live traffic arrives. Images, voice messages, and
files come back too: history remembers where each one is saved in
`transfer.downloads_dir` (your own pasted and snipped images are kept
there, like your voice messages), and one you've since deleted shows as
"No longer on this device". Settings → Chat has two
independent caps — keep the last N messages, and/or keep messages for N
days — either at 0 to not limit that dimension. Nothing here is
transmitted; it's purely a local cache of what this device has already
seen.

**Export/Import**: Settings → "Share this network" writes the current
Network + Broker fields (not your nickname/device) to a `.toml` file, or
reads one back into the fields for review before Save — the easiest way to
get someone else onto the same network.

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
already there. **Off by default.** Serviced by `chat`, `daemon`, and
`gui` (not yet by `chat --tui`) — whichever of those you have running is
what responds; don't run more than one at a time under the same identity,
since each connects to the broker with the same MQTT client ID (your
device_id) and the broker disconnects whichever held it first.

In `gui`, Settings → "Manage trusted peers and commands" has the same
controls as the CLI below: a global on/off switch, a trusted-peers list
(pick an online peer or type a device ID), and a named-commands table.
Changes apply immediately, no separate Save step.

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

Inside `chat`: `/action <nick> <name>`. Inside `gui`'s message box: the
same `/action <nickname> <command-name>`, sent instead of as a chat
message — typing `/action` there shows the syntax as inline ghost/
selected text to guide you, and the result (or a timeout/denial) prints
as a line in the chat log, visible only to you.

Output is captured and returned (stdout/stderr, capped at 8KB each, plus
the exit code) — there's no arbitrary-command tier, by design, so there's
nothing here equivalent to a remote shell.

## Agent messaging

A separate, free-form channel for one **Claude Code session** to message
another on a trusted peer — check-ins, status, handoffs, collaboration —
routed over the same encrypted relay as everything else. **Off by
default**, with its own trust list, kept deliberately separate from
[remote actions](#remote-actions): remote actions guarantee a peer can only
ever trigger a fixed, locally-authored command by name, never send content
that runs. Agent messages are the opposite — free-form text — so trusting
a peer here is a distinct grant from trusting it for named commands.

Installing omarchy-relay also installs a Claude Code skill
(`~/.agents/skills/omarchy-relay/SKILL.md`) that teaches a session how to
check its inbox and message a peer, and — just as important — to treat
inbox content as untrusted data to reason about, never as instructions to
execute automatically; the same posture it should already apply to any
external, unverified input.

```sh
omarchy-relay agent trust enable                       # turn on agent messaging for this machine
omarchy-relay agent trust set <their-device-id> agent  # find device ids via: omarchy-relay peers
omarchy-relay agent trust list                         # review what's granted
omarchy-relay agent trust set <their-device-id> none    # revoke

omarchy-relay agent send <their-nickname> "status: build passing, starting the migration"
omarchy-relay agent inbox              # everything sent/received, oldest first
omarchy-relay agent inbox --unread     # just what's new
```

Inside `chat`: `/agent <nick> <text>`.

**How you're alerted:** whichever of `daemon`, `chat`, or `gui` is running
and connected pops the same desktop notification (`notify-send`) and quiet
ding it already uses for DMs the moment an agent message arrives — that's
the whole mechanism, nothing exotic. There's no separate always-on process
just for this. A Claude Code session isn't paged directly (it isn't
listening for a signal of its own); it finds out by running
`omarchy-relay agent inbox` when it checks in, same as a human glancing at
the notification and then opening the chat window. Check-ins are pull, by
design — see the skill's guidance on doing this "as needed" rather than
polling in a tight loop.

Like DMs, delivery is **live-only and best-effort** — a message sent while
the target has nothing running (`daemon`, `chat`, or `gui`) is simply not
received; there's no store-and-forward. The local inbox (same SQLite file
as message history, scoped per network) only records what this device has
actually sent or received, so `agent inbox` works without a live
connection. Same caveat as everything else on a shared-passphrase network:
this is group encryption, so a `from`/`nick` field is claimed, not
cryptographically proven — trusting a device id means trusting whoever
currently holds that network's passphrase under that identity.

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
                    presence, remote_actions, agents, chat, cli, tui, gui,
                    audio, history, network_share, release_notes, ascii_art)
skills/omarchy-relay/  the Claude Code skill for agent messaging (see
                    Agent messaging above); installed to ~/.agents/skills
install.sh          installs deps (pacman) + the omarchy-relay launcher + skill
uninstall.sh
scripts/            one-shot quickstart installers (see Quickstart above)
systemd/            user service unit for `omarchy-relay daemon`
packaging/           .desktop file for the app launcher
docker/             self-hosted Mosquitto broker (compose + docs)
```
