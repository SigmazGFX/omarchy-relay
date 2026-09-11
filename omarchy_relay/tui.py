"""Full-screen chat UI built on Textual.

A presentation layer over the same client, message handling, and commands
as the line-oriented `chat` command (chat.py) — this module renders them
and adds typing status. MQTT callbacks fire on paho's own network thread,
and commands run in a worker thread (a remote action can wait 30s for its
result), so every UI update is marshalled back onto Textual's thread via
`call_from_thread`.
"""
from __future__ import annotations

import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input, ListItem, ListView, Log, Static

from .chat import _build_client, _notify, run_command
from .config import Config
from .presence import PeerDirectory
from .transfer import FileReceiver

# Typing status, as the GUI does it: say so at most this often while the
# input keeps changing, and drop someone's indicator once their updates
# stop for this long.
_TYPING_SEND_SECONDS = 3.0
_TYPING_EXPIRY_SECONDS = 6.0


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
        self.client = _build_client(cfg, self.peers, self._print_line)
        self.receiver = FileReceiver(
            cfg,
            on_complete=self._on_file_complete,
            on_error=self._on_file_error,
        )
        self.connection_status = f"connecting to {cfg.broker_host}:{cfg.broker_port} ..."
        self.status_text = ""  # what the status line shows: the connection, or who's typing
        self._typing: dict[str, tuple[str, float]] = {}  # device_id -> (nick, monotonic time it lapses)
        self._typing_sent_at = 0.0  # monotonic time we last said we're typing; 0 = we aren't

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(self.connection_status, id="status")
        with Horizontal(id="body"):
            yield Log(id="log", highlight=False)
            yield ListView(id="sidebar")
        yield Input(placeholder="message, or /help for commands", id="input")
        yield Footer()

    def on_mount(self) -> None:
        # chat.py's handlers keep the peer directory; the sidebar follows it.
        on_presence, on_removed = self.client.on_presence, self.peers.on_removed

        def presence(device_id, data):
            on_presence(device_id, data)
            self.call_from_thread(self._refresh_sidebar)

        def removed(device_id, last_known):
            on_removed(device_id, last_known)
            self.call_from_thread(self._refresh_sidebar)

        self.client.on_presence = presence
        self.peers.on_removed = removed
        self.client.on_typing = lambda obj: self.call_from_thread(self._handle_typing, obj)
        self.client.on_file_meta = self.receiver.handle_meta
        self.client.on_file_chunk = self.receiver.handle_chunk
        self.set_interval(1.0, self._render_status)
        self.run_worker(self._connect, thread=True)
        self.query_one("#input", Input).focus()

    def _print_line(self, text: str) -> None:
        # Only ever called off Textual's thread: paho's, a worker's, or a timer's.
        self.call_from_thread(self._log, text)

    def _connect(self) -> None:
        try:
            self.client.connect()
            self.call_from_thread(self._on_connected)
        except ConnectionError as exc:
            self.call_from_thread(self._log, f"! connection failed: {exc}")

    def _on_connected(self) -> None:
        self.connection_status = f"connected as '{self.cfg.nickname}' on network '{self.cfg.network_name}'"
        self._render_status()
        self._log(f"connected to {self.cfg.broker_host}:{self.cfg.broker_port}. /help for commands.")

    def _log(self, text: str) -> None:
        self.query_one("#log", Log).write_line(text)

    def _refresh_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar", ListView)
        sidebar.clear()
        for device_id, data in sorted(self.peers.snapshot().items(), key=lambda kv: kv[1].get("nick", "")):
            sidebar.append(ListItem(Static(data.get("nick", device_id))))

    # -- typing status -----------------------------------------------------

    def _handle_typing(self, obj: dict) -> None:
        device_id = obj.get("from")
        if not device_id or device_id == self.cfg.device_id:
            return
        if obj.get("state") == "typing":
            self._typing[device_id] = (obj.get("nick", "?"), time.monotonic() + _TYPING_EXPIRY_SECONDS)
        else:
            self._typing.pop(device_id, None)
        self._render_status()

    def _render_status(self) -> None:
        now = time.monotonic()
        self._typing = {device_id: entry for device_id, entry in self._typing.items() if entry[1] > now}
        nicks = sorted(nick for nick, _expiry in self._typing.values())
        if not nicks:
            text = self.connection_status
        elif len(nicks) == 1:
            text = f"{nicks[0]} is typing…"
        elif len(nicks) == 2:
            text = f"{nicks[0]} and {nicks[1]} are typing…"
        else:
            text = f"{len(nicks)} people are typing…"
        if text != self.status_text:
            self.status_text = text
            self.query_one("#status", Static).update(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.value.strip():
            now = time.monotonic()
            if now - self._typing_sent_at >= _TYPING_SEND_SECONDS:
                self._typing_sent_at = now
                self._send_typing("typing")
        elif self._typing_sent_at:
            self._typing_sent_at = 0.0
            self._send_typing("stopped")

    def _send_typing(self, state: str) -> None:
        if self.client.connected.is_set():
            self.client.send_typing({"from": self.cfg.device_id, "nick": self.cfg.nickname, "ts": time.time(), "state": state})

    # -- files and commands ------------------------------------------------

    def _on_file_complete(self, meta: dict, path: Path) -> None:
        self._print_line(f"* received '{meta['filename']}' from {meta['nick']} -> {path}")
        _notify("File received", f"{meta['filename']} from {meta['nick']}")

    def _on_file_error(self, meta: dict, msg: str) -> None:
        self._print_line(f"* file '{meta.get('filename', '?')}' failed: {msg}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""  # also says we've stopped typing, via on_input_changed
        if line:
            self.run_worker(lambda: self._run_command(line), thread=True)

    def _run_command(self, line: str) -> None:
        if not run_command(line, self.client, self.cfg, self.peers, self._print_line):
            self.call_from_thread(self.exit)

    def action_quit(self) -> None:
        self.exit()

    def on_unmount(self) -> None:
        self.client.disconnect()


def run_tui(cfg: Config) -> None:
    RelayApp(cfg).run()
