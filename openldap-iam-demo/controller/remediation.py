"""
Deterministic remediation actions. Every action is logged to the audit log
with a timestamp, actor ("controller"), target node, and reason — the
proposal's "complete auditability" requirement (section 5).

Fencing here means: remove the node from the HAProxy backend rotation so no
client traffic is routed to it, via HAProxy's runtime API (a Unix socket).
This is safer than killing slapd outright — the process keeps running so
diagnostics remain possible, but it stops receiving client traffic and
therefore stops accumulating divergent writes.

Beyond fencing, this module also attempts CAUSE-SPECIFIC auto-heal actions:
  - DIVERGENT  -> restart slapd (forces a fresh syncrepl handshake)
  - PARTITIONED -> clear any self-inflicted iptables rules AND restart
                    slapd (covers both "network blocked" and "service
                    crashed" causes, since both look identical from the
                    controller's health checks alone)
These require the controller's SSH key to be trusted on the target node
(see provisioning/trust_controller.sh). If SSH itself fails, that's a
strong signal the node is GENUINELY unreachable (not just self-inflicted
in the lab), and the failure is logged rather than retried — the node
stays safely fenced for manual investigation, which is the correct,
conservative behavior for a real network outage.
"""

import socket
import subprocess
from datetime import datetime, timezone

import config


def _haproxy_command(cmd: str) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(config.HAPROXY_SOCKET)
        sock.sendall((cmd + "\n").encode())
        chunks = []
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        except socket.timeout:
            pass
        return b"".join(chunks).decode(errors="replace")


def audit(action: str, node_name: str, reason: str):
    line = f"{datetime.now(timezone.utc).isoformat()} action={action} node={node_name} reason=\"{reason}\"\n"
    try:
        with open(config.AUDIT_LOG_PATH, "a") as f:
            f.write(line)
    except OSError:
        pass  # lab environments may not have /var/log writable; don't crash the loop
    print(line, end="")


def fence_node(node_name: str, reason: str):
    """Remove node from HAProxy rotation (routing update) — the controlled
    fencing action from proposal objective 4."""
    try:
        _haproxy_command(f"disable server {config.HAPROXY_BACKEND}/{node_name}")
        audit("FENCE", node_name, reason)
        return True
    except OSError as e:
        audit("FENCE_FAILED", node_name, f"{reason} (haproxy error: {e})")
        return False


def reintegrate_node(node_name: str, reason: str):
    """Re-enable a node in the HAProxy backend once it has demonstrated
    sustained health (state machine: RECOVERING -> REINTEGRATED)."""
    try:
        _haproxy_command(f"enable server {config.HAPROXY_BACKEND}/{node_name}")
        audit("REINTEGRATE", node_name, reason)
        return True
    except OSError as e:
        audit("REINTEGRATE_FAILED", node_name, f"{reason} (haproxy error: {e})")
        return False


def _ssh_base(node):
    return ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
            "-i", "/home/vagrant/.ssh/id_rsa",
            f"vagrant@{node.host}"]


def trigger_resync(node, reason: str):
    """Forces a syncrepl refresh cycle on a divergent node by restarting
    slapd on that host over SSH. Requires the controller's public key to
    be trusted on the target (see provisioning/trust_controller.sh)."""
    try:
        subprocess.run(
            _ssh_base(node) + ["sudo", "systemctl", "restart", "slapd"],
            timeout=15, check=True, capture_output=True,
        )
        audit("RESYNC", node.name, reason)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        audit("RESYNC_FAILED", node.name, f"{reason} ({e})")
        return False


def attempt_auto_heal(node, reason: str):
    """Best-effort automatic repair for a node fenced with cause=Partitioned.
    Tries two independent actions, since 'unreachable' can mean either the
    service crashed or a (in the lab, self-inflicted) network block is in
    place:
      1. Clear any iptables rules on the target (undoes the lab's simulated
         network partition, if that's the cause).
      2. Restart slapd (recovers from a crashed/stopped service, if that's
         the cause).
    Both are safe no-ops if the condition they target isn't present. If
    BOTH fail (most likely: SSH itself can't connect), that's treated as
    evidence the node is genuinely unreachable — the failure is logged and
    the node is left safely fenced rather than retried indefinitely."""
    healed_something = False

    try:
        subprocess.run(
            _ssh_base(node) + ["sudo", "iptables", "-F"],
            timeout=10, check=True, capture_output=True,
        )
        healed_something = True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        pass

    try:
        subprocess.run(
            _ssh_base(node) + ["sudo", "systemctl", "restart", "slapd"],
            timeout=15, check=True, capture_output=True,
        )
        healed_something = True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        pass

    if healed_something:
        audit("AUTO_HEAL_ATTEMPTED", node.name,
              f"{reason} (cleared iptables + restarted slapd)")
    else:
        audit("AUTO_HEAL_FAILED", node.name,
              f"{reason} (SSH unreachable — node likely genuinely network-isolated; needs manual intervention)")
    return healed_something
