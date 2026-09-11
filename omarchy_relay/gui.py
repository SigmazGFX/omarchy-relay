"""Native chat window built with GTK4 + libadwaita.

Like the Textual TUI, this is a presentation layer over the same
RelayClient/FileReceiver used everywhere else — the transport logic lives
in mqttclient.py/chat.py/transfer.py, this module only renders it.
RelayClient callbacks fire on paho's own network thread, so every UI
update is marshalled onto GTK's main loop via GLib.idle_add.
"""
from __future__ import annotations

import dataclasses
import math
import os
import random
import re
import tempfile
import threading
import time
import tomllib
import uuid
import zlib
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import audio, history, network_share
from .chat import _ding, _notify
from .config import Config
from .mqttclient import RelayClient
from .presence import PeerDirectory
from .transfer import FileReceiver, send_file

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_AUDIO_EXTENSIONS = {".ogg", ".opus", ".oga"}
_CLIPBOARD_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/bmp", "image/gif", "image/tiff", "image/webp")

# Consecutive messages from the same sender within this many seconds render
# as one group: a single avatar and name, tighter spacing, joined corners.
_GROUP_WINDOW_SECONDS = 5 * 60
_IMAGE_MAX_WIDTH = 320
_IMAGE_MAX_HEIGHT = 260
_NICK_COLORS = 6
_CONTENT_MAX_WIDTH = 860

# Every color here is one of libadwaita's own CSS variables, so the window
# follows light/dark mode out of the box; _omarchy_theme_css() then points
# those variables at the active Omarchy theme's palette when there is one.
_BASE_CSS = """
:root {
  --relay-nick-0: var(--accent-blue);
  --relay-nick-1: var(--accent-green);
  --relay-nick-2: var(--accent-orange);
  --relay-nick-3: var(--accent-purple);
  --relay-nick-4: var(--accent-teal);
  --relay-nick-5: var(--accent-pink);
}

.relay-chat {
  background-color: var(--view-bg-color);
}

.bubble {
  border-radius: 18px;
  padding: 7px 12px;
}
.bubble.theirs {
  background-color: color-mix(in srgb, var(--card-bg-color), var(--card-fg-color) 6%);
  color: var(--card-fg-color);
  box-shadow: 0 1px 2px var(--card-shade-color);
}
.bubble.mine {
  background-color: var(--accent-bg-color);
  color: var(--accent-fg-color);
}
.bubble.dm {
  box-shadow: inset 0 0 0 1px var(--accent-color), 0 1px 2px var(--card-shade-color);
}
.bubble.media {
  padding: 0;
}
.bubble.theirs.joined-above { border-top-left-radius: 6px; }
.bubble.theirs.joined-below { border-bottom-left-radius: 6px; }
.bubble.mine.joined-above { border-top-right-radius: 6px; }
.bubble.mine.joined-below { border-bottom-right-radius: 6px; }
.bubble .stamp {
  opacity: 0.65;
}
.bubble .emoji-only {
  font-size: 2.4em;
}

.sparkle-particle {
  font-size: 1.6em;
}

/* background-color alone is unreliable on a .flat button — the theme's
   flat styling tends to suppress it, leaving no visible change at all.
   color reliably tints the symbolic icon instead. */
button.recording {
  color: #e01b24;
}

.recording-dot {
  color: #e01b24;
  font-size: 1.1em;
}
.recording-indicator label {
  color: #e01b24;
}

.reactions-row {
  margin-top: 2px;
}
.reaction-pill {
  min-width: 0;
  min-height: 0;
  padding: 1px 8px;
  border-radius: 9999px;
  font-size: 0.85em;
  background-color: color-mix(in srgb, var(--card-bg-color), var(--card-fg-color) 6%);
  box-shadow: 0 1px 1px var(--card-shade-color);
}
.reaction-pill.mine {
  background-color: color-mix(in srgb, var(--accent-bg-color) 30%, var(--card-bg-color));
  box-shadow: inset 0 0 0 1px var(--accent-color);
}
.react-btn {
  min-width: 22px;
  min-height: 22px;
  opacity: 0.55;
}
.react-btn:hover {
  opacity: 1;
}
.reaction-popover .reaction-pick {
  font-size: 1.3em;
  min-width: 32px;
  min-height: 32px;
  padding: 0;
  border-radius: 9999px;
}

.nick-0 { color: oklab(from var(--relay-nick-0) var(--standalone-color-oklab)); }
.nick-1 { color: oklab(from var(--relay-nick-1) var(--standalone-color-oklab)); }
.nick-2 { color: oklab(from var(--relay-nick-2) var(--standalone-color-oklab)); }
.nick-3 { color: oklab(from var(--relay-nick-3) var(--standalone-color-oklab)); }
.nick-4 { color: oklab(from var(--relay-nick-4) var(--standalone-color-oklab)); }
.nick-5 { color: oklab(from var(--relay-nick-5) var(--standalone-color-oklab)); }
.dm-tag {
  color: var(--accent-color);
}

.file-icon {
  min-width: 40px;
  min-height: 40px;
  border-radius: 12px;
  background-color: color-mix(in srgb, var(--accent-bg-color) 20%, transparent);
  color: var(--accent-color);
}
.bubble.mine .file-icon {
  background-color: rgba(0, 0, 0, 0.15);
  color: inherit;
}

.composer {
  padding: 4px 12px 12px 12px;
}
.composer-field {
  background-color: color-mix(in srgb, var(--card-bg-color), var(--card-fg-color) 6%);
  border-radius: 9999px;
  padding: 3px;
  box-shadow: 0 1px 2px var(--card-shade-color);
}
.composer-field > entry,
.composer-field > entry:focus-within {
  background: none;
  box-shadow: none;
  outline: none;
  min-height: 34px;
}
button.send-button {
  min-width: 40px;
  min-height: 40px;
  padding: 0;
}

.sidebar-heading {
  margin: 6px 18px 2px 18px;
}
.presence-dot {
  min-width: 10px;
  min-height: 10px;
  border-radius: 9999px;
  background-color: var(--success-bg-color);
  box-shadow: 0 0 0 2px var(--sidebar-bg-color);
}
.profile {
  padding: 8px 10px;
}
"""

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?")


def _omarchy_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "omarchy" / "current"


def _omarchy_theme_colors() -> dict[str, str]:
    """The active Omarchy theme's colors.toml, or {} when not on Omarchy."""
    state_dir = _omarchy_state_dir()
    candidates = [state_dir / "theme" / "colors.toml"]
    try:
        name = (state_dir / "theme.name").read_text().strip()
    except OSError:
        name = ""
    if name:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        candidates.append(config_home / "omarchy" / "themes" / name / "colors.toml")
        candidates.append(Path("/usr/share/omarchy/themes") / name / "colors.toml")
    for path in candidates:
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        return {key: value for key, value in data.items() if isinstance(value, str)}
    return {}


