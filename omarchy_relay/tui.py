"""Full-screen chat UI built on Textual.

This is a presentation layer over the same RelayClient/FileReceiver used by
the line-oriented `chat` command — the transport logic lives there, this
module only renders it. MQTT callbacks fire on paho's own network thread, so
every UI update is marshalled back onto Textual's thread via
`call_from_thread`.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, ListItem, ListView, Log, Static

from .chat import _is_quiet, _notify, _reply_prefix
from .config import Config
from .mqttclient import RelayClient
from .presence import PeerDirectory
from .transfer import FileReceiver, send_file


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


class RelayApp(App):
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #log { width: 3fr; border: solid $accent; }
    #sidebar { width: 1fr; border: solid $accent; }
    #status { height: 1; color: $text-muted; }
    """
    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.peers = PeerDirectory()
        self.client = RelayClient(cfg)
        self.receiver = FileReceiver(
            cfg,
            on_complete=self._on_file_complete,
            on_error=self._on_file_error,
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(f"connecting to {self.cfg.broker_host}:{self.cfg.broker_port} ...", id="status")
        with Horizontal(id="body"):
            yield Log(id="log", highlight=False)
            yield ListView(id="sidebar")
        yield Input(placeholder="message, or /send <path>, /msg <nick> <text>, /peers, /quit", id="input")
        yield Footer()

    def on_mount(self) -> None:
        self.client.on_chat = self._threaded(self._handle_chat)
        self.client.on_dm = self._threaded(self._handle_dm)
        self.client.on_presence = self._threaded(self._handle_presence)
        self.client.on_file_meta = self._threaded(self.receiver.handle_meta)
        self.client.on_file_chunk = self._threaded(self.receiver.handle_chunk)
        self.run_worker(self._connect, thread=True)
        self.query_one("#input", Input).focus()

    def _threaded(self, fn):
        def wrapper(*args, **kwargs):
            self.call_from_thread(fn, *args, **kwargs)
        return wrapper

    def _connect(self) -> None:
        try:
            self.client.connect()
            self.call_from_thread(self._on_connected)
        except ConnectionError as exc:
            self.call_from_thread(self._log, f"! connection failed: {exc}")

    def _on_connected(self) -> None:
        self.query_one("#status", Static).update(
            f"connected as '{self.cfg.nickname}' on network '{self.cfg.network_name}'"
        )
        self._log(f"connected to {self.cfg.broker_host}:{self.cfg.broker_port}")

    def _log(self, text: str) -> None:
        self.query_one("#log", Log).write_line(text)

    def _handle_chat(self, obj: dict) -> None:
        self._log(f"{_fmt_ts(obj['ts'])} <{obj['nick']}> {_reply_prefix(obj)}{obj['text']}")
        # Broadcasts echo back to the sender too (we're subscribed to our own
        # publish topic) — don't pop a notification for our own messages.
        if obj.get("from") != self.cfg.device_id and not _is_quiet(obj):
            _notify(obj["nick"], obj["text"])

    def _handle_dm(self, obj: dict) -> None:
        self._log(f"{_fmt_ts(obj['ts'])} [DM from {obj['nick']}] {_reply_prefix(obj)}{obj['text']}")
        if not _is_quiet(obj):
            _notify(f"DM from {obj['nick']}", obj["text"])

    def _handle_presence(self, device_id: str, data) -> None:
        changed, previous = self.peers.update(device_id, data)
        if not changed:
            return  # includes a peer starting its 5s offline debounce — list is unchanged until it actually fires
        if data is not None and previous is None:
            self._log(f"* {data['nick']} is online")
        self._refresh_sidebar()

    def _handle_peer_removed(self, device_id: str, last_known: dict) -> None:
        self._log(f"* {last_known.get('nick', device_id)} went offline")
        self._refresh_sidebar()

    def _refresh_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar", ListView)
        sidebar.clear()
        for device_id, data in sorted(self.peers.snapshot().items(), key=lambda kv: kv[1].get("nick", "")):
            sidebar.append(ListItem(Static(data.get("nick", device_id))))

    def _on_file_complete(self, meta: dict, path: Path) -> None:
        self.call_from_thread(self._log, f"* received '{meta['filename']}' from {meta['nick']} -> {path}")
        _notify("File received", f"{meta['filename']} from {meta['nick']}")

    def _on_file_error(self, meta: dict, msg: str) -> None:
        self.call_from_thread(self._log, f"* file '{meta.get('filename', '?')}' failed: {msg}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        if not line:
            return
        if line in ("/quit", "/exit"):
            self.exit()
        elif line == "/peers":
            names = ", ".join(d.get("nick", "?") for d in self.peers.snapshot().values()) or "(nobody else online)"
            self._log(f"* peers: {names}")
        elif line.startswith("/msg "):
            rest = line[len("/msg ") :]
            if " " not in rest:
                self._log("usage: /msg <nick> <text>")
                return
            nick, text = rest.split(" ", 1)
            target = self.peers.resolve(nick)
            if not target:
                self._log(f"! no such peer online: {nick}")
                return
            self.client.send_dm(target, {"id": uuid.uuid4().hex, "ts": time.time(), "from": self.cfg.device_id, "nick": self.cfg.nickname, "text": text})
            self._log(f"{_fmt_ts(time.time())} [DM to {nick}] {text}")
        elif line.startswith("/send "):
            args = line[len("/send ") :].split()
            if not args:
                self._log("usage: /send <path> [nick]")
                return
            path = Path(args[0])
            to = "*"
            if len(args) > 1:
                target = self.peers.resolve(args[1])
                if not target:
                    self._log(f"! no such peer online: {args[1]}")
                    return
                to = target
            try:
                transfer_id, total_chunks = send_file(self.client, self.cfg, path, to=to)
                self._log(f"* sending '{path.name}' ({total_chunks} chunks, transfer {transfer_id})")
            except (FileNotFoundError, ValueError) as exc:
                self._log(f"! {exc}")
        elif line.startswith("/"):
            self._log(f"! unknown command: {line}")
        else:
            self.client.send_chat({"id": uuid.uuid4().hex, "ts": time.time(), "from": self.cfg.device_id, "nick": self.cfg.nickname, "text": line})
            self._log(f"{_fmt_ts(time.time())} <{self.cfg.nickname}> {line}")

    def action_quit(self) -> None:
        self.exit()

    def on_unmount(self) -> None:
        self.client.disconnect()


def run_tui(cfg: Config) -> None:
    RelayApp(cfg).run()
