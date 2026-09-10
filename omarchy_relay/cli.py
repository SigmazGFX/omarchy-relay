from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

from . import config as configmod
from .chat import run_chat, run_daemon
from .mqttclient import RelayClient
from .presence import PeerDirectory
from .transfer import send_file


def cmd_init(args: argparse.Namespace) -> int:
    if configmod.CONFIG_PATH.exists() and not args.force:
        print(f"Config already exists at {configmod.CONFIG_PATH} (use --force to overwrite)")
        return 1

    print("omarchy-relay setup\n")
    nickname = input(f"Nickname [{configmod.default_device_id()}]: ").strip() or None
    device_id = configmod.default_device_id()
    if nickname is None:
        nickname = device_id

    network_name = input("Network name (a room/group of devices) [default]: ").strip() or "default"
    passphrase = getpass.getpass("Network passphrase (shared with everyone in this network): ").strip()
    if not passphrase:
        print("A passphrase is required — it's what keeps your traffic private on a shared broker.")
        return 1

    print(
        "\nBroker: point this at an MQTT broker you control (see docker/ in the repo "
        "for a self-hosted Mosquitto setup), or a managed provider.\n"
        "This will NOT default to a public broker.\n"
    )
    broker_host = input("Broker host: ").strip()
    if not broker_host:
        print("Broker host is required.")
        return 1
    broker_port_raw = input("Broker port [8883]: ").strip() or "8883"
    broker_tls_raw = input("Use TLS? [Y/n]: ").strip().lower() or "y"
    broker_username = input("Broker username (blank if none): ").strip()
    broker_password = getpass.getpass("Broker password (blank if none): ").strip() if broker_username else ""

    cfg = configmod.Config(
        nickname=nickname,
        device_id=device_id,
        network_name=network_name,
        passphrase=passphrase,
        broker_host=broker_host,
        broker_port=int(broker_port_raw),
        broker_tls=broker_tls_raw not in ("n", "no"),
        broker_username=broker_username,
        broker_password=broker_password,
    )
    cfg.save()
    print(f"\nSaved to {configmod.CONFIG_PATH} (mode 600). Run: omarchy-relay chat")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    cfg = _load_config()
    if args.tui:
        try:
            from .tui import run_tui
        except ImportError:
            print("Textual isn't installed. Install with: sudo pacman -S python-textual", file=sys.stderr)
            return 1
        run_tui(cfg)
    else:
        run_chat(cfg)
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    cfg = _load_config()
    run_daemon(cfg)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    cfg = _load_config()
    client = RelayClient(cfg)
    peers = PeerDirectory()
    # Register before connect(): retained presence can arrive as soon as the
    # subscribe completes inside connect(), which races a callback set after.
    client.on_presence = lambda device_id, data: peers.update(device_id, data)
    client.connect()
    try:
        to = "*"
        if args.to:
            time.sleep(1.5)  # let retained presence settle so nickname resolution works
            resolved = peers.resolve(args.to)
            if not resolved:
                print(f"error: no such peer online: {args.to}", file=sys.stderr)
                return 1
            to = resolved
        transfer_id, total_chunks = send_file(client, cfg, Path(args.path), to=to)
        print(f"sent '{Path(args.path).name}' as transfer {transfer_id} ({total_chunks} chunks)")
        time.sleep(0.5)  # let paho flush the outgoing queue before disconnecting
    finally:
        client.disconnect()
    return 0


def cmd_msg(args: argparse.Namespace) -> int:
    cfg = _load_config()
    client = RelayClient(cfg)
    peers = PeerDirectory()
    client.on_presence = lambda device_id, data: peers.update(device_id, data)
    client.connect()
    try:
        import uuid

        payload = {"id": uuid.uuid4().hex, "ts": time.time(), "from": cfg.device_id, "nick": cfg.nickname, "text": args.text}
        if args.to:
            time.sleep(1.5)
            target = peers.resolve(args.to)
            if not target:
                print(f"error: no such peer online: {args.to}", file=sys.stderr)
                return 1
            client.send_dm(target, payload)
        else:
            client.send_chat(payload)
        time.sleep(0.5)
    finally:
        client.disconnect()
    return 0


def cmd_peers(args: argparse.Namespace) -> int:
    cfg = _load_config()
    client = RelayClient(cfg)
    peers = PeerDirectory()
    client.on_presence = lambda device_id, data: peers.update(device_id, data)
    client.connect()
    time.sleep(args.wait)
    snapshot = peers.snapshot()
    client.disconnect()
    if not snapshot:
        print("(nobody else is online)")
        return 0
    for device_id, data in sorted(snapshot.items(), key=lambda kv: kv[1].get("nick", "")):
        print(f"{data.get('nick', '?')}\t{device_id}")
    return 0


def _load_config() -> configmod.Config:
    try:
        return configmod.Config.load()
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omarchy-relay", description="Realtime chat/file relay between Omarchy machines over MQTT.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="interactive setup wizard")
    p.add_argument("--force", action="store_true", help="overwrite existing config")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("chat", help="interactive chat session")
    p.add_argument("--tui", action="store_true", help="use the full-screen Textual UI")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("daemon", help="headless listener: prints chat, auto-saves incoming files")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("send", help="send a file")
    p.add_argument("path", help="path to the file")
    p.add_argument("--to", help="nickname or device id to DM (default: broadcast to everyone)")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("msg", help="send a one-off message without an interactive session")
    p.add_argument("text", help="message text")
    p.add_argument("--to", help="nickname or device id to DM (default: broadcast)")
    p.set_defaults(func=cmd_msg)

    p = sub.add_parser("peers", help="list who's currently online")
    p.add_argument("--wait", type=float, default=1.5, help="seconds to wait for presence to arrive (default 1.5)")
    p.set_defaults(func=cmd_peers)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
