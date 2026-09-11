from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

from . import config as configmod
from .agents import AgentMailbox, send_agent_message
from .chat import run_chat, run_daemon
from .history import HistoryStore
from .mqttclient import RelayClient
from .presence import PeerDirectory
from .remote_actions import run_action
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
        "\nBroker — where this network's traffic relays through:\n"
        "  1) A broker you control (self-hosted, or a managed provider)\n"
        "  2) Your own HiveMQ Cloud cluster (free tier) — sign up first, then paste\n"
        "     in its connection details here — recommended if you don't want to run\n"
        "     a broker yourself but still want it private to you\n"
        "  3) HiveMQ's free public test broker — zero setup, but it's shared with the\n"
        "     whole internet: no auth, no uptime/persistence guarantee. Your content is\n"
        "     still encrypted with your passphrase, but don't rely on this for anything\n"
        "     you care about.\n"
    )
    broker_choice = input("Choice [1]: ").strip() or "1"
    if broker_choice == "2":
        print(
            "\nDon't have a cluster yet? Sign up free at https://console.hivemq.cloud,\n"
            "create a Serverless cluster, then under Access Management create a\n"
            "username/password credential. The cluster URL is on its Connection tab\n"
            "(looks like <id>.s1.<region>.hivemq.cloud).\n"
        )
        broker_host = input("HiveMQ Cloud cluster URL: ").strip()
        if not broker_host:
            print("Cluster URL is required.")
            return 1
        broker_port = 8883
        broker_tls = True
        broker_username = input("Username: ").strip()
        broker_password = getpass.getpass("Password: ").strip()
        if not broker_username or not broker_password:
            print("HiveMQ Cloud requires a username and password.")
            return 1
        print(f"Using HiveMQ Cloud cluster at {broker_host}:{broker_port} (TLS).")
    elif broker_choice == "3":
        broker_host = "mqttdashboard.com"
        broker_port = 8883
        broker_tls = True
        broker_username = ""
        broker_password = ""
        print(f"Using HiveMQ's public test broker at {broker_host}:{broker_port} (TLS).")
    else:
        broker_host = input("Broker host: ").strip()
        if not broker_host:
            print("Broker host is required.")
            return 1
        broker_port = int(input("Broker port [8883]: ").strip() or "8883")
        broker_tls = (input("Use TLS? [Y/n]: ").strip().lower() or "y") not in ("n", "no")
        broker_username = input("Broker username (blank if none): ").strip()
        broker_password = getpass.getpass("Broker password (blank if none): ").strip() if broker_username else ""

    cfg = configmod.Config(
        nickname=nickname,
        device_id=device_id,
        network_name=network_name,
        passphrase=passphrase,
        broker_host=broker_host,
        broker_port=broker_port,
        broker_tls=broker_tls,
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


def cmd_gui(args: argparse.Namespace) -> int:
    cfg = _load_config()
    try:
        from .gui import run_gui
    except (ImportError, ValueError) as exc:
        print(f"GTK4/libadwaita bindings aren't available: {exc}", file=sys.stderr)
        print("Install with: sudo pacman -S python-gobject gtk4 libadwaita", file=sys.stderr)
        return 1
    run_gui(cfg)
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    cfg = _load_config()
    run_daemon(cfg)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    cfg = _load_config()
    client = RelayClient(cfg, persistent=False)
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
    client = RelayClient(cfg, persistent=False)
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


def cmd_action(args: argparse.Namespace) -> int:
    cfg = _load_config()
    client = RelayClient(cfg, persistent=False)
    peers = PeerDirectory()
    client.on_presence = lambda device_id, data: peers.update(device_id, data)
    client.connect()
    try:
        time.sleep(1.5)
        target = peers.resolve(args.peer)
        if not target:
            print(f"error: no such peer online: {args.peer}", file=sys.stderr)
            return 1
        try:
            result = run_action(client, cfg, target, args.name, timeout=args.timeout)
        except TimeoutError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if result.get("ok"):
            if result.get("stdout"):
                print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")
            if result.get("stderr"):
                print(result["stderr"], end="" if result["stderr"].endswith("\n") else "\n", file=sys.stderr)
            return result.get("exit_code", 0)
        print(f"error: {result.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    finally:
        client.disconnect()


def cmd_agent_send(args: argparse.Namespace) -> int:
    cfg = _load_config()
    client = RelayClient(cfg, persistent=False)
    peers = PeerDirectory()
    client.on_presence = lambda device_id, data: peers.update(device_id, data)
    client.connect()
    try:
        time.sleep(1.5)
        target = peers.resolve(args.peer)
        if not target and args.peer in cfg.agents_peers:
            target = args.peer  # a trusted device that's offline: the broker holds it until they connect
        if not target:
            print(f"error: no such peer online: {args.peer}", file=sys.stderr)
            return 1
        mailbox = AgentMailbox()
        try:
            send_agent_message(client, cfg, mailbox, target, args.peer, args.text)
        finally:
            mailbox.close()
        time.sleep(0.5)
    finally:
        client.disconnect()
    return 0


def cmd_agent_inbox(args: argparse.Namespace) -> int:
    cfg = _load_config()
    mailbox = AgentMailbox()
    try:
        messages = mailbox.list(cfg.network_name, unread_only=args.unread, limit=args.limit)
        if not messages:
            print("(no agent messages)")
            return 0
        for m in messages:
            marker = "" if m["direction"] == "out" or m["read"] else " [new]"
            arrow = "->" if m["direction"] == "out" else "<-"
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["ts"]))
            print(f"{ts} {arrow} {m['peer_nick']} ({m['peer_device_id']}) [{m['id']}]{marker}\n    {m['text']}")
        if args.mark_read:
            mailbox.mark_read(cfg.network_name)
    finally:
        mailbox.close()
    return 0


