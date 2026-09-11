"""Native chat window built with GTK4 + libadwaita.

Like the Textual TUI, this is a presentation layer over the same
RelayClient/FileReceiver used everywhere else — the transport logic lives
in mqttclient.py/chat.py/transfer.py, this module only renders it.
RelayClient callbacks fire on paho's own network thread, so every UI
update is marshalled onto GTK's main loop via GLib.idle_add.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import math
import os
import random
import re
import shutil
import signal
import socket
import subprocess
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
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

from . import audio, history, network_share, release_notes
from .chat import _ding, _notify
from .config import Config
from .mqttclient import RelayClient
from .presence import PeerDirectory
from .remote_actions import RemoteActionHandler, run_action
from .transfer import FileReceiver, send_file

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_AUDIO_EXTENSIONS = {".ogg", ".opus", ".oga"}
# History kinds for file bubbles, kept by where the file is saved (see _remember_file).
_FILE_KINDS = ("image", "voice", "file")
_CLIPBOARD_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/bmp", "image/gif", "image/tiff", "image/webp")

# Consecutive messages from the same sender within this many seconds render
# as one group: a single avatar and name, tighter spacing, joined corners.
_GROUP_WINDOW_SECONDS = 5 * 60
_IMAGE_MAX_WIDTH = 320
_IMAGE_MAX_HEIGHT = 260
_NICK_COLORS = 6
_CONTENT_MAX_WIDTH = 860

# Typing status: while the composer has text and keeps changing, a "typing"
# event goes out at most this often; a peer's indicator lapses if no refresh
# arrives in time (which also covers a lost "stopped").
_TYPING_SEND_INTERVAL = 3.0
_TYPING_EXPIRE_SECONDS = 6

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

/* Replies: the quote at the top of a reply, the bar above the composer
   while writing one, and the flash on a message a quote jumps to. */
.reply-quote {
  min-height: 0;
  padding: 3px 8px;
  border-radius: 8px;
  border-left: 3px solid var(--accent-color);
  background-color: color-mix(in srgb, currentColor 8%, transparent);
}
.bubble.mine .reply-quote {
  border-left-color: var(--accent-fg-color);
}
.reply-quote .reply-nick {
  font-weight: bold;
  font-size: 0.85em;
}
.reply-quote .reply-text {
  font-size: 0.9em;
  opacity: 0.85;
}
.reply-bar {
  margin: 6px 10px 0 10px;
  padding: 4px 4px 4px 10px;
  border-radius: 10px;
  border-left: 3px solid var(--accent-color);
  background-color: color-mix(in srgb, var(--card-bg-color), var(--card-fg-color) 6%);
}
.bubble {
  transition: box-shadow 500ms ease-out;
}
.bubble.flash {
  box-shadow: 0 0 0 3px var(--accent-color);
}

/* /ascii: a drawing keeps its spacing, in a box that scrolls sideways when
   it's wider than the chat. */
.ascii-art {
  font-family: monospace;
  font-size: 0.95em;
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
.typing {
  color: var(--accent-color);
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

/* Typing indicator: three dots taking turns to hop. */
@keyframes relay-typing-bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
  30% { transform: translateY(-5px); opacity: 1; }
}
.typing-dots {
  margin-top: 5px;
}
.typing-dot {
  min-width: 7px;
  min-height: 7px;
  border-radius: 9999px;
  background-color: currentColor;
  animation: relay-typing-bounce 1.2s ease-in-out infinite;
}
.typing-dots.small .typing-dot {
  min-width: 5px;
  min-height: 5px;
}
.typing-dot.dot-2 { animation-delay: 0.15s; }
.typing-dot.dot-3 { animation-delay: 0.3s; }
.bubble.typing-bubble {
  padding: 9px 14px 11px 14px;
}

/* Treats (/coffee, /cocktail, /dancer): the card in the chat, and the
   animation that pops up over the window when one is sent or received. */
.treat-card .treat-emoji {
  font-size: 2.4em;
}
/* Every keyframe of an animation spells out the same transform list (e.g.
   translateY() scale()): GTK falls back to matrix interpolation between
   mismatched transform lists, and a color emoji drawn through that renders
   as a solid pink square. */
@keyframes relay-coffee-pop {
  0% { transform: translateY(0) scale(0.2); opacity: 0; }
  14% { transform: translateY(0) scale(1.15); opacity: 1; }
  24% { transform: translateY(0) scale(1); opacity: 1; }
  78% { transform: translateY(0) scale(1); opacity: 1; }
  100% { transform: translateY(-30px) scale(1); opacity: 0; }
}
@keyframes relay-cocktail-clink {
  0% { transform: translateY(0) scale(0.2) rotate(0deg); opacity: 0; }
  14% { transform: translateY(0) scale(1.15) rotate(0deg); opacity: 1; }
  24% { transform: translateY(0) scale(1) rotate(-16deg); opacity: 1; }
  34% { transform: translateY(0) scale(1) rotate(12deg); opacity: 1; }
  44% { transform: translateY(0) scale(1) rotate(-7deg); opacity: 1; }
  54% { transform: translateY(0) scale(1) rotate(0deg); opacity: 1; }
  78% { transform: translateY(0) scale(1) rotate(0deg); opacity: 1; }
  100% { transform: translateY(-30px) scale(1) rotate(0deg); opacity: 0; }
}
@keyframes relay-dancer-dance {
  0% { transform: translate(0, 0) scale(0.2) rotate(0deg); opacity: 0; }
  12% { transform: translate(0, 0) scale(1.1) rotate(0deg); opacity: 1; }
  22% { transform: translate(-28px, -16px) scale(1) rotate(-12deg); opacity: 1; }
  32% { transform: translate(0, 0) scale(1) rotate(0deg); opacity: 1; }
  42% { transform: translate(28px, -16px) scale(1) rotate(12deg); opacity: 1; }
  52% { transform: translate(0, 0) scale(1) rotate(0deg); opacity: 1; }
  62% { transform: translate(-28px, -16px) scale(1) rotate(-12deg); opacity: 1; }
  72% { transform: translate(0, 0) scale(1) rotate(0deg); opacity: 1; }
  82% { transform: translate(0, 0) scale(1) rotate(0deg); opacity: 1; }
  100% { transform: translate(0, -30px) scale(1) rotate(0deg); opacity: 0; }
}
@keyframes relay-treat-rise {
  0% { transform: translateY(12px) scale(0.6); opacity: 0; }
  35% { opacity: 0.75; }
  100% { transform: translateY(-70px) scale(1.5); opacity: 0; }
}
.treat-burst {
  font-size: 6em;
}
.coffee-burst { animation: relay-coffee-pop 2.8s ease-out both; }
.cocktail-burst { animation: relay-cocktail-clink 2.8s ease-out both; }
.dancer-burst { animation: relay-dancer-dance 2.8s ease-in-out both; }
.treat-rise {
  font-size: 2.2em;
  font-weight: bold;
  opacity: 0;
  animation: relay-treat-rise 2.2s ease-out both;
}
.treat-rise.rise-1 { animation-delay: 0.35s; }
.treat-rise.rise-2 { animation-delay: 0.6s; }
.treat-rise.rise-3 { animation-delay: 0.85s; }
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


# WhatsApp-style doodle wallpaper behind the chat log: a tile of little line
# drawings, repeated. Each path is drawn around (0, 0) in a ~24px box. The
# tile is inked in the theme's own foreground color at a low opacity, so it
# reads as texture rather than content and follows theme switches too.
_WALLPAPER_TILE = 280
_WALLPAPER_DOODLES = (
    # chat bubble
    "M-10 -7h20a3 3 0 0 1 3 3v9a3 3 0 0 1 -3 3h-11l-6 5v-5h-3a3 3 0 0 1 -3 -3v-9a3 3 0 0 1 3 -3z"
    "M-5 .5h.01M0 .5h.01M5 .5h.01",
    # heart
    "M0 9C-9 3 -12 -2 -10 -6C-8 -10 -3 -10 0 -5C3 -10 8 -10 10 -6C12 -2 9 3 0 9z",
    # star
    "M0 -10L2.5 -3.4L9.5 -3.1L4 1.3L5.9 8.1L0 4.2L-5.9 8.1L-4 1.3L-9.5 -3.1L-2.5 -3.4z",
    # paper plane
    "M-11 -1L11 -9L4 10L0 2zM0 2L11 -9",
    # music note
    "M-3 7V-9L8 -11V4M-3 7A3 2.5 0 1 1 -9 7A3 2.5 0 1 1 -3 7M8 4A3 2.5 0 1 1 2 4A3 2.5 0 1 1 8 4",
    # camera
    "M-10 -5h5l2 -3h6l2 3h5a2 2 0 0 1 2 2v11a2 2 0 0 1 -2 2h-20a2 2 0 0 1 -2 -2v-11a2 2 0 0 1 2 -2z"
    "M4 2.5a4 4 0 1 1 -8 0a4 4 0 1 1 8 0",
    # cloud
    "M-9 6H9A4 4 0 0 0 9 -2A6 6 0 0 0 -2 -5A6.5 6.5 0 0 0 -9 6z",
    # smiley
    "M10 0a10 10 0 1 1 -20 0a10 10 0 1 1 20 0M-3.5 -3h.01M3.5 -3h.01M-5 3q5 5 10 0",
    # envelope
    "M-11 -7h22v14h-22zM-11 -7L0 2L11 -7",
    # lightning bolt
    "M2 -11L-7 2H0L-2 11L7 -2H0z",
    # coffee cup
    "M-8 -3h13v7a6 6 0 0 1 -6 6h-1a6 6 0 0 1 -6 -6zM5 -1h2a3 3 0 0 1 0 6h-2M-4 -10q2 2 0 4M1 -10q2 2 0 4",
    # crescent moon
    "M4 -10A10 10 0 1 0 10 5A8 8 0 0 1 4 -10z",
    # microphone
    "M0 -11a3.5 3.5 0 0 1 3.5 3.5v6a3.5 3.5 0 0 1 -7 0v-6a3.5 3.5 0 0 1 3.5 -3.5zM-7 -2a7 7 0 0 0 14 0M0 5v5M-4 10h8",
    # signal waves
    "M-9 -2A13 13 0 0 1 9 -2M-5.5 2A8 8 0 0 1 5.5 2M-2 6A3 3 0 0 1 2 6M0 10h.01",
    # sparkle
    "M0 -10Q1 -1 10 0Q1 1 0 10Q-1 1 -10 0Q-1 -1 0 -10z",
    # terminal prompt
    "M-11 -8h22v16h-22zM-7 -3l4 3l-4 3M0 4h6",
)


def _wallpaper_css(ink: str, dark: bool) -> str:
    rng = random.Random(7)  # fixed seed: the same scattering on every launch
    cell = _WALLPAPER_TILE // 4
    marks = []
    for index, doodle in enumerate(_WALLPAPER_DOODLES):
        row, col = divmod(index, 4)
        x = cell * (col + 0.5) + rng.uniform(-8, 8)
        y = cell * (row + 0.5) + rng.uniform(-8, 8)
        angle = rng.uniform(-30, 30)
        marks.append(f'<path transform="translate({x:.1f} {y:.1f}) rotate({angle:.0f}) scale(1.1)" d="{doodle}"/>')
        # A speck in the gap below-right of each doodle, so there are no
        # obvious empty lanes between the rows and columns.
        speck = "h.01" if index % 2 else "m-3 0h6m-3 -3v6"
        marks.append(f'<path d="M{cell * (col + 1) - 5} {cell * (row + 1) - 5}{speck}"/>')
    # Rasterized at 2x and scaled back down by background-size, so it stays
    # crisp on HiDPI screens.
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WALLPAPER_TILE * 2}" height="{_WALLPAPER_TILE * 2}"'
        f' viewBox="0 0 {_WALLPAPER_TILE} {_WALLPAPER_TILE}">'
        f'<g fill="none" stroke="{ink}" stroke-opacity="{0.07 if dark else 0.08}" stroke-width="1.5"'
        f' stroke-linecap="round" stroke-linejoin="round">{"".join(marks)}</g></svg>'
    )
    data = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return (
        ".chat-wallpaper {\n"
        f'  background-image: url("data:image/svg+xml;base64,{data}");\n'
        f"  background-size: {_WALLPAPER_TILE}px {_WALLPAPER_TILE}px;\n"
        "}\n"
    )


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

# /coffee, /cocktail, /dancer: how long the animation stays up over the window.
_TREAT_BURST_MS = 2900

# Replies quote the start of the message they answer: a preview, not a copy.
_REPLY_PREVIEW_CHARS = 200


@dataclasses.dataclass(frozen=True)
class _Treat:
    """Something to send: /<kind> for everyone, /<kind> <nickname> [note] for
    one person. `kind` is also the chat message's "type" and the history
    row's kind, so it never changes once shipped."""

    kind: str
    emoji: str
    noun: str  # the card's "a coffee"
    gift: str  # notifications' and older clients' "a cup of coffee"
    everyone_verb: str  # "bought" everyone a coffee
    rising: str  # one glyph per floating wisp over the animation
    self_toast: str

    def phrase(self, *, to_one: bool) -> str:
        """For notifications and older clients: "sent you a cup of coffee"."""
        return f"sent you {self.gift}" if to_one else f"{self.everyone_verb} everyone {self.gift}"

    def headline(self, *, is_mine: bool, to_one: bool, to_nick: str) -> str:
        """The card's heading: "You bought everyone a coffee", "Sent you a coffee"."""
        if is_mine:
            if to_one:
                return f"You sent {to_nick or 'them'} a {self.noun}"
            return f"You {self.everyone_verb} everyone a {self.noun}"
        return f"Sent you a {self.noun}" if to_one else f"{self.everyone_verb.capitalize()} everyone a {self.noun}"


