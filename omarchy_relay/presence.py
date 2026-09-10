"""In-memory peer directory, fed by RelayClient.on_presence.

Presence is retained + LWT on the MQTT side: a peer's presence topic holds
its last-known {nick, ts} JSON, cleared (empty retained payload) either by
the peer disconnecting gracefully or by the broker firing its Last Will on
an ungraceful drop. So this class just mirrors whatever the broker already
knows — no polling or timeouts needed.
"""
from __future__ import annotations

import threading
from typing import Optional


class PeerDirectory:
    def __init__(self):
        self._lock = threading.Lock()
        self._peers: dict[str, dict] = {}

    def update(self, device_id: str, data: Optional[dict]) -> tuple[bool, Optional[dict]]:
        """Returns (changed, previous_data)."""
        with self._lock:
            if data is None:
                previous = self._peers.pop(device_id, None)
                return (previous is not None), previous
            previous = self._peers.get(device_id)
            self._peers[device_id] = data
            return True, previous

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._peers)

    def resolve(self, nick_or_id: str) -> Optional[str]:
        """Best-effort resolve a nickname or device_id to a device_id."""
        with self._lock:
            if nick_or_id in self._peers:
                return nick_or_id
            for device_id, data in self._peers.items():
                if data.get("nick") == nick_or_id:
                    return device_id
        return None
