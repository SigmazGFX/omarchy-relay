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
    # "status" -> uptime ships pre-populated on new configs. Unlike other
    # commands, "status" runs for ANY peer on the encrypted network once
    # remote_actions.enabled is on — see _ALWAYS_ALLOWED in
    # remote_actions.py — so it doubles as a connectivity smoke test.
    # Existing configs loaded from disk aren't touched (Config.load()
    # below reads this straight from the file, not from this default), so
    # removing/renaming it sticks.
    remote_actions_commands: dict = dataclasses.field(default_factory=lambda: {"status": "uptime"})
    # Agent-to-agent messaging, off by default. Separate trust table from
    # remote_actions_peers on purpose: trusting a peer's agent to exchange
    # free-form messages with yours is a different grant than trusting it to
    # trigger your named shell commands. See agents.py.
    agents_enabled: bool = False
    agents_peers: dict = dataclasses.field(default_factory=dict)  # device_id -> "none"|"agent"
    # Display preference: show "X is online"/"X went offline" lines in the
    # chat log. Peer list/sidebar accuracy is unaffected either way.
    show_presence: bool = True
    # Read receipts: let a sender see when you've read their message.
    # Delivered receipts are sent either way.
    send_read_receipts: bool = True
    # Local message history (see history.py): shown on startup, before any
    # live traffic arrives. Either cap can be 0 for "unlimited" on that
    # dimension; both apply together when both are set.
    history_retain_count: int = 200
    history_retain_days: float = 0
    # Window lock: a fixed-size window that can't be resized, maximized, or
    # fullscreened. On Hyprland it's also floated, since a tiled window gets
    # resized by the layout no matter what size it asks for.
    lock_window_size: bool = False
    window_width: int = 900
    window_height: int = 640
    # The newest release whose notes this user has seen — "What's New" opens
    # by itself once whenever the running version is newer than this.
    last_seen_version: str = ""

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
        agents = data.get("agents", {})
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
            agents_enabled=agents.get("enabled", False),
            agents_peers=dict(agents.get("peers", {})),
            show_presence=ui.get("show_presence", True),
            send_read_receipts=ui.get("send_read_receipts", True),
            history_retain_count=ui.get("history_retain_count", 200),
            history_retain_days=ui.get("history_retain_days", 0),
            lock_window_size=ui.get("lock_window_size", False),
            window_width=ui.get("window_width", 900),
            window_height=ui.get("window_height", 640),
            last_seen_version=ui.get("last_seen_version", ""),
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

[agents]
enabled = {"true" if self.agents_enabled else "false"}

[agents.peers]
{_toml_table(self.agents_peers)}

[ui]
show_presence = {"true" if self.show_presence else "false"}
send_read_receipts = {"true" if self.send_read_receipts else "false"}
history_retain_count = {self.history_retain_count}
history_retain_days = {self.history_retain_days}
lock_window_size = {"true" if self.lock_window_size else "false"}
window_width = {self.window_width}
window_height = {self.window_height}
last_seen_version = {s(self.last_seen_version)}
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
