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
        self.on_bad_message: Optional[Callable[[str, Exception], None]] = None

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

    def send_file_meta(self, transfer_id: str, obj: dict) -> None:
        self._publish_encrypted(f"{self.ns}/file/{transfer_id}/meta", obj)

    def send_file_chunk(self, transfer_id: str, obj: dict) -> None:
        self._publish_encrypted(f"{self.ns}/file/{transfer_id}/chunk", obj)

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
                (f"{self.ns}/presence/+", 1),
                (f"{self.ns}/file/+/meta", 1),
                (f"{self.ns}/file/+/chunk", 1),
            ]
        )
        self._publish_presence()
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
            elif len(parts) >= 2 and parts[-2] == "presence":
                self._handle_presence(parts[-1], msg.payload)
            elif len(parts) >= 2 and parts[-1] == "meta" and "file" in parts:
                self._dispatch_encrypted(msg.payload, self.on_file_meta)
            elif len(parts) >= 2 and parts[-1] == "chunk" and "file" in parts:
                self._dispatch_encrypted(msg.payload, self.on_file_chunk)
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

    def _dispatch_encrypted(self, payload: bytes, handler) -> None:
        if handler is None:
            return
        plaintext = self.cipher.decrypt(payload)
        handler(json.loads(plaintext.decode("utf-8")))
