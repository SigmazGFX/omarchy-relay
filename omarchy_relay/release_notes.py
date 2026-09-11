"""What changed in each release, newest first — shown in the GUI's
"What's New" window. Add an entry here (and bump __version__) with every
release; the window opens by itself once for anyone whose last-seen
version is older than the newest entry.
"""
from __future__ import annotations

import dataclasses

from . import __version__


@dataclasses.dataclass(frozen=True)
class Release:
    version: str
    date: str
    items: tuple[tuple[str, str], ...]  # (headline, one-sentence description)


RELEASES: tuple[Release, ...] = (
    Release(
        version="0.3.0",
        date="2026-09-11",
        items=(
            (
                "Talk while screen sharing",
                "Everyone in a screen share can talk: use the microphone button in the viewer window, or "
                "next to LIVE while you share. Microphones start muted, and echo cancellation keeps the "
                "others from hearing themselves through your speakers.",
            ),
            (
                "Coffee by command only",
                "The cup buttons next to names are gone. Send a coffee with /coffee <nickname> [note], or "
                "/coffee for everyone.",
            ),
        ),
    ),
    Release(
        version="0.2.0",
        date="2026-09-11",
        items=(
            (
                "Screen sharing",
                "Share a monitor or window with everyone on the network from the button in the chat header. "
                "Viewers open it from the chat, and nothing is sent while nobody is watching.",
            ),
            (
                "Send a coffee",
                "Treat someone to a virtual cup of coffee: the cup button next to their name in the online "
                "list, /coffee <nickname> for one person, or /coffee for everyone.",
            ),
            (
                "Bouncing typing dots",
                "When someone is typing, three bouncing dots show at the bottom of the chat and next to "
                "their name.",
            ),
            (
                "Lock window size",
                "Settings → Window keeps the chat window at a fixed size: no resizing, maximizing, or "
                "fullscreen. On Hyprland the window floats while it's locked.",
            ),
            (
                "What's New",
                "This window. It opens once after each update, and any time from the main menu.",
            ),
        ),
    ),
    Release(
        version="0.1.0",
        date="2026-09-10",
        items=(
            (
                "Chat, direct messages, and files",
                "Talk to any Omarchy machine that can reach the same MQTT broker, encrypted with your "
                "network's passphrase.",
            ),
            (
                "Native chat window",
                "A GTK4/libadwaita window that follows the Omarchy theme, with a doodle wallpaper and a "
                "top-bar icon.",
            ),
            ("Images and voice messages", "Paste images straight into the chat, and record voice messages."),
            ("Reactions and emoji", "Emoji picker, message reactions, bigger emoji-only messages, and /sparkles."),
            (
                "Presence and notifications",
                "Who's online, typing status, desktop notifications, and a quiet message sound.",
            ),
            ("Message history", "Recent messages are kept locally and shown again on startup."),
            ("Share network settings", "Export your network and broker details to a file someone else can import."),
            (
                "Remote commands",
                "Let trusted peers trigger named commands on your machine. Off by default.",
            ),
        ),
    ),
)


def _version_key(version: str) -> tuple[int, ...]:
    parts = []
    for piece in version.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def unseen(last_seen_version: str) -> tuple[Release, ...]:
    """Releases newer than last_seen_version. Someone who has never seen any
    notes (an empty last_seen_version) only gets the newest release, not the
    whole backlog."""
    if not last_seen_version:
        return RELEASES[:1]
    seen = _version_key(last_seen_version)
    return tuple(r for r in RELEASES if _version_key(r.version) > seen)


def current_version() -> str:
    return __version__
