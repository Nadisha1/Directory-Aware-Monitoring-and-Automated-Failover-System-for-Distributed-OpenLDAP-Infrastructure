"""
Health-indicator collectors. Each function returns a plain value (or raises /
returns None on failure) so the state machine can reason about missing data
explicitly rather than guessing.
"""

import socket
import time
from datetime import datetime, timezone

import ldap

from config import Node, BASE_DN, ADMIN_DN, ADMIN_PW


def check_reachable(node: Node, timeout=1.5) -> bool:
    """TCP-level reachability of the LDAP port. Distinguishes 'host down /
    network partitioned' from 'host up but slapd unhealthy'."""
    try:
        with socket.create_connection((node.host, node.port), timeout=timeout):
            return True
    except OSError:
        return False


def check_bind_latency_ms(node: Node, timeout=2.0):
    """Times an ldapwhoami-equivalent simple bind. Returns milliseconds,
    or None if the bind failed outright (distinct from 'slow')."""
    uri = f"ldap://{node.host}:{node.port}"
    start = time.monotonic()
    try:
        conn = ldap.initialize(uri)
        conn.set_option(ldap.OPT_NETWORK_TIMEOUT, timeout)
        conn.set_option(ldap.OPT_TIMEOUT, timeout)
        conn.simple_bind_s(ADMIN_DN, ADMIN_PW)
        conn.whoami_s()
        conn.unbind_s()
        return (time.monotonic() - start) * 1000
    except ldap.LDAPError:
        return None


def get_context_csn(node: Node, timeout=2.0):
    """Reads the contextCSN operational attribute from the node's base DN.
    contextCSN encodes the timestamp of the last write the node has applied
    — comparing it across nodes is the standard way to detect replication
    divergence in OpenLDAP without parsing the full changelog."""
    uri = f"ldap://{node.host}:{node.port}"
    try:
        conn = ldap.initialize(uri)
        conn.set_option(ldap.OPT_NETWORK_TIMEOUT, timeout)
        conn.set_option(ldap.OPT_TIMEOUT, timeout)
        conn.simple_bind_s(ADMIN_DN, ADMIN_PW)
        result = conn.search_s(BASE_DN, ldap.SCOPE_BASE, attrlist=["contextCSN"])
        conn.unbind_s()
        if not result:
            return None
        _, attrs = result[0]
        values = attrs.get("contextCSN", [])
        if not values:
            return None
        # Multiple values can appear (one per originating server-id in a
        # multi-master setup); take the most recent.
        parsed = []
        for raw in values:
            raw = raw.decode() if isinstance(raw, bytes) else raw
            ts_str = raw.split("#")[0]  # format: YYYYMMDDHHMMSS.ffffffZ#...
            parsed.append(datetime.strptime(ts_str, "%Y%m%d%H%M%S.%fZ").replace(tzinfo=timezone.utc))
        return max(parsed)
    except (ldap.LDAPError, ValueError, IndexError):
        return None


def collect_node_snapshot(node: Node) -> dict:
    """One consolidated read of all health indicators for a single node."""
    reachable = check_reachable(node)
    latency_ms = check_bind_latency_ms(node) if reachable else None
    context_csn = get_context_csn(node) if latency_ms is not None else None
    return {
        "node": node.name,
        "reachable": reachable,
        "bind_latency_ms": latency_ms,
        "context_csn": context_csn,
        "sampled_at": datetime.now(timezone.utc),
    }
