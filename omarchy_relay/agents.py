"""Agent-to-agent messaging — free-form text between Claude Code sessions
(or any other automation) running on different peers, routed over the
same encrypted relay as human chat/DMs.

Deliberately kept separate from remote_actions.py: that system's whole
security guarantee is "a peer can only pick a *name* from this machine's
own fixed command table, never send content that runs." Agent messages
are the opposite — free-form text — so they get their own trust table
(agents.peers) and never reach a shell. A receiving Claude session reads
inbox content as untrusted data to reason about, exactly like a chat
message from a human; nothing here executes anything automatically.

Off by default (agents.enabled = false). Even when enabled, a peer not
listed in agents.peers as "agent" is refused — there's no "everyone on
the network" fallback, matching remote_actions' posture.

Note on trust: this is *group* encryption (see README's Security model) —
any device holding the network passphrase can forge the `from`/`nick`
fields on a message. Trusting a device_id here means trusting whoever
currently holds that passphrase, not a cryptographically verified
identity.

Delivery is best-effort and live-only, like DMs and remote actions: a
message sent while the target has nothing running (no daemon/chat/gui
connected) is simply not received — there's no store-and-forward on the
broker side (clean_session=True). The mailbox only records what this
device has actually seen or sent.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .history import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_messages (
    id TEXT PRIMARY KEY,
    network TEXT NOT NULL,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,
    peer_device_id TEXT NOT NULL,
    peer_nick TEXT NOT NULL,
    text TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS agent_messages_network_ts ON agent_messages (network, ts);
"""


class AgentMailbox:
    """Local persisted log of agent messages sent/received by this device.

    Not shared, not synced — purely a local record so `omarchy-relay agent
    inbox` can read what arrived without needing a live MQTT connection.

    Incoming messages are persisted from inside RelayClient's on_message
    callback, which paho runs on its own network thread — a different
    thread than whichever one constructed this mailbox (the CLI's main
    thread, or chat.py/gui.py's setup code). sqlite3 connections are
    bound to one thread by default, so this opens with
    check_same_thread=False and serializes every access through a lock
    instead (same approach as PendingActions in mqttclient.py).
    """

    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def add(
        self,
        network: str,
        msg_id: str,
        ts: float,
        direction: str,
        peer_device_id: str,
        peer_nick: str,
        text: str,
    ) -> bool:
        """Returns True if this was a new message (False if msg_id was
        already recorded — an MQTT redelivery, or a duplicate send)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO agent_messages (id, network, ts, direction, peer_device_id, peer_nick, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg_id, network, ts, direction, peer_device_id, peer_nick, text),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list(self, network: str, unread_only: bool = False, limit: Optional[int] = None) -> list[dict]:
        """Oldest-first."""
        where = "network = ?"
        params: list = [network]
        if unread_only:
            where += " AND direction = 'in' AND read = 0"
        query = f"SELECT id, ts, direction, peer_device_id, peer_nick, text, read FROM agent_messages WHERE {where} ORDER BY ts ASC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "id": r[0],
                "ts": r[1],
                "direction": r[2],
                "peer_device_id": r[3],
                "peer_nick": r[4],
                "text": r[5],
                "read": bool(r[6]),
            }
            for r in rows
        ]

    def mark_read(self, network: str, msg_id: Optional[str] = None) -> None:
        """Mark one message read, or every incoming message if msg_id is None."""
        with self._lock:
            if msg_id:
                self._conn.execute("UPDATE agent_messages SET read = 1 WHERE network = ? AND id = ?", (network, msg_id))
            else:
                self._conn.execute("UPDATE agent_messages SET read = 1 WHERE network = ? AND direction = 'in'", (network,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class AgentMessageHandler:
    """Receiver side: decides whether to accept an incoming agent message
    and persists it. No reply/ack is sent — this is fire-and-forget, like
    chat, not request/response like remote actions."""

    def __init__(self, cfg, mailbox: AgentMailbox, on_received: Optional[Callable[[dict], None]] = None):
        self.cfg = cfg
        self.mailbox = mailbox
        # (message_obj) -> None, only called for newly-accepted messages —
        # for UI/log lines, e.g. "bob (agent): <text>"
        self.on_received = on_received

    def trust_level(self, device_id: str) -> str:
        return self.cfg.agents_peers.get(device_id, "none")

    def handle_message(self, obj: dict) -> None:
        if not self.cfg.agents_enabled:
            return
        sender = obj.get("from", "")
        if not sender or self.trust_level(sender) != "agent":
            return
        msg_id = obj.get("id") or ""
        text = obj.get("text", "")
        if not msg_id or not text:
            return
        is_new = self.mailbox.add(
            self.cfg.network_name,
            msg_id,
            obj.get("ts", time.time()),
            "in",
            sender,
            obj.get("nick", sender),
            text,
        )
        if is_new and self.on_received:
            self.on_received(obj)


def send_agent_message(client, cfg, mailbox: AgentMailbox, target_device_id: str, target_nick: str, text: str) -> dict:
    """Sender side: publish a message to a peer and record it as sent."""
    obj = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "from": cfg.device_id,
        "nick": cfg.nickname,
        "text": text,
    }
    client.send_agent_message(target_device_id, obj)
    mailbox.add(cfg.network_name, obj["id"], obj["ts"], "out", target_device_id, target_nick, text)
    return obj