_TREATS = {
    treat.kind: treat
    for treat in (
        _Treat("coffee", "☕", "coffee", "a cup of coffee", "bought", "∿∿∿", "That one you'll have to make yourself ☕"),
        _Treat("cocktail", "🍸", "cocktail", "a cocktail", "bought", "∘°∘", "That one you'll have to mix yourself 🍸"),
        _Treat("dancer", "💃", "dancer", "a dancer", "sent", "♪♫♪", "You'll have to dance for yourself 💃"),
    )
}

# The composer's inline syntax hints. If Enter is pressed while one is still
# showing, its placeholders arrive as literal text and are dropped.
_COMMAND_HINTS = ("/action <nickname> <command-name>", "/ascii <drawing>") + tuple(
    f"/{kind} <nickname> <note>" for kind in _TREATS
)
_HINT_PLACEHOLDER = re.compile(r"\s*<(?:nickname|command-name|note|drawing)>")


def _reply_ref(obj: dict) -> Optional[dict]:
    """A message's "reply_to" ({"id", "nick", "text"}), or None when it has
    none. It comes from other clients, so anything malformed is dropped and
    the quoted text is capped."""
    ref = obj.get("reply_to")
    if not isinstance(ref, dict) or not ref.get("id"):
        return None
    return {
        "id": str(ref["id"]),
        "nick": str(ref.get("nick") or "?"),
        "text": str(ref.get("text") or "")[:_REPLY_PREVIEW_CHARS],
    }


