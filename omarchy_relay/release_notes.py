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
        version="0.11.0",
        date="2026-09-11",
        items=(
            (
                "React to, reply to, and delete files",
                "Files, images, and voice messages have the react, reply, and ⋯ buttons, like any message. "
                "Deleting one leaves the saved file where it is.",
            ),
        ),
    ),
    Release(
        version="0.10.0",
        date="2026-09-11",
        items=(
            (
                "Edit and delete messages",
                "The ⋯ button under a message edits your own or deletes it for everyone, and deletes anyone's "
                "from just your chat.",
            ),
        ),
    ),
    Release(
        version="0.9.1",
        date="2026-09-11",
        items=(
            (
                "Agent message alerts",
                "An agent message from a trusted peer now pops a desktop notification with a sound, the "
                "same as a direct message.",
            ),
            (
                "Agent messages are saved",
                "Fixed received agent messages failing to reach the inbox (omarchy-relay agent inbox).",
            ),
        ),
    ),
    Release(
        version="0.9.0",
        date="2026-09-11",
        items=(
            (
                "/ascii draws for you",
                "/ascii cat sends a ready-made drawing of a cat, and there are dozens more; /ascii on its "
                "own lists them. Anything else comes out in big letters.",
            ),
        ),
    ),
    Release(
        version="0.8.0",
        date="2026-09-11",
        items=(
            (
                "Agent messaging",
                "A new, separate channel for free-form messages between Claude Code sessions on trusted "
                "peers — off by default, its own trust list. CLI: omarchy-relay agent send/inbox/trust. "
                "Installs a Claude Code skill so a session can check in and collaborate with a peer's agent.",
            ),
        ),
    ),
    Release(
        version="0.7.0",
        date="2026-09-11",
        items=(
            (
                "Reply to a message",
                "The reply arrow under a message quotes it in your next one. Click a quote to jump to the "
                "original, and a reply to a direct message stays private.",
            ),
            (
                "ASCII drawings",
                "/ascii followed by a drawing sends it in a monospaced box that keeps every space. Paste a "
                "multi-line drawing right after /ascii.",
            ),
        ),
    ),
    Release(
        version="0.6.0",
        date="2026-09-11",
        items=(
            ("Expand images", "Double-click an image in the chat to see it full size. Escape closes it."),
            (
                "Images stay in the conversation",
                "Images, voice messages, and files now come back with the rest of the chat after a restart. "
                "Your own pasted and snipped images are saved to your downloads folder so they can.",
            ),
            (
                "Cocktails and dancers",
                "Like /coffee: /cocktail and /dancer send one to everyone, or to one person with "
                "/cocktail <nickname> [note].",
            ),
        ),
    ),
    Release(
        version="0.5.0",
        date="2026-09-11",
        items=(
            (
                "Snip and send",
                "The snip button in the chat header lets you drag over any part of your screen, or click a "
                "window, and sends that picture to the chat.",
            ),
        ),
    ),
    Release(
        version="0.4.0",
        date="2026-09-11",
        items=(
            (
                "Screen sharing removed",
                "Screen sharing and talking during a share are gone. Voice messages still work as before.",
            ),
        ),
    ),
    Release(
        version="0.3.0",
        date="2026-09-11",
        items=(
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
