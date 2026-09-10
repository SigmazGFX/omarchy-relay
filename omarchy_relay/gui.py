"""Native chat window built with GTK4 + libadwaita.

Like the Textual TUI, this is a presentation layer over the same
RelayClient/FileReceiver used everywhere else — the transport logic lives
in mqttclient.py/chat.py/transfer.py, this module only renders it.
RelayClient callbacks fire on paho's own network thread, so every UI
update is marshalled onto GTK's main loop via GLib.idle_add.
"""
from __future__ import annotations

import dataclasses
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .chat import _ding, _notify
from .config import Config
from .mqttclient import RelayClient
from .presence import PeerDirectory
from .transfer import FileReceiver, send_file

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_CLIPBOARD_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/bmp", "image/gif", "image/tiff", "image/webp")

# WhatsApp-inspired palette: teal header/accent, mint bubble for our own
# messages, white bubble for everyone else's, on the classic warm-beige
# chat wallpaper.
_WHATSAPP_CSS = """
headerbar.whatsapp-header {
  background: #075E54;
  color: #ffffff;
}
headerbar.whatsapp-header windowtitle > label.title {
  color: #ffffff;
}
headerbar.whatsapp-header windowtitle > label.subtitle {
  color: rgba(255, 255, 255, 0.75);
}
headerbar.whatsapp-header button {
  color: #ffffff;
}

list.whatsapp-chat-bg {
  background-color: #E5DDD5;
}

box.whatsapp-system-pill {
  background-color: rgba(255, 255, 255, 0.9);
  border-radius: 8px;
  padding: 3px 10px;
}

box.bubble-mine {
  background-color: #DCF8C6;
  border-radius: 12px;
  padding: 6px 10px;
}
box.bubble-theirs {
  background-color: #ffffff;
  border-radius: 12px;
  padding: 6px 10px;
  box-shadow: 0 1px 1px rgba(0, 0, 0, 0.15);
}
label.bubble-sender {
  color: #075E54;
}
label.bubble-text {
  color: #111b21;
}
label.bubble-stamp {
  color: rgba(17, 27, 33, 0.6);
}
label.whatsapp-system-text {
  color: rgba(17, 27, 33, 0.75);
}

box.whatsapp-composer {
  background-color: #F0F0F0;
  padding: 8px;
}
entry.whatsapp-entry {
  background-color: #ffffff;
  color: #111b21;
  border-radius: 18px;
  padding: 8px 14px;
  border: 1px solid #d9d9d9;
  box-shadow: none;
  outline: none;
}
entry.whatsapp-entry:focus,
entry.whatsapp-entry:focus-within {
  border: 1px solid #25D366;
  box-shadow: none;
  outline: none;
}
entry.whatsapp-entry text {
  color: #111b21;
}
entry.whatsapp-entry text selection {
  background-color: #25D366;
  color: #ffffff;
}
button.whatsapp-send-btn {
  background-color: #25D366;
  color: #ffffff;
  border-radius: 9999px;
  min-width: 34px;
  min-height: 34px;
  padding: 0;
}
button.whatsapp-attach-btn {
  color: #54656F;
  background: transparent;
}

list.whatsapp-sidebar {
  background-color: #ffffff;
}
list.whatsapp-sidebar row {
  border-bottom: 1px solid #ededed;
}
"""


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

        self._install_whatsapp_theme()

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.add_css_class("whatsapp-header")
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
        self.peer_list.add_css_class("whatsapp-sidebar")
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
        self.chat_list.add_css_class("whatsapp-chat-bg")
        self.chat_scroller.set_child(self.chat_list)
        content_box.append(self.chat_scroller)

        entry_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        entry_row.add_css_class("whatsapp-composer")
        self.entry = Gtk.Entry(hexpand=True, placeholder_text="Message… (paste an image to send it)")
        self.entry.add_css_class("whatsapp-entry")
        self.entry.connect("activate", self._on_send)
        paste_controller = Gtk.EventControllerKey()
        paste_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        paste_controller.connect("key-pressed", self._on_entry_key_pressed)
        self.entry.add_controller(paste_controller)
        attach_btn = Gtk.Button(icon_name="mail-attachment-symbolic", tooltip_text="Send a file")
        attach_btn.add_css_class("whatsapp-attach-btn")
        attach_btn.add_css_class("flat")
        attach_btn.connect("clicked", self._on_attach)
        send_btn = Gtk.Button(icon_name="mail-send-symbolic", tooltip_text="Send")
        send_btn.add_css_class("whatsapp-send-btn")
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

    def _install_whatsapp_theme(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_string(_WHATSAPP_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _wire_client_callbacks(self) -> None:
        self.client.on_chat = self._threaded(self._handle_chat)
        self.client.on_dm = self._threaded(self._handle_dm)
        self.client.on_presence = self._threaded(self._handle_presence)
        self.client.on_file_meta = self._threaded(self.receiver.handle_meta)
        self.client.on_file_chunk = self._threaded(self.receiver.handle_chunk)
        self.peers.on_removed = self._threaded(self._handle_peer_removed)

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

    def _bubble(self, is_mine: bool) -> tuple[Gtk.Box, Gtk.Box]:
        """A left/right-aligned outer wrapper plus the bubble box inside it."""
        outer = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            halign=Gtk.Align.END if is_mine else Gtk.Align.START,
            margin_start=12,
            margin_end=12,
            margin_top=3,
            margin_bottom=3,
        )
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        bubble.add_css_class("bubble-mine" if is_mine else "bubble-theirs")
        outer.append(bubble)
        return outer, bubble

    def _append_message(self, header_text: str, ts: float, text: str, is_mine: bool = False) -> None:
        outer, bubble = self._bubble(is_mine)
        if not is_mine:
            sender = Gtk.Label(xalign=0)
            sender.add_css_class("bubble-sender")
            sender.set_markup(f"<b>{GLib.markup_escape_text(header_text)}</b>")
            bubble.append(sender)
        body = Gtk.Label(xalign=0, wrap=True, selectable=True)
        body.add_css_class("bubble-text")
        body.set_max_width_chars(42)
        body.set_text(text)
        bubble.append(body)
        stamp = Gtk.Label(xalign=1)
        stamp.add_css_class("bubble-stamp")
        stamp.set_markup(f"<span size='small'>{_fmt_ts(ts)}</span>")
        bubble.append(stamp)
        self._append_row(outer)

    def _append_system(self, text: str) -> None:
        label = Gtk.Label(xalign=0.5)
        label.add_css_class("whatsapp-system-text")
        label.set_markup(f"<span size='small'>{GLib.markup_escape_text(text)}</span>")
        pill = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        pill.add_css_class("whatsapp-system-pill")
        pill.append(label)
        self._append_row(pill)

    def _append_file_received(self, meta: dict, path: Path) -> None:
        is_mine = meta.get("nick") == self.cfg.nickname
        outer, bubble = self._bubble(is_mine)
        if not is_mine:
            sender = Gtk.Label(xalign=0)
            sender.add_css_class("bubble-sender")
            sender.set_markup(f"<b>{GLib.markup_escape_text(meta['nick'])}</b>")
            bubble.append(sender)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(xalign=0, wrap=True)
        label.add_css_class("bubble-text")
        label.set_markup(f"📎 {GLib.markup_escape_text(meta['filename'])}")
        open_btn = Gtk.Button(label="Open Folder")
        open_btn.connect("clicked", lambda _b: self._open_containing_folder(path))
        row.append(label)
        row.append(open_btn)
        bubble.append(row)
        self._append_row(outer)

    def _open_containing_folder(self, path: Path) -> None:
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).open_containing_folder(self, None, None)

    def _append_image(self, nick: str, path: Path) -> None:
        is_mine = nick == self.cfg.nickname
        outer, bubble = self._bubble(is_mine)
        if not is_mine:
            caption = Gtk.Label(xalign=0)
            caption.add_css_class("bubble-sender")
            caption.set_markup(f"<b>{GLib.markup_escape_text(nick)}</b>")
            bubble.append(caption)
        picture = Gtk.Picture.new_for_filename(str(path))
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_halign(Gtk.Align.START)
        picture.set_size_request(-1, 240)  # cap the thumbnail height; width follows aspect ratio
        picture.set_can_shrink(True)
        open_btn = Gtk.Button(label="Open Folder", halign=Gtk.Align.START)
        open_btn.connect("clicked", lambda _b: self._open_containing_folder(path))
        bubble.append(picture)
        bubble.append(open_btn)
        self._append_row(outer)

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
            label.add_css_class("bubble-text")
            row.append(dot)
            row.append(label)
            self.peer_list.append(row)

    # -- RelayClient callbacks (already marshalled onto the GTK thread) ----

    def _handle_chat(self, obj: dict) -> None:
        is_mine = obj.get("from") == self.cfg.device_id
        self._append_message(obj["nick"], obj["ts"], obj["text"], is_mine=is_mine)
        # Broadcasts echo back to the sender too (we're subscribed to our
        # own publish topic) — don't notify ourselves for our own messages.
        if not is_mine:
            _notify(obj["nick"], obj["text"])
            _ding()

    def _handle_dm(self, obj: dict) -> None:
        self._append_message(f"DM from {obj['nick']}", obj["ts"], obj["text"])
        _notify(f"DM from {obj['nick']}", obj["text"])
        _ding()

    def _handle_presence(self, device_id: str, data) -> None:
        changed, previous = self.peers.update(device_id, data)
        if not changed:
            return  # includes a peer starting its 5s offline debounce — sidebar is unchanged until it actually fires
        if data is not None and previous is None and self.cfg.show_presence:
            self._append_system(f"{data['nick']} is online")
        self._refresh_peer_list()

    def _handle_peer_removed(self, device_id: str, last_known: dict) -> None:
        if self.cfg.show_presence:
            self._append_system(f"{last_known.get('nick', device_id)} went offline")
        self._refresh_peer_list()

    def _on_file_complete(self, meta: dict, path: Path) -> None:
        if path.suffix.lower() in _IMAGE_EXTENSIONS:
            GLib.idle_add(self._append_image, meta["nick"], path)
        else:
            GLib.idle_add(self._append_file_received, meta, path)
        _notify("File received", f"{meta['filename']} from {meta['nick']}")
        _ding()

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

    def _on_entry_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        is_paste = keyval == Gdk.KEY_v and bool(state & Gdk.ModifierType.CONTROL_MASK)
        if not is_paste:
            return False  # not our shortcut — let it through
        clipboard = self.get_clipboard()
        formats = clipboard.get_formats()
        if not any(formats.contain_mime_type(mime) for mime in _CLIPBOARD_IMAGE_MIME_TYPES):
            return False  # no image on the clipboard — fall through to normal text paste
        clipboard.read_texture_async(None, self._on_clipboard_texture_ready)
        return True  # image found: handle it ourselves, suppress the default text paste

    def _on_clipboard_texture_ready(self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
        try:
            texture = clipboard.read_texture_finish(result)
        except GLib.Error as exc:
            self._append_system(f"clipboard paste failed: {exc}")
            return
        if texture is None:
            return
        tmp_path = Path(tempfile.gettempdir()) / f"omarchy-relay-paste-{uuid.uuid4().hex[:8]}.png"
        texture.save_to_png(str(tmp_path))
        try:
            send_file(self.client, self.cfg, tmp_path, to="*")
        except (FileNotFoundError, ValueError) as exc:
            self._append_system(f"! {exc}")
            tmp_path.unlink(missing_ok=True)
            return
        # Our own broadcast files are deliberately not echoed back to us
        # (FileReceiver skips its own sender), so without this we'd never
        # see the image we just pasted in our own chat.
        self._append_image(self.cfg.nickname, tmp_path)
        # Gtk.Picture decodes the file into a texture at construction time,
        # so it's safe to clean up shortly after rather than keep it around.
        GLib.timeout_add_seconds(5, lambda: tmp_path.unlink(missing_ok=True) or False)

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

        chat_group = Adw.PreferencesGroup(title="Chat")
        presence_row = Adw.SwitchRow(
            title="Show online/offline messages",
            subtitle="Peer list stays accurate either way — this only mutes the log lines",
        )
        presence_row.set_active(self.cfg.show_presence)
        chat_group.add(presence_row)
        page.add(chat_group)

        toolbar_view.set_content(page)
        win.set_content(toolbar_view)

        # Fields that require tearing down and reconnecting RelayClient —
        # toggling a display-only preference like show_presence shouldn't
        # pay that cost (and would itself cause a spurious offline/online
        # flicker, which is exactly what this switch is meant to reduce).
        _RECONNECT_FIELDS = (
            "nickname",
            "network_name",
            "passphrase",
            "broker_host",
            "broker_port",
            "broker_tls",
            "broker_username",
            "broker_password",
        )

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
                show_presence=presence_row.get_active(),
            )
            if not new_cfg.broker_host or not new_cfg.network_name or not new_cfg.passphrase:
                self.toast_overlay.add_toast(Adw.Toast(title="Host, network name, and passphrase are required"))
                return
            new_cfg.save()
            win.close()
            needs_reconnect = any(
                getattr(new_cfg, field) != getattr(self.cfg, field) for field in _RECONNECT_FIELDS
            )
            if needs_reconnect:
                self._apply_new_config(new_cfg)
            else:
                self.cfg = new_cfg
                self.receiver.cfg = new_cfg
                self.toast_overlay.add_toast(Adw.Toast(title="Settings saved"))

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
        self.window: Optional[RelayWindow] = None
        self.connect("activate", self._on_activate)

    def _on_activate(self, app: "RelayApp") -> None:
        # GTK/GIO single-instance apps re-fire "activate" on the SAME
        # running process for every subsequent launch (app launcher click,
        # `omarchy-relay gui` run again, etc.) — without this guard each
        # one created a brand new window *and* a brand new RelayClient
        # sharing the same device_id, which just fight over the same MQTT
        # client identity. Re-present the existing window instead.
        if self.window is not None:
            self.window.present()
            return
        self.window = RelayWindow(app, self.cfg)
        self.window.present()


def run_gui(cfg: Config) -> None:
    app = RelayApp(cfg)
    app.run(None)
