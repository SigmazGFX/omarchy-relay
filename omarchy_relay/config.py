from __future__ import annotations

import dataclasses
import os
import socket
import tomllib
import uuid
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omarchy-relay"
CONFIG_PATH = CONFIG_DIR / "config.toml"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "omarchy-relay"


def default_device_id() -> str:
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"


@dataclasses.dataclass
class Config:
    nickname: str
    device_id: str
    network_name: str
    passphrase: str
    broker_host: str
    broker_port: int = 8883
    broker_tls: bool = True
    broker_username: str = ""
    broker_password: str = ""
    chunk_size: int = 65536
    max_file_size: int = 26_214_400
    downloads_dir: str = str(Path.home() / "Downloads" / "omarchy-relay")
    # Remote-triggered actions, off by default. A peer can only ever
    # trigger a name from this machine's own fixed-string command table
    # below — never send text that gets run. See remote_actions.py.
    remote_actions_enabled: bool = False
    remote_actions_peers: dict = dataclasses.field(default_factory=dict)  # device_id -> "none"|"commands"
    remote_actions_commands: dict = dataclasses.field(default_factory=dict)  # name -> fixed shell string
    # Display preference: show "X is online"/"X went offline" lines in the
    # chat log. Peer list/sidebar accuracy is unaffected either way.
    show_presence: bool = True
    # Local message history (see history.py): shown on startup, before any
    # live traffic arrives. Either cap can be 0 for "unlimited" on that
    # dimension; both apply together when both are set.
    history_retain_count: int = 200
    history_retain_days: float = 0

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        if not path.exists():
            raise FileNotFoundError(f"No config at {path}. Run: omarchy-relay init")
        with open(path, "rb") as f:
            data = tomllib.load(f)
        identity = data.get("identity", {})
        network = data.get("network", {})
        broker = data.get("broker", {})
        transfer = data.get("transfer", {})
        remote_actions = data.get("remote_actions", {})
        ui = data.get("ui", {})
        cfg = cls(
            nickname=identity.get("nickname") or socket.gethostname(),
            device_id=identity.get("device_id") or default_device_id(),
            network_name=network.get("name", ""),
            passphrase=network.get("passphrase", ""),
            broker_host=broker.get("host", ""),
            broker_port=broker.get("port", 8883),
            broker_tls=broker.get("tls", True),
            broker_username=broker.get("username", ""),
            broker_password=broker.get("password", ""),
            chunk_size=transfer.get("chunk_size", 65536),
            max_file_size=transfer.get("max_file_size", 26_214_400),
            downloads_dir=transfer.get("downloads_dir", str(Path.home() / "Downloads" / "omarchy-relay")),
            remote_actions_enabled=remote_actions.get("enabled", False),
            remote_actions_peers=dict(remote_actions.get("peers", {})),
            remote_actions_commands=dict(remote_actions.get("commands", {})),
            show_presence=ui.get("show_presence", True),
            history_retain_count=ui.get("history_retain_count", 200),
            history_retain_days=ui.get("history_retain_days", 0),
        )
        if not cfg.broker_host:
            raise ValueError(
                "broker.host is empty in the config. Point it at a broker you control "
                "(see docker/ for a self-hosted Mosquitto setup, or a managed MQTT "
                "provider) — omarchy-relay will not default to a public broker."
            )
        if not cfg.network_name or not cfg.passphrase:
            raise ValueError("network.name and network.passphrase must both be set. Run: omarchy-relay init")
        return cfg

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._to_toml())
        path.chmod(0o600)

    def _to_toml(self) -> str:
        s = _toml_str
        return f"""\
# omarchy-relay config — keep this file private (chmod 600 already applied),
# it contains your network passphrase and any broker credentials.

[identity]
nickname = {s(self.nickname)}
device_id = {s(self.device_id)}

[network]
name = {s(self.network_name)}
passphrase = {s(self.passphrase)}

[broker]
host = {s(self.broker_host)}
port = {self.broker_port}
tls = {"true" if self.broker_tls else "false"}
username = {s(self.broker_username)}
password = {s(self.broker_password)}

[transfer]
chunk_size = {self.chunk_size}
max_file_size = {self.max_file_size}
downloads_dir = {s(self.downloads_dir)}

[remote_actions]
enabled = {"true" if self.remote_actions_enabled else "false"}

[remote_actions.peers]
{_toml_table(self.remote_actions_peers)}

[remote_actions.commands]
{_toml_table(self.remote_actions_commands)}

[ui]
show_presence = {"true" if self.show_presence else "false"}
history_retain_count = {self.history_retain_count}
history_retain_days = {self.history_retain_days}
"""


def _toml_str(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_table(table: dict) -> str:
    return "\n".join(f"{_toml_str(str(k))} = {_toml_str(str(v))}" for k, v in table.items())