def _ascii_drawing(raw: str) -> str:
    """What follows "/ascii", spacing intact: only the space after the
    command, blank lines around the drawing, and trailing spaces go. An
    unfilled "<drawing>" hint counts as no drawing."""
    body = raw.replace("\r\n", "\n").lstrip()[len("/ascii") :]
    if body[:1] == " ":
        body = body[1:]
    lines = [line.rstrip() for line in body.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    drawing = "\n".join(lines)
    return "" if drawing.strip() == "<drawing>" else drawing


def _text_extra(reply: Optional[dict], ascii_art: bool) -> Optional[dict]:
    """History's extra for a text message: only what sets it apart from plain text."""
    extra = {}
    if reply:
        extra["reply_to"] = reply
    if ascii_art:
        extra["format"] = "ascii"
    return extra or None


def _is_emoji_only(text: str, max_len: int = 12) -> bool:
    """A short WhatsApp/iMessage-style check: render standalone emoji (with
    no other text) larger in the bubble. max_len caps it so someone can't
    get a giant wall of emoji."""
    stripped = text.strip()
    return bool(stripped) and len(stripped) <= max_len and bool(_EMOJI_ONLY_RE.match(stripped))


def _typing_dots(small: bool = False) -> Gtk.Box:
    """Three bouncing dots, drawn in the surrounding text color."""
    box = Gtk.Box(spacing=3 if small else 4, valign=Gtk.Align.CENTER, css_classes=["typing-dots"])
    if small:
        box.add_css_class("small")
    for n in (1, 2, 3):
        box.append(Gtk.Box(valign=Gtk.Align.CENTER, css_classes=["typing-dot", f"dot-{n}"]))
    return box


def _fmt_release_date(date: str) -> str:
    try:
        return time.strftime("%B %-d, %Y", time.strptime(date, "%Y-%m-%d"))
    except ValueError:
        return date


class RelayWindow(Adw.ApplicationWindow):
    def __init__(self, app: "RelayApp", cfg: Config):
        # Closing only hides the window: the connection stays up so you stay
        # online and keep getting notifications. RelayApp's Quit disconnects.
        super().__init__(application=app, title="Omarchy Relay", hide_on_close=True)
        self.cfg = cfg
        self.peers = PeerDirectory()
        if cfg.lock_window_size:
            self.set_default_size(cfg.window_width, cfg.window_height)
        else:
            self.set_default_size(900, 640)
        self.set_size_request(360, 420)

        self.client = RelayClient(cfg)
        self.receiver = FileReceiver(cfg, on_complete=self._on_file_complete, on_error=self._on_file_error)
        # Services incoming remote-action requests directly on this same
        # connection — no separate `daemon` process. Running `daemon`
        # alongside an already-open `gui` under the same identity would
        # connect a second MQTT client with the same client_id (=
        # device_id), and the broker disconnects whichever held it first —
        # a flapping loop, not redundancy. One connection does everything.
        self.action_handler = RemoteActionHandler(cfg, on_handled=self._threaded(self._on_action_handled))

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
        self._message_meta: dict[str, dict] = {}  # msg_id -> {"is_dm", "peer_device_id", "nick", "preview"}
        self._message_bubbles: dict[str, Gtk.Widget] = {}  # msg_id -> its bubble, for replies to jump to
        self._reaction_slots: dict[str, Gtk.Box] = {}  # msg_id -> its reaction-pills row
        self._reactions: dict[str, dict[str, dict[str, str]]] = {}  # msg_id -> emoji -> device_id -> nick
        self._sparkle_active = False
        self._recorder: Optional[audio.Recorder] = None
        self._recording_path: Optional[Path] = None
        self._recording_started_at: float = 0.0
        self._recording_timer_id: Optional[int] = None
        self._voice_players: list[dict] = []  # each: {"player": audio.Player | None, "reset": callable}
        self._typing_sent_at = 0.0  # monotonic time we last told peers we're typing; 0 = we aren't
        self._typing_peers: dict[str, tuple[str, int]] = {}  # device_id -> (nick, expiry GLib source id)
        self._treat_active = False
        self._reply_to: Optional[dict] = None  # the message being replied to: id plus its _message_meta
        self._hyprland_lock = threading.Lock()
        self._hyprland_floated = False  # the window lock floated the window, so unlocking tiles it again
        self._hyprland_watch: Optional[socket.socket] = None  # Hyprland's event socket, followed while locked

        whats_new = Gio.SimpleAction.new("whats-new", None)
        whats_new.connect("activate", lambda *_: self._show_whats_new())
        self.add_action(whats_new)
        self.connect("map", lambda *_: self._apply_window_lock())
        self.connect("notify::maximized", self._on_window_state_changed)
        self.connect("notify::fullscreened", self._on_window_state_changed)

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

        threading.Thread(target=self._connect_worker, daemon=True).start()

    # -- layout --------------------------------------------------------------

    def _build_sidebar(self) -> Adw.NavigationPage:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Omarchy Relay"))
        menu = Gio.Menu()
        menu.append("_What's New", "win.whats-new")
        menu.append("_Quit", "app.quit")
        header.pack_end(
            Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Main Menu", menu_model=menu, primary=True)
        )

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
        self.snip_btn = Gtk.Button(
            icon_name="applets-screenshooter-symbolic", tooltip_text="Snip part of your screen and send it"
        )
        self.snip_btn.connect("clicked", self._on_snip_clicked)
        header.pack_end(self.snip_btn)

        self.banner = Adw.Banner(title="Couldn't reach the broker", button_label="Settings")
        self.banner.connect("button-clicked", self._on_open_settings)

        empty_page = Adw.StatusPage(
            icon_name="user-available-symbolic",
            title="No messages yet",
            description="Messages on this network show up here.\nPaste or snip an image, or attach a file to share it.",
        )
        self.chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=8)
        # The typing row sits below the messages rather than among them, so
        # new messages and message grouping never have to step around it.
        chat_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_bottom=8)
        chat_column.append(self.chat_box)
        chat_column.append(self._build_typing_row())
        clamp = Adw.Clamp(maximum_size=_CONTENT_MAX_WIDTH, tightening_threshold=600, child=chat_column)
        self._chat_content = clamp  # the scrolled content, whose coordinates the scrollbar uses
        self.chat_scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp)
        adjustment = self.chat_scroller.get_vadjustment()
        adjustment.connect("changed", self._on_chat_resized)
        adjustment.connect("notify::upper", lambda adj, _pspec: self._on_chat_resized(adj))
        adjustment.connect("notify::page-size", lambda adj, _pspec: self._on_chat_resized(adj))
        adjustment.connect("value-changed", self._on_chat_scrolled)

        self.chat_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, css_classes=["chat-wallpaper"])
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

        # Ghost/inline type-ahead: as soon as what's typed prefix-matches
        # this one candidate string, GTK shows the rest of it inline as
        # selected text — typing further characters that match extends the
        # selection, anything else clears it. No popup (popup_completion
        # off), just the inline suffix.
        action_hint = Gtk.EntryCompletion()
        hint_model = Gtk.ListStore(str)
        for hint in _COMMAND_HINTS:
            hint_model.append([hint])
        action_hint.set_model(hint_model)
        action_hint.set_text_column(0)
        action_hint.set_inline_completion(True)
        action_hint.set_popup_completion(False)
        self.entry.set_completion(action_hint)
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
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        column.append(self._build_reply_bar())
        column.append(composer)
        return Adw.Clamp(maximum_size=_CONTENT_MAX_WIDTH, tightening_threshold=600, child=column)

    def _build_reply_bar(self) -> Gtk.Widget:
        self.reply_title = Gtk.Label(xalign=0, css_classes=["caption-heading"])
        self.reply_preview = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["caption", "dim-label"])
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        labels.append(self.reply_title)
        labels.append(self.reply_preview)
        cancel_btn = Gtk.Button(
            icon_name="window-close-symbolic",
            tooltip_text="Cancel reply (Esc)",
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular"],
        )
        cancel_btn.connect("clicked", lambda _b: self._cancel_reply())
        bar = Gtk.Box(spacing=6, css_classes=["reply-bar"])
        bar.append(labels)
        bar.append(cancel_btn)
        self.reply_revealer = Gtk.Revealer(
            child=bar, transition_type=Gtk.RevealerTransitionType.SLIDE_UP, transition_duration=150
        )
        return self.reply_revealer

    def _build_typing_row(self) -> Gtk.Widget:
        self.typing_avatar = Adw.Avatar(size=32, show_initials=True, valign=Gtk.Align.END)
        bubble = Gtk.Box(css_classes=["bubble", "theirs", "typing-bubble"], valign=Gtk.Align.END)
        bubble.append(_typing_dots())
        self.typing_names = Gtk.Label(xalign=0, valign=Gtk.Align.CENTER, css_classes=["caption", "dim-label"])
        row = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=10)
        row.append(self.typing_avatar)
        row.append(bubble)
        row.append(self.typing_names)
        self.typing_revealer = Gtk.Revealer(
            child=row, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, transition_duration=150
        )
        return self.typing_revealer

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
        colors = _omarchy_theme_colors()
        theme_css = _omarchy_theme_css(colors, dark)
        # A non-empty theme_css means the Omarchy palette validated and applies.
        ink = colors["foreground"] if theme_css else ("#ffffff" if dark else "#000000")
        self._css_provider.load_from_string(_BASE_CSS + theme_css + _wallpaper_css(ink, dark))

    def _wire_client_callbacks(self) -> None:
        self.client.on_chat = self._threaded(self._handle_chat)
        self.client.on_typing = self._threaded(self._handle_typing)
        self.client.on_dm = self._threaded(self._handle_dm)
        self.client.on_presence = self._threaded(self._handle_presence)
        self.client.on_file_meta = self._threaded(self.receiver.handle_meta)
        self.client.on_file_chunk = self._threaded(self.receiver.handle_chunk)
        # Not wrapped in _threaded: handle_request runs subprocess.run()
        # (up to 20s) and a client.publish() — neither touches GTK, and
        # wrapping it would block the whole window for however long the
        # command takes. Only the on_handled callback above (which does
        # touch a widget) needs the main-loop marshalling.
        self.client.on_action_request = lambda obj: self.action_handler.handle_request(self.client, obj)
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
        subtitle = f"{others} online" if self._connection_state == "connected" else state
        self.title_widget.set_subtitle(self._typing_summary() or subtitle)
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
        preview: str = "",
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
            self._message_meta[msg_id] = {
                "is_dm": is_dm,
                "peer_device_id": dm_peer_device_id,
                "nick": nick,
                "preview": preview,
            }
            self._message_bubbles[msg_id] = bubble
            reactions_row = Gtk.Box(spacing=4, css_classes=["reactions-row"])
            self._reaction_slots[msg_id] = reactions_row
            footer = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
            footer.append(reactions_row)
            footer.append(self._build_react_button(msg_id))
            footer.append(self._build_reply_button(msg_id))
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
        reply_to: Optional[dict] = None,
        ascii_art: bool = False,
    ) -> None:
        if ascii_art:
            art = Gtk.Label(label=text, xalign=0, selectable=True, css_classes=["ascii-art"])
            # A drawing never wraps, so one wider than the chat scrolls sideways
            # rather than stretching the whole window.
            body = Gtk.ScrolledWindow(
                child=art,
                hexpand=True,
                vscrollbar_policy=Gtk.PolicyType.NEVER,
                propagate_natural_width=True,
                propagate_natural_height=True,
            )
        else:
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
        line = Gtk.Box(spacing=10, css_classes=[] if reply_to else ["bubble"])
        line.append(body)
        line.append(stamp)
        bubble = line
        if reply_to:
            bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["bubble"])
            bubble.append(self._build_reply_quote(reply_to))
            bubble.append(line)
        self._append_bubble(
            bubble,
            nick=nick,
            ts=ts,
            is_mine=is_mine,
            is_dm=is_dm,
            msg_id=msg_id,
            dm_peer_device_id=dm_peer_device_id,
            preview=text,
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

    def _append_file_received(
        self, meta: dict, path: Path, *, ts: Optional[float] = None, is_mine: Optional[bool] = None, missing: bool = False
    ) -> None:
        ts = time.time() if ts is None else ts
        icon = Gtk.Image(icon_name="text-x-generic-symbolic", pixel_size=20, css_classes=["file-icon"])
        name = Gtk.Label(
            label=meta["filename"],
            xalign=0,
            ellipsize=Pango.EllipsizeMode.MIDDLE,
            max_width_chars=32,
            css_classes=["heading"],
        )
        if missing:
            details = ["No longer on this device"]
        else:
            details = [GLib.format_size(meta["size"])] if meta.get("size") is not None else []
        details.append(_fmt_time(ts))
        info_line = Gtk.Label(label=" · ".join(details), xalign=0, css_classes=["caption", "stamp"])
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        info.append(name)
        info.append(info_line)
        bubble = Gtk.Box(spacing=10, css_classes=["bubble"])
        bubble.append(icon)
        bubble.append(info)
        if not missing:
            open_btn = Gtk.Button(
                icon_name="folder-open-symbolic",
                tooltip_text="Show in folder",
                valign=Gtk.Align.CENTER,
                css_classes=["flat", "circular"],
            )
            open_btn.connect("clicked", lambda _b: self._open_containing_folder(path))
            bubble.append(open_btn)
        if is_mine is None:
            is_mine = meta.get("nick") == self.cfg.nickname
        self._append_bubble(bubble, nick=meta["nick"], ts=ts, is_mine=is_mine)

    def _open_containing_folder(self, path: Path) -> None:
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).open_containing_folder(self, None, None)

    def _append_voice(
        self, meta: dict, path: Path, *, ts: Optional[float] = None, is_mine: Optional[bool] = None
    ) -> None:
        ts = time.time() if ts is None else ts
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
        if is_mine is None:
            is_mine = meta.get("nick") == self.cfg.nickname
        self._append_bubble(bubble, nick=meta["nick"], ts=ts, is_mine=is_mine)

    def _stop_other_voice_players(self, exclude: dict) -> None:
        for state in self._voice_players:
            if state is exclude:
                continue
            player = state.get("player")
            if player is not None and player.playing:
                player.stop()
                state["reset"]()

    def _append_image(
        self, nick: str, path: Path, *, ts: Optional[float] = None, is_mine: Optional[bool] = None
    ) -> None:
        ts = time.time() if ts is None else ts
        if is_mine is None:
            is_mine = nick == self.cfg.nickname
        try:
            texture = Gdk.Texture.new_from_filename(str(path))
        except GLib.Error:
            self._append_file_received({"nick": nick, "filename": path.name}, path, ts=ts, is_mine=is_mine)
            return
        scale = min(1.0, _IMAGE_MAX_WIDTH / texture.get_width(), _IMAGE_MAX_HEIGHT / texture.get_height())
        picture = Gtk.Picture(
            paintable=texture, content_fit=Gtk.ContentFit.COVER, can_shrink=True, tooltip_text="Double-click to expand"
        )
        picture.set_size_request(max(1, round(texture.get_width() * scale)), max(1, round(texture.get_height() * scale)))
        expand = Gtk.GestureClick()
        expand.connect("pressed", self._on_image_pressed, texture, "Your image" if is_mine else f"Image from {nick}")
        picture.add_controller(expand)
        bubble = Gtk.Overlay(child=picture, overflow=Gtk.Overflow.HIDDEN, css_classes=["bubble", "media"])
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
        self._append_bubble(bubble, nick=nick, ts=ts, is_mine=is_mine)

    def _on_image_pressed(self, _gesture, n_press: int, _x: float, _y: float, texture: Gdk.Texture, title: str) -> None:
        if n_press == 2:
            ImageViewerWindow(self, texture, title).present()

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
            is_me = device_id == self.cfg.device_id
            name = Gtk.Label(
                label=f"{nick} (you)" if is_me else nick,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            if device_id in self._typing_peers:
                device = _typing_dots(small=True)
                device.set_halign(Gtk.Align.START)
                device.set_tooltip_text("typing…")
                device.add_css_class("typing")
            else:
                device = Gtk.Label(
                    label=device_id, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, css_classes=["caption", "dim-label"]
                )
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
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
        if obj.get("type") in _TREATS:
            self._handle_treat(_TREATS[obj["type"]], obj, is_dm=False)
            return
        self._clear_typing(obj.get("from", ""))
        is_mine = obj.get("from") == self.cfg.device_id
        reply, ascii_art = _reply_ref(obj), obj.get("format") == "ascii"
        self._append_text(
            obj["nick"], obj["ts"], obj["text"], msg_id=obj.get("id"), is_mine=is_mine, reply_to=reply, ascii_art=ascii_art
        )
        self._remember(obj, is_dm=False, peer_device_id=None, extra=_text_extra(reply, ascii_art))
        # Broadcasts echo back to the sender too (we're subscribed to our
        # own publish topic) — don't notify ourselves for our own messages.
        if not is_mine:
            _notify(obj["nick"], obj["text"])
            _ding()

    def _handle_dm(self, obj: dict) -> None:
        if obj.get("type") == "reaction":
            self._handle_reaction(obj)
            return
        if obj.get("type") in _TREATS:
            self._handle_treat(_TREATS[obj["type"]], obj, is_dm=True)
            return
        reply, ascii_art = _reply_ref(obj), obj.get("format") == "ascii"
        self._append_text(
            obj["nick"],
            obj["ts"],
            obj["text"],
            msg_id=obj.get("id"),
            is_dm=True,
            dm_peer_device_id=obj.get("from"),
            reply_to=reply,
            ascii_art=ascii_art,
        )
        self._remember(obj, is_dm=True, peer_device_id=obj.get("from"), extra=_text_extra(reply, ascii_art))
        _notify(f"DM from {obj['nick']}", obj["text"])
        _ding()

    def _remember(
        self,
        obj: dict,
        *,
        is_dm: bool,
        peer_device_id: Optional[str],
        kind: str = "text",
        extra: Optional[dict] = None,
    ) -> None:
        if not obj.get("id"):
            return  # nothing stable to key on — skip rather than store an unfindable row
        self.history.add_message(
            self.cfg.network_name,
            obj["id"],
            obj["ts"],
            obj["from"],
            obj["nick"],
            obj["text"],
            is_dm,
            peer_device_id,
            kind=kind,
            extra=extra,
        )
        self.history.prune(self.cfg.network_name, self.cfg.history_retain_count, self.cfg.history_retain_days)

    def _remember_file(self, kind: str, transfer_id: str, meta: dict, path: Path) -> None:
        """Keeps a file bubble (one of _FILE_KINDS) in history by where the file
        is saved: received files, and our own sent images and voice messages,
        all stay in downloads_dir."""
        is_dm = meta.get("to", "*") != "*"
        obj = {
            "id": transfer_id,
            "ts": meta.get("ts") or time.time(),
            "from": meta["from"],
            "nick": meta["nick"],
            "text": path.name,
        }
        extra = {"path": str(path), "filename": meta.get("filename", path.name), "size": meta.get("size")}
        if meta.get("duration") is not None:
            extra["duration"] = meta["duration"]
        self._remember(obj, is_dm=is_dm, peer_device_id=meta["from"] if is_dm else None, kind=kind, extra=extra)

    def _remember_sent_file(self, kind: str, transfer_id: str, path: Path, **meta) -> None:
        # Our own sends never come back through FileReceiver, so there's no
        # received meta to keep: describe the file the same way instead.
        own = {"from": self.cfg.device_id, "nick": self.cfg.nickname, "to": "*", "ts": time.time()}
        own.update(filename=path.name, size=path.stat().st_size, **meta)
        self._remember_file(kind, transfer_id, own, path)

    def _load_history(self) -> None:
        limit = self.cfg.history_retain_count or None
        messages = self.history.recent_messages(self.cfg.network_name, limit=limit)
        if not messages:
            return
        for m in messages:
            is_mine = m["from_device"] == self.cfg.device_id
            if m["kind"] in _FILE_KINDS:
                self._append_history_file(m, is_mine=is_mine)
                continue
            if m["kind"] in _TREATS:
                self._append_treat(
                    _TREATS[m["kind"]],
                    m["nick"],
                    m["ts"],
                    to_nick=m["extra"].get("to_nick", ""),
                    note=m["extra"].get("note", ""),
                    msg_id=m["id"],
                    is_mine=is_mine,
                    is_dm=m["is_dm"],
                    dm_peer_device_id=m["peer_device_id"],
                )
                continue
            self._append_text(
                m["nick"],
                m["ts"],
                m["text"],
                msg_id=m["id"],
                is_mine=is_mine,
                is_dm=m["is_dm"],
                dm_peer_device_id=m["peer_device_id"],
                is_history=True,
                reply_to=_reply_ref(m["extra"]),
                ascii_art=m["extra"].get("format") == "ascii",
            )
        count = len(messages)
        self._append_system(f"{count} earlier message{'s' if count != 1 else ''} loaded")

    def _append_history_file(self, m: dict, *, is_mine: bool) -> None:
        extra = m["extra"]
        meta = {
            "nick": m["nick"],
            "filename": extra.get("filename") or m["text"],
            "size": extra.get("size"),
            "duration": extra.get("duration"),
        }
        path = Path(extra["path"]) if extra.get("path") else None
        if path is None or not path.is_file():
            # Deleted from downloads_dir since (or never saved): say so rather than drop it.
            self._append_file_received(meta, path or Path(meta["filename"]), ts=m["ts"], is_mine=is_mine, missing=True)
        elif m["kind"] == "image":
            self._append_image(m["nick"], path, ts=m["ts"], is_mine=is_mine)
        elif m["kind"] == "voice":
            self._append_voice(meta, path, ts=m["ts"], is_mine=is_mine)
        else:
            self._append_file_received(meta, path, ts=m["ts"], is_mine=is_mine)

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
        self._clear_typing(device_id)
        if self.cfg.show_presence:
            self._append_system(f"{last_known.get('nick', device_id)} went offline")
        self._refresh_peer_list()

    def _on_action_handled(self, request_obj: dict, outcome: str) -> None:
        nick = request_obj.get("nick", "?")
        action = request_obj.get("action", "?")
        self._append_system(f"{nick} triggered '{action}' ({outcome})")

    def _handle_typing(self, obj: dict) -> None:
        device_id = obj.get("from")
        if not device_id or device_id == self.cfg.device_id:
            return  # our own events echo back on the broadcast topic
        if obj.get("state") != "typing":
            self._clear_typing(device_id)
            return
        previous = self._typing_peers.get(device_id)
        if previous:
            GLib.source_remove(previous[1])
        expiry = GLib.timeout_add_seconds(_TYPING_EXPIRE_SECONDS, self._on_typing_expired, device_id)
        self._typing_peers[device_id] = (obj.get("nick", "?"), expiry)
        if not previous:
            self._refresh_peer_list()
            self._render_typing()

    def _clear_typing(self, device_id: str) -> None:
        entry = self._typing_peers.pop(device_id, None)
        if entry:
            GLib.source_remove(entry[1])
            self._refresh_peer_list()
            self._render_typing()

    def _on_typing_expired(self, device_id: str) -> bool:
        self._typing_peers.pop(device_id, None)
        self._refresh_peer_list()
        self._render_typing()
        return False

    def _render_typing(self) -> None:
        nicks = sorted(nick for nick, _expiry in self._typing_peers.values())
        if nicks:
            self.typing_avatar.set_text(nicks[0])
            if len(nicks) == 1:
                names = nicks[0]
            elif len(nicks) == 2:
                names = f"{nicks[0]} and {nicks[1]}"
            else:
                names = f"{nicks[0]} and {len(nicks) - 1} others"
            self.typing_names.set_label(names)
            self.chat_stack.set_visible_child_name("chat")
        self.typing_revealer.set_reveal_child(bool(nicks))

    def _typing_summary(self) -> str:
        nicks = sorted(nick for nick, _expiry in self._typing_peers.values())
        if not nicks:
            return ""
        if len(nicks) == 1:
            return f"{nicks[0]} is typing…"
        if len(nicks) == 2:
            return f"{nicks[0]} and {nicks[1]} are typing…"
        return f"{len(nicks)} people are typing…"

    def _on_file_complete(self, meta: dict, path: Path) -> None:
        if meta.get("kind") == "voice" or path.suffix.lower() in _AUDIO_EXTENSIONS:
            kind = "voice"
            GLib.idle_add(self._append_voice, meta, path)
            _notify("Voice message", f"from {meta['nick']}")
        elif path.suffix.lower() in _IMAGE_EXTENSIONS:
            kind = "image"
            GLib.idle_add(self._append_image, meta["nick"], path)
            _notify("File received", f"{meta['filename']} from {meta['nick']}")
        else:
            kind = "file"
            GLib.idle_add(self._append_file_received, meta, path)
            _notify("File received", f"{meta['filename']} from {meta['nick']}")
        self._remember_file(kind, meta["transfer_id"], meta, path)
        _ding()

    def _on_file_error(self, meta: dict, msg: str) -> None:
        GLib.idle_add(self._append_system, f"Couldn't receive {meta.get('filename', 'a file')}: {msg}")

    # -- UI actions ----------------------------------------------------------

    def _on_entry_changed(self, entry: Gtk.Entry) -> None:
        has_text = bool(entry.get_text().strip())
        self.send_btn.set_sensitive(has_text)
        if has_text:
            now = time.monotonic()
            if now - self._typing_sent_at >= _TYPING_SEND_INTERVAL:
                self._typing_sent_at = now
                self._send_typing("typing")
        elif self._typing_sent_at:
            # Replacing the text (set_text, pasting over a selection) empties
            # the entry for an instant first — only an entry that's still
            # empty once the edit settles means we've stopped.
            GLib.idle_add(self._on_entry_maybe_emptied)

    def _on_entry_maybe_emptied(self) -> bool:
        if self._typing_sent_at and not self.entry.get_text().strip():
            self._typing_sent_at = 0.0
            self._send_typing("stopped")
        return False

    def _send_typing(self, state: str) -> None:
        if self.client.connected.is_set():
            self.client.send_typing(
                {"from": self.cfg.device_id, "nick": self.cfg.nickname, "ts": time.time(), "state": state}
            )

    def _on_send(self, _widget) -> None:
        raw = self.entry.get_text()
        text = raw.strip()
        if not text:
            return
        self.entry.set_text("")
        if text.split(maxsplit=1)[0] == "/ascii":
            # Ahead of the hint cleanup below, which would eat into the drawing's spacing.
            drawing = _ascii_drawing(raw)
            if drawing:
                self._send_text(drawing, ascii_art=True)
            else:
                self._append_system("Usage: /ascii <drawing> — paste a multi-line drawing after /ascii")
            return
        if text.startswith("/"):
            text = _HINT_PLACEHOLDER.sub("", text).strip()
        parts = text.split(maxsplit=2)
        if parts and parts[0] == "/action":
            self._handle_action_command(parts)
            return
        if parts and parts[0].startswith("/") and parts[0][1:] in _TREATS:
            self._handle_treat_command(_TREATS[parts[0][1:]], text)
            return
        self._send_text(text)

    def _send_text(self, text: str, *, ascii_art: bool = False) -> None:
        payload = {
            "id": uuid.uuid4().hex,
            "ts": time.time(),
            "from": self.cfg.device_id,
            "nick": self.cfg.nickname,
            "text": text,
        }
        if ascii_art:
            payload["format"] = "ascii"
        reply = self._reply_to
        self._cancel_reply()
        if reply is None:
            self.client.send_chat(payload)
            return
        payload["reply_to"] = {"id": reply["id"], "nick": reply["nick"], "text": reply["preview"][:_REPLY_PREVIEW_CHARS]}
        peer = reply["peer_device_id"]
        if not (reply["is_dm"] and peer):
            self.client.send_chat(payload)
            return
        # A reply to a direct message stays one, so its quote never goes out to everyone.
        self.client.send_dm(peer, payload)
        # DMs aren't echoed back to the sender the way broadcasts are.
        self._append_text(
            self.cfg.nickname,
            payload["ts"],
            text,
            msg_id=payload["id"],
            is_mine=True,
            is_dm=True,
            dm_peer_device_id=peer,
            reply_to=payload["reply_to"],
            ascii_art=ascii_art,
        )
        self._remember(payload, is_dm=True, peer_device_id=peer, extra=_text_extra(payload["reply_to"], ascii_art))

    def _handle_action_command(self, parts: list[str]) -> None:
        if len(parts) != 3:
            self._append_system("Usage: /action <nickname> <command-name>")
            return
        _, peer_ref, action_name = parts
        target = self.peers.resolve(peer_ref)
        if not target:
            self._append_system(f"No such peer online: {peer_ref}")
            return
        self._append_system(f"Running '{action_name}' on {peer_ref}…")

        def work() -> None:
            try:
                result = run_action(self.client, self.cfg, target, action_name)
            except TimeoutError as exc:
                GLib.idle_add(self._append_system, str(exc))
                return
            GLib.idle_add(self._show_action_result, peer_ref, action_name, result)

        threading.Thread(target=work, daemon=True).start()

    def _show_action_result(self, peer_ref: str, action_name: str, result: dict) -> bool:
        if result.get("ok"):
            lines = [f"'{action_name}' on {peer_ref} (exit {result.get('exit_code', 0)}):"]
            if result.get("stdout"):
                lines.append(result["stdout"].rstrip("\n"))
            if result.get("stderr"):
                lines.append("stderr: " + result["stderr"].rstrip("\n"))
            self._append_system("\n".join(lines))
        else:
            self._append_system(f"'{action_name}' on {peer_ref} failed: {result.get('error', 'unknown error')}")
        return False

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
            transfer_id, _chunks = send_file(
                self.client, self.cfg, final_path, to="*", extra_meta={"kind": "voice", "duration": duration}
            )
            self._append_voice({"nick": self.cfg.nickname, "duration": duration}, final_path, is_mine=True)
            self._remember_sent_file("voice", transfer_id, final_path, duration=duration)
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
        if keyval == Gdk.KEY_Escape and self._reply_to is not None:
            self._cancel_reply()
            return True
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
        path = self._new_download_path("paste", ".png")
        texture.save_to_png(str(path))
        self._send_own_image(path)

    def _new_download_path(self, prefix: str, suffix: str) -> Path:
        downloads_dir = Path(self.cfg.downloads_dir).expanduser()
        downloads_dir.mkdir(parents=True, exist_ok=True)
        return downloads_dir / f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:6]}{suffix}"

    def _send_own_image(self, path: Path) -> None:
        """Broadcasts an image just saved to downloads_dir (a paste or a snip),
        shows it in our own chat, and keeps it in history. The file stays, as
        our own voice messages do, so history can show it after a restart."""
        try:
            transfer_id, _chunks = send_file(self.client, self.cfg, path, to="*")
        except (FileNotFoundError, ValueError) as exc:
            self._append_system(str(exc))
            path.unlink(missing_ok=True)
            return
        # Our own broadcast files are deliberately not echoed back to us
        # (FileReceiver skips its own sender), so without this we'd never
        # see the image we just sent in our own chat.
        self._append_image(self.cfg.nickname, path, is_mine=True)
        self._remember_sent_file("image", transfer_id, path)

    # -- snipping ----------------------------------------------------------

    def _on_snip_clicked(self, _button) -> None:
        if not shutil.which("grim") or not (shutil.which("omarchy-capture-region") or shutil.which("slurp")):
            self.toast_overlay.add_toast(Adw.Toast(title="Snipping needs grim and slurp installed"))
            return
        self.snip_btn.set_sensitive(False)  # one picker at a time
        threading.Thread(target=self._snip_worker, daemon=True).start()

    def _snip_worker(self) -> None:
        # Off the GTK thread: the picker waits on the user for as long as they take.
        path = self._new_download_path("snip", ".png")
        try:
            snipped = _snip_region(path)
        except (OSError, subprocess.SubprocessError, SnipError) as exc:
            path.unlink(missing_ok=True)
            GLib.idle_add(self._on_snip_done, None, f"Couldn't snip the screen: {exc}")
            return
        GLib.idle_add(self._on_snip_done, path if snipped else None, None)

    def _on_snip_done(self, path: Optional[Path], error: Optional[str]) -> bool:
        self.snip_btn.set_sensitive(True)
        if error:
            self.toast_overlay.add_toast(Adw.Toast(title=error))
        elif path is not None:
            self._send_own_image(path)
        return False

    # -- replies -----------------------------------------------------------

    def _build_reply_button(self, msg_id: str) -> Gtk.Button:
        button = Gtk.Button(
            icon_name="mail-reply-sender-symbolic",
            tooltip_text="Reply",
            valign=Gtk.Align.CENTER,
            css_classes=["flat", "circular", "react-btn"],
        )
        button.connect("clicked", lambda _b: self._start_reply(msg_id))
        return button

    def _build_reply_quote(self, reply_to: dict) -> Gtk.Widget:
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        labels.append(Gtk.Label(label=reply_to["nick"], xalign=0, css_classes=["reply-nick"]))
        labels.append(
            Gtk.Label(
                label=reply_to["text"],
                xalign=0,
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
                natural_wrap_mode=Gtk.NaturalWrapMode.NONE,
                lines=2,
                ellipsize=Pango.EllipsizeMode.END,
                max_width_chars=48,
                css_classes=["reply-text"],
            )
        )
        quote = Gtk.Button(child=labels, tooltip_text="Show the original message", css_classes=["flat", "reply-quote"])
        quote.connect("clicked", lambda _b: self._jump_to_message(reply_to["id"]))
        return quote

    def _start_reply(self, msg_id: str) -> None:
        meta = self._message_meta.get(msg_id)
        if meta is None:
            return
        self._reply_to = {"id": msg_id, **meta}
        privately = " privately" if meta["is_dm"] else ""
        self.reply_title.set_label(f"Replying{privately} to {meta['nick']}")
        self.reply_preview.set_label(" ".join(meta["preview"].split()))
        self.reply_revealer.set_reveal_child(True)
        self.entry.grab_focus()

    def _cancel_reply(self) -> None:
        self._reply_to = None
        self.reply_revealer.set_reveal_child(False)

    def _jump_to_message(self, msg_id: str) -> None:
        bubble = self._message_bubbles.get(msg_id)
        found, point = (False, None) if bubble is None else bubble.compute_point(self._chat_content, Graphene.Point().init(0, 0))
        if not found or bubble.get_root() is not self:
            self.toast_overlay.add_toast(Adw.Toast(title="The original message isn't in this chat anymore"))
            return
        adjustment = self.chat_scroller.get_vadjustment()
        centered = point.y - (adjustment.get_page_size() - bubble.get_height()) / 2
        adjustment.set_value(max(0.0, min(centered, adjustment.get_upper() - adjustment.get_page_size())))
        bubble.add_css_class("flash")
        GLib.timeout_add(900, lambda: bubble.remove_css_class("flash") or False)

    # -- treats: coffee, cocktails, dancers ---------------------------------

    def _handle_treat_command(self, treat: _Treat, text: str) -> None:
        # "/coffee" is for everyone; "/coffee <nickname> [note]" for one
        # person. When the first word isn't anyone online, it's all a note.
        rest = text[len(treat.kind) + 1 :].strip()
        target, note = None, rest
        if rest:
            first, _space, remainder = rest.partition(" ")
            resolved = self.peers.resolve(first)
            if resolved:
                target, note = resolved, remainder.strip()
        self._send_treat(treat, target, note)

    def _send_treat(self, treat: _Treat, target_device_id: Optional[str], note: str = "") -> None:
        if target_device_id == self.cfg.device_id:
            self.toast_overlay.add_toast(Adw.Toast(title=treat.self_toast))
            return
        if not self.client.connected.is_set():
            self.toast_overlay.add_toast(Adw.Toast(title=f"Not connected — the {treat.noun} will have to wait"))
            return
        to_nick = self.peers.snapshot().get(target_device_id, {}).get("nick", "") if target_device_id else ""
        fallback = f"{treat.emoji} {treat.phrase(to_one=bool(target_device_id))}"
        payload = {
            "type": treat.kind,
            "id": uuid.uuid4().hex,
            "ts": time.time(),
            "from": self.cfg.device_id,
            "nick": self.cfg.nickname,
            "to": target_device_id or "*",
            "to_nick": to_nick,
            "note": note,
            # What older clients, and the terminal chat/TUI, show instead.
            "text": f"{fallback}: {note}" if note else fallback,
        }
        if target_device_id:
            self.client.send_dm(target_device_id, payload)
            # DMs aren't echoed back to the sender the way broadcasts are.
            self._handle_treat(treat, payload, is_dm=True)
        else:
            self.client.send_chat(payload)

    def _handle_treat(self, treat: _Treat, obj: dict, *, is_dm: bool) -> None:
        sender = obj.get("from", "")
        is_mine = sender == self.cfg.device_id
        self._clear_typing(sender)
        peer_device_id = (obj.get("to") if is_mine else sender) if is_dm else None
        to_nick, note = obj.get("to_nick", ""), obj.get("note", "")
        self._append_treat(
            treat,
            obj["nick"],
            obj["ts"],
            to_nick=to_nick,
            note=note,
            msg_id=obj.get("id"),
            is_mine=is_mine,
            is_dm=is_dm,
            dm_peer_device_id=peer_device_id,
        )
        self._remember(
            obj, is_dm=is_dm, peer_device_id=peer_device_id, kind=treat.kind, extra={"to_nick": to_nick, "note": note}
        )
        self._play_treat(treat)
        if not is_mine:
            _notify(obj["nick"], f"{treat.phrase(to_one=is_dm)} {treat.emoji}")
            _ding()

    def _append_treat(
        self,
        treat: _Treat,
        nick: str,
        ts: float,
        *,
        to_nick: str,
        note: str,
        msg_id: Optional[str] = None,
        is_mine: bool = False,
        is_dm: bool = False,
        dm_peer_device_id: Optional[str] = None,
    ) -> None:
        headline = treat.headline(is_mine=is_mine, to_one=is_dm, to_nick=to_nick)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        info.append(Gtk.Label(label=headline, xalign=0, wrap=True, css_classes=["heading"]))
        if note:
            info.append(
                Gtk.Label(
                    label=note,
                    xalign=0,
                    wrap=True,
                    wrap_mode=Pango.WrapMode.WORD_CHAR,
                    max_width_chars=40,
                    selectable=True,
                )
            )
        stamp = self._stamp(ts)
        stamp.set_halign(Gtk.Align.START)
        info.append(stamp)
        bubble = Gtk.Box(spacing=12, css_classes=["bubble", "treat-card", f"{treat.kind}-card"])
        bubble.append(Gtk.Label(label=treat.emoji, valign=Gtk.Align.CENTER, css_classes=["treat-emoji"]))
        bubble.append(info)
        self._append_bubble(
            bubble,
            nick=nick,
            ts=ts,
            is_mine=is_mine,
            is_dm=is_dm,
            msg_id=msg_id,
            dm_peer_device_id=dm_peer_device_id,
            preview=f"{treat.emoji} {note or treat.noun.capitalize()}",
        )

    def _play_treat(self, treat: _Treat) -> None:
        if self._treat_active:
            return  # one at a time
        self._treat_active = True
        rising = Gtk.Box(spacing=14, halign=Gtk.Align.CENTER)
        for n, glyph in enumerate(treat.rising, start=1):
            rising.append(Gtk.Label(label=glyph, css_classes=["treat-rise", f"rise-{n}"]))
        stage = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, can_target=False
        )
        stage.append(rising)
        stage.append(Gtk.Label(label=treat.emoji, css_classes=["treat-burst", f"{treat.kind}-burst"]))
        self.sparkle_overlay.add_overlay(stage)

        def finish() -> bool:
            self.sparkle_overlay.remove_overlay(stage)
            self._treat_active = False
            return False

        GLib.timeout_add(_TREAT_BURST_MS, finish)

    # -- what's new --------------------------------------------------------

    def maybe_show_whats_new(self) -> None:
        releases = release_notes.unseen(self.cfg.last_seen_version)
        if releases:
            self._show_whats_new(releases)

    def _show_whats_new(self, releases: Optional[tuple] = None) -> None:
        showing_all = releases is None
        if releases is None:
            releases = release_notes.RELEASES
        dialog = Adw.Dialog(title="What's New", content_width=480, content_height=640)
        page = Adw.PreferencesPage()
        for release in releases:
            group = Adw.PreferencesGroup(
                title=f"Version {release.version}", description=_fmt_release_date(release.date)
            )
            for headline, description in release.items:
                # Row titles are Pango markup, and notes mention things like
                # "<nickname>".
                group.add(
                    Adw.ActionRow(
                        title=GLib.markup_escape_text(headline), subtitle=GLib.markup_escape_text(description)
                    )
                )
            page.add(group)
        if not showing_all and len(releases) < len(release_notes.RELEASES):
            earlier_group = Adw.PreferencesGroup()
            earlier_row = Adw.ActionRow(title="All releases", activatable=True)
            earlier_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))

            def on_earlier(_row) -> None:
                dialog.connect("closed", lambda *_: self._show_whats_new())
                dialog.close()

            earlier_row.connect("activated", on_earlier)
            earlier_group.add(earlier_row)
            page.add(earlier_group)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar())
        dialog.set_child(view)
        dialog.present(self)
        current = release_notes.current_version()
        if self.cfg.last_seen_version != current:
            self.cfg.last_seen_version = current
            self.cfg.save()

    # -- window lock -------------------------------------------------------

    def _apply_window_lock(self) -> None:
        locked = self.cfg.lock_window_size
        width, height = self.cfg.window_width, self.cfg.window_height
        if locked:
            self.unmaximize()
            self.unfullscreen()
            self.set_size_request(width, height)
            self.set_default_size(width, height)
        else:
            self.set_size_request(360, 420)
        self.set_resizable(not locked)
        if locked:
            self._start_hyprland_lock_watch(self.get_title())
        else:
            self._stop_hyprland_lock_watch()
        if locked or self._hyprland_floated:
            threading.Thread(
                target=self._sync_hyprland_lock, args=(locked, width, height, self.get_title()), daemon=True
            ).start()

    def _on_window_state_changed(self, *_args) -> None:
        if not self.cfg.lock_window_size:
            return
        # GTK's own controls are already gone on a non-resizable window, but
        # a compositor keybinding can still maximize or fullscreen any window.
        # Most compositors tell the window, which lands here; Hyprland doesn't
        # (see _watch_hyprland_events).
        if self.is_maximized():
            GLib.idle_add(lambda: self.unmaximize() or False)
        if self.is_fullscreen():
            GLib.idle_add(lambda: self.unfullscreen() or False)

    def _sync_hyprland_lock(self, locked: bool, width: int, height: int, title: str, recenter: bool = True) -> None:
        """Runs on a worker thread. Hyprland's tiling layout sizes a window no
        matter what size it asks for, so a locked window has to float.
        recenter=False, used when putting the lock back after a keybinding,
        leaves a floating window wherever it was moved to."""
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or not shutil.which("hyprctl"):
            return
        with self._hyprland_lock:
            client = None
            for _attempt in range(10):  # right after "map", Hyprland may not list the window yet
                clients = _hyprctl_json("clients") or []
                client = next((c for c in clients if c.get("pid") == os.getpid() and c.get("title") == title), None)
                if client is not None:
                    break
                time.sleep(0.1)
            if client is None:
                return
            window = f"address:{client['address']}"
            if not locked:
                if self._hyprland_floated and client.get("floating"):
                    _hyprland_dispatch(
                        [_lua_dispatch("window.float", action="disable", window=window)], [f"settiled {window}"]
                    )
                self._hyprland_floated = False
                return
            lua, legacy = [], []
            if client.get("fullscreen"):
                # A floating window gets its old size and position back once
                # fullscreen ends.
                lua.append(_lua_dispatch("window.fullscreen_state", internal=0, client=0, window=window))
            if not client.get("floating"):
                lua.append(_lua_dispatch("window.float", action="enable", window=window))
                legacy.append(f"setfloating {window}")
                self._hyprland_floated = True
            elif not recenter and (client.get("fullscreen") or list(client.get("size") or []) == [width, height]):
                _hyprland_dispatch(lua, legacy)
                return
            lua.append(_lua_dispatch("window.resize", x=width, y=height, window=window))
            legacy.append(f"resizewindowpixel exact {width} {height},{window}")
            monitor = next((m for m in _hyprctl_json("monitors") or [] if m.get("id") == client.get("monitor")), None)
            if monitor:
                scale = monitor.get("scale") or 1
                logical_w, logical_h = monitor["width"] / scale, monitor["height"] / scale
                if monitor.get("transform", 0) % 2:
                    logical_w, logical_h = logical_h, logical_w
                x = round(monitor["x"] + max(0, (logical_w - width) / 2))
                y = round(monitor["y"] + max(0, (logical_h - height) / 2))
                lua.append(_lua_dispatch("window.move", x=x, y=y, window=window))
                legacy.append(f"movewindowpixel exact {x} {y},{window}")
            _hyprland_dispatch(lua, legacy)

    def _start_hyprland_lock_watch(self, title: str) -> None:
        """Hyprland keybindings (fullscreen, toggle floating) change a window
        without telling it, so GTK never hears about them. While the window
        is locked, follow Hyprland's event socket and put the lock back."""
        if self._hyprland_watch is not None:
            return
        path = _hyprland_event_socket()
        if path is None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
        except OSError:
            sock.close()
            return
        self._hyprland_watch = sock
        threading.Thread(target=self._watch_hyprland_events, args=(sock, title), daemon=True).start()

    def _stop_hyprland_lock_watch(self) -> None:
        sock, self._hyprland_watch = self._hyprland_watch, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)  # wakes the watcher's blocked recv()
        except OSError:
            pass
        sock.close()

    def _watch_hyprland_events(self, sock: socket.socket, title: str) -> None:
        buffer = b""
        while True:
            try:
                data = sock.recv(4096)
            except OSError:
                return
            if not data or self._hyprland_watch is not sock:
                return
            buffer += data
            *lines, buffer = buffer.split(b"\n")
            # fullscreen>> doesn't say which window; _sync_hyprland_lock looks
            # this one up and leaves it alone if nothing about it changed.
            if any(line.split(b">>", 1)[0] in (b"fullscreen", b"changefloatingmode") for line in lines):
                if self.cfg.lock_window_size:
                    self._sync_hyprland_lock(
                        True, self.cfg.window_width, self.cfg.window_height, title, recenter=False
                    )

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

        window_group = Adw.PreferencesGroup(title="Window")
        lock_row = Adw.SwitchRow(
            title="Lock window size",
            subtitle="No resizing, maximizing, or fullscreen — the window floats on Hyprland",
        )
        lock_row.set_active(self.cfg.lock_window_size)
        window_group.add(lock_row)
        width_row = Adw.SpinRow.new_with_range(360, 7680, 10)
        width_row.set_title("Width")
        height_row = Adw.SpinRow.new_with_range(420, 4320, 10)
        height_row.set_title("Height")
        # While unlocked these follow the window's current size, so locking
        # keeps the window exactly as it is unless you change them.
        width_row.set_value(self.cfg.window_width if self.cfg.lock_window_size else self.get_width())
        height_row.set_value(self.cfg.window_height if self.cfg.lock_window_size else self.get_height())
        for row in (width_row, height_row):
            lock_row.bind_property("active", row, "sensitive", GObject.BindingFlags.SYNC_CREATE)
            window_group.add(row)
        page.add(window_group)

        remote_group = Adw.PreferencesGroup(
            title="Remote Commands",
            description="Let trusted peers trigger fixed commands on this machine.",
        )
        remote_row = Adw.ActionRow(
            title="Manage trusted peers and commands",
            subtitle="Currently " + ("enabled" if self.cfg.remote_actions_enabled else "disabled"),
            activatable=True,
        )
        remote_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        remote_row.connect("activated", lambda _r: self._on_open_remote_commands())
        remote_group.add(remote_row)
        page.add(remote_group)

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
                lock_window_size=lock_row.get_active(),
                window_width=int(width_row.get_value()),
                window_height=int(height_row.get_value()),
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
            window_changed = any(
                getattr(new_cfg, field) != getattr(self.cfg, field)
                for field in ("lock_window_size", "window_width", "window_height")
            )
            if needs_reconnect:
                self._apply_new_config(new_cfg)
            else:
                self.cfg = new_cfg
                self.receiver.cfg = new_cfg
                self.toast_overlay.add_toast(Adw.Toast(title="Settings saved"))
            if window_changed:
                self._apply_window_lock()

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

    def _on_open_remote_commands(self) -> None:
        dialog = Adw.Dialog(title="Remote Commands", content_width=480, content_height=680)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda _b: dialog.close())
        header.pack_start(close_btn)

        page = Adw.PreferencesPage()
        toasts = Adw.ToastOverlay(child=page)

        intro_group = Adw.PreferencesGroup(
            description=(
                "A peer can only ever trigger a NAME from the table below — the command "
                "that actually runs is fixed here and never sent by them. Off by default."
            )
        )
        enabled_row = Adw.SwitchRow(title="Allow trusted peers to trigger commands on this machine")
        enabled_row.set_active(self.cfg.remote_actions_enabled)

        def on_enabled_toggled(row: Adw.SwitchRow, _pspec) -> None:
            self.cfg.remote_actions_enabled = row.get_active()
            self.cfg.save()

        enabled_row.connect("notify::active", on_enabled_toggled)
        intro_group.add(enabled_row)
        page.add(intro_group)

        # -- Trusted peers ----------------------------------------------------
        trust_group = Adw.PreferencesGroup(
            title="Trusted Peers",
            description=(
                "Only these devices may trigger the named commands below — except "
                "'status', which any peer on the network may run as a connectivity check."
            ),
        )
        page.add(trust_group)
        trust_rows: list[Adw.ActionRow] = []

        def render_trust_rows() -> None:
            for row in trust_rows:
                trust_group.remove(row)
            trust_rows.clear()
            online = self.peers.snapshot()
            for device_id, level in sorted(self.cfg.remote_actions_peers.items()):
                nick = online.get(device_id, {}).get("nick")
                row = Adw.ActionRow(
                    title=nick if nick else device_id,
                    subtitle=device_id if nick else f"trust: {level}",
                )
                remove_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])

                def on_remove(_b, dev: str = device_id) -> None:
                    self.cfg.remote_actions_peers.pop(dev, None)
                    self.cfg.save()
                    render_trust_rows()

                remove_btn.connect("clicked", on_remove)
                row.add_suffix(remove_btn)
                trust_group.add(row)
                trust_rows.append(row)

        render_trust_rows()

        add_trust_group = Adw.PreferencesGroup()
        online_now = {d: v for d, v in self.peers.snapshot().items() if d != self.cfg.device_id}
        online_ids = sorted(online_now.keys())
        peer_choices = ["Pick an online peer…"] + [f"{online_now[d].get('nick', '?')} ({d})" for d in online_ids]
        peer_combo = Adw.ComboRow(title="Online peers")
        peer_combo.set_model(Gtk.StringList.new(peer_choices))
        device_entry = Adw.EntryRow(title="Device ID")

        def on_peer_picked(combo: Adw.ComboRow, _pspec) -> None:
            idx = combo.get_selected()
            if 1 <= idx <= len(online_ids):
                device_entry.set_text(online_ids[idx - 1])

        peer_combo.connect("notify::selected", on_peer_picked)
        add_trust_group.add(peer_combo)
        add_trust_group.add(device_entry)
        add_trust_row = Adw.ActionRow(title="Trust this device", activatable=True)
        add_trust_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])

        def on_add_trust(_b) -> None:
            dev = device_entry.get_text().strip()
            if not dev:
                toasts.add_toast(Adw.Toast(title="Enter or pick a device ID"))
                return
            self.cfg.remote_actions_peers[dev] = "commands"
            self.cfg.save()
            device_entry.set_text("")
            render_trust_rows()
            toasts.add_toast(Adw.Toast(title=f"Trusted {dev}"))

        add_trust_btn.connect("clicked", on_add_trust)
        add_trust_row.add_suffix(add_trust_btn)
        add_trust_row.set_activatable_widget(add_trust_btn)
        add_trust_group.add(add_trust_row)
        page.add(add_trust_group)

        # -- Named commands ----------------------------------------------------
        commands_group = Adw.PreferencesGroup(
            title="Named Commands",
            description="The fixed shell command that runs locally when a trusted peer triggers this name.",
        )
        page.add(commands_group)
        command_rows: list[Adw.ActionRow] = []

        def render_command_rows() -> None:
            for row in command_rows:
                commands_group.remove(row)
            command_rows.clear()
            for name, shell_cmd in sorted(self.cfg.remote_actions_commands.items()):
                subtitle = f"{shell_cmd}  ·  any peer" if name == "status" else shell_cmd
                row = Adw.ActionRow(title=name, subtitle=subtitle)
                remove_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])

                def on_remove(_b, nm: str = name) -> None:
                    self.cfg.remote_actions_commands.pop(nm, None)
                    self.cfg.save()
                    render_command_rows()

                remove_btn.connect("clicked", on_remove)
                row.add_suffix(remove_btn)
                commands_group.add(row)
                command_rows.append(row)

        render_command_rows()

        add_command_group = Adw.PreferencesGroup()
        name_entry = Adw.EntryRow(title="Name")
        shell_entry = Adw.EntryRow(title="Shell command")
        add_command_group.add(name_entry)
        add_command_group.add(shell_entry)
        add_command_row = Adw.ActionRow(title="Add command", activatable=True)
        add_command_btn = Gtk.Button(
            icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat", "circular"]
        )

        def on_add_command(_b) -> None:
            name = name_entry.get_text().strip()
            shell_cmd = shell_entry.get_text().strip()
            if not name or not shell_cmd:
                toasts.add_toast(Adw.Toast(title="Name and shell command are both required"))
                return
            self.cfg.remote_actions_commands[name] = shell_cmd
            self.cfg.save()
            name_entry.set_text("")
            shell_entry.set_text("")
            render_command_rows()
            toasts.add_toast(Adw.Toast(title=f"Added '{name}'"))

        add_command_btn.connect("clicked", on_add_command)
        add_command_row.add_suffix(add_command_btn)
        add_command_row.set_activatable_widget(add_command_btn)
        add_command_group.add(add_command_row)
        page.add(add_command_group)

        toolbar_view = Adw.ToolbarView(content=toasts)
        toolbar_view.add_top_bar(header)
        dialog.set_child(toolbar_view)
        dialog.present(self)

    def _apply_new_config(self, new_cfg: Config) -> None:
        self.client.disconnect()
        self._cancel_reply()  # the message being answered belongs to the old network

        self.cfg = new_cfg
        self.peers = PeerDirectory()
        for _nick, expiry in self._typing_peers.values():
            GLib.source_remove(expiry)
        self._typing_peers.clear()
        self._typing_sent_at = 0.0
        self._render_typing()
        self.receiver = FileReceiver(new_cfg, on_complete=self._on_file_complete, on_error=self._on_file_error)
        self.action_handler = RemoteActionHandler(new_cfg, on_handled=self._threaded(self._on_action_handled))
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


