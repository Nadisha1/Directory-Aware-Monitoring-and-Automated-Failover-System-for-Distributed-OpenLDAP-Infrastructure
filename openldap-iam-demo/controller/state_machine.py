"""
Directory-state-aware classification.

This is the core research artifact: a decision policy driven by MULTIPLE
health indicators (reachability, bind latency, replication freshness)
rather than a single metric such as "is the process running".

States (per proposal section 5 / 11):
    HEALTHY       - reachable, fast bind, contextCSN in sync with peers
    DEGRADED      - reachable but slow bind (approaching threshold)
    PARTITIONED   - unreachable at the TCP level (network / node down)
    DIVERGENT     - reachable, but contextCSN has drifted from peers beyond
                    the tolerance window (replication lag/failure)
    FENCED        - controller has deliberately removed the node from
                    traffic pending investigation/recovery. Both a sustained
                    PARTITIONED condition and a sustained DIVERGENT condition
                    lead here (removing traffic is the correct remediation
                    action either way) — but the underlying cause is
                    preserved on the tracker (last_bad_raw_state) so audit
                    logs and reports can distinguish WHY a node was fenced.
    RECOVERING    - node has been reintroduced to the network/service but
                    has not yet demonstrated sustained health
    REINTEGRATED  - recovered node has passed the hysteresis window and is
                    back in normal rotation
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

import config


class NodeState(str, Enum):
    HEALTHY = "Healthy"
    DEGRADED = "Degraded"
    PARTITIONED = "Partitioned"
    DIVERGENT = "Divergent"
    FENCED = "Fenced"
    RECOVERING = "Recovering"
    REINTEGRATED = "Reintegrated"


@dataclass
class NodeTracker:
    """Per-node hysteresis counters so a single bad sample doesn't trigger
    fencing, and a single good sample doesn't trigger reintegration."""
    name: str
    state: NodeState = NodeState.HEALTHY
    consecutive_bad: int = 0
    consecutive_good: int = 0
    last_snapshot: dict = field(default_factory=dict)
    fenced_at: datetime = None
    # Most recent "bad" raw classification (PARTITIONED or DIVERGENT) seen
    # for this node. Updated on every bad sample, so once the node is
    # FENCED this reflects the specific condition that caused it.
    last_bad_raw_state: NodeState = None

    def classify_sample(self, snapshot: dict, peer_csns: list) -> NodeState:
        """Classify a single raw snapshot in isolation (before hysteresis)."""
        if not snapshot["reachable"]:
            return NodeState.PARTITIONED

        if snapshot["bind_latency_ms"] is None:
            # Reachable at TCP level but LDAP bind failed/timed out —
            # treat conservatively as partitioned (service-level outage).
            return NodeState.PARTITIONED

        if snapshot["context_csn"] and peer_csns:
            newest_peer = max(peer_csns)
            drift = abs((newest_peer - snapshot["context_csn"]).total_seconds())
            if drift > config.CONTEXT_CSN_DIVERGENCE_SECONDS:
                return NodeState.DIVERGENT

        if snapshot["bind_latency_ms"] > config.BIND_LATENCY_DEGRADED_MS:
            return NodeState.DEGRADED

        return NodeState.HEALTHY

    def update(self, snapshot: dict, peer_csns: list) -> NodeState:
        """Apply hysteresis: only transition into a fenced-worthy state after
        CONSECUTIVE_FAILURES_TO_FENCE bad samples in a row, and only clear
        RECOVERING -> REINTEGRATED after CONSECUTIVE_SUCCESSES_TO_REINTEGRATE
        good samples. This is what keeps the controller from flapping a node
        in and out of rotation on transient blips."""
        self.last_snapshot = snapshot
        raw_state = self.classify_sample(snapshot, peer_csns)
        is_bad = raw_state in (NodeState.PARTITIONED, NodeState.DIVERGENT)

        if is_bad:
            self.consecutive_bad += 1
            self.consecutive_good = 0
            # Remember which specific bad condition this was — this is what
            # lets the FENCE audit log report "cause=Divergent" vs
            # "cause=Partitioned" even though both collapse to FENCED.
            self.last_bad_raw_state = raw_state
        else:
            self.consecutive_good += 1
            self.consecutive_bad = 0

        if self.state == NodeState.FENCED:
            # Fenced nodes only move to RECOVERING once they look healthy
            # again at all; actual reintegration is a deliberate action.
            if not is_bad:
                self.state = NodeState.RECOVERING
        elif self.state == NodeState.RECOVERING:
            if is_bad:
                self.state = NodeState.FENCED  # relapsed — back to fenced
            elif self.consecutive_good >= config.CONSECUTIVE_SUCCESSES_TO_REINTEGRATE:
                self.state = NodeState.REINTEGRATED
        else:
            if is_bad and self.consecutive_bad >= config.CONSECUTIVE_FAILURES_TO_FENCE:
                self.state = NodeState.FENCED
            elif not is_bad:
                self.state = raw_state
            # else: bad but under threshold — hold previous displayed state
            # to avoid flapping the dashboard/log on a single blip.

        return self.state


def cluster_summary(trackers: dict) -> str:
    """Rolls per-node states up into one cluster-level label for reporting."""
    states = {t.state for t in trackers.values()}
    if states == {NodeState.HEALTHY}:
        return "Healthy"
    if NodeState.FENCED in states or NodeState.PARTITIONED in states:
        healthy_count = sum(1 for t in trackers.values() if t.state == NodeState.HEALTHY)
        if healthy_count == 0:
            return "Critical"
        return "Degraded (fenced/partitioned node present)"
    if NodeState.DIVERGENT in states:
        return "Degraded (replication divergence detected)"
    if NodeState.RECOVERING in states:
        return "Recovering"
    return "Degraded"
