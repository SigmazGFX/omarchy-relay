"""Local message history — shown on startup, before any live traffic
arrives. Nothing here is transmitted; it's purely a local cache of
messages this device has already seen, so restarting the client doesn't
lose the conversation.

Confined to a single thread: callers (gui.py) only touch it from GTK's
main loop, including from RelayClient callbacks — but only after they've
been marshalled onto the main loop via GLib.idle_add, never from paho's
own network thread directly. sqlite3 connections aren't safe to share
across threads, so this relies on that discipline rather than locking.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .config import DATA_DIR

DB_PATH = DATA_DIR / "history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    network TEXT NOT NULL,
    ts REAL NOT NULL,
    from_device TEXT NOT NULL,
    nick TEXT NOT NULL,
    text TEXT NOT NULL,
    is_dm INTEGER NOT NULL,
    peer_device_id TEXT,
    kind TEXT NOT NULL DEFAULT 'text',
    extra TEXT
);
CREATE INDEX IF NOT EXISTS messages_network_ts ON messages (network, ts);
CREATE TABLE IF NOT EXISTS reactions (
    network TEXT NOT NULL,
    target_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    device_id TEXT NOT NULL,
    nick TEXT NOT NULL,
    PRIMARY KEY (network, target_id, emoji, device_id)
);
"""


class HistoryStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)
        # kind ("text", a treat like "coffee", or a file: "image", "voice",
        # "file") and extra (JSON with whatever else that kind's renderer
        # needs) arrived in 0.2.0. Older databases get them added in place,
        # and their existing rows read back as plain text.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(messages)")}
        if "kind" not in columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'")
        if "extra" not in columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN extra TEXT")
        self._conn.commit()

    def add_message(
        self,
        network: str,
        msg_id: str,
        ts: float,
        from_device: str,
        nick: str,
        text: str,
        is_dm: bool,
        peer_device_id: Optional[str],
        kind: str = "text",
        extra: Optional[dict] = None,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO messages (id, network, ts, from_device, nick, text, is_dm, peer_device_id, kind, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (msg_id, network, ts, from_device, nick, text, int(is_dm), peer_device_id, kind, json.dumps(extra) if extra else None),
        )
        self._conn.commit()

    def recent_messages(self, network: str, limit: Optional[int] = None) -> list[dict]:
        """Oldest-first, ready to replay through the same render path as
        live messages. `limit` caps how many of the most recent are
        returned (None = everything stored for this network)."""
        columns = "id, ts, from_device, nick, text, is_dm, peer_device_id, kind, extra"
        if limit:
            rows = self._conn.execute(
                f"SELECT {columns} FROM messages WHERE network = ? AND kind != 'hidden' ORDER BY ts DESC LIMIT ?",
                (network, limit),
            ).fetchall()
            rows.reverse()
        else:
            rows = self._conn.execute(
                f"SELECT {columns} FROM messages WHERE network = ? AND kind != 'hidden' ORDER BY ts ASC",
                (network,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "ts": r[1],
                "from_device": r[2],
                "nick": r[3],
                "text": r[4],
                "is_dm": bool(r[5]),
                "peer_device_id": r[6],
                "kind": r[7] or "text",
                "extra": json.loads(r[8]) if r[8] else {},
            }
            for r in rows
        ]

    def has_message(self, network: str, msg_id: str) -> bool:
        """Whether msg_id is stored, in any state (deleted and hidden too)."""
        row = self._conn.execute("SELECT 1 FROM messages WHERE network = ? AND id = ?", (network, msg_id)).fetchone()
        return row is not None

    def edit_message(self, network: str, msg_id: str, from_device: str, text: str) -> None:
        """Replaces a text message's text and marks it edited. Only a row
        sent by from_device changes."""
        row = self._conn.execute(
            "SELECT extra FROM messages WHERE network = ? AND id = ? AND from_device = ? AND kind = 'text'",
            (network, msg_id, from_device),
        ).fetchone()
        if row is None:
            return
        extra = json.loads(row[0]) if row[0] else {}
        extra["edited"] = True
        self._conn.execute(
            "UPDATE messages SET text = ?, extra = ? WHERE network = ? AND id = ?",
            (text, json.dumps(extra), network, msg_id),
        )
        self._conn.commit()

    def mark_deleted(self, network: str, msg_id: str, from_device: str) -> None:
        """Deleted for everyone: the row stays, as kind "deleted", to show a
        marker where the message was, but its content is gone. Only a row
        sent by from_device changes."""
        changed = self._conn.execute(
            "UPDATE messages SET kind = 'deleted', text = '', extra = NULL "
            "WHERE network = ? AND id = ? AND from_device = ?",
            (network, msg_id, from_device),
        ).rowcount
        if changed:
            self._conn.execute("DELETE FROM reactions WHERE network = ? AND target_id = ?", (network, msg_id))
        self._conn.commit()

    def hide_message(self, network: str, msg_id: str) -> None:
        """Deleted for me: the row stays, as kind "hidden", so a redelivered
        copy isn't stored again, but its content is gone and it never loads."""
        self._conn.execute(
            "UPDATE messages SET kind = 'hidden', text = '', extra = NULL WHERE network = ? AND id = ?",
            (network, msg_id),
        )
        self._conn.execute("DELETE FROM reactions WHERE network = ? AND target_id = ?", (network, msg_id))
        self._conn.commit()

    def set_reaction(self, network: str, target_id: str, emoji: str, device_id: str, nick: str, present: bool) -> None:
        """Records one device's reaction on a message, or its removal."""
        if present:
            self._conn.execute(
                "INSERT OR REPLACE INTO reactions (network, target_id, emoji, device_id, nick) VALUES (?, ?, ?, ?, ?)",
                (network, target_id, emoji, device_id, nick),
            )
        else:
            self._conn.execute(
                "DELETE FROM reactions WHERE network = ? AND target_id = ? AND emoji = ? AND device_id = ?",
                (network, target_id, emoji, device_id),
            )
        self._conn.commit()

    def reactions(self, network: str) -> list[tuple[str, str, str, str]]:
        """(target_id, emoji, device_id, nick) for every reaction kept on
        this network, oldest first."""
        return self._conn.execute(
            "SELECT target_id, emoji, device_id, nick FROM reactions WHERE network = ? ORDER BY rowid",
            (network,),
        ).fetchall()

    def prune(self, network: str, retain_count: int, retain_days: float) -> None:
        """Both caps are independent and additive — a positive value on
        either dimension trims to that; 0 means unlimited on that
        dimension."""
        if retain_days and retain_days > 0:
            cutoff = time.time() - retain_days * 86400
            self._conn.execute("DELETE FROM messages WHERE network = ? AND ts < ?", (network, cutoff))
        if retain_count and retain_count > 0:
            self._conn.execute(
                "DELETE FROM messages WHERE network = ? AND id NOT IN ("
                "  SELECT id FROM messages WHERE network = ? ORDER BY ts DESC LIMIT ?"
                ")",
                (network, network, retain_count),
            )
        self._conn.execute(
            "DELETE FROM reactions WHERE network = ? AND target_id NOT IN (SELECT id FROM messages WHERE network = ?)",
            (network, network),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