class SnipError(RuntimeError):
    pass


def _snip_region(path: Path) -> bool:
    """Lets the user pick part of the screen and saves it to `path` as a PNG;
    False if they cancel. On Omarchy this is the picker its screenshot key
    uses (omarchy-capture-region: the screen freezes while you pick, and a
    click takes a whole window), elsewhere plain slurp. Blocks until the
    pick is done, so keep it off the GTK thread."""
    freeze_pid = None
    try:
        if shutil.which("omarchy-capture-region"):
            # --keep-freeze prints the screen freeze's PID first and leaves it
            # up, so grim captures the frozen picture rather than whatever
            # moved meanwhile. Ending it is up to us.
            result = subprocess.run(
                ["omarchy-capture-region", "smart", "--keep-freeze"], capture_output=True, text=True, check=False
            )
            lines = result.stdout.splitlines()
            if lines and lines[0].strip().isdigit():
                freeze_pid = int(lines[0])
            selection = lines[1].strip() if result.returncode == 0 and len(lines) > 1 else ""
        else:
            result = subprocess.run(["slurp"], capture_output=True, text=True, check=False)
            selection = result.stdout.strip() if result.returncode == 0 else ""
        if not selection:
            return False
        grim = subprocess.run(["grim", "-g", selection, str(path)], capture_output=True, text=True, check=False)
        if grim.returncode != 0:
            raise SnipError(grim.stderr.strip() or f"grim exited with status {grim.returncode}")
        return True
    finally:
        if freeze_pid is not None:
            try:
                os.kill(freeze_pid, signal.SIGTERM)
            except OSError:
                pass


