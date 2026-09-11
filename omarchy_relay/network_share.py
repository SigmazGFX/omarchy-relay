"""The shareable subset of a Config: network + broker only, never identity.

Used by Settings' Export/Import so someone can hand a file to a peer and
have them join the same network with almost no manual entry. Deliberately
excludes nickname/device_id — baking in your own device_id would make the
importer's client collide with yours on presence and the remote-actions
trust table.
"""
from __future__ import annotations

import tomllib

from .config import _toml_str


def build_export_toml(
    network_name: str,
    passphrase: str,
    broker_host: str,
    broker_port: int,
    broker_tls: bool,
    broker_username: str,
    broker_password: str,
) -> str:
    s = _toml_str
    return f"""\
# omarchy-relay network settings — exported from Settings, ready to import.
# Contains no device identity, just what's needed to join this network.
# Keep this file private, it contains the network passphrase and any broker
# credentials.

[network]
name = {s(network_name)}
passphrase = {s(passphrase)}

[broker]
host = {s(broker_host)}
port = {broker_port}
tls = {"true" if broker_tls else "false"}
username = {s(broker_username)}
password = {s(broker_password)}
"""


def parse_export(text: str) -> dict:
    """Returns a dict with network_name/passphrase/broker_host/broker_port/
    broker_tls/broker_username/broker_password. Raises ValueError if the
    file doesn't have what's needed to join a network."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"not a valid settings file: {exc}") from exc
    network = data.get("network", {})
    broker = data.get("broker", {})
    name = network.get("name", "")
    passphrase = network.get("passphrase", "")
    host = broker.get("host", "")
    if not name or not passphrase:
        raise ValueError("file is missing network name/passphrase")
    if not host:
        raise ValueError("file is missing broker.host")
    return {
        "network_name": name,
        "passphrase": passphrase,
        "broker_host": host,
        "broker_port": broker.get("port", 8883),
        "broker_tls": broker.get("tls", True),
        "broker_username": broker.get("username", ""),
        "broker_password": broker.get("password", ""),
    }
