"""
Main remediation loop. Polls every node, classifies state, and takes the
deterministic remediation action associated with each transition.

This is intentionally a single-process, easy-to-read control loop rather
than an event-driven system — for a dissertation experiment, readability
and reproducibility of the decision policy matter more than throughput.
"""

import time
from datetime import datetime, timezone

import config
import health
import remediation
from state_machine import NodeTracker, NodeState, cluster_summary


class Controller:
    def __init__(self, nodes=None):
        self.nodes = nodes or config.NODES
        self.trackers = {n.name: NodeTracker(name=n.name) for n in self.nodes}
        self.mttr_log = []  # list of (node, fenced_at, recovered_at) for the MTTR metric

    def poll_once(self):
        snapshots = {n.name: health.collect_node_snapshot(n) for n in self.nodes}

        transitions = {}
        for node in self.nodes:
            tracker = self.trackers[node.name]
            prev_state = tracker.state
            peer_csns = [c for name, c in
                         ((n2.name, snapshots[n2.name]["context_csn"]) for n2 in self.nodes)
                         if name != node.name and c]
            new_state = tracker.update(snapshots[node.name], peer_csns)
            if new_state != prev_state:
                transitions[node.name] = (prev_state, new_state)

        self._act_on_transitions(transitions)
        return snapshots, transitions

    def _act_on_transitions(self, transitions):
        for node_name, (prev, new) in transitions.items():
            node = next(n for n in self.nodes if n.name == node_name)
            tracker = self.trackers[node_name]

            if new == NodeState.FENCED:
                # Report WHY this node was fenced — the specific bad
                # condition (Partitioned or Divergent) that triggered it,
                # even though the remediation action (remove from routing)
                # is the same either way.
                cause = tracker.last_bad_raw_state.value if tracker.last_bad_raw_state else "unknown"
                remediation.fence_node(
                    node_name,
                    reason=f"transitioned {prev.value} -> {new.value} (cause={cause})"
                )
                self.mttr_log.append({"node": node_name,
                                       "fenced_at": datetime.now(timezone.utc),
                                       "recovered_at": None,
                                       "cause": cause})
                # Attempt a cause-specific automatic repair. If it works,
                # the node's health checks will naturally start passing
                # again on the next poll, and it'll flow through
                # Recovering -> Reintegrated on its own — no further
                # manual steps needed. If it fails (SSH unreachable), the
                # node stays safely fenced, which is the correct fallback.
                if tracker.last_bad_raw_state == NodeState.DIVERGENT:
                    remediation.trigger_resync(node, reason="divergence-triggered fence")
                elif tracker.last_bad_raw_state == NodeState.PARTITIONED:
                    remediation.attempt_auto_heal(node, reason="partition-triggered fence")

            elif new == NodeState.REINTEGRATED:
                remediation.reintegrate_node(
                    node_name,
                    reason=f"sustained health for {config.CONSECUTIVE_SUCCESSES_TO_REINTEGRATE} samples"
                )
                for entry in reversed(self.mttr_log):
                    if entry["node"] == node_name and entry["recovered_at"] is None:
                        entry["recovered_at"] = datetime.now(timezone.utc)
                        break

    def run_forever(self, interval=None):
        interval = interval or config.POLL_INTERVAL_SECONDS
        remediation.audit("CONTROLLER_START", "-", f"monitoring {[n.name for n in self.nodes]}")
        try:
            while True:
                snapshots, transitions = self.poll_once()
                self._print_status(snapshots)
                time.sleep(interval)
        except KeyboardInterrupt:
            remediation.audit("CONTROLLER_STOP", "-", "keyboard interrupt")

    def _print_status(self, snapshots):
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        parts = []
        for node in self.nodes:
            t = self.trackers[node.name]
            label = t.state.value
            if t.state == NodeState.FENCED and t.last_bad_raw_state:
                label += f"({t.last_bad_raw_state.value})"
            parts.append(f"{node.name}={label}")
        print(f"[{ts}] cluster={cluster_summary(self.trackers)} " + " ".join(parts))

    def mean_time_to_recovery(self):
        """Primary evaluation metric (proposal section 7): average of
        (recovered_at - fenced_at) across completed fence/recover cycles."""
        durations = [
            (e["recovered_at"] - e["fenced_at"]).total_seconds()
            for e in self.mttr_log if e["recovered_at"] is not None
        ]
        if not durations:
            return None
        return sum(durations) / len(durations)


if __name__ == "__main__":
    Controller().run_forever()
