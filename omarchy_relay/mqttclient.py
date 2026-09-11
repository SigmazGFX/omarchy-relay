"""MQTT transport for a relay network.

Targets paho-mqtt 1.x (the version Arch ships as python-paho-mqtt), which
uses the pre-CallbackAPIVersion2 callback signatures.
"""
from __future__ import annotations

import json
import ssl
import threading
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from .config import Config
from .crypto import Cipher, InvalidToken, topic_namespace


class PendingActions:
    """Matches outgoing remote-action requests to their results by request_id.

    Also doubles as replay protection on the receiving side: a request_id
    seen before (e.g. an MQTT QoS-1 redelivery of the same request) is
    tracked separately by the caller via `seen()`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._results: dict[str, dict] = {}
        self._seen_requests: set[str] = set()

    def register(self, request_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._events[request_id] = event
        return event

    def resolve(self, obj: dict) -> None:
        request_id = obj.get("request_id")
        if not request_id:
            return
        with self._lock:
            event = self._events.get(request_id)
            if event is None:
                return
            self._results[request_id] = obj
            event.set()

    def pop_result(self, request_id: str) -> Optional[dict]:
        with self._lock:
            self._events.pop(request_id, None)
            return self._results.pop(request_id, None)

    def seen(self, request_id: str) -> bool:
        """True if this request_id was already handled; marks it seen either way."""
        with self._lock:
            if request_id in self._seen_requests:
                return True
            self._seen_requests.add(request_id)
            if len(self._seen_requests) > 1000:
                self._seen_requests = set(list(self._seen_requests)[-500:])
            return False


class RelayClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ns = topic_namespace(cfg.network_name)
        self.cipher = Cipher(cfg.passphrase, cfg.network_name)
        self.presence_topic = f"{self.ns}/presence/{cfg.device_id}"

        self._client = mqtt.Client(client_id=cfg.device_id, protocol=mqtt.MQTTv311, clean_session=True)
        if cfg.broker_username:
            self._client.username_pw_set(cfg.broker_username, cfg.broker_password or None)
        if cfg.broker_tls:
            self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self._client.will_set(self.presence_topic, payload=b"", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        # Callbacks the app layer fills in. Invoked on the paho network thread.
        self.on_chat: Optional[Callable[[dict], None]] = None
        self.on_dm: Optional[Callable[[dict], None]] = None
        self.on_presence: Optional[Callable[[str, Optional[dict]], None]] = None
        self.on_file_meta: Optional[Callable[[dict], None]] = None
        self.on_file_chunk: Optional[Callable[[dict], None]] = None
        self.on_action_request: Optional[Callable[[dict], None]] = None
        self.on_typing: Optional[Callable[[dict], None]] = None
        self.on_screen_state: Optional[Callable[[str, Optional[dict]], None]] = None
        self.on_screen_watch: Optional[Callable[[dict], None]] = None
        self.on_screen_frame: Optional[Callable[[dict, bytes], None]] = None
        self.on_screen_audio: Optional[Callable[[dict, bytes], None]] = None
        self.on_bad_message: Optional[Callable[[str, Exception], None]] = None

        # Remote-action request/result matching (see remote_actions.py).
        self.pending_actions = PendingActions()

        # Screen sharing (see screenshare.py): our own share state, retained
        # and republished on every (re)connect, and the shares being watched
        # here — frame topics are only subscribed while a viewer is open.
        self.screen_state_topic = f"{self.ns}/screen/{cfg.device_id}"
        self.screen_state: Optional[dict] = None
        self._watched_shares: set[str] = set()
        self._voice_shares: set[str] = set()  # shares whose voice room this client is in (see voice.py)

        self.connected = threading.Event()

    def connect(self, timeout: float = 10.0) -> None:
        self._client.connect(self.cfg.broker_host, self.cfg.broker_port, keepalive=30)
        self._client.loop_start()
        if not self.connected.wait(timeout=timeout):
            self._client.loop_stop()
            raise ConnectionError(
                f"Timed out connecting to {self.cfg.broker_host}:{self.cfg.broker_port}"
            )

    def disconnect(self) -> None:
        try:
            self._client.publish(self.screen_state_topic, payload=b"", qos=1, retain=True)
            self._client.publish(self.presence_topic, payload=b"", qos=1, retain=True).wait_for_publish(3)
        except Exception:
            pass
        self._client.loop_stop()
        self._client.disconnect()

    # -- outgoing --------------------------------------------------------

    def send_chat(self, obj: dict) -> None:
        self._publish_encrypted(f"{self.ns}/chat", obj)

    def send_dm(self, target_device_id: str, obj: dict) -> None:
        self._publish_encrypted(f"{self.ns}/dm/{target_device_id}", obj)

    def send_typing(self, obj: dict) -> None:
        # Resent every few seconds while typing continues, so QoS 0: a dropped
        # one just means the indicator lapses a moment early.
        self._publish_encrypted(f"{self.ns}/typing", obj, qos=0)

    def send_file_meta(self, transfer_id: str, obj: dict) -> None:
        self._publish_encrypted(f"{self.ns}/file/{transfer_id}/meta", obj)

    def send_file_chunk(self, transfer_id: str, obj: dict) -> None:
        self._publish_encrypted(f"{self.ns}/file/{transfer_id}/chunk", obj)

    def send_action_request(self, target_device_id: str, obj: dict) -> None:
        self._publish_encrypted(f"{self.ns}/action/{target_device_id}", obj)

    def send_action_result(self, target_device_id: str, obj: dict) -> None:
        self._publish_encrypted(f"{self.ns}/action-result/{target_device_id}", obj)

    def set_screen_state(self, obj: Optional[dict]) -> None:
        """Retained, so a peer who comes online mid-share still sees it.
        None clears it."""
        self.screen_state = obj
        if obj is None:
            self._client.publish(self.screen_state_topic, payload=b"", qos=1, retain=True)
        else:
            self._publish_encrypted(self.screen_state_topic, obj, retain=True)

    def send_screen_watch(self, sharer_device_id: str, obj: dict) -> None:
        # Repeated every few seconds while watching, so QoS 0 like typing.
        self._publish_encrypted(f"{self.ns}/screen-watch/{sharer_device_id}", obj, qos=0)

    def send_screen_frame(self, share_id: str, header: dict, chunk: bytes):
        """A JSON header line, then the raw JPEG bytes — no base64 layer on
        top of Fernet's own. QoS 0: a lost chunk only loses that frame, and
        the next one replaces it anyway. Returns paho's MQTTMessageInfo so
        the sender can tell when the frame has actually gone out."""
        plaintext = json.dumps(header).encode("utf-8") + b"\n" + chunk
        return self._client.publish(
            f"{self.ns}/screen-frame/{share_id}", payload=self.cipher.encrypt(plaintext), qos=0
        )

    def send_screen_audio(self, share_id: str, header: dict, packet: bytes) -> None:
        """Same framing as screen frames: a JSON header line, then the raw
        Opus packet. QoS 0 — a late packet is useless anyway."""
        plaintext = json.dumps(header).encode("utf-8") + b"\n" + packet
        self._client.publish(f"{self.ns}/screen-audio/{share_id}", payload=self.cipher.encrypt(plaintext), qos=0)

    def join_screen_audio(self, share_id: str) -> None:
        self._voice_shares.add(share_id)
        if self.connected.is_set():
            self._client.subscribe(f"{self.ns}/screen-audio/{share_id}", 0)

    def leave_screen_audio(self, share_id: str) -> None:
        self._voice_shares.discard(share_id)
        if self.connected.is_set():
            self._client.unsubscribe(f"{self.ns}/screen-audio/{share_id}")

    def watch_screen(self, share_id: str) -> None:
        self._watched_shares.add(share_id)
        if self.connected.is_set():
            self._client.subscribe(f"{self.ns}/screen-frame/{share_id}", 0)

    def unwatch_screen(self, share_id: str) -> None:
        self._watched_shares.discard(share_id)
        if self.connected.is_set():
            self._client.unsubscribe(f"{self.ns}/screen-frame/{share_id}")

    def _publish_encrypted(self, topic: str, obj: dict, qos: int = 1, retain: bool = False) -> None:
        token = self.cipher.encrypt(json.dumps(obj).encode("utf-8"))
        self._client.publish(topic, payload=token, qos=qos, retain=retain)

    # -- paho callbacks ---------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            return
        client.subscribe(
            [
                (f"{self.ns}/chat", 1),
                (f"{self.ns}/dm/{self.cfg.device_id}", 1),
                (f"{self.ns}/typing", 0),
                (f"{self.ns}/presence/+", 1),
                (f"{self.ns}/file/+/meta", 1),
                (f"{self.ns}/file/+/chunk", 1),
                (f"{self.ns}/action/{self.cfg.device_id}", 1),
                (f"{self.ns}/action-result/{self.cfg.device_id}", 1),
                (f"{self.ns}/screen/+", 1),
                (f"{self.ns}/screen-watch/{self.cfg.device_id}", 0),
            ]
            + [(f"{self.ns}/screen-frame/{share_id}", 0) for share_id in self._watched_shares]
            + [(f"{self.ns}/screen-audio/{share_id}", 0) for share_id in self._voice_shares]
        )
        self._publish_presence()
        # Republishing (or clearing) on every connect also wipes a stale
        # share left retained by a crash, which has no Last Will of its own.
        self.set_screen_state(self.screen_state)
        self.connected.set()

    def _on_disconnect(self, client, userdata, rc):
        self.connected.clear()

    def _publish_presence(self) -> None:
        payload = json.dumps({"nick": self.cfg.nickname, "ts": time.time()}).encode("utf-8")
        self._client.publish(self.presence_topic, payload=payload, qos=1, retain=True)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        parts = topic.split("/")
        try:
            if topic == f"{self.ns}/chat":
                self._dispatch_encrypted(msg.payload, self.on_chat)
            elif topic == f"{self.ns}/dm/{self.cfg.device_id}":
                self._dispatch_encrypted(msg.payload, self.on_dm)
            elif topic == f"{self.ns}/typing":
                self._dispatch_encrypted(msg.payload, self.on_typing)
            elif len(parts) >= 2 and parts[-2] == "screen":
                self._handle_screen_state(parts[-1], msg.payload)
            elif topic == f"{self.ns}/screen-watch/{self.cfg.device_id}":
                self._dispatch_encrypted(msg.payload, self.on_screen_watch)
            elif len(parts) >= 2 and parts[-2] == "screen-frame":
                self._dispatch_framed(msg.payload, self.on_screen_frame)
            elif len(parts) >= 2 and parts[-2] == "screen-audio":
                self._dispatch_framed(msg.payload, self.on_screen_audio)
            elif len(parts) >= 2 and parts[-2] == "presence":
                self._handle_presence(parts[-1], msg.payload)
            elif len(parts) >= 2 and parts[-1] == "meta" and "file" in parts:
                self._dispatch_encrypted(msg.payload, self.on_file_meta)
            elif len(parts) >= 2 and parts[-1] == "chunk" and "file" in parts:
                self._dispatch_encrypted(msg.payload, self.on_file_chunk)
            elif topic == f"{self.ns}/action/{self.cfg.device_id}":
                self._dispatch_encrypted(msg.payload, self.on_action_request)
            elif topic == f"{self.ns}/action-result/{self.cfg.device_id}":
                self._dispatch_encrypted(msg.payload, self.pending_actions.resolve)
        except (InvalidToken, ValueError, KeyError) as exc:
            # Wrong passphrase, foreign traffic sharing the broker, or a
            # malformed message — drop it rather than crash the listener.
            if self.on_bad_message:
                self.on_bad_message(topic, exc)

    def _handle_presence(self, device_id: str, payload: bytes) -> None:
        if not payload:
            if self.on_presence:
                self.on_presence(device_id, None)
            return
        data = json.loads(payload.decode("utf-8"))
        if self.on_presence:
            self.on_presence(device_id, data)

    def _handle_screen_state(self, device_id: str, payload: bytes) -> None:
        if self.on_screen_state is None:
            return
        if not payload:
            self.on_screen_state(device_id, None)
            return
        self.on_screen_state(device_id, json.loads(self.cipher.decrypt(payload).decode("utf-8")))

    def _dispatch_framed(self, payload: bytes, handler) -> None:
        """A JSON header line followed by raw bytes (screen frames, voice)."""
        if handler is None:
            return
        header, _newline, data = self.cipher.decrypt(payload).partition(b"\n")
        handler(json.loads(header.decode("utf-8")), data)

    def _dispatch_encrypted(self, payload: bytes, handler) -> None:
        if handler is None:
            return
        plaintext = self.cipher.decrypt(payload)
        handler(json.loads(plaintext.decode("utf-8")))