def cmd_agent_trust_list(args: argparse.Namespace) -> int:
    cfg = _load_config()
    print(f"agent messaging: {'enabled' if cfg.agents_enabled else 'disabled'}")
    if not cfg.agents_peers:
        print("(no peers configured — everyone defaults to 'none', i.e. denied)")
    else:
        for device_id, level in sorted(cfg.agents_peers.items()):
            print(f"  {device_id}\t{level}")
    return 0


def cmd_agent_trust_enable(args: argparse.Namespace) -> int:
    cfg = _load_config()
    cfg.agents_enabled = True
    cfg.save()
    print("agent messaging enabled for this machine (peers still need individual trust via: omarchy-relay agent trust set)")
    return 0


def cmd_agent_trust_disable(args: argparse.Namespace) -> int:
    cfg = _load_config()
    cfg.agents_enabled = False
    cfg.save()
    print("agent messaging disabled for this machine")
    return 0


def cmd_agent_trust_set(args: argparse.Namespace) -> int:
    cfg = _load_config()
    if args.level == "none":
        cfg.agents_peers.pop(args.device_id, None)
    else:
        cfg.agents_peers[args.device_id] = args.level
    cfg.save()
    print(f"{args.device_id} -> {args.level}")
    return 0


def cmd_trust_list(args: argparse.Namespace) -> int:
    cfg = _load_config()
    print(f"remote actions: {'enabled' if cfg.remote_actions_enabled else 'disabled'}")
    if not cfg.remote_actions_peers:
        print("(no peers configured — everyone defaults to 'none', i.e. denied)")
    else:
        for device_id, level in sorted(cfg.remote_actions_peers.items()):
            print(f"  {device_id}\t{level}")
    return 0


def cmd_trust_enable(args: argparse.Namespace) -> int:
    cfg = _load_config()
    cfg.remote_actions_enabled = True
    cfg.save()
    print("remote actions enabled for this machine (peers still need individual trust via: omarchy-relay trust set)")
    return 0


def cmd_trust_disable(args: argparse.Namespace) -> int:
    cfg = _load_config()
    cfg.remote_actions_enabled = False
    cfg.save()
    print("remote actions disabled for this machine")
    return 0


def cmd_trust_set(args: argparse.Namespace) -> int:
    cfg = _load_config()
    if args.level == "none":
        cfg.remote_actions_peers.pop(args.device_id, None)
    else:
        cfg.remote_actions_peers[args.device_id] = args.level
    cfg.save()
    print(f"{args.device_id} -> {args.level}")
    return 0


def cmd_commands_list(args: argparse.Namespace) -> int:
    cfg = _load_config()
    if not cfg.remote_actions_commands:
        print("(no named actions configured)")
    else:
        for name, shell_cmd in sorted(cfg.remote_actions_commands.items()):
            print(f"  {name}\t{shell_cmd}")
    return 0


def cmd_commands_set(args: argparse.Namespace) -> int:
    cfg = _load_config()
    cfg.remote_actions_commands[args.name] = args.shell_command
    cfg.save()
    print(f"'{args.name}' -> {args.shell_command}")
    return 0


def cmd_commands_remove(args: argparse.Namespace) -> int:
    cfg = _load_config()
    if cfg.remote_actions_commands.pop(args.name, None) is None:
        print(f"no such action: {args.name}", file=sys.stderr)
        return 1
    cfg.save()
    print(f"removed '{args.name}'")
    return 0


def cmd_peers(args: argparse.Namespace) -> int:
    cfg = _load_config()
    client = RelayClient(cfg, persistent=False)
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


