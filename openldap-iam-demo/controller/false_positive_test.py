"""
False-positive test for the dissertation's evaluation section.

The proposal (section 7) commits to reporting a "false-positive
remediation" rate as a secondary metric. This is the only place that
metric is actually measured: the OTHER experiments (experiment_runner.py)
all deliberately inject a real fault, so success on them tells you the
controller detects real problems, not that it leaves healthy nodes alone.

This script does the opposite: it leaves the cluster completely
untouched — no injected faults of any kind — and just runs the SAME
Controller class the real monitor uses, for an extended period, logging
every single poll. Anything other than all three nodes staying HEALTHY
the entire time is evidence of a false positive.

Two levels are reported, since they mean different things:
  - "raw blips": a single bad sample (PARTITIONED/DIVERGENT) BEFORE
    hysteresis is applied. These represent transient noise the hysteresis
    policy is specifically designed to absorb (e.g. one slow bind under
    momentary VM scheduler contention) — some raw blips with ZERO actual
    fences is a sign the hysteresis policy is doing its job, not a
    failure.
  - "false fences": a node actually reaching FENCED with no fault
    injected. This is the real false-positive metric for the proposal —
    it means client traffic was needlessly routed away from a healthy
    node. This should be exactly zero.

Usage (same environment as cli.py / experiment_runner.py):
    sudo /opt/remediation-venv/bin/python3 false_positive_test.py --minutes 60
    sudo /opt/remediation-venv/bin/python3 false_positive_test.py --minutes 60 --output /home/vagrant/fp_test.csv

IMPORTANT: do not run this at the same time as experiment_runner.py, and
make sure no scenario script / manual fault injection is active — any
real fault during this window invalidates the run (it's testing for
FALSE positives specifically, not general detection accuracy).
"""

import argparse
import csv
import time
from datetime import datetime, timezone

import config
from controller import Controller
from state_machine import NodeState


def main():
    parser = argparse.ArgumentParser(description="False-positive fencing test against an untouched, healthy cluster")
    parser.add_argument("--minutes", type=float, default=60.0,
                         help="how long to passively monitor (default 60 minutes)")
    parser.add_argument("--output", type=str, default="/home/vagrant/false_positive_test.csv")
    args = parser.parse_args()

    ctrl = Controller()
    fieldnames = ["timestamp", "node", "reachable", "bind_latency_ms", "context_csn",
                  "raw_state", "consecutive_bad", "consecutive_good", "state"]

    deadline = time.monotonic() + args.minutes * 60
    poll_count = 0
    raw_blip_count = {n.name: 0 for n in config.NODES}
    fence_events = []  # (timestamp, node_name) — the metric that actually matters

    print(f"### False-positive test: monitoring untouched cluster for {args.minutes:.1f} minutes ###")
    print("Do NOT inject any faults during this run.\n")

    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        while time.monotonic() < deadline:
            snapshots, transitions = ctrl.poll_once()
            now = datetime.now(timezone.utc).isoformat()
            poll_count += 1

            for node in config.NODES:
                tr = ctrl.trackers[node.name]
                snap = snapshots[node.name]
                peer_csns = [snapshots[p.name]["context_csn"] for p in config.NODES
                             if p.name != node.name and snapshots[p.name]["context_csn"]]
                raw_state = tr.classify_sample(snap, peer_csns)

                if raw_state != NodeState.HEALTHY and raw_state != NodeState.DEGRADED:
                    raw_blip_count[node.name] += 1
                elif raw_state == NodeState.DEGRADED:
                    raw_blip_count[node.name] += 1  # still worth counting, just not fence-eligible

                if tr.state == NodeState.FENCED and (not fence_events or fence_events[-1][1] != node.name):
                    # Newly fenced (avoid re-logging the same ongoing fence every poll)
                    fence_events.append((now, node.name))
                    print(f"  [!!] FALSE FENCE: {node.name} fenced at {now} with NO injected fault "
                          f"(last_bad_raw_state={tr.last_bad_raw_state})")

                writer.writerow({
                    "timestamp": now, "node": node.name,
                    "reachable": snap["reachable"], "bind_latency_ms": snap["bind_latency_ms"],
                    "context_csn": snap["context_csn"], "raw_state": raw_state.value,
                    "consecutive_bad": tr.consecutive_bad, "consecutive_good": tr.consecutive_good,
                    "state": tr.state.value,
                })
            f.flush()

            if poll_count % 24 == 0:  # ~every 2 minutes at 5s poll interval
                elapsed_min = (args.minutes * 60 - (deadline - time.monotonic())) / 60
                print(f"  ... {elapsed_min:.1f} / {args.minutes:.1f} min elapsed, "
                      f"{poll_count} polls, {sum(raw_blip_count.values())} raw blips so far, "
                      f"{len(fence_events)} false fences so far")

            time.sleep(config.POLL_INTERVAL_SECONDS)

    total_node_polls = poll_count * len(config.NODES)
    total_blips = sum(raw_blip_count.values())
    false_positive_rate = len(fence_events) / total_node_polls if total_node_polls else 0

    print(f"\n=== False-positive test complete ===")
    print(f"Duration: {args.minutes:.1f} minutes, {poll_count} poll cycles, "
          f"{total_node_polls} total node-polls")
    print(f"Raw blips (pre-hysteresis, absorbed by policy): {total_blips} "
          f"({dict(raw_blip_count)})")
    print(f"False fences (post-hysteresis, the metric that matters): {len(fence_events)}")
    print(f"False-positive rate: {len(fence_events)}/{total_node_polls} = {false_positive_rate:.4%}")
    if fence_events:
        print("Fence events:")
        for ts, name in fence_events:
            print(f"  {ts}  {name}")
    print(f"\nFull per-poll log written to {args.output}")


if __name__ == "__main__":
    main()
