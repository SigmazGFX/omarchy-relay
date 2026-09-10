"""Native chat window built with GTK4 + libadwaita.

Like the Textual TUI, this is a presentation layer over the same
RelayClient/FileReceiver used everywhere else — the transport logic lives
in mqttclient.py/chat.py/transfer.py, this module only renders it.
RelayClient callbacks fire on paho's own network thread, so every UI
update is marshalled onto GTK's main loop via GLib.idle_add.
"""
from __future__ import annotations

import dataclasses
import threading
import time
import uuid
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .chat import _notify
from .config import Config
from .mqttclient import RelayClient
from .presence import PeerDirectory
from .transfer import FileReceiver, send_file


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


class RelayWindow(Adw.ApplicationWindow):
    def __init__(self, app: "RelayApp", cfg: Config):
        super().__init__(application=app)
        self.cfg = cfg
        self.peers = PeerDirectory()
        self.set_default_size(780, 540)

        self.client = RelayClient(cfg)
        self.receiver = FileReceiver(cfg, on_complete=self._on_file_complete, on_error=self._on_file_error)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title=f"Omarchy Relay — {cfg.nickname}", subtitle=f"{cfg.network_name} · connecting…")
        header.set_title_widget(self.title_widget)
        settings_btn = Gtk.Button(icon_name="preferences-system-symbolic", tooltip_text="Settings")
        settings_btn.connect("clicked", self._on_open_settings)
        header.pack_end(settings_btn)
        toolbar_view.add_top_bar(header)

        split = Adw.NavigationSplitView()
        split.set_min_sidebar_width(160)
        split.set_max_sidebar_width(240)

        sidebar_page = Adw.NavigationPage(title="Online")
        sidebar_scroller = Gtk.ScrolledWindow(vexpand=True)
        self.peer_list = Gtk.ListBox()
        self.peer_list.add_css_class("navigation-sidebar")
        self.peer_list.set_selection_mode(Gtk.SelectionMode.NONE)
        sidebar_scroller.set_child(self.peer_list)
        sidebar_page.set_child(sidebar_scroller)
        split.set_sidebar(sidebar_page)

        content_page = Adw.NavigationPage(title=cfg.network_name)
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.chat_scroller = Gtk.ScrolledWindow(vexpand=True)
        self.chat_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.chat_list = Gtk.ListBox()
        self.chat_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.chat_list.add_css_class("background")
        self.chat_scroller.set_child(self.chat_list)
        content_box.append(self.chat_scroller)

        content_box.append(Gtk.Separator())

        entry_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_start=8,
            margin_end=8,
            margin_top=8,
            margin_bottom=8,
        )
        self.entry = Gtk.Entry(hexpand=True, placeholder_text="Message…")
        self.entry.connect("activate", self._on_send)
        attach_btn = Gtk.Button(icon_name="mail-attachment-symbolic", tooltip_text="Send a file")
        attach_btn.connect("clicked", self._on_attach)
        send_btn = Gtk.Button(icon_name="mail-send-symbolic", tooltip_text="Send")
        send_btn.add_css_class("suggested-action")
        send_btn.connect("clicked", self._on_send)
        entry_row.append(self.entry)
        entry_row.append(attach_btn)
        entry_row.append(send_btn)
        content_box.append(entry_row)

        content_page.set_child(content_box)
        split.set_content(content_page)

        toolbar_view.set_content(split)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(toolbar_view)
        self.set_content(self.toast_overlay)

        self._wire_client_callbacks()

        self.connect("close-request", self._on_close_request)
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _wire_client_callbacks(self) -> None:
        self.client.on_chat = self._threaded(self._handle_chat)
        self.client.on_dm = self._threaded(self._handle_dm)
        self.client.on_presence = self._threaded(self._handle_presence)
        self.client.on_file_meta = self._threaded(self.receiver.handle_meta)
        self.client.on_file_chunk = self._threaded(self.receiver.handle_chunk)

    # -- thread marshalling ------------------------------------------------

    def _threaded(self, fn):
        def wrapper(*args):
            def call():
                fn(*args)
                return False

            GLib.idle_add(call)

        return wrapper

    def _connect_worker(self) -> None:
        try:
            self.client.connect()
            GLib.idle_add(self._on_connected)
        except ConnectionError as exc:
            GLib.idle_add(self._append_system, f"connection failed: {exc}")

    def _on_connected(self) -> None:
        self.title_widget.set_subtitle(f"{self.cfg.network_name} · connected")

    # -- rendering -----------------------------------------------------------

    def _scroll_to_bottom(self) -> bool:
        adj = self.chat_scroller.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def _append_row(self, widget: Gtk.Widget) -> None:
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.set_child(widget)
        self.chat_list.append(row)
        GLib.idle_add(self._scroll_to_bottom)

    def _append_message(self, header_text: str, ts: float, text: str) -> None:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_start=12,
            margin_end=12,
            margin_top=6,
            margin_bottom=2,
        )
        header = Gtk.Label(xalign=0)
        header.set_markup(
            f"<b>{GLib.markup_escape_text(header_text)}</b>  "
            f"<span alpha='55%' size='small'>{_fmt_ts(ts)}</span>"
        )
        body = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
        box.append(header)
        box.append(body)
        self._append_row(box)

    def _append_system(self, text: str) -> None:
        label = Gtk.Label(xalign=0.5, margin_top=4, margin_bottom=4)
        label.set_markup(f"<span alpha='55%' size='small'>— {GLib.markup_escape_text(text)} —</span>")
        self._append_row(label)

    def _refresh_peer_list(self) -> None:
        self.peer_list.remove_all()
        for _device_id, data in sorted(self.peers.snapshot().items(), key=lambda kv: kv[1].get("nick", "")):
            row = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=8,
                margin_start=10,
                margin_end=10,
                margin_top=6,
                margin_bottom=6,
            )
            dot = Gtk.Image.new_from_icon_name("user-available-symbolic")
            dot.add_css_class("success")
            label = Gtk.Label(label=data.get("nick", "?"), xalign=0)
            row.append(dot)
            row.append(label)
            self.peer_list.append(row)

    # -- RelayClient callbacks (already marshalled onto the GTK thread) ----

    def _handle_chat(self, obj: dict) -> None:
        self._append_message(obj["nick"], obj["ts"], obj["text"])
        # Broadcasts echo back to the sender too (we're subscribed to our
        # own publish topic) — don't notify ourselves for our own messages.
        if obj.get("from") != self.cfg.device_id:
            _notify(obj["nick"], obj["text"])

    def _handle_dm(self, obj: dict) -> None:
        self._append_message(f"DM from {obj['nick']}", obj["ts"], obj["text"])
        _notify(f"DM from {obj['nick']}", obj["text"])

    def _handle_presence(self, device_id: str, data) -> None:
        changed, previous = self.peers.update(device_id, data)
        if not changed:
            return
        if data is None:
            name = previous.get("nick", device_id) if previous else device_id
            self._append_system(f"{name} went offline")
        elif previous is None:
            self._append_system(f"{data['nick']} is online")
        self._refresh_peer_list()

    def _on_file_complete(self, meta: dict, path: Path) -> None:
        GLib.idle_add(self._append_system, f"received '{meta['filename']}' from {meta['nick']} → {path}")
        _notify("File received", f"{meta['filename']} from {meta['nick']}")

    def _on_file_error(self, meta: dict, msg: str) -> None:
        GLib.idle_add(self._append_system, f"file '{meta.get('filename', '?')}' failed: {msg}")

    # -- UI actions ----------------------------------------------------------

    def _on_send(self, _widget) -> None:
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self.client.send_chat(
            {"id": uuid.uuid4().hex, "ts": time.time(), "from": self.cfg.device_id, "nick": self.cfg.nickname, "text": text}
        )

    def _on_attach(self, _widget) -> None:
        dialog = Gtk.FileDialog()
        dialog.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return  # user cancelled
        if gfile is None:
            return
        path = Path(gfile.get_path())
        try:
            transfer_id, total_chunks = send_file(self.client, self.cfg, path, to="*")
            self._append_system(f"sending '{path.name}' ({total_chunks} chunks, transfer {transfer_id})")
        except (FileNotFoundError, ValueError) as exc:
            self._append_system(f"! {exc}")

    def _on_close_request(self, _window) -> bool:
        self.client.disconnect()
        return False  # allow the window to close

    # -- settings --------------------------------------------------------

    def _on_open_settings(self, _widget) -> None:
        win = Adw.Window(transient_for=self, modal=True)
        win.set_default_size(440, 480)
        win.set_title("Settings")

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Settings"))
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        header.pack_end(save_btn)
        toolbar_view.add_top_bar(header)

        page = Adw.PreferencesPage()

        identity_group = Adw.PreferencesGroup(title="Identity")
        nickname_row = Adw.EntryRow(title="Nickname")
        nickname_row.set_text(self.cfg.nickname)
        identity_group.add(nickname_row)
        page.add(identity_group)

        network_group = Adw.PreferencesGroup(
            title="Network",
            description="Devices with the same name + passphrase can see and talk to each other.",
        )
        network_name_row = Adw.EntryRow(title="Network name")
        network_name_row.set_text(self.cfg.network_name)
        passphrase_row = Adw.PasswordEntryRow(title="Passphrase")
        passphrase_row.set_text(self.cfg.passphrase)
        network_group.add(network_name_row)
        network_group.add(passphrase_row)
        page.add(network_group)

        broker_group = Adw.PreferencesGroup(title="Broker")
        host_row = Adw.EntryRow(title="Host")
        host_row.set_text(self.cfg.broker_host)
        port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        port_row.set_title("Port")
        port_row.set_value(self.cfg.broker_port)
        tls_row = Adw.SwitchRow(title="Use TLS")
        tls_row.set_active(self.cfg.broker_tls)
        username_row = Adw.EntryRow(title="Username (optional)")
        username_row.set_text(self.cfg.broker_username)
        password_row = Adw.PasswordEntryRow(title="Password (optional)")
        password_row.set_text(self.cfg.broker_password)
        for row in (host_row, port_row, tls_row, username_row, password_row):
            broker_group.add(row)
        page.add(broker_group)

        toolbar_view.set_content(page)
        win.set_content(toolbar_view)

        def on_save(_btn) -> None:
            new_cfg = dataclasses.replace(
                self.cfg,
                nickname=nickname_row.get_text().strip() or self.cfg.nickname,
                network_name=network_name_row.get_text().strip(),
                passphrase=passphrase_row.get_text(),
                broker_host=host_row.get_text().strip(),
                broker_port=int(port_row.get_value()),
                broker_tls=tls_row.get_active(),
                broker_username=username_row.get_text().strip(),
                broker_password=password_row.get_text(),
            )
            if not new_cfg.broker_host or not new_cfg.network_name or not new_cfg.passphrase:
                self.toast_overlay.add_toast(Adw.Toast(title="Host, network name, and passphrase are required"))
                return
            new_cfg.save()
            win.close()
            self._apply_new_config(new_cfg)

        save_btn.connect("clicked", on_save)
        win.present()

    def _apply_new_config(self, new_cfg: Config) -> None:
        self.client.disconnect()

        self.cfg = new_cfg
        self.peers = PeerDirectory()
        self.receiver = FileReceiver(new_cfg, on_complete=self._on_file_complete, on_error=self._on_file_error)
        self.client = RelayClient(new_cfg)
        self._wire_client_callbacks()

        self.title_widget.set_title(f"Omarchy Relay — {new_cfg.nickname}")
        self.title_widget.set_subtitle(f"{new_cfg.network_name} · connecting…")
        self._refresh_peer_list()
        self._append_system("settings updated, reconnecting…")
        self.toast_overlay.add_toast(Adw.Toast(title="Settings saved"))

        threading.Thread(target=self._connect_worker, daemon=True).start()


class RelayApp(Adw.Application):
    def __init__(self, cfg: Config):
        super().__init__(application_id="net.omarchy.Relay")
        self.cfg = cfg
        self.connect("activate", self._on_activate)

    def _on_activate(self, app: "RelayApp") -> None:
        win = RelayWindow(app, self.cfg)
        win.present()


def run_gui(cfg: Config) -> None:
    app = RelayApp(cfg)
    app.run(None)