def cmd_search(args: argparse.Namespace) -> int:
    cfg = _load_config()
    store = HistoryStore()
    try:
        matches = store.search(cfg.network_name, args.query, limit=args.limit)
    finally:
        store.close()
    if not matches:
        print("(no messages match)")
        return 0
    for m in reversed(matches):  # oldest first, reading down like the chat
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["ts"]))
        dm = " [DM]" if m["is_dm"] else ""
        print(f"{ts}{dm} {m['nick']}: {m['text']}")
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

    p = sub.add_parser("gui", help="native GTK4/libadwaita chat window")
    p.set_defaults(func=cmd_gui)

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

    p = sub.add_parser("search", help="search the message history the GUI keeps")
    p.add_argument("query", help="text to look for, ignoring case (file names count)")
    p.add_argument("--limit", type=int, default=50, help="how many of the newest matches to show (default 50)")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("action", help="ask a peer to run a named remote action and print the result")
    p.add_argument("peer", help="nickname or device id of the peer to ask")
    p.add_argument("name", help="the action name (must exist in that peer's OWN remote_actions.commands)")
    p.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for a response (default 30)")
    p.set_defaults(func=cmd_action)

    agent_parser = sub.add_parser(
        "agent", help="free-form agent-to-agent messaging between Claude Code sessions on trusted peers (off by default)"
    )
    agent_sub = agent_parser.add_subparsers(dest="agent_command", required=True)

    p = agent_sub.add_parser("send", help="send an agent message to a peer")
    p.add_argument("peer", help="nickname or device id of the peer to message")
    p.add_argument("text", help="message text")
    p.set_defaults(func=cmd_agent_send)

    p = agent_sub.add_parser("inbox", help="show agent messages recorded locally (sent and received)")
    p.add_argument("--unread", action="store_true", help="only show unread incoming messages")
    p.add_argument("--limit", type=int, default=None, help="cap how many to show (default: all)")
    p.add_argument("--mark-read", action="store_true", help="mark all incoming messages read after showing them")
    p.set_defaults(func=cmd_agent_inbox)

    agent_trust_parser = agent_sub.add_parser(
        "trust", help="manage which peers' agents may message THIS machine's agent"
    )
    agent_trust_sub = agent_trust_parser.add_subparsers(dest="agent_trust_command", required=True)

    p = agent_trust_sub.add_parser("list", help="show current agent trust settings")
    p.set_defaults(func=cmd_agent_trust_list)

    p = agent_trust_sub.add_parser("enable", help="turn on agent messaging for this machine")
    p.set_defaults(func=cmd_agent_trust_enable)

    p = agent_trust_sub.add_parser("disable", help="turn off agent messaging for this machine")
    p.set_defaults(func=cmd_agent_trust_disable)

    p = agent_trust_sub.add_parser("set", help="grant or revoke a specific device's agent-messaging trust")
    p.add_argument("device_id", help="the peer's device id (see: omarchy-relay peers)")
    p.add_argument(
        "level",
        choices=["none", "agent"],
        help="'none' revokes access, 'agent' allows exchanging agent messages with this machine",
    )
    p.set_defaults(func=cmd_agent_trust_set)

    trust_parser = sub.add_parser(
        "trust", help="manage who may trigger remote actions on THIS machine (off by default)"
    )
    trust_sub = trust_parser.add_subparsers(dest="trust_command", required=True)

    p = trust_sub.add_parser("list", help="show current trust settings")
    p.set_defaults(func=cmd_trust_list)

    p = trust_sub.add_parser("enable", help="turn on remote actions for this machine")
    p.set_defaults(func=cmd_trust_enable)

    p = trust_sub.add_parser("disable", help="turn off remote actions for this machine")
    p.set_defaults(func=cmd_trust_disable)

    p = trust_sub.add_parser("set", help="grant or revoke a specific device's trust level")
    p.add_argument("device_id", help="the peer's device id (see: omarchy-relay peers)")
    p.add_argument(
        "level",
        choices=["none", "commands"],
        help="'none' revokes access, 'commands' allows triggering this machine's named actions",
    )
    p.set_defaults(func=cmd_trust_set)

    commands_parser = sub.add_parser(
        "commands", help="manage the named actions THIS machine will run when a trusted peer triggers them"
    )
    commands_sub = commands_parser.add_subparsers(dest="commands_command", required=True)

    p = commands_sub.add_parser("list", help="show configured named actions")
    p.set_defaults(func=cmd_commands_list)

    p = commands_sub.add_parser("set", help="add or update a named action")
    p.add_argument("name", help="the name peers will refer to")
    p.add_argument("shell_command", help="the fixed shell command run locally when triggered — never sender-supplied")
    p.set_defaults(func=cmd_commands_set)

    p = commands_sub.add_parser("remove", help="delete a named action")
    p.add_argument("name")
    p.set_defaults(func=cmd_commands_remove)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
