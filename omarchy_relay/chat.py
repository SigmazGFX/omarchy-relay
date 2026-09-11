from __future__ import annotations

import collections
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

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
  /react <emoji>         react to the last message from someone else (again to take it back)
  /edit <text>           replace the text of your last message
  /delete                delete your last message for everyone
  /action <nick> <name>  run a named remote action on that peer (if they've granted you access)
  /agent <nick> <text>   send an agent message (if they've granted your agent access)
  /help                  show this help
  /quit                  leave
Anything else is sent as a broadcast chat message.\
"""

# How much of a message a reaction or edit line quotes.
_QUOTE_CHARS = 40


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _reply_prefix(obj: dict) -> str:
    """"(reply to Alice) " when the GUI sent this as a reply to someone's message."""
    ref = obj.get("reply_to")
    return f"(reply to {ref.get('nick') or '?'}) " if isinstance(ref, dict) else ""


def _quote(text: str) -> str:
    text = " ".join(text.split())
    return f'"{text if len(text) <= _QUOTE_CHARS else text[: _QUOTE_CHARS - 1] + "…"}"'


def _mentions(text: str, nick: str) -> bool:
    """Whether text @mentions nick: case-insensitive, and the whole name."""
    return bool(nick) and re.search(rf"(?<![\w@])@{re.escape(nick)}(?![\w-])", text, re.IGNORECASE) is not None


def _notify(summary: str, body: str, urgent: bool = False) -> None:
    """urgent: stays on screen until dismissed (for a message that mentions you)."""
    if shutil.which("notify-send"):
        try:
            body = body if len(body) <= 200 else body[:197] + "..."
            urgency = ["-u", "critical"] if urgent else []
            subprocess.run(["notify-send", *urgency, "omarchy-relay", f"{summary}: {body}"], check=False, timeout=2)
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


class Transcript:
    """The terminal clients' memory of recent messages, so a reaction, edit,
    or delete can say which message it's about, and /react, /edit, and
    /delete know which one they mean. Written from paho's network thread and
    read from the input's, hence the lock."""

    LIMIT = 500

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self._lock = threading.Lock()
        self._messages: collections.OrderedDict[str, dict] = collections.OrderedDict()

    def add(self, obj: dict, *, peer: Optional[str]) -> None:
        """peer: the other device, for a direct message; None for a broadcast."""
        if not obj.get("id"):
            return
        with self._lock:
            self._messages[obj["id"]] = {
                "from": obj.get("from"),
                "nick": obj.get("nick", "?"),
                "ts": obj.get("ts") or time.time(),
                "text": obj.get("text", ""),
                "peer": peer,
                # Only plain text can be edited: not a drawing, or a treat's fallback text.
                "editable": obj.get("format") != "ascii" and not obj.get("type"),
                "reactions": {},  # emoji -> {device_id: nick}
            }
            while len(self._messages) > self.LIMIT:
                self._messages.popitem(last=False)

    def get(self, msg_id: Optional[str]) -> Optional[dict]:
        with self._lock:
            message = self._messages.get(msg_id)
            return dict(message) if message else None

    def forget(self, msg_id: str) -> None:
        with self._lock:
            self._messages.pop(msg_id, None)

    def set_text(self, msg_id: str, text: str) -> None:
        with self._lock:
            if msg_id in self._messages:
                self._messages[msg_id]["text"] = text

    def react(self, msg_id: str, emoji: str, device_id: str, nick: str, present: bool) -> None:
        with self._lock:
            message = self._messages.get(msg_id)
            if message is None:
                return
            by_device = message["reactions"].setdefault(emoji, {})
            if present:
                by_device[device_id] = nick
            else:
                by_device.pop(device_id, None)

    def has_reacted(self, msg_id: str, emoji: str) -> bool:
        with self._lock:
            message = self._messages.get(msg_id)
            return message is not None and self.device_id in message["reactions"].get(emoji, {})

    def last(self, *, mine: bool, editable: bool = False) -> Optional[str]:
        """The latest message of ours (mine) or someone else's."""
        with self._lock:
            for msg_id in reversed(self._messages):
                message = self._messages[msg_id]
                if (message["from"] == self.device_id) == mine and (message["editable"] or not editable):
                    return msg_id
        return None


def _message(cfg: Config, text: str) -> dict:
    return {"id": uuid.uuid4().hex, "ts": time.time(), "from": cfg.device_id, "nick": cfg.nickname, "text": text}


def _build_client(cfg: Config, peers: PeerDirectory, print_line) -> RelayClient:
    client = RelayClient(cfg)
    transcript = Transcript(cfg.device_id)

    def on_message(obj, *, is_dm):
        kind = obj.get("type")
        if kind == "reaction":
            on_reaction(obj)
        elif kind == "edit":
            on_edit(obj)
        elif kind == "delete":
            on_delete(obj)
        elif kind == "receipt":
            return  # the GUI's delivered/read ticks: nothing to print
        else:
            on_text(obj, is_dm=is_dm)

    def on_text(obj, *, is_dm):
        sender, text = obj.get("from"), obj["text"]
        where = f"[DM from {obj['nick']}]" if is_dm else f"<{obj['nick']}>"
        print_line(f"{_fmt_ts(obj['ts'])} {where} {_reply_prefix(obj)}{text}")
        transcript.add(obj, peer=sender if is_dm else None)
        # Broadcasts echo back to the sender too (we're subscribed to our own
        # publish topic) — don't notify ourselves for our own messages, nor
        # for what the broker held while we were offline.
        if sender == cfg.device_id or client.is_backlog(obj):
            return
        if _mentions(text, cfg.nickname):
            _notify(f"{obj['nick']} mentioned you", text, urgent=True)
        else:
            _notify(f"DM from {obj['nick']}" if is_dm else obj["nick"], text)
        _ding()

    def on_reaction(obj):
        target, emoji, sender, nick = obj.get("target_id"), obj.get("emoji"), obj.get("from"), obj.get("nick", "?")
        if not target or not emoji or not sender:
            return
        present = obj.get("op") != "remove"
        transcript.react(target, emoji, sender, nick, present)
        about = transcript.get(target)
        if present:
            print_line(f"* {nick} reacted {emoji}" + (f" to {_quote(about['text'])}" if about else ""))
        else:
            print_line(f"* {nick} took back {emoji}" + (f" on {_quote(about['text'])}" if about else ""))

    def on_edit(obj):
        target, sender, text, nick = obj.get("target_id"), obj.get("from"), obj.get("new_text"), obj.get("nick", "?")
        if not isinstance(text, str) or not text.strip():
            return
        about = transcript.get(target)
        if about is None:
            print_line(f"* {nick} edited a message: {text}")
            return
        if about["from"] != sender or not about["editable"]:
            return  # only the device that sent a text message can edit it
        transcript.set_text(target, text)
        print_line(f"* {nick} edited {_quote(about['text'])}: {text}")

    def on_delete(obj):
        target, sender, nick = obj.get("target_id"), obj.get("from"), obj.get("nick", "?")
        about = transcript.get(target)
        if about is None:
            print_line(f"* {nick} deleted a message")
            return
        if about["from"] != sender:
            return  # only the device that sent a message can delete it
        transcript.forget(target)
        # Not quoted: it was deleted so it wouldn't be read again.
        print_line(f"* {nick} deleted their message from {_fmt_ts(about['ts'])}")

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
        if not client.is_backlog(obj):
            _notify(f"Agent message from {nick}", text)
            _ding()

    mailbox = AgentMailbox()
    agent_handler = AgentMessageHandler(cfg, mailbox, on_received=on_agent_received)

    client.on_chat = lambda obj: on_message(obj, is_dm=False)
    client.on_dm = lambda obj: on_message(obj, is_dm=True)
    client.on_presence = on_presence
    client.on_action_request = lambda obj: action_handler.handle_request(client, obj)
    client.on_agent_message = agent_handler.handle_message
    client.agent_mailbox = mailbox
    client.transcript = transcript
    return client


def _send_change(client: RelayClient, cfg: Config, target: str, about: dict, change: dict) -> None:
    """A reaction, edit, or delete of one message, sent where the message
    went. A broadcast's comes back to us like any broadcast; a direct
    message's doesn't, so it's applied here."""
    payload = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "from": cfg.device_id,
        "nick": cfg.nickname,
        "target_id": target,
        **change,
    }
    if about["peer"]:
        client.send_dm(about["peer"], payload)
        client.on_dm(payload)
    else:
        client.send_chat(payload)


def run_command(line: str, client: RelayClient, cfg: Config, peers: PeerDirectory, out) -> bool:
    """Handles one line typed into `chat` or `chat --tui`, writing any reply
    with out. False means quit."""
    transcript: Transcript = client.transcript
    command, _, rest = line.partition(" ")
    rest = rest.strip()
    if command in ("/quit", "/exit"):
        return False
    if command == "/help":
        out(_HELP)
    elif command == "/peers":
        snapshot = peers.snapshot()
        if not snapshot:
            out("(nobody else is online)")
        for device_id, data in sorted(snapshot.items(), key=lambda kv: kv[1].get("nick", "")):
            out(f"  {data.get('nick', '?')}  ({device_id})")
    elif command == "/msg":
        nick, _, text = rest.partition(" ")
        if not text:
            out("usage: /msg <nick> <text>")
            return True
        target = peers.resolve(nick)
        if not target:
            out(f"no such peer online: {nick}")
            return True
        payload = _message(cfg, text)
        client.send_dm(target, payload)
        transcript.add(payload, peer=target)
        out(f"{_fmt_ts(payload['ts'])} [DM to {nick}] {text}")
    elif command == "/send":
        args = rest.split()
        if not args:
            out("usage: /send <path> [nick]")
            return True
        path = Path(args[0])
        to = "*"
        if len(args) > 1:
            target = peers.resolve(args[1])
            if not target:
                out(f"no such peer online: {args[1]}")
                return True
            to = target
        try:
            transfer_id, total_chunks = send_file(client, cfg, path, to=to)
            out(f"* sending '{path.name}' ({total_chunks} chunks, transfer {transfer_id})")
        except (FileNotFoundError, ValueError) as exc:
            out(f"! {exc}")
    elif command == "/react":
        target = transcript.last(mine=False)
        about = transcript.get(target)
        if not rest:
            out("usage: /react <emoji>")
        elif about is None:
            out("! nothing from anyone else to react to yet")
        else:
            op = "remove" if transcript.has_reacted(target, rest) else "add"
            _send_change(client, cfg, target, about, {"type": "reaction", "emoji": rest, "op": op})
    elif command == "/edit":
        target = transcript.last(mine=True, editable=True)
        about = transcript.get(target)
        if not rest:
            out("usage: /edit <text>")
        elif about is None:
            out("! no message of yours to edit")
        else:
            # "text" is what older clients show instead.
            _send_change(client, cfg, target, about, {"type": "edit", "new_text": rest, "text": f"(edited) {rest}"})
    elif command == "/delete":
        target = transcript.last(mine=True)
        about = transcript.get(target)
        if about is None:
            out("! no message of yours to delete")
        else:
            _send_change(client, cfg, target, about, {"type": "delete", "text": "(deleted a message)"})
    elif command == "/action":
        peer_ref, _, name = rest.partition(" ")
        if not name:
            out("usage: /action <nick> <name>")
            return True
        target = peers.resolve(peer_ref)
        if not target:
            out(f"no such peer online: {peer_ref}")
            return True
        out(f"* asking {peer_ref} to run '{name}' ...")
        try:
            result = run_action(client, cfg, target, name)
        except TimeoutError as exc:
            out(f"! {exc}")
            return True
        if result.get("ok"):
            out(f"* exit code {result['exit_code']}")
            if result.get("stdout"):
                out(result["stdout"].rstrip("\n"))
            if result.get("stderr"):
                out(f"[stderr] {result['stderr'].rstrip(chr(10))}")
        else:
            out(f"! {result.get('error', 'unknown error')}")
    elif command == "/agent":
        nick, _, text = rest.partition(" ")
        if not text:
            out("usage: /agent <nick> <text>")
            return True
        target = peers.resolve(nick)
        if not target:
            out(f"no such peer online: {nick}")
            return True
        send_agent_message(client, cfg, client.agent_mailbox, target, nick, text)
        out(f"* sent agent message to {nick}")
    elif command.startswith("/"):
        out(f"unknown command: {line}  (try /help)")
    else:
        client.send_chat(_message(cfg, line))
    return True


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
            # Replies to a command print plainly: input() redraws the prompt itself.
            if line and not run_command(line, client, cfg, peers, print):
                break
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
