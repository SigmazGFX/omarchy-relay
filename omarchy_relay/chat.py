from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .agents import AgentMailbox, AgentMessageHandler, send_agent_message
from .config import Config
from .mqttclient import RelayClient
from .presence import PeerDirectory
from .remote_actions import RemoteActionHandler, run_action
from .transfer import FileReceiver, send_file

_HELP = """\
Commands:
  /peers                 list who's online
  /msg <nick> <text>     send a direct message
  /send <path> [nick]    send a file (broadcast, or DM if nick given)
  /action <nick> <name>  run a named remote action on that peer (if they've granted you access)
  /agent <nick> <text>   send an agent message (if they've granted your agent access)
  /help                  show this help
  /quit                  leave
Anything else is sent as a broadcast chat message.\
"""


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _reply_prefix(obj: dict) -> str:
    """"(reply to Alice) " when the GUI sent this as a reply to someone's message."""
    ref = obj.get("reply_to")
    return f"(reply to {ref.get('nick') or '?'}) " if isinstance(ref, dict) else ""


def _is_quiet(obj: dict) -> bool:
    """An edit or delete of an earlier GUI message: logged as its fallback text, without a notification."""
    return obj.get("type") in ("edit", "delete")


def _notify(summary: str, body: str) -> None:
    if shutil.which("notify-send"):
        try:
            body = body if len(body) <= 200 else body[:197] + "..."
            subprocess.run(["notify-send", "omarchy-relay", f"{summary}: {body}"], check=False, timeout=2)
        except Exception:
            pass


def _ding() -> None:
    """Plays the desktop's own themed "new message" sound, quietly — not a
    bundled sound file, so it matches whatever sound theme is installed."""
    if shutil.which("canberra-gtk-play"):
        try:
            subprocess.run(
                ["canberra-gtk-play", "-i", "message-new-instant", "-V", "-6"],
                check=False,
                timeout=2,
            )
        except Exception:
            pass


def _build_client(cfg: Config, peers: PeerDirectory, print_line) -> RelayClient:
    client = RelayClient(cfg)

    def on_chat(obj):
        print_line(f"{_fmt_ts(obj['ts'])} <{obj['nick']}> {_reply_prefix(obj)}{obj['text']}")
        # Broadcasts echo back to the sender too (we're subscribed to our own
        # publish topic) — don't pop a notification/sound for our own messages.
        if obj.get("from") != cfg.device_id and not _is_quiet(obj):
            _notify(obj["nick"], obj["text"])
            _ding()

    def on_dm(obj):
        print_line(f"{_fmt_ts(obj['ts'])} [DM from {obj['nick']}] {_reply_prefix(obj)}{obj['text']}")
        if not _is_quiet(obj):
            _notify(f"DM from {obj['nick']}", obj["text"])
            _ding()

    def on_presence(device_id, data):
        changed, previous = peers.update(device_id, data)
        if changed and data is not None and previous is None:
            print_line(f"* {data['nick']} is online")
        # "went offline" is debounced — see peers.on_removed below, which
        # only fires once a peer has actually stayed gone for 5s.

    def on_removed(device_id, last_known):
        print_line(f"* {last_known.get('nick', device_id)} went offline")

    peers.on_removed = on_removed

    def on_action_handled(request_obj, outcome):
        print_line(f"* remote action '{request_obj.get('action', '?')}' from {request_obj.get('nick', '?')}: {outcome}")

    action_handler = RemoteActionHandler(cfg, on_handled=on_action_handled)

    def on_agent_received(obj):
        nick = obj.get("nick", obj.get("from", "?"))
        text = obj.get("text", "")
        print_line(f"* [agent] {nick}: {text}")
        # Nothing else pages a running Claude session — this desktop
        # notification/ding, same as DMs get, is the alert. A session
        # (or the human at the keyboard) checks `omarchy-relay agent
        # inbox` in response; nothing here executes the message content.
        _notify(f"Agent message from {nick}", text)
        _ding()

    mailbox = AgentMailbox()
    agent_handler = AgentMessageHandler(cfg, mailbox, on_received=on_agent_received)

    client.on_chat = on_chat
    client.on_dm = on_dm
    client.on_presence = on_presence
    client.on_action_request = lambda obj: action_handler.handle_request(client, obj)
    client.on_agent_message = agent_handler.handle_message
    client.agent_mailbox = mailbox
    return client


