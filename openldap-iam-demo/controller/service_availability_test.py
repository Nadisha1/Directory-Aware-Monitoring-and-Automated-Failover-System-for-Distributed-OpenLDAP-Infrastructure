"""
Service availability metric for the proposal's Section 7 evaluation
("service availability" is named alongside MTTR, detection latency,
etc., and is the last of the seven listed metrics not yet measured).

WHAT THIS MEASURES THAT THE OTHER SCENARIOS DON'T:
experiment_runner.py measures the SERVER'S OWN health state (what the
controller sees when it queries each node directly). It never asks the
question a real client actually cares about: "if I had been continuously
querying the directory through the front-end during this failure, how
often would MY request have actually succeeded?" That's a different,
client-facing measurement, and it can diverge from the controller's view
in both directions:
  - HAProxy's OWN health check (independent of the controller, "check
    inter 3s fall 2 rise 2" per provisioning/controller_host.sh) may mark
    a dead backend down and reroute traffic within a few seconds — often
    FASTER than the controller's own detection latency — so availability
    may stay high even before the controller has acted.
  - Conversely, HAProxy round-robins across all 3 backends, so during
    the few seconds before ITS check catches up, roughly 1-in-3 client
    requests may still be misrouted to the bad node and fail, even
    though the other 2 nodes are perfectly fine — a real, if partial,
    client-visible availability dip that the controller's own
    node-by-node health view doesn't directly report.

This script does not re-implement fault injection: it wraps the EXACT
SAME run_once() used by the real experiments (experiment_runner.py) with
a background thread that fires a lightweight anonymous LDAP query at the
HAProxy front-end (127.0.0.1:389, the same address real clients would
use) once per second for the whole duration of the fault-to-recovered
window, and records the fraction that succeeded.

Usage (run on ctrl, same environment as the other scripts):
    sudo /opt/remediation-venv/bin/python3 service_availability_test.py --runs 10 --scenarios crash,partition,divergence

Fewer runs than the 30 used for the main experiments is a deliberate,
reasonable choice here: this metric is a simple percentage per run, not
a latency distribution needing many samples to characterise — 10 runs
per scenario already gives a stable read, and keeps the added client
query load from confounding the very fault-injection process it's
measuring around.
"""

import argparse
import csv
import subprocess
import threading
import time
from datetime import datetime, timezone

import config
from experiment_runner import run_once


def probe_once(timeout=2):
    """One lightweight, unauthenticated LDAP query against the HAProxy
    front-end on localhost — a root-DSE base search, which only requires
    the LDAP service itself to be reachable and responding, not valid
    directory content. This is deliberately the same kind of minimal
    request a load balancer health check or a client's own liveness
    check would make. Returns True/False; never raises."""
    try:
        result = subprocess.run(
            ["ldapsearch", "-x", "-H", "ldap://127.0.0.1:389",
             "-b", "", "-s", "base", "-LLL", "(objectClass=*)", "1.1"],
            timeout=timeout, capture_output=True)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


class AvailabilityProbe:
    """Background thread that probes the front-end at a fixed interval
    for as long as it's running, recording a timestamped success/failure
    and latency for every probe. Started just before fault injection,
    stopped the moment run_once returns (i.e. the whole fault-to-
    recovered window, or fault-to-timeout if remediation failed)."""

    def __init__(self, interval=1.0):
        self.interval = interval
        self.results = []
        self._stop = threading.Event()
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            ok = probe_once()
            latency_ms = (time.monotonic() - t0) * 1000
            self.results.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "success": ok, "latency_ms": round(latency_ms, 1),
            })
            self._stop.wait(self.interval)

    def start(self):
        self.results = []
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def run_scenario_with_availability(scenario_name, run_id, probe_interval):
    target = config.NODES[(run_id - 1) % len(config.NODES)]
    peers = [n for n in config.NODES if n.name != target.name]

    probe = AvailabilityProbe(interval=probe_interval)
    probe.start()
    result = run_once(scenario_name, target, peers, f"{scenario_name}{run_id}", verbose=False)
    probe.stop()

    total = len(probe.results)
    successes = sum(1 for r in probe.results if r["success"])
    availability_pct = (successes / total * 100) if total else 0.0
    successful_latencies = [r["latency_ms"] for r in probe.results if r["success"]]
    avg_latency_ms = (sum(successful_latencies) / len(successful_latencies)
                       if successful_latencies else None)

    result.update({
        "total_probes": total, "successful_probes": successes,
        "availability_pct": round(availability_pct, 2),
        "avg_successful_latency_ms": round(avg_latency_ms, 1) if avg_latency_ms else "",
    })
    return result


def main():
    parser = argparse.ArgumentParser(description="Client-facing service availability during real fault/remediation cycles")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--scenarios", type=str, default="crash,partition,divergence")
    parser.add_argument("--probe-interval", type=float, default=1.0)
    parser.add_argument("--output", type=str, default="/home/vagrant/service_availability_results.csv")
    args = parser.parse_args()
    scenario_names = args.scenarios.split(",")

    fieldnames = ["scenario", "run", "target", "cause", "outcome",
                  "detect_latency_s", "recover_latency_s", "total_mttr_s",
                  "total_probes", "successful_probes", "availability_pct",
                  "avg_successful_latency_ms"]

    all_results = {s: [] for s in scenario_names}
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for scenario_name in scenario_names:
            print(f"\n### Starting scenario: {scenario_name} ({args.runs} runs, "
                  f"client availability probing enabled) ###")
            for run_id in range(1, args.runs + 1):
                print(f"[{scenario_name} {run_id}/{args.runs}] ...", end=" ", flush=True)
                result = run_scenario_with_availability(scenario_name, run_id, args.probe_interval)
                row = {k: result.get(k, "") for k in fieldnames}
                row["scenario"] = scenario_name
                writer.writerow(row)
                f.flush()
                all_results[scenario_name].append(result)
                print(f"outcome={result.get('outcome')} "
                      f"availability={result.get('availability_pct')}% "
                      f"({result.get('successful_probes')}/{result.get('total_probes')} probes)")
                time.sleep(3)

    print(f"\n=== Service availability results ===")
    for scenario_name, results in all_results.items():
        if not results:
            continue
        pcts = [r["availability_pct"] for r in results]
        mean_pct = sum(pcts) / len(pcts)
        print(f"  {scenario_name}: mean availability = {mean_pct:.2f}% "
              f"(min={min(pcts):.1f}%, max={max(pcts):.1f}%, n={len(pcts)})")

    print(f"\nFull results written to {args.output}")


if __name__ == "__main__":
    main()