def _readable_on(hex_color: str) -> str:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "rgba(0, 0, 0, 0.87)" if luminance > 0.5 else "#ffffff"


def _omarchy_theme_css(colors: dict[str, str], dark: bool) -> str:
    """Maps an Omarchy palette onto libadwaita's color variables. Empty when
    there's no usable theme, or its mode disagrees with the current
    light/dark style — stock libadwaita colors are used as-is then."""

    def color(key: str) -> Optional[str]:
        value = colors.get(key, "")
        return value if _HEX_COLOR.fullmatch(value) else None

    background, foreground, accent = color("background"), color("foreground"), color("accent")
    if not (background and foreground and accent):
        return ""
    if colors.get("mode") in ("dark", "light") and (colors["mode"] == "dark") != dark:
        return ""
    sidebar = color("dark_background") or background
    raised = color("lighter_background") or background
    variables = {
        "--window-bg-color": background,
        "--window-fg-color": foreground,
        "--view-bg-color": background,
        "--view-fg-color": foreground,
        "--headerbar-bg-color": background,
        "--headerbar-fg-color": foreground,
        "--headerbar-backdrop-color": background,
        "--sidebar-bg-color": sidebar,
        "--sidebar-fg-color": foreground,
        "--sidebar-backdrop-color": sidebar,
        "--card-bg-color": raised,
        "--card-fg-color": foreground,
        "--dialog-bg-color": sidebar,
        "--dialog-fg-color": foreground,
        "--popover-bg-color": raised,
        "--popover-fg-color": foreground,
        "--accent-bg-color": accent,
        "--accent-fg-color": _readable_on(accent),
    }
    for role, key in (("success", "green"), ("warning", "yellow"), ("error", "red"), ("destructive", "red")):
        value = color(key)
        if value:
            variables[f"--{role}-bg-color"] = value
            variables[f"--{role}-fg-color"] = _readable_on(value)
    for index, key in enumerate(("blue", "green", "orange", "magenta", "cyan", "red")):
        value = color(key)
        if value:
            variables[f"--relay-nick-{index}"] = value
    body = "\n".join(f"  {name}: {value};" for name, value in variables.items())
    return f":root {{\n{body}\n}}\n"