_hyprland_lua: Optional[bool] = None  # whether this Hyprland takes Lua dispatchers; probed once


def _hyprctl(args: list[str]) -> Optional[str]:
    try:
        return subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=2, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _hyprctl_json(what: str):
    try:
        return json.loads(_hyprctl(["-j", what]) or "")
    except ValueError:
        return None


def _hyprland_event_socket() -> Optional[str]:
    signature, runtime_dir = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"), os.environ.get("XDG_RUNTIME_DIR")
    if not signature or not runtime_dir:
        return None
    path = Path(runtime_dir) / "hypr" / signature / ".socket2.sock"
    return str(path) if path.exists() else None


def _lua_dispatch(dispatcher: str, **fields) -> str:
    args = ", ".join(f"{key} = {json.dumps(value)}" for key, value in fields.items())
    return f"hl.dispatch(hl.dsp.{dispatcher}({{ {args} }}))"


def _hyprland_dispatch(lua: list[str], legacy: list[str]) -> None:
    """Lua-configured Hyprland only accepts dispatchers as hl.dispatch()
    calls, rejecting the classic `dispatch setfloating address:…` strings;
    older releases only understand those classic strings."""
    global _hyprland_lua
    if _hyprland_lua is None:
        _hyprland_lua = (_hyprctl(["repl", "return 1"]) or "").strip() == "1"
    if _hyprland_lua:
        if lua:
            _hyprctl(["eval", "\n".join(lua)])
    elif legacy:
        _hyprctl(["--batch", " ; ".join(f"dispatch {command}" for command in legacy)])


