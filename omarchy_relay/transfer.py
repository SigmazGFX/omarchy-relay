"""Chunked file transfer over the encrypted relay.

Chunks are sent with an explicit index in the payload and reassembled by
seeking to `index * chunk_size` in a `.part` file — this tolerates MQTT QoS 1
duplicate delivery and out-of-order arrival across reconnects. The whole file
is SHA-256 verified against the sender's hash before the `.part` file is
renamed into place.
"""
from __future__ import annotations

import base64
import hashlib
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

_META_SETTLE_DELAY = 0.1


def send_file(client, cfg, path: Path, to: str = "*", extra_meta: Optional[dict] = None) -> tuple[str, int]:
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(path)
    size = path.stat().st_size
    if size > cfg.max_file_size:
        raise ValueError(
            f"{path.name} is {size} bytes, which exceeds transfer.max_file_size "
            f"({cfg.max_file_size}) in the config. Raise it if you really want to send this."
        )
    if size == 0:
        raise ValueError(f"{path.name} is empty, refusing to send")

    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    chunk_size = cfg.chunk_size
    total_chunks = (len(data) + chunk_size - 1) // chunk_size
    transfer_id = uuid.uuid4().hex[:12]

    meta = {
        "transfer_id": transfer_id,
        "from": cfg.device_id,
        "nick": cfg.nickname,
        "to": to,
        "filename": path.name,
        "size": size,
        "sha256": sha256,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks,
        "ts": time.time(),
    }
    if extra_meta:
        meta.update(extra_meta)
    client.send_file_meta(transfer_id, meta)
    # Give the broker a moment to fan the meta message out before the flood
    # of chunk messages arrives — receivers ignore chunks for an unseen
    # transfer_id, so this just reduces (harmless) dropped early chunks.
    time.sleep(_META_SETTLE_DELAY)

    for i in range(total_chunks):
        chunk = data[i * chunk_size : (i + 1) * chunk_size]
        client.send_file_chunk(
            transfer_id,
            {
                "transfer_id": transfer_id,
                "index": i,
                "data": base64.b64encode(chunk).decode("ascii"),
            },
        )
    return transfer_id, total_chunks


class FileReceiver:
    """Tracks in-flight incoming transfers and reassembles them to disk."""

    def __init__(
        self,
        cfg,
        on_complete: Optional[Callable[[dict, Path], None]] = None,
        on_error: Optional[Callable[[dict, str], None]] = None,
        on_progress: Optional[Callable[[dict, int, int], None]] = None,
    ):
        self.cfg = cfg
        self.downloads_dir = Path(cfg.downloads_dir).expanduser()
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._transfers: dict[str, dict] = {}
        self.on_complete = on_complete
        self.on_error = on_error
        self.on_progress = on_progress

    def handle_meta(self, obj: dict) -> None:
        if obj.get("from") == self.cfg.device_id:
            return  # ignore our own broadcasts
        to = obj.get("to", "*")
        if to not in ("*", self.cfg.device_id):
            return

        transfer_id = obj["transfer_id"]
        size = obj["size"]
        if size > self.cfg.max_file_size:
            if self.on_error:
                self.on_error(obj, f"refusing: {size} bytes exceeds local max_file_size")
            return

        part_path = self.downloads_dir / f".{transfer_id}.part"
        with open(part_path, "wb") as f:
            f.truncate(size)
        self._transfers[transfer_id] = {
            "meta": obj,
            "part_path": part_path,
            "received": set(),
            "total_chunks": obj["total_chunks"],
            "chunk_size": obj["chunk_size"],
        }

    def handle_chunk(self, obj: dict) -> None:
        transfer_id = obj.get("transfer_id")
        state = self._transfers.get(transfer_id)
        if state is None:
            return  # meta not seen yet (not addressed to us, or arrived late)

        index = obj["index"]
        if index in state["received"]:
            return  # duplicate delivery (QoS 1 redelivery)

        chunk_bytes = base64.b64decode(obj["data"])
        offset = index * state["chunk_size"]
        with open(state["part_path"], "r+b") as f:
            f.seek(offset)
            f.write(chunk_bytes)
        state["received"].add(index)

        if self.on_progress:
            self.on_progress(state["meta"], len(state["received"]), state["total_chunks"])
        if len(state["received"]) == state["total_chunks"]:
            self._finalize(transfer_id, state)

    def _finalize(self, transfer_id: str, state: dict) -> None:
        meta = state["meta"]
        part_path = state["part_path"]
        actual_hash = hashlib.sha256(part_path.read_bytes()).hexdigest()
        del self._transfers[transfer_id]
        if actual_hash != meta["sha256"]:
            part_path.unlink(missing_ok=True)
            if self.on_error:
                self.on_error(meta, "checksum mismatch after reassembly, file discarded")
            return
        final_path = self._unique_path(meta["filename"])
        part_path.rename(final_path)
        if self.on_complete:
            self.on_complete(meta, final_path)

    def _unique_path(self, filename: str) -> Path:
        candidate = self.downloads_dir / filename
        if not candidate.exists():
            return candidate
        stem, suffix = Path(filename).stem, Path(filename).suffix
        i = 1
        while True:
            candidate = self.downloads_dir / f"{stem} ({i}){suffix}"
            if not candidate.exists():
                return candidate
            i += 1
