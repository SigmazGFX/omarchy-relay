"""In-memory peer directory, fed by RelayClient.on_presence.

Presence is retained + LWT on the MQTT side: a peer's presence topic holds
its last-known {nick, ts} JSON, cleared (empty retained payload) either by
the peer disconnecting gracefully or by the broker firing its Last Will on
an ungraceful drop. So this class just mirrors whatever the broker already
knows — no polling needed.

A peer going offline is debounced: instead of vanishing from the list the
instant their presence clears (which flickers for anything as mundane as
an MQTT client reconnecting), they stay listed for DEBOUNCE_SECONDS. If
they come back online within that window, nothing visible ever happened.
If not, `on_removed` fires with their last-known data.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional


class PeerDirectory:
    DEBOUNCE_SECONDS = 5.0

    def __init__(self):
        self._lock = threading.Lock()
        self._peers: dict[str, dict] = {}
        self._pending_removal: dict[str, threading.Timer] = {}
        # (device_id, last_known_data) -> None — fires on the debounce
        # timer's own thread once a peer has actually stayed gone.
        self.on_removed: Optional[Callable[[str, dict], None]] = None

    def update(self, device_id: str, data: Optional[dict]) -> tuple[bool, Optional[dict]]:
        """Returns (changed, previous_data). `changed` reflects what's
        visibly true right now — a peer mid-debounce is still present, so
        the offline signal that started its debounce never reports as a
        change here; the eventual real removal is reported via
        `on_removed` instead."""
        with self._lock:
            if data is None:
                pending = self._pending_removal.get(device_id)
                if pending is not None:
                    pending.cancel()
                last_known = self._peers.get(device_id)
                if last_known is None:
                    return False, None  # already gone, nothing to debounce
                timer = threading.Timer(self.DEBOUNCE_SECONDS, self._finalize_removal, args=(device_id,))
                timer.daemon = True
                self._pending_removal[device_id] = timer
                timer.start()
                return False, last_known
            else:
                pending = self._pending_removal.pop(device_id, None)
                if pending is not None:
                    pending.cancel()
                previous = self._peers.get(device_id)
                self._peers[device_id] = data
                return previous is None, previous

    def _finalize_removal(self, device_id: str) -> None:
        with self._lock:
            self._pending_removal.pop(device_id, None)
            previous = self._peers.pop(device_id, None)
        if previous is not None and self.on_removed:
            self.on_removed(device_id, previous)

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
