"""Remote-triggered actions.

A peer can ask this machine to run one of a fixed set of *locally
authored* commands. The command string that actually executes always
comes from this machine's own config (remote_actions.commands) — a
requester can only pick a name to look up, never supply code to run. This
is closer to a webhook triggering a predefined script than to a remote
shell.

Off by default (remote_actions.enabled = false). Even when enabled, a
peer not explicitly listed in remote_actions.peers as "commands" is
refused — the default trust level for any unlisted device_id is "none" —
except for the "status" action (see _ALWAYS_ALLOWED below), which any
peer on the encrypted network may run without being listed, so it can
double as a connectivity smoke test. The receiving machine's own config
is always the sole authority over what runs on it; nothing about how a
request is granted can be influenced by the sender.
"""
from __future__ import annotations

import subprocess
import time
import uuid
from typing import Callable, Optional

_MAX_OUTPUT = 8192
_RUN_TIMEOUT = 20

# Actions any peer may trigger regardless of remote_actions.peers trust
# level — still gated by remote_actions.enabled and by the action having
# to be present in remote_actions.commands. "status" ships pre-populated
# (config.py) specifically so it works as an out-of-the-box "is the
# remote comms tool working" probe.
_ALWAYS_ALLOWED = {"status"}


def _truncate(s: str) -> str:
    if len(s) <= _MAX_OUTPUT:
        return s
    return s[:_MAX_OUTPUT] + f"\n...[truncated, {len(s) - _MAX_OUTPUT} more bytes]"


class RemoteActionHandler:
    """Receiver side: decides whether to run a requested action and replies."""

    def __init__(self, cfg, on_handled: Optional[Callable[[dict, str], None]] = None):
        self.cfg = cfg
        # (request_obj, outcome_str) -> None — for UI/log lines, e.g. "bob ran 'status'"
        self.on_handled = on_handled

    def trust_level(self, device_id: str) -> str:
        return self.cfg.remote_actions_peers.get(device_id, "none")

    def handle_request(self, client, obj: dict) -> None:
        requester = obj.get("from", "")
        request_id = obj.get("request_id", "")

        if request_id and client.pending_actions.seen(request_id):
            return  # duplicate delivery (MQTT QoS 1 redelivery) — already handled once

        result = {"type": "action_result", "request_id": request_id, "ts": time.time(), "from": self.cfg.device_id}

        if not self.cfg.remote_actions_enabled:
            result.update(ok=False, error="remote actions are disabled on this machine")
            self._reply(client, requester, result, obj, "disabled")
            return

        name = obj.get("action", "")
        if name not in _ALWAYS_ALLOWED and self.trust_level(requester) != "commands":
            result.update(ok=False, error="no remote-action permission granted to this device")
            self._reply(client, requester, result, obj, "denied")
            return

        shell_cmd = self.cfg.remote_actions_commands.get(name)
        if shell_cmd is None:
            result.update(ok=False, error=f"no such action: {name!r}")
            self._reply(client, requester, result, obj, "unknown-action")
            return

        try:
            proc = subprocess.run(shell_cmd, shell=True, capture_output=True, text=True, timeout=_RUN_TIMEOUT)
            result.update(ok=True, exit_code=proc.returncode, stdout=_truncate(proc.stdout), stderr=_truncate(proc.stderr))
            self._reply(client, requester, result, obj, "ok")
        except subprocess.TimeoutExpired:
            result.update(ok=False, error=f"'{name}' timed out after {_RUN_TIMEOUT}s")
            self._reply(client, requester, result, obj, "timeout")
        except Exception as exc:  # keep the listener alive regardless of what the command does
            result.update(ok=False, error=str(exc))
            self._reply(client, requester, result, obj, "error")

    def _reply(self, client, requester: str, result: dict, request_obj: dict, outcome: str) -> None:
        if requester:
            client.send_action_result(requester, result)
        if self.on_handled:
            self.on_handled(request_obj, outcome)


def run_action(client, cfg, target_device_id: str, action: str, timeout: float = _RUN_TIMEOUT + 10) -> dict:
    """Sender side: request a named action and block for the result."""
    request_id = uuid.uuid4().hex
    event = client.pending_actions.register(request_id)
    client.send_action_request(
        target_device_id,
        {
            "type": "action_request",
            "request_id": request_id,
            "ts": time.time(),
            "from": cfg.device_id,
            "nick": cfg.nickname,
            "action": action,
        },
    )
    if not event.wait(timeout=timeout):
        client.pending_actions.pop_result(request_id)
        raise TimeoutError(f"no response from {target_device_id} within {timeout}s")
    return client.pending_actions.pop_result(request_id)
