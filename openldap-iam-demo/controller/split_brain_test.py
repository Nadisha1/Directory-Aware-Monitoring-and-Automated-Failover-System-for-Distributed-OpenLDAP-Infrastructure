"""
Split-brain scenario for the proposal's Section 6 methodology (a fourth
controlled-experiment failure type) and Section 7's "split-brain
prevention rate" evaluation metric.

WHY THIS IS A DIFFERENT KIND OF TEST than crash/partition/divergence:
Those three all measure DETECTION AND REMEDIATION SPEED — how fast the
controller notices something's wrong and fixes it. Split-brain measures
something else entirely: DATA CORRECTNESS. Here, TWO masters both stay
fully writable while cut off from each other, and each receives a
DIFFERENT, CONFLICTING value for the SAME attribute on the SAME entry —
the classic split-brain condition. When the partition heals, OpenLDAP's
own multi-master conflict resolution (CSN-timestamp-based, generally
last-write-wins) has to reconcile them. The question this test answers
is not "how fast" but "does every node end up agreeing on ONE final
value, or can the cluster silently retain inconsistent data forever?"
That is what "split-brain prevention rate" actually means, and it is not
something the other three scenarios exercise at all.

MECHANISM:
- Reuses the exact same isolation technique as inject_divergence in
  experiment_runner.py (proven working): the target node is cut off from
  its two peers via iptables, while the controller itself can still
  reach it directly. This is what lets us write to the "isolated" side
  independently of the "cluster" side during the split.
- A single, STABLE, pre-created entry (not a fresh one per run, unlike
  the other scenarios' throwaway markers) is used across every run, to
  model a realistic recurring conflict on a real piece of directory data
  (e.g. two admins editing the same user's phone number at once) rather
  than a synthetic one-off.
- During isolation, a conflicting write goes to a peer (representing
  "the rest of the cluster") and a different conflicting write goes to
  the isolated node itself (representing "the split side"), then the
  partition heals and syncrepl is given time to reconcile.
- The controller (Controller class) polls throughout, purely to record
  whether it detected the anomaly — that's supplementary information,
  not the primary metric here.

METRIC: after healing + a settle period, the SAME attribute is read back
from ALL THREE nodes. "Consistent" means all three agree on one value.
Anything else — even a 2-vs-1 split, let alone three different values —
is a split-brain PREVENTION FAILURE and is recorded as such, not hidden.

Usage:
    sudo /opt/remediation-venv/bin/python3 split_brain_test.py --runs 30 --output /home/vagrant/split_brain_results.csv
"""

import argparse
import csv
import time
import uuid
from datetime import datetime, timezone

import config
from controller import Controller
from state_machine import NodeState
from experiment_runner import ssh_exec, ssh_exec_stdin, ssh_exec_capture

TEST_DN = f"cn=split-brain-test,{config.BASE_DN}"


def ensure_test_entry_exists():
    """Creates the shared conflict-test entry once, tolerant of it already
    existing from a prior run (ldapadd failing with 'already exists' is
    expected and fine — this call is intentionally best-effort)."""
    ldif = (f"dn: {TEST_DN}\nobjectClass: organizationalRole\n"
            f"cn: split-brain-test\ndescription: baseline\n")
    ssh_exec_stdin(config.NODES[0].host,
                   f"ldapadd -x -D {config.ADMIN_DN} -w {config.ADMIN_PW}", ldif)


def write_conflicting_value(host, side_label, run_tag):
    """Writes a distinctly-tagged value to the shared entry's `description`
    attribute on `host`. Returns the value written, or None on failure."""
    value = f"{side_label}-{run_tag}-{uuid.uuid4().hex[:6]}"
    ldif = f"dn: {TEST_DN}\nchangetype: modify\nreplace: description\ndescription: {value}\n"
    ok = ssh_exec_stdin(host, f"ldapmodify -x -D {config.ADMIN_DN} -w {config.ADMIN_PW}", ldif)
    return value if ok else None


def read_value(host):
    """Reads the current `description` value on `host`, or None on failure."""
    ok, out = ssh_exec_capture(
        host, "ldapsearch", "-x", "-D", config.ADMIN_DN, "-w", config.ADMIN_PW,
        "-b", TEST_DN, "-s", "base", "-LLL", "description")
    if not ok:
        return None
    for line in out.splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return None


def isolate(node, peers):
    """Same technique as inject_divergence: cuts `node` off from its peers
    only — the controller (and our direct writes to `node`) still work."""
    for peer in peers:
        ssh_exec(node.host, "sudo", "iptables", "-A", "INPUT", "-s", peer.host,
                  "-p", "tcp", "--dport", "389", "-j", "REJECT")
        ssh_exec(node.host, "sudo", "iptables", "-A", "OUTPUT", "-d", peer.host,
                  "-p", "tcp", "--dport", "389", "-j", "REJECT")


def heal(node):
    ssh_exec(node.host, "sudo", "iptables", "-F")
    ssh_exec(node.host, "sudo", "systemctl", "restart", "slapd")


