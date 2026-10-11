"""
Cluster topology and remediation policy thresholds.

Edit NODES to match your lab. Everything else in the controller reads
from this module so there is a single source of truth.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    name: str
    host: str
    port: int = 389
    haproxy_backend_name: str = ""  # e.g. "ldap1" — must match haproxy.cfg

    def __post_init__(self):
        if not self.haproxy_backend_name:
            object.__setattr__(self, "haproxy_backend_name", self.name)


NODES = [
    Node(name="ldap1", host="192.168.56.11"),
    Node(name="ldap2", host="192.168.56.12"),
    Node(name="ldap3", host="192.168.56.13"),
]

BASE_DN = "dc=research,dc=lab"
ADMIN_DN = f"cn=admin,{BASE_DN}"
ADMIN_PW = "admin"

HAPROXY_SOCKET = "/run/haproxy/admin.sock"
HAPROXY_BACKEND = "ldap_nodes"

# --- Policy thresholds --------------------------------------------------
BIND_LATENCY_DEGRADED_MS = 200      # bind slower than this -> Degraded contribution
BIND_LATENCY_TIMEOUT_MS = 2000      # bind slower than this -> treat as unreachable
CONTEXT_CSN_DIVERGENCE_SECONDS = 30  # contextCSN timestamps further apart than this -> Divergent
POLL_INTERVAL_SECONDS = 5
CONSECUTIVE_FAILURES_TO_FENCE = 3    # avoid flapping: require repeated bad reads before fencing
CONSECUTIVE_SUCCESSES_TO_REINTEGRATE = 5

AUDIT_LOG_PATH = "/var/log/remediation-controller/audit.log"