def _fmt_time(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _fmt_full_time(ts: float) -> str:
    return time.strftime("%A %-d %B %Y, %H:%M:%S", time.localtime(ts))


def _nick_color_class(nick: str) -> str:
    return f"nick-{zlib.crc32(nick.encode()) % _NICK_COLORS}"


# Covers the emoji-bearing Unicode blocks in one contiguous sweep per range
# (unassigned code points inside them just never match), plus variation
# selector/ZWJ/keycap modifiers so multi-codepoint sequences (flags, skin
# tones, ZWJ combos) still count as "emoji" rather than breaking the match.
_EMOJI_ONLY_RE = re.compile(
    r"^[\s0-9"
    r"\U0001F300-\U0001FAFF"
    r"\U00002600-\U000027BF"
    r"\U00002300-\U000023FF"
    r"\U00002B00-\U00002BFF"
    r"\U0001F1E6-\U0001F1FF"
    r"️‍⃣]+$"
)


_QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🎉"]

# A magic word: any message containing it (case-insensitive) sets off a
# pixie-dust burst on every client that renders it — sender included, via
# the broadcast echo.
_SPARKLE_TRIGGER = "/sparkles"
_SPARKLE_GLYPHS = ["✨", "💫", "⭐", "🌟"]
_SPARKLE_COUNT = 28
_SPARKLE_DURATION_MS = 5000
_SPARKLE_STEP_MS = 50


def _is_emoji_only(text: str, max_len: int = 12) -> bool:
    """A short WhatsApp/iMessage-style check: render standalone emoji (with
    no other text) larger in the bubble. max_len caps it so someone can't
    get a giant wall of emoji."""
    stripped = text.strip()
    return bool(stripped) and len(stripped) <= max_len and bool(_EMOJI_ONLY_RE.match(stripped))


class RelayWindow(Adw.ApplicationWindow):
    def __init__(self, app: "RelayApp", cfg: Config):
        super().__init__(application=app, title="Omarchy Relay")
        self.cfg = cfg
        self.peers = PeerDirectory()
        self.set_default_size(900, 640)
        self.set_size_request(360, 420)

        self.client = RelayClient(cfg)
        self.receiver = FileReceiver(cfg, on_complete=self._on_file_complete, on_error=self._on_file_error)

        self._connection_state = "connecting"
        # Message grouping: whose bubble came last, when, and the bubble
        # itself (its bottom corner joins onto the next one in the group).
        self._group_key: Optional[tuple] = None
        self._group_ts = 0.0
        self._group_bubble: Optional[Gtk.Widget] = None
        self._last_row_was_system = False
        self._stick_to_bottom = True
        self._scroll_pending = False
        self._chat_extent = (0.0, 0.0)  # (upper, page_size) as of the last layout change

        # Reactions, keyed by message id — session-only, like the rest of the
        # chat log (nothing here is persisted to disk).
        self._message_meta: dict[str, dict] = {}  # msg_id -> {"is_dm", "peer_device_id"}
        self._reaction_slots: dict[str, Gtk.Box] = {}  # msg_id -> its reaction-pills row
        self._reactions: dict[str, dict[str, dict[str, str]]] = {}  # msg_id -> emoji -> device_id -> nick
        self._sparkle_active = False
        self._recorder: Optional[audio.Recorder] = None
        self._recording_path: Optional[Path] = None
        self._recording_started_at: float = 0.0
        self._recording_timer_id: Optional[int] = None
        self._voice_players: list[dict] = []  # each: {"player": audio.Player | None, "reset": callable}

        self._install_theme()

        split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=280, show_content=True)
        split.set_sidebar(self._build_sidebar())
        split.set_content(self._build_content())

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        narrow.add_setter(split, "collapsed", True)
        self.add_breakpoint(narrow)

        self.toast_overlay = Adw.ToastOverlay(child=split)
        # Outermost layer, above toasts/sidebar/composer, so a /sparkles
        # burst can cover the whole window without a widget hierarchy of
        # its own to keep updated — particles just get added/removed here.
        self.sparkle_overlay = Gtk.Overlay(child=self.toast_overlay)
        self.set_content(self.sparkle_overlay)
        self.set_focus(self.entry)
        self._update_status()

        self.history = history.HistoryStore()
        self._load_history()

        self._wire_client_callbacks()

        self.connect("close-request", self._on_close_request)
        threading.Thread(target=self._connect_worker, daemon=True).start()

    # -- layout --------------------------------------------------------------

    def _build_sidebar(self) -> Adw.NavigationPage:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Omarchy Relay"))

        self.online_heading = Gtk.Label(xalign=0, css_classes=["caption-heading", "dim-label", "sidebar-heading"])
        self.peer_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["navigation-sidebar"])
        self.peer_list.set_placeholder(
            Gtk.Label(label="Nobody else is online", wrap=True, margin_top=12, margin_bottom=12, css_classes=["dim-label"])
        )
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.online_heading)
        body.append(self.peer_list)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=body)

        # Who you are on this network, pinned to the bottom of the sidebar.
        self.profile_avatar = Adw.Avatar(size=36, text=self.cfg.nickname, show_initials=True)
        self.profile_name = Gtk.Label(
            label=self.cfg.nickname, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"]
        )
        self.profile_status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["caption", "dim-label"])
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        labels.append(self.profile_name)
        labels.append(self.profile_status)
        settings_btn = Gtk.Button(
            icon_name="preferences-system-symbolic",
            tooltip_text="Settings",
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular"],
        )
        settings_btn.connect("clicked", self._on_open_settings)
        profile = Gtk.Box(spacing=10, css_classes=["profile"])
        profile.append(self.profile_avatar)
        profile.append(labels)
        profile.append(settings_btn)

        view = Adw.ToolbarView(content=scroller)
        view.add_top_bar(header)
        view.add_bottom_bar(profile)
        return Adw.NavigationPage(title="Online", child=view)

    def _build_content(self) -> Adw.NavigationPage:
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title=self.cfg.network_name)
        header.set_title_widget(self.title_widget)

        self.banner = Adw.Banner(title="Couldn't reach the broker", button_label="Settings")
        self.banner.connect("button-clicked", self._on_open_settings)

        empty_page = Adw.StatusPage(
            icon_name="user-available-symbolic",
            title="No messages yet",
            description="Messages on this network show up here.\nPaste an image or attach a file to share it.",
        )
        self.chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=8, margin_bottom=8)
        clamp = Adw.Clamp(maximum_size=_CONTENT_MAX_WIDTH, tightening_threshold=600, child=self.chat_box)
        self.chat_scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp)
        adjustment = self.chat_scroller.get_vadjustment()
        adjustment.connect("changed", self._on_chat_resized)
        adjustment.connect("notify::upper", lambda adj, _pspec: self._on_chat_resized(adj))
        adjustment.connect("notify::page-size", lambda adj, _pspec: self._on_chat_resized(adj))
        adjustment.connect("value-changed", self._on_chat_scrolled)

        self.chat_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.chat_stack.add_named(empty_page, "empty")
        self.chat_stack.add_named(self.chat_scroller, "chat")

        view = Adw.ToolbarView(content=self.chat_stack, css_classes=["relay-chat"])
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        view.add_bottom_bar(self._build_composer())
        self.content_page = Adw.NavigationPage(title=self.cfg.network_name, child=view)
        return self.content_page

    def _build_composer(self) -> Gtk.Widget:
        attach_btn = Gtk.Button(
            icon_name="mail-attachment-symbolic",
            tooltip_text="Send a file",
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular"],
        )
        attach_btn.connect("clicked", self._on_attach)
        self.entry = Gtk.Entry(hexpand=True, placeholder_text=self._composer_placeholder())
        self.entry.connect("activate", self._on_send)
        self.entry.connect("changed", self._on_entry_changed)
        paste_controller = Gtk.EventControllerKey()
        paste_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        paste_controller.connect("key-pressed", self._on_entry_key_pressed)
        self.entry.add_controller(paste_controller)

        emoji_chooser = Gtk.EmojiChooser()
        emoji_chooser.connect("emoji-picked", self._on_emoji_picked)
        emoji_btn = Gtk.MenuButton(
            icon_name="face-smile-symbolic",
            tooltip_text="Insert an emoji",
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular"],
            popover=emoji_chooser,
        )

        # Swapped in for self.entry while recording — unmissable even
        # without a real audio meter: a red dot, "Recording...", and a
        # live-ticking timer, so there's no ambiguity about whether a click
        # actually started anything.
        self.recording_indicator = Gtk.Box(
            spacing=8, hexpand=True, valign=Gtk.Align.CENTER, visible=False, css_classes=["recording-indicator"]
        )
        self.recording_indicator.append(Gtk.Label(label="⏺", css_classes=["recording-dot"]))
        self.recording_indicator.append(Gtk.Label(label="Recording…"))
        self.recording_time_label = Gtk.Label(label="0:00", hexpand=True, xalign=0)
        self.recording_indicator.append(self.recording_time_label)

        field = Gtk.Box(spacing=2, hexpand=True, css_classes=["composer-field"])
        field.append(attach_btn)
        field.append(self.entry)
        field.append(self.recording_indicator)
        field.append(emoji_btn)

        self.send_btn = Gtk.Button(
            icon_name="mail-send-symbolic",
            tooltip_text="Send",
            valign=Gtk.Align.CENTER,
            sensitive=False,
            css_classes=["circular", "suggested-action", "send-button"],
        )
        self.send_btn.connect("clicked", self._on_send)

        self.mic_btn = Gtk.Button(
            icon_name="audio-input-microphone-symbolic",
            tooltip_text="Record a voice message",
            valign=Gtk.Align.CENTER,
            css_classes=["circular", "flat"],
        )
        self.mic_btn.connect("clicked", self._on_mic_clicked)

        composer = Gtk.Box(spacing=8, css_classes=["composer"])
        composer.append(field)
        composer.append(self.mic_btn)
        composer.append(self.send_btn)
        return Adw.Clamp(maximum_size=_CONTENT_MAX_WIDTH, tightening_threshold=600, child=composer)

    def _composer_placeholder(self) -> str:
        return f"Message {self.cfg.network_name}"

    def _install_theme(self) -> None:
        self._css_provider = Gtk.CssProvider()
        self._reload_theme()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._reload_theme())
        # Omarchy rewrites its current-theme state on every theme switch, so
        # watching it restyles an open window along with the rest of the desktop.
        try:
            self._theme_monitor = Gio.File.new_for_path(str(_omarchy_state_dir())).monitor_directory(
                Gio.FileMonitorFlags.NONE, None
            )
            self._theme_monitor.connect("changed", lambda *_: self._reload_theme())
        except GLib.Error:
            self._theme_monitor = None

    def _reload_theme(self) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        self._css_provider.load_from_string(_BASE_CSS + _omarchy_theme_css(_omarchy_theme_colors(), dark))

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
            GLib.idle_add(self._on_connect_failed, str(exc))

    def _on_connected(self) -> None:
        self._connection_state = "connected"
        self.banner.set_revealed(False)
        self._update_status()

    def _on_connect_failed(self, reason: str) -> None:
        self._connection_state = "failed"
        self.banner.set_revealed(True)
        self._update_status()
        self._append_system(f"Connection failed: {reason}")

    def _update_status(self) -> None:
        others = sum(1 for device_id in self.peers.snapshot() if device_id != self.cfg.device_id)
        state = {"connecting": "Connecting…", "connected": "Connected", "failed": "Not connected"}[self._connection_state]
        self.title_widget.set_subtitle(f"{others} online" if self._connection_state == "connected" else state)
        self.profile_status.set_label(state)
        self.online_heading.set_label(f"Online — {others}")

    # -- rendering -----------------------------------------------------------

    def _on_chat_resized(self, adjustment: Gtk.Adjustment) -> None:
        self._chat_extent = (adjustment.get_upper(), adjustment.get_page_size())
        # This fires mid-layout, where a scroll's own re-layout request gets
        # dropped (the scrollbar moves but the content doesn't). Scroll once
        # layout has finished instead.
        if self._stick_to_bottom and not self._scroll_pending:
            self._scroll_pending = True
            GLib.idle_add(self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> bool:
        self._scroll_pending = False
        if self._stick_to_bottom:
            adjustment = self.chat_scroller.get_vadjustment()
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        return False

    def _on_chat_scrolled(self, adjustment: Gtk.Adjustment) -> None:
        # Only follow new messages while the reader is already at the bottom —
        # scrolling up to reread something shouldn't get yanked back down.
        # GTK can report a value change from the content growing before it
        # reports the growth itself; that isn't the reader scrolling.
        if (adjustment.get_upper(), adjustment.get_page_size()) != self._chat_extent:
            return
        self._stick_to_bottom = adjustment.get_value() >= adjustment.get_upper() - adjustment.get_page_size() - 48

    def _append_row(self, widget: Gtk.Widget, follow: bool = False) -> None:
        if follow:
            self._stick_to_bottom = True
        self._last_row_was_system = False
        self.chat_box.append(widget)
        self.chat_stack.set_visible_child_name("chat")

    def _stamp(self, ts: float) -> Gtk.Label:
        return Gtk.Label(label=_fmt_time(ts), tooltip_text=_fmt_full_time(ts), css_classes=["caption", "stamp"])

    def _append_bubble(
        self,
        bubble: Gtk.Widget,
        *,
        nick: str,
        ts: float,
        is_mine: bool,
        is_dm: bool = False,
        msg_id: Optional[str] = None,
        dm_peer_device_id: Optional[str] = None,
    ) -> None:
        """Places a bubble on its side of the chat, folding it into the
        previous message's group when it's the same sender shortly after."""
        key = ("mine",) if is_mine else ("theirs", nick, is_dm)
        joined = key == self._group_key and ts - self._group_ts <= _GROUP_WINDOW_SECONDS
        bubble.add_css_class("mine" if is_mine else "theirs")
        if is_dm:
            bubble.add_css_class("dm")
        if joined:
            bubble.add_css_class("joined-above")
            self._group_bubble.add_css_class("joined-below")
        self._group_key, self._group_ts, self._group_bubble = key, ts, bubble

        row = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=2 if joined else 10)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        if is_mine:
            row.set_halign(Gtk.Align.END)
            row.set_margin_start(72)
        else:
            row.set_halign(Gtk.Align.START)
            row.set_margin_end(72)
            if joined:
                row.append(Gtk.Box(width_request=32))  # keeps the bubble under the group's avatar
            else:
                row.append(Adw.Avatar(size=32, text=nick, show_initials=True, valign=Gtk.Align.START))
                sender = Gtk.Box(spacing=6)
                sender.append(Gtk.Label(label=nick, xalign=0, css_classes=["caption-heading", _nick_color_class(nick)]))
                if is_dm:
                    sender.append(Gtk.Label(label="Direct message", css_classes=["caption", "dm-tag"]))
                column.append(sender)
        column.append(bubble)
        if msg_id:
            self._message_meta[msg_id] = {"is_dm": is_dm, "peer_device_id": dm_peer_device_id}
            reactions_row = Gtk.Box(spacing=4, css_classes=["reactions-row"])
            self._reaction_slots[msg_id] = reactions_row
            footer = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
            footer.append(reactions_row)
            footer.append(self._build_react_button(msg_id))
            if is_mine:
                footer.set_halign(Gtk.Align.END)
            column.append(footer)
        row.append(column)
        self._append_row(row, follow=is_mine)

    def _append_text(
        self,
        nick: str,
        ts: float,
        text: str,
        msg_id: Optional[str] = None,
        is_mine: bool = False,
        is_dm: bool = False,
        dm_peer_device_id: Optional[str] = None,
        is_history: bool = False,
    ) -> None:
        body = Gtk.Label(
            label=text,
            xalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            # Without this a WORD_CHAR label asks for its narrowest wrap as
            # its natural width, leaving short lines inside a wide bubble.
            natural_wrap_mode=Gtk.NaturalWrapMode.NONE,
            hexpand=True,
            selectable=True,
            max_width_chars=52,
        )
        if _is_emoji_only(text):
            body.add_css_class("emoji-only")
        stamp = self._stamp(ts)
        stamp.set_valign(Gtk.Align.END)
        bubble = Gtk.Box(spacing=10, css_classes=["bubble"])
        bubble.append(body)
        bubble.append(stamp)
        self._append_bubble(
            bubble, nick=nick, ts=ts, is_mine=is_mine, is_dm=is_dm, msg_id=msg_id, dm_peer_device_id=dm_peer_device_id
        )
        if not is_history and _SPARKLE_TRIGGER in text.lower():
            self._play_sparkles()

    def _append_system(self, text: str) -> None:
        label = Gtk.Label(
            label=text,
            wrap=True,
            justify=Gtk.Justification.CENTER,
            margin_top=2 if self._last_row_was_system else 12,
            margin_bottom=4,
            margin_start=24,
            margin_end=24,
            css_classes=["caption", "dim-label"],
        )
        self._group_key = None
        self._append_row(label)
        self._last_row_was_system = True

    def _append_file_received(self, meta: dict, path: Path) -> None:
        now = time.time()
        icon = Gtk.Image(icon_name="text-x-generic-symbolic", pixel_size=20, css_classes=["file-icon"])
        name = Gtk.Label(
            label=meta["filename"],
            xalign=0,
            ellipsize=Pango.EllipsizeMode.MIDDLE,
            max_width_chars=32,
            css_classes=["heading"],
        )
        details = [GLib.format_size(meta["size"])] if meta.get("size") is not None else []
        details.append(_fmt_time(now))
        info_line = Gtk.Label(label=" · ".join(details), xalign=0, css_classes=["caption", "stamp"])
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        info.append(name)
        info.append(info_line)
        open_btn = Gtk.Button(
            icon_name="folder-open-symbolic",
            tooltip_text="Show in folder",
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular"],
        )
        open_btn.connect("clicked", lambda _b: self._open_containing_folder(path))
        bubble = Gtk.Box(spacing=10, css_classes=["bubble"])
        bubble.append(icon)
        bubble.append(info)
        bubble.append(open_btn)
        self._append_bubble(bubble, nick=meta["nick"], ts=now, is_mine=meta.get("nick") == self.cfg.nickname)

    def _open_containing_folder(self, path: Path) -> None:
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).open_containing_folder(self, None, None)

    def _append_voice(self, meta: dict, path: Path) -> None:
        now = time.time()
        play_btn = Gtk.Button(
            icon_name="media-playback-start-symbolic",
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular"],
        )
        icon = Gtk.Image(icon_name="audio-x-generic-symbolic", pixel_size=20, css_classes=["file-icon"])
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        info.append(Gtk.Label(label="Voice message", xalign=0, css_classes=["heading"]))
        info.append(
            Gtk.Label(label=audio.format_duration(meta.get("duration")), xalign=0, css_classes=["caption", "stamp"])
        )
        bubble = Gtk.Box(spacing=10, css_classes=["bubble"])
        bubble.append(icon)
        bubble.append(info)
        bubble.append(play_btn)

        state = {"player": None, "reset": lambda: play_btn.set_icon_name("media-playback-start-symbolic")}
        self._voice_players.append(state)

        def on_finished() -> bool:
            state["reset"]()
            return False

        def on_click(_btn) -> None:
            player = state["player"]
            if player is not None and player.playing:
                player.stop()
                state["reset"]()
                return
            self._stop_other_voice_players(state)
            if player is None:
                player = audio.Player(path, on_finished=lambda: GLib.idle_add(on_finished))
                state["player"] = player
            player.play()
            play_btn.set_icon_name("media-playback-stop-symbolic")

        play_btn.connect("clicked", on_click)
        self._append_bubble(bubble, nick=meta["nick"], ts=now, is_mine=meta.get("nick") == self.cfg.nickname)

    def _stop_other_voice_players(self, exclude: dict) -> None:
        for state in self._voice_players:
            if state is exclude:
                continue
            player = state.get("player")
            if player is not None and player.playing:
                player.stop()
                state["reset"]()

    def _append_image(self, nick: str, path: Path) -> None:
        is_mine = nick == self.cfg.nickname
        try:
            texture = Gdk.Texture.new_from_filename(str(path))
        except GLib.Error:
            self._append_file_received({"nick": nick, "filename": path.name}, path)
            return
        scale = min(1.0, _IMAGE_MAX_WIDTH / texture.get_width(), _IMAGE_MAX_HEIGHT / texture.get_height())
        picture = Gtk.Picture(paintable=texture, content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        picture.set_size_request(max(1, round(texture.get_width() * scale)), max(1, round(texture.get_height() * scale)))
        bubble = Gtk.Overlay(child=picture, overflow=Gtk.Overflow.HIDDEN, css_classes=["bubble", "media"])
        # Our own images are pasted from a temp file that's gone moments
        # later, so there's no folder worth opening for those.
        if not is_mine:
            open_btn = Gtk.Button(
                icon_name="folder-open-symbolic",
                tooltip_text="Show in folder",
                halign=Gtk.Align.END,
                valign=Gtk.Align.END,
                margin_end=8,
                margin_bottom=8,
                css_classes=["osd", "circular"],
            )
            open_btn.connect("clicked", lambda _b: self._open_containing_folder(path))
            bubble.add_overlay(open_btn)
        self._append_bubble(bubble, nick=nick, ts=time.time(), is_mine=is_mine)

    def _refresh_peer_list(self) -> None:
        self.peer_list.remove_all()
        peers = sorted(
            self.peers.snapshot().items(),
            key=lambda kv: (kv[0] != self.cfg.device_id, kv[1].get("nick", "").lower()),
        )
        for device_id, data in peers:
            nick = data.get("nick", "?")
            avatar = Gtk.Overlay(child=Adw.Avatar(size=32, text=nick, show_initials=True), valign=Gtk.Align.CENTER)
            avatar.add_overlay(Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.END, css_classes=["presence-dot"]))
            name = Gtk.Label(
                label=f"{nick} (you)" if device_id == self.cfg.device_id else nick,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            device = Gtk.Label(
                label=device_id, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, css_classes=["caption", "dim-label"]
            )
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            labels.append(name)
            labels.append(device)
            box = Gtk.Box(spacing=10, margin_top=3, margin_bottom=3)
            box.append(avatar)
            box.append(labels)
            self.peer_list.append(Gtk.ListBoxRow(child=box, activatable=False, selectable=False))
        self._update_status()

    # -- RelayClient callbacks (already marshalled onto the GTK thread) ----

    def _handle_chat(self, obj: dict) -> None:
        if obj.get("type") == "reaction":
            self._handle_reaction(obj)
            return
        is_mine = obj.get("from") == self.cfg.device_id
        self._append_text(obj["nick"], obj["ts"], obj["text"], msg_id=obj.get("id"), is_mine=is_mine)
        self._remember(obj, is_dm=False, peer_device_id=None)
        # Broadcasts echo back to the sender too (we're subscribed to our
        # own publish topic) — don't notify ourselves for our own messages.
        if not is_mine:
            _notify(obj["nick"], obj["text"])
            _ding()

    def _handle_dm(self, obj: dict) -> None:
        if obj.get("type") == "reaction":
            self._handle_reaction(obj)
            return
        self._append_text(
            obj["nick"], obj["ts"], obj["text"], msg_id=obj.get("id"), is_dm=True, dm_peer_device_id=obj.get("from")
        )
        self._remember(obj, is_dm=True, peer_device_id=obj.get("from"))
        _notify(f"DM from {obj['nick']}", obj["text"])
        _ding()

    def _remember(self, obj: dict, *, is_dm: bool, peer_device_id: Optional[str]) -> None:
        if not obj.get("id"):
            return  # nothing stable to key on — skip rather than store an unfindable row
        self.history.add_message(
            self.cfg.network_name, obj["id"], obj["ts"], obj["from"], obj["nick"], obj["text"], is_dm, peer_device_id
        )
        self.history.prune(self.cfg.network_name, self.cfg.history_retain_count, self.cfg.history_retain_days)

    def _load_history(self) -> None:
        limit = self.cfg.history_retain_count or None
        messages = self.history.recent_messages(self.cfg.network_name, limit=limit)
        if not messages:
            return
        for m in messages:
            is_mine = not m["is_dm"] and m["from_device"] == self.cfg.device_id
            self._append_text(
                m["nick"],
                m["ts"],
                m["text"],
                msg_id=m["id"],
                is_mine=is_mine,
                is_dm=m["is_dm"],
                dm_peer_device_id=m["peer_device_id"],
                is_history=True,
            )
        count = len(messages)
        self._append_system(f"{count} earlier message{'s' if count != 1 else ''} loaded")

    def _handle_reaction(self, obj: dict) -> None:
        target_id, emoji, device_id = obj.get("target_id"), obj.get("emoji"), obj.get("from")
        if not target_id or not emoji or not device_id or target_id not in self._message_meta:
            return  # unknown/expired message (e.g. from before this window opened) — nothing to anchor it to
        by_emoji = self._reactions.setdefault(target_id, {}).setdefault(emoji, {})
        if obj.get("op") == "remove":
            by_emoji.pop(device_id, None)
            if not by_emoji:
                self._reactions[target_id].pop(emoji, None)
        else:
            by_emoji[device_id] = obj.get("nick", "?")
        self._render_reactions(target_id)

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
        if meta.get("kind") == "voice" or path.suffix.lower() in _AUDIO_EXTENSIONS:
            GLib.idle_add(self._append_voice, meta, path)
            _notify("Voice message", f"from {meta['nick']}")
        elif path.suffix.lower() in _IMAGE_EXTENSIONS:
            GLib.idle_add(self._append_image, meta["nick"], path)
            _notify("File received", f"{meta['filename']} from {meta['nick']}")
        else:
            GLib.idle_add(self._append_file_received, meta, path)
            _notify("File received", f"{meta['filename']} from {meta['nick']}")
        _ding()

    def _on_file_error(self, meta: dict, msg: str) -> None:
        GLib.idle_add(self._append_system, f"Couldn't receive {meta.get('filename', 'a file')}: {msg}")

    # -- UI actions ----------------------------------------------------------

    def _on_entry_changed(self, entry: Gtk.Entry) -> None:
        self.send_btn.set_sensitive(bool(entry.get_text().strip()))

    def _on_send(self, _widget) -> None:
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self.client.send_chat(
            {"id": uuid.uuid4().hex, "ts": time.time(), "from": self.cfg.device_id, "nick": self.cfg.nickname, "text": text}
        )

    def _build_react_button(self, msg_id: str) -> Gtk.MenuButton:
        picks = Gtk.Box(spacing=2, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for emoji in _QUICK_REACTIONS:
            pick_btn = Gtk.Button(label=emoji, css_classes=["flat", "reaction-pick"])
            pick_btn.connect("clicked", self._on_quick_react, msg_id)
            picks.append(pick_btn)
        popover = Gtk.Popover(css_classes=["reaction-popover"], child=picks)
        return Gtk.MenuButton(
            icon_name="face-smile-symbolic",
            tooltip_text="React",
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular", "react-btn"],
            popover=popover,
        )

    def _on_quick_react(self, button: Gtk.Button, msg_id: str) -> None:
        button.get_ancestor(Gtk.Popover).popdown()
        self._toggle_reaction(msg_id, button.get_label())

    def _toggle_reaction(self, msg_id: str, emoji: str) -> None:
        meta = self._message_meta.get(msg_id)
        if meta is None:
            return
        already_mine = self.cfg.device_id in self._reactions.get(msg_id, {}).get(emoji, {})
        payload = {
            "type": "reaction",
            "id": uuid.uuid4().hex,
            "ts": time.time(),
            "from": self.cfg.device_id,
            "nick": self.cfg.nickname,
            "target_id": msg_id,
            "emoji": emoji,
            "op": "remove" if already_mine else "add",
        }
        if meta["is_dm"]:
            self.client.send_dm(meta["peer_device_id"], payload)
            # DMs aren't echoed back to the sender the way broadcasts are —
            # reflect our own reaction locally instead of waiting for one.
            self._handle_reaction(payload)
        else:
            self.client.send_chat(payload)

    def _render_reactions(self, msg_id: str) -> None:
        box = self._reaction_slots.get(msg_id)
        if box is None:
            return
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt
        for emoji, by_device in self._reactions.get(msg_id, {}).items():
            if not by_device:
                continue
            pill = Gtk.Button(
                css_classes=["reaction-pill"] + (["mine"] if self.cfg.device_id in by_device else []),
                valign=Gtk.Align.CENTER,
                tooltip_text=", ".join(sorted(by_device.values())),
                child=Gtk.Label(label=f"{emoji} {len(by_device)}"),
            )
            pill.connect("clicked", lambda _b, e=emoji: self._toggle_reaction(msg_id, e))
            box.append(pill)

    def _play_sparkles(self) -> None:
        if self._sparkle_active:
            return  # a burst is already running — don't stack another on top
        self._sparkle_active = True

        width = max(self.get_width(), 400)
        height = max(self.get_height(), 300)
        particles = []
        for _ in range(_SPARKLE_COUNT):
            label = Gtk.Label(
                label=random.choice(_SPARKLE_GLYPHS),
                halign=Gtk.Align.START,
                valign=Gtk.Align.START,
                can_target=False,
                css_classes=["sparkle-particle"],
                opacity=0.0,
            )
            self.sparkle_overlay.add_overlay(label)
            particles.append(
                {
                    "widget": label,
                    "x": random.uniform(0, width),
                    "y": random.uniform(-height * 0.2, height * 0.9),
                    "vy": random.uniform(20, 55) * (_SPARKLE_STEP_MS / 1000),
                    "drift": random.uniform(-1.5, 1.5),
                    "phase": random.uniform(0, 2 * math.pi),
                }
            )

        started = time.time()

        def step() -> bool:
            elapsed_ms = (time.time() - started) * 1000
            fade_in = min(elapsed_ms / 300, 1.0)
            fade_out = 1.0 - max(0.0, (elapsed_ms - (_SPARKLE_DURATION_MS - 700)) / 700)
            for p in particles:
                p["x"] += p["drift"]
                p["y"] += p["vy"]
                p["widget"].set_margin_start(int(p["x"]))
                p["widget"].set_margin_top(int(p["y"]))
                twinkle = 0.5 + 0.5 * math.sin(elapsed_ms / 120 + p["phase"])
                p["widget"].set_opacity(max(0.0, twinkle * fade_in * fade_out))
            if elapsed_ms >= _SPARKLE_DURATION_MS:
                for p in particles:
                    self.sparkle_overlay.remove_overlay(p["widget"])
                self._sparkle_active = False
                return False
            return True

        GLib.timeout_add(_SPARKLE_STEP_MS, step)

    def _on_emoji_picked(self, _chooser: Gtk.EmojiChooser, emoji: str) -> None:
        pos = self.entry.get_position()
        self.entry.get_buffer().insert_text(pos, emoji, -1)
        self.entry.set_position(pos + len(emoji))
        self.entry.grab_focus()

    def _on_mic_clicked(self, _widget) -> None:
        if self._recorder is None:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        # Recorded directly into downloads_dir (not the system tempdir,
        # which is typically a separate tmpfs mount) so the later rename
        # in _send_voice_message stays on one filesystem — os.rename()
        # across filesystems raises OSError, which used to fail silently.
        downloads_dir = Path(self.cfg.downloads_dir).expanduser()
        downloads_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(tempfile.mkstemp(prefix="omarchy-relay-voice-", suffix=".ogg", dir=downloads_dir)[1])
        try:
            recorder = audio.Recorder(tmp_path)
            recorder.start()
        except audio.RecordingError as exc:
            self.toast_overlay.add_toast(Adw.Toast(title=str(exc)))
            tmp_path.unlink(missing_ok=True)
            return
        recorder.on_error = self._on_recording_error
        self._recorder = recorder
        self._recording_path = tmp_path
        self._recording_started_at = time.time()
        self.mic_btn.set_icon_name("media-playback-stop-symbolic")
        self.mic_btn.add_css_class("recording")
        self.mic_btn.set_tooltip_text("Stop recording and send")
        self.entry.set_visible(False)
        self.recording_indicator.set_visible(True)
        self.recording_time_label.set_label("0:00")
        self._recording_timer_id = GLib.timeout_add(200, self._tick_recording_timer)

    def _tick_recording_timer(self) -> bool:
        if self._recorder is None:
            return False
        self.recording_time_label.set_label(audio.format_duration(time.time() - self._recording_started_at))
        return True

    def _reset_recording_ui(self) -> None:
        self.mic_btn.set_icon_name("audio-input-microphone-symbolic")
        self.mic_btn.remove_css_class("recording")
        self.mic_btn.set_tooltip_text("Record a voice message")
        self.entry.set_visible(True)
        self.recording_indicator.set_visible(False)
        if self._recording_timer_id is not None:
            GLib.source_remove(self._recording_timer_id)
            self._recording_timer_id = None

    def _on_recording_error(self, message: str) -> None:
        # Fires asynchronously if the pipeline fails *after* start()
        # already returned — e.g. a live source that negotiates fine but
        # produces no data. Without this the failure would stay invisible
        # until stop() was clicked, and even then might just look like a
        # too-short recording.
        if self._recorder is None:
            return  # already stopped through the normal path
        self._recorder = None
        self._reset_recording_ui()
        self.toast_overlay.add_toast(Adw.Toast(title=f"Recording failed: {message}"))

    def _stop_recording(self) -> None:
        recorder, path = self._recorder, self._recording_path
        self._recorder = None
        self._reset_recording_ui()

        def work() -> None:
            # EOS finalization can take a moment — off the GTK thread so
            # the window doesn't freeze while the Ogg container flushes.
            duration = recorder.stop()
            GLib.idle_add(self._send_voice_message, path, duration)

        threading.Thread(target=work, daemon=True).start()

    def _send_voice_message(self, tmp_path: Path, duration: float) -> bool:
        # Runs as a GLib.idle_add callback: an uncaught exception anywhere
        # in here doesn't crash the app or print anything a user would
        # see — it just vanishes, which is exactly how a real bug here
        # showed up before (a cross-filesystem rename raising OSError with
        # zero feedback). Catch broadly and always surface something.
        try:
            if duration < 0.3 or tmp_path.stat().st_size == 0:
                self.toast_overlay.add_toast(Adw.Toast(title="Recording too short, not sent"))
                tmp_path.unlink(missing_ok=True)
                return False
            # Renamed to a nicer name (not deleted after send) so the
            # sender has a real, stable path too — file transfers don't
            # echo back to their own sender the way broadcast chat
            # messages do, so without this the sender would never see or
            # be able to replay their own voice message.
            final_path = tmp_path.with_name(f"voice-{int(time.time())}-{uuid.uuid4().hex[:6]}.ogg")
            tmp_path.rename(final_path)
            duration = round(duration, 1)
            send_file(self.client, self.cfg, final_path, to="*", extra_meta={"kind": "voice", "duration": duration})
            self._append_voice({"nick": self.cfg.nickname, "duration": duration}, final_path)
        except Exception as exc:
            self.toast_overlay.add_toast(Adw.Toast(title=f"Couldn't send voice message: {exc}"))
            tmp_path.unlink(missing_ok=True)  # no-op via missing_ok if it was already renamed
        return False

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
            send_file(self.client, self.cfg, path, to="*")
            self._append_system(f"Sending {path.name}…")
        except (FileNotFoundError, ValueError) as exc:
            self._append_system(str(exc))

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
            self._append_system(f"Couldn't paste the image: {exc}")
            return
        if texture is None:
            return
        tmp_path = Path(tempfile.gettempdir()) / f"omarchy-relay-paste-{uuid.uuid4().hex[:8]}.png"
        texture.save_to_png(str(tmp_path))
        try:
            send_file(self.client, self.cfg, tmp_path, to="*")
        except (FileNotFoundError, ValueError) as exc:
            self._append_system(str(exc))
            tmp_path.unlink(missing_ok=True)
            return
        # Our own broadcast files are deliberately not echoed back to us
        # (FileReceiver skips its own sender), so without this we'd never
        # see the image we just pasted in our own chat.
        self._append_image(self.cfg.nickname, tmp_path)
        # _append_image decodes the file into a texture up front, so it's
        # safe to clean up shortly after rather than keep it around.
        GLib.timeout_add_seconds(5, lambda: tmp_path.unlink(missing_ok=True) or False)

    def _on_close_request(self, _window) -> bool:
        self.client.disconnect()
        return False  # allow the window to close

    # -- settings --------------------------------------------------------

    def _on_open_settings(self, _widget) -> None:
        dialog = Adw.Dialog(title="Settings", content_width=460, content_height=640)

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _b: dialog.close())
        save_btn = Gtk.Button(label="Save", css_classes=["suggested-action"])
        header.pack_start(cancel_btn)
        header.pack_end(save_btn)

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

        share_group = Adw.PreferencesGroup(
            title="Share this network",
            description="Network + broker details only — not your nickname or device.",
        )
        export_row = Adw.ActionRow(title="Export to file", subtitle="Save so someone else can import it")
        export_btn = Gtk.Button(icon_name="document-save-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        export_row.add_suffix(export_btn)
        export_row.set_activatable_widget(export_btn)
        share_group.add(export_row)

        import_row = Adw.ActionRow(title="Import from file", subtitle="Fills in Network + Broker below — review, then Save")
        import_btn = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        import_row.add_suffix(import_btn)
        import_row.set_activatable_widget(import_btn)
        share_group.add(import_row)
        page.add(share_group)

        chat_group = Adw.PreferencesGroup(title="Chat")
        presence_row = Adw.SwitchRow(
            title="Show online/offline messages",
            subtitle="Peer list stays accurate either way — this only mutes the log lines",
        )
        presence_row.set_active(self.cfg.show_presence)
        chat_group.add(presence_row)
        history_count_row = Adw.SpinRow.new_with_range(0, 10000, 10)
        history_count_row.set_title("Keep last N messages")
        history_count_row.set_subtitle("Shown again on startup — 0 = don't limit by count")
        history_count_row.set_value(self.cfg.history_retain_count)
        chat_group.add(history_count_row)
        history_days_row = Adw.SpinRow.new_with_range(0, 3650, 1)
        history_days_row.set_title("Keep messages for N days")
        history_days_row.set_subtitle("0 = don't limit by age")
        history_days_row.set_value(self.cfg.history_retain_days)
        chat_group.add(history_days_row)
        page.add(chat_group)

        toasts = Adw.ToastOverlay(child=page)
        toolbar_view = Adw.ToolbarView(content=toasts)
        toolbar_view.add_top_bar(header)
        dialog.set_child(toolbar_view)

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
                history_retain_count=int(history_count_row.get_value()),
                history_retain_days=history_days_row.get_value(),
            )
            if not new_cfg.broker_host or not new_cfg.network_name or not new_cfg.passphrase:
                toasts.add_toast(Adw.Toast(title="Host, network name, and passphrase are required"))
                return
            self.history.prune(new_cfg.network_name, new_cfg.history_retain_count, new_cfg.history_retain_days)
            new_cfg.save()
            dialog.close()
            needs_reconnect = any(
                getattr(new_cfg, field) != getattr(self.cfg, field) for field in _RECONNECT_FIELDS
            )
            if needs_reconnect:
                self._apply_new_config(new_cfg)
            else:
                self.cfg = new_cfg
                self.receiver.cfg = new_cfg
                self.toast_overlay.add_toast(Adw.Toast(title="Settings saved"))

        def on_export(_btn) -> None:
            network_name = network_name_row.get_text().strip()
            passphrase = passphrase_row.get_text()
            broker_host = host_row.get_text().strip()
            if not network_name or not passphrase or not broker_host:
                toasts.add_toast(Adw.Toast(title="Network name, passphrase, and broker host are required to export"))
                return
            text = network_share.build_export_toml(
                network_name,
                passphrase,
                broker_host,
                int(port_row.get_value()),
                tls_row.get_active(),
                username_row.get_text().strip(),
                password_row.get_text(),
            )

            file_dialog = Gtk.FileDialog(initial_name=f"{network_name}-omarchy-relay.toml")

            def on_save_finish(fd: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
                try:
                    gfile = fd.save_finish(result)
                except GLib.Error:
                    return  # cancelled
                if gfile is None:
                    return
                path = Path(gfile.get_path())
                try:
                    path.write_text(text)
                    path.chmod(0o600)
                except OSError as exc:
                    toasts.add_toast(Adw.Toast(title=f"Couldn't save: {exc}"))
                    return
                toasts.add_toast(Adw.Toast(title=f"Exported to {path.name}"))

            # Gtk.FileDialog needs the actual top-level Gtk.Window as parent —
            # `dialog` here is the Adw.Dialog sheet, not a window, so it must
            # be `self` (RelayWindow) or the portal call fails silently.
            file_dialog.save(self, None, on_save_finish)

        def on_import(_btn) -> None:
            file_dialog = Gtk.FileDialog()

            def on_open_finish(fd: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
                try:
                    gfile = fd.open_finish(result)
                except GLib.Error:
                    return  # cancelled
                if gfile is None:
                    return
                path = Path(gfile.get_path())
                try:
                    imported = network_share.parse_export(path.read_text())
                except (OSError, ValueError) as exc:
                    toasts.add_toast(Adw.Toast(title=f"Couldn't import: {exc}"))
                    return
                network_name_row.set_text(imported["network_name"])
                passphrase_row.set_text(imported["passphrase"])
                host_row.set_text(imported["broker_host"])
                port_row.set_value(imported["broker_port"])
                tls_row.set_active(imported["broker_tls"])
                username_row.set_text(imported["broker_username"])
                password_row.set_text(imported["broker_password"])
                toasts.add_toast(Adw.Toast(title="Imported — review below, then Save"))

            file_dialog.open(self, None, on_open_finish)

        export_btn.connect("clicked", on_export)
        import_btn.connect("clicked", on_import)
        save_btn.connect("clicked", on_save)
        dialog.present(self)

    def _apply_new_config(self, new_cfg: Config) -> None:
        self.client.disconnect()

        self.cfg = new_cfg
        self.peers = PeerDirectory()
        self.receiver = FileReceiver(new_cfg, on_complete=self._on_file_complete, on_error=self._on_file_error)
        self.client = RelayClient(new_cfg)
        self._wire_client_callbacks()

        self._connection_state = "connecting"
        self.banner.set_revealed(False)
        self.title_widget.set_title(new_cfg.network_name)
        self.content_page.set_title(new_cfg.network_name)
        self.profile_avatar.set_text(new_cfg.nickname)
        self.profile_name.set_label(new_cfg.nickname)
        self.entry.set_placeholder_text(self._composer_placeholder())
        self._refresh_peer_list()
        self._append_system("Settings updated — reconnecting…")
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