def run_once(run_id, isolated, peers, settle_s):
    run_tag = f"sb{run_id}-{uuid.uuid4().hex[:6]}"
    ctrl = Controller()
    anomaly_detected = False

    isolate(isolated, peers)
    t_isolate = datetime.now(timezone.utc)

    # Conflicting writes: one to a connected peer ("cluster" side), one
    # directly to the isolated node ("isolated" side). Neither side can
    # see the other's write yet — that's the split-brain condition.
    #
    # ORDER ALTERNATES BY RUN: if the isolated side always wrote SECOND,
    # it would deterministically win every time under CSN-timestamp-based
    # conflict resolution — that would only confirm timestamp ordering
    # works, not genuinely test split-brain resolution in both
    # directions. Alternating which side writes last means roughly half
    # the runs expect "cluster" to win and half expect "isolated" to
    # win; a real split-brain-prevention failure is a run where the
    # actual winner doesn't match either write, or where the three nodes
    # disagree entirely — not merely "the expected side won."
    isolated_writes_last = (run_id % 2 == 1)
    if isolated_writes_last:
        cluster_value = write_conflicting_value(peers[0].host, "cluster", run_tag)
        time.sleep(1)
        isolated_value = write_conflicting_value(isolated.host, "isolated", run_tag)
        expected_winner = "isolated"
    else:
        isolated_value = write_conflicting_value(isolated.host, "isolated", run_tag)
        time.sleep(1)
        cluster_value = write_conflicting_value(peers[0].host, "cluster", run_tag)
        expected_winner = "cluster"

    # Let the controller observe the split for a few poll cycles — purely
    # informational (did it notice?), not the primary metric.
    for _ in range(3):
        snapshots, _ = ctrl.poll_once()
        tr = ctrl.trackers[isolated.name]
        if tr.state != NodeState.HEALTHY:
            anomaly_detected = True
        time.sleep(config.POLL_INTERVAL_SECONDS)

    heal(isolated)
    t_healed = datetime.now(timezone.utc)

    # Give syncrepl time to reconcile the conflict across all three nodes.
    time.sleep(settle_s)

    final_values = {n.name: read_value(n.host) for n in config.NODES}
    distinct = set(v for v in final_values.values() if v is not None)
    consistent = (len(distinct) == 1) and (None not in final_values.values())

    winning_side = "unknown"
    if consistent:
        winner = next(iter(distinct))
        if winner == cluster_value:
            winning_side = "cluster"
        elif winner == isolated_value:
            winning_side = "isolated"
        else:
            winning_side = "neither (unexpected value)"

    resolution_correct = consistent and (winning_side == expected_winner)

    return {
        "run": run_id, "isolated_node": isolated.name,
        "cluster_write": cluster_value, "isolated_write": isolated_value,
        "expected_winner": expected_winner,
        "final_ldap1": final_values.get("ldap1"), "final_ldap2": final_values.get("ldap2"),
        "final_ldap3": final_values.get("ldap3"), "consistent": consistent,
        "winning_side": winning_side, "resolution_correct": resolution_correct,
        "controller_detected_anomaly": anomaly_detected,
        "t_isolate": t_isolate.isoformat(), "t_healed": t_healed.isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(description="Split-brain (conflicting concurrent write) test")
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--settle", type=float, default=15.0,
                         help="seconds to wait after healing for syncrepl to reconcile (default 15)")
    parser.add_argument("--output", type=str, default="/home/vagrant/split_brain_results.csv")
    args = parser.parse_args()

    print("### Ensuring shared test entry exists ###")
    ensure_test_entry_exists()

    fieldnames = ["run", "isolated_node", "cluster_write", "isolated_write", "expected_winner",
                  "final_ldap1", "final_ldap2", "final_ldap3", "consistent",
                  "winning_side", "resolution_correct", "controller_detected_anomaly",
                  "t_isolate", "t_healed"]

    print(f"### Starting split-brain scenario ({args.runs} runs) ###")
    results = []
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for run_id in range(1, args.runs + 1):
            isolated = config.NODES[(run_id - 1) % len(config.NODES)]
            peers = [n for n in config.NODES if n.name != isolated.name]
            print(f"[split-brain {run_id}/{args.runs}] isolated={isolated.name} ...", end=" ", flush=True)

            result = run_once(run_id, isolated, peers, args.settle)
            writer.writerow(result)
            f.flush()
            results.append(result)

            status = "CONSISTENT" if result["consistent"] else "*** INCONSISTENT ***"
            correctness = "as expected" if result["resolution_correct"] else "*** UNEXPECTED WINNER ***"
            print(f"{status} (winner={result['winning_side']}, expected={result['expected_winner']}, "
                  f"{correctness}, anomaly_detected={result['controller_detected_anomaly']})")

            time.sleep(3)  # brief gap before next run, same as other scenarios

    n_consistent = sum(1 for r in results if r["consistent"])
    n_correct = sum(1 for r in results if r["resolution_correct"])
    n_total = len(results)
    print(f"\n=== Split-brain scenario complete ===")
    print(f"Consistency rate (all 3 nodes agree): {n_consistent}/{n_total} "
          f"({n_consistent / n_total * 100:.1f}%)")
    print(f"Split-brain prevention rate (consistent AND resolved to the "
          f"expected/later write): {n_correct}/{n_total} ({n_correct / n_total * 100:.1f}%)")
    if n_correct < n_total:
        print("Runs that did NOT resolve correctly (real findings — investigate the "
              "corresponding rows in the CSV):")
        for r in results:
            if not r["resolution_correct"]:
                print(f"  run {r['run']}: expected={r['expected_winner']} actual={r['winning_side']} "
                      f"ldap1={r['final_ldap1']} ldap2={r['final_ldap2']} ldap3={r['final_ldap3']}")
    anomaly_rate = sum(1 for r in results if r["controller_detected_anomaly"]) / n_total
    print(f"Controller anomaly-detection rate during the split: {anomaly_rate:.1%} "
          f"(informational — not the primary metric)")
    print(f"\nFull results written to {args.output}")


if __name__ == "__main__":
    main()