def run_chat(cfg: Config) -> None:
    peers = PeerDirectory()

    def print_line(text: str) -> None:
        sys.stdout.write(f"\r\x1b[2K{text}\n> ")
        sys.stdout.flush()

    client = _build_client(cfg, peers, print_line)

    receiver = FileReceiver(
        cfg,
        on_complete=lambda meta, path: (
            print_line(f"* received '{meta['filename']}' from {meta['nick']} -> {path}"),
            _notify("File received", f"{meta['filename']} from {meta['nick']}"),
        ),
        on_error=lambda meta, msg: print_line(f"* file '{meta.get('filename', '?')}' from {meta.get('nick', '?')} failed: {msg}"),
    )
    client.on_file_meta = receiver.handle_meta
    client.on_file_chunk = receiver.handle_chunk

    print(f"Connecting to {cfg.broker_host}:{cfg.broker_port} ...")
    client.connect()
    print(f"Connected as '{cfg.nickname}' on network '{cfg.network_name}'. /help for commands.")

    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                break
            line = line.strip()
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line == "/help":
                print(_HELP)
            elif line == "/peers":
                snapshot = peers.snapshot()
                if not snapshot:
                    print("(nobody else is online)")
                else:
                    for device_id, data in sorted(snapshot.items(), key=lambda kv: kv[1].get("nick", "")):
                        print(f"  {data.get('nick', '?')}  ({device_id})")
            elif line.startswith("/msg "):
                rest = line[len("/msg ") :]
                if " " not in rest:
                    print("usage: /msg <nick> <text>")
                    continue
                nick, text = rest.split(" ", 1)
                target = peers.resolve(nick)
                if not target:
                    print(f"no such peer online: {nick}")
                    continue
                client.send_dm(target, {"id": uuid.uuid4().hex, "ts": time.time(), "from": cfg.device_id, "nick": cfg.nickname, "text": text})
            elif line.startswith("/send "):
                args = line[len("/send ") :].split()
                if not args:
                    print("usage: /send <path> [nick]")
                    continue
                path = Path(args[0])
                to = "*"
                if len(args) > 1:
                    target = peers.resolve(args[1])
                    if not target:
                        print(f"no such peer online: {args[1]}")
                        continue
                    to = target
                try:
                    transfer_id, total_chunks = send_file(client, cfg, path, to=to)
                    print(f"* sending '{path.name}' ({total_chunks} chunks, transfer {transfer_id})")
                except (FileNotFoundError, ValueError) as exc:
                    print(f"! {exc}")
            elif line.startswith("/action "):
                rest = line[len("/action ") :]
                if " " not in rest:
                    print("usage: /action <nick> <name>")
                    continue
                nick, name = rest.split(" ", 1)
                target = peers.resolve(nick)
                if not target:
                    print(f"no such peer online: {nick}")
                    continue
                print(f"* asking {nick} to run '{name}' ...")
                try:
                    result = run_action(client, cfg, target, name)
                except TimeoutError as exc:
                    print(f"! {exc}")
                    continue
                if result.get("ok"):
                    print(f"* exit code {result['exit_code']}")
                    if result.get("stdout"):
                        print(result["stdout"].rstrip("\n"))
                    if result.get("stderr"):
                        print(f"[stderr] {result['stderr'].rstrip(chr(10))}")
                else:
                    print(f"! {result.get('error', 'unknown error')}")
            elif line.startswith("/agent "):
                rest = line[len("/agent ") :]
                if " " not in rest:
                    print("usage: /agent <nick> <text>")
                    continue
                nick, text = rest.split(" ", 1)
                target = peers.resolve(nick)
                if not target:
                    print(f"no such peer online: {nick}")
                    continue
                send_agent_message(client, cfg, client.agent_mailbox, target, nick, text)
                print(f"* sent agent message to {nick}")
            elif line.startswith("/"):
                print(f"unknown command: {line}  (try /help)")
            else:
                client.send_chat({"id": uuid.uuid4().hex, "ts": time.time(), "from": cfg.device_id, "nick": cfg.nickname, "text": line})
    finally:
        print("\ndisconnecting...")
        client.disconnect()


def run_daemon(cfg: Config) -> None:
    peers = PeerDirectory()

    def print_line(text: str) -> None:
        print(text, flush=True)

    client = _build_client(cfg, peers, print_line)

    receiver = FileReceiver(
        cfg,
        on_complete=lambda meta, path: (
            print_line(f"received '{meta['filename']}' from {meta['nick']} -> {path}"),
            _notify("File received", f"{meta['filename']} from {meta['nick']}"),
        ),
        on_error=lambda meta, msg: print_line(f"file '{meta.get('filename', '?')}' from {meta.get('nick', '?')} failed: {msg}"),
    )
    client.on_file_meta = receiver.handle_meta
    client.on_file_chunk = receiver.handle_chunk

    print_line(f"omarchy-relay daemon: connecting to {cfg.broker_host}:{cfg.broker_port} as '{cfg.nickname}' on '{cfg.network_name}'")
    client.connect()
    print_line("connected, listening (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