class ImageViewerWindow(Adw.Window):
    """A chat image at full size, scaled down to fit the screen when it's
    bigger. Escape closes it. It shows the texture the chat already decoded,
    since a pasted or snipped image's file is deleted moments after sending."""

    _HEADER_HEIGHT = 47  # Adw.HeaderBar, so the image gets the rest of the window

    def __init__(self, parent: Gtk.Window, texture: Gdk.Texture, title: str):
        width, height = texture.get_width(), texture.get_height()
        super().__init__(transient_for=parent, modal=True, title=title)
        header = Adw.HeaderBar(title_widget=Adw.WindowTitle(title=title, subtitle=f"{width} × {height}"))
        picture = Gtk.Picture(paintable=texture, content_fit=Gtk.ContentFit.SCALE_DOWN, can_shrink=True)
        view = Adw.ToolbarView(content=picture)
        view.add_top_bar(header)
        self.set_content(view)

        # Opens at the image's own size, or within 90% of the monitor.
        scale = 1.0
        monitor = self._monitor_for(parent)
        if monitor is not None:
            geometry = monitor.get_geometry()
            scale = min(1.0, geometry.width * 0.9 / width, (geometry.height * 0.9 - self._HEADER_HEIGHT) / height)
        self.set_default_size(max(240, round(width * scale)), max(160, round(height * scale) + self._HEADER_HEIGHT))

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

    @staticmethod
    def _monitor_for(parent: Gtk.Window) -> Optional[Gdk.Monitor]:
        display, surface = parent.get_display(), parent.get_surface()
        if surface is not None:
            monitor = display.get_monitor_at_surface(surface)
            if monitor is not None:
                return monitor
        monitors = display.get_monitors()
        return monitors.get_item(0) if monitors.get_n_items() else None

    def _on_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False


class RelayApp(Adw.Application):
    def __init__(self, cfg: Config):
        super().__init__(application_id="net.omarchy.Relay")
        self.cfg = cfg
        self.window: Optional[RelayWindow] = None
        self.connect("activate", self._on_activate)
        self.connect("shutdown", self._on_shutdown)

        # Closing the window only hides it, so this is how you actually go
        # offline: Quit in the sidebar's main menu, or Ctrl+Q.
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)

    def _on_shutdown(self, _app: "RelayApp") -> None:
        if self.window is not None:
            self.window.client.disconnect()

    def _on_activate(self, app: "RelayApp") -> None:
        self.set_accels_for_action("app.quit", ["<Control>q"])
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
        self.window.maybe_show_whats_new()


def run_gui(cfg: Config) -> None:
    app = RelayApp(cfg)
    app.run(None)
