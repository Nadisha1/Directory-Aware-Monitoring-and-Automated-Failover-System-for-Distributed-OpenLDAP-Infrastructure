"""
Automated experiment runner for the dissertation's evaluation section.

Runs each of the three failure scenarios (crash, partition, divergence) N
times unattended, injecting the failure, timing how long the controller
takes to detect + fence it, timing how long it takes to auto-heal +
reintegrate, and logging every run to a CSV file. This is what turns a
handful of manually-watched demo runs into a real statistical evaluation
(mean/median/stdev detection latency and MTTR per scenario, as called for
in the proposal's section 6-7).

Usage (run inside the ctrl VM, same way as cli.py):
    sudo /opt/remediation-venv/bin/python3 experiment_runner.py --runs 30
    sudo /opt/remediation-venv/bin/python3 experiment_runner.py --runs 3 --scenarios crash
    sudo /opt/remediation-venv/bin/python3 experiment_runner.py --runs 30 --output /home/vagrant/results.csv

Each run:
    1. Waits for the cluster to be fully Healthy (baseline).
    2. Injects the failure on a target node (rotates across ldap1/2/3).
    3. Polls (using the SAME Controller class the real monitor uses) until
       the node reaches FENCED, recording the cause and elapsed time.
    4. Keeps polling until the node reaches REINTEGRATED (or Healthy),
       recording elapsed time.
    5. Writes one CSV row. If a step times out, the run is marked
       'timeout' rather than hanging forever, and a safety-net cleanup
       (start slapd / clear iptables) runs regardless before moving on.
"""

import argparse
import csv
import shlex
import subprocess
import statistics
import time
import uuid
from datetime import datetime, timezone

import config
from controller import Controller
from state_machine import NodeState

SSH_KEY = "/home/vagrant/.ssh/id_rsa"
DETECTION_TIMEOUT_S = 90
DIVERGENCE_DETECTION_TIMEOUT_S = 150  # accounts for the deliberate threshold-wait
RECOVERY_TIMEOUT_S = 120
SETTLE_TIMEOUT_S = 60  # max wait for baseline Healthy before starting a run


def ssh_exec(host, *cmd_parts, timeout=15):
    """Runs a command on a node over SSH, using the controller's key.
    Returns True/False for success; never raises.

    Builds ONE pre-quoted remote command string ourselves (shlex.quote per
    token) rather than passing multiple raw argv items to ssh. ssh joins
    ALL trailing arguments into a single space-separated string and hands
    THAT to the remote shell to re-parse (see ssh_exec_stdin's docstring
    for the fuller history) — any token containing shell metacharacters
    would otherwise be reinterpreted by the remote shell before the
    target program ever sees it. Pre-quoting everything ourselves and
    passing it as a single trailing argument makes ssh's own joining a
    no-op, so only our intended quoting applies."""
    remote_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    full_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                "-i", SSH_KEY, f"vagrant@{host}", remote_cmd]
    try:
        subprocess.run(full_cmd, timeout=timeout, check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def ssh_exec_stdin(host, remote_command, stdin_text, timeout=15, verbose=False):
    """Runs a single remote command over SSH, piping `stdin_text` to its
    stdin. Used instead of a 'bash -c "... <<EOF"' heredoc string, because
    ssh joins all trailing argv items into one space-separated string and
    hands THAT to the remote shell to re-parse — so a multi-word, embedded
    heredoc payload passed as one argv element does not survive intact
    (the remote 'bash -c' only takes the first word as its script; every
    flag after it is silently discarded as an unused positional argument,
    so e.g. 'ldapadd -x -D ... -w admin' actually runs as bare 'ldapadd'
    with no auth flags and fails). Piping via stdin sidesteps re-parsing
    entirely: `remote_command` is one simple, unambiguous command line,
    and the data travels over the SSH stdin stream instead of being
    embedded in shell syntax.
    Returns True/False for success; never raises. If verbose=True, prints
    the underlying exception/stderr on failure instead of swallowing it —
    use this while diagnosing, since a bare False gives no clue whether
    the failure was SSH-level (connection, auth, host key) or the remote
    command itself (e.g. ldapadd rejecting the entry)."""
    full_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                "-i", SSH_KEY, f"vagrant@{host}", remote_command]
    try:
        subprocess.run(full_cmd, input=stdin_text.encode(), timeout=timeout,
                        check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        if verbose:
            print(f"    [ssh_exec_stdin] {host}: exit={e.returncode} "
                  f"stderr={e.stderr.decode(errors='replace').strip()!r}")
        return False
    except subprocess.TimeoutExpired as e:
        if verbose:
            print(f"    [ssh_exec_stdin] {host}: TIMEOUT after {timeout}s")
        return False
    except OSError as e:
        if verbose:
            print(f"    [ssh_exec_stdin] {host}: OSError {e}")
        return False


def ssh_exec_capture(host, *cmd_parts, timeout=15):
    """Like ssh_exec, but returns (success, stdout) so callers can parse
    the remote command's output instead of only knowing pass/fail. Uses
    the same shlex-quoting approach as ssh_exec, for the same reason —
    this is what get_entry_count/check_marker_present rely on, and their
    ldapsearch filters (e.g. '(objectClass=*)') are full of shell
    metacharacters ('(', ')', '*') that MUST be quoted before reaching
    the remote shell, or the remote shell mangles them (grouping /
    globbing) before ldapsearch ever sees the filter — which is exactly
    what was causing every consistency check to silently fail."""
    remote_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    full_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                "-i", SSH_KEY, f"vagrant@{host}", remote_cmd]
    try:
        result = subprocess.run(full_cmd, timeout=timeout, check=True,
                                 capture_output=True, text=True)
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stdout or ""
    except (subprocess.TimeoutExpired, OSError):
        return False, ""


def write_consistency_marker(peer, run_tag):
    """Writes ONE uniquely-tagged entry to `peer` and returns its DN, or
    None on failure. Used to empirically verify the 'replication
    consistency' metric named in the proposal's evaluation section
    (7): a node that was down/partitioned/diverged must actually receive
    every write it missed once it's remediated — not just LOOK healthy
    again by the controller's own CSN-drift proxy check. Call this right
    after fault injection (while the target is still faulty for
    crash/partition; divergence's own injection already exercises this
    same property internally via its multi-write sequence, so this marker
    additionally confirms POST-recovery propagation stays correct too)."""
    marker_id = f"consistency-{run_tag}-{uuid.uuid4().hex[:8]}"
    dn = f"cn=exp-{marker_id},{config.BASE_DN}"
    ldif = f"dn: {dn}\nobjectClass: organizationalRole\ncn: exp-{marker_id}\n"
    remote_cmd = f"ldapadd -x -D {config.ADMIN_DN} -w {config.ADMIN_PW}"
    ok = ssh_exec_stdin(peer.host, remote_cmd, ldif)
    return dn if ok else None


def get_entry_count(host):
    """Total entry count under BASE_DN, or None if the query fails."""
    ok, out = ssh_exec_capture(
        host, "ldapsearch", "-x", "-D", config.ADMIN_DN, "-w", config.ADMIN_PW,
        "-b", config.BASE_DN, "-LLL", "(objectClass=*)", "dn")
    if not ok:
        return None
    return sum(1 for line in out.splitlines() if line.startswith("dn:"))


def check_marker_present(host, marker_dn):
    """True iff `marker_dn` exists on `host` — i.e. the node actually
    received a write it would have missed while faulty, not just one that
    a CSN-drift proxy check happens to consider 'close enough'."""
    ok, out = ssh_exec_capture(
        host, "ldapsearch", "-x", "-D", config.ADMIN_DN, "-w", config.ADMIN_PW,
        "-b", marker_dn, "-s", "base", "-LLL", "(objectClass=*)")
    return ok and "dn:" in out


def check_replication_consistency(target, peers, marker_dn):
    """Runs the two checks that back the proposal's 'replication
    consistency' evaluation metric:
      1. The target actually has the marker entry it was written while
         faulty (content-level catch-up, not just a CSN proxy).
      2. Total entry counts match across the target and every peer (a
         coarse but cheap check against silent data loss or duplication).
    Returns a dict with booleans/counts for the CSV row; never raises —
    an inability to check is recorded as 'unknown', not a false pass."""
    if marker_dn is None:
        return {"marker_present_on_target": "unknown",
                "entry_counts_match": "unknown",
                "target_entry_count": "", "replication_consistent": "unknown"}

    marker_ok = check_marker_present(target.host, marker_dn)
    counts = {n.name: get_entry_count(n.host) for n in [target] + peers}
    known_counts = [c for c in counts.values() if c is not None]
    counts_match = (len(known_counts) == len(counts) and len(set(known_counts)) == 1)

    return {
        "marker_present_on_target": marker_ok,
        "entry_counts_match": counts_match,
        "target_entry_count": counts.get(target.name, ""),
        "replication_consistent": bool(marker_ok and counts_match),
    }


def inject_crash(node):
    ssh_exec(node.host, "sudo", "systemctl", "stop", "slapd")


def cleanup_crash(node):
    ssh_exec(node.host, "sudo", "iptables", "-F")
    ssh_exec(node.host, "sudo", "systemctl", "start", "slapd")


def inject_partition(node, peers):
    """Blocks ALL traffic on the LDAP port — including from the controller
    itself — so the node genuinely becomes unreachable, matching a real
    network partition. (Blocking only peer-to-peer traffic, as the
    divergence scenario does, leaves the controller able to reach the node
    directly, so it would never be classified Partitioned.)"""
    ssh_exec(node.host, "sudo", "iptables", "-A", "INPUT", "-p", "tcp",
              "--dport", "389", "-j", "DROP")
    ssh_exec(node.host, "sudo", "iptables", "-A", "OUTPUT", "-p", "tcp",
              "--sport", "389", "-j", "DROP")


def cleanup_partition(node):
    ssh_exec(node.host, "sudo", "iptables", "-F")


def inject_divergence(node, peers, run_id, num_writes=10, write_interval_s=5):
    """Isolates `node` from its peers only (controller can still reach it),
    then writes a SERIES of unique entries to one of the peers, spaced out
    over time, so that peer's contextCSN keeps genuinely advancing while
    `node`'s stays frozen — reachable but stale, the DIVERGENT condition.

    FIX (see dissertation limitations discussion): contextCSN records the
    "timestamp of the last write applied", not "time since isolation". A
    SINGLE write immediately after isolating a freshly-synced node creates
    only a small, FIXED gap — (write_time - node's_last_sync_time) — that
    does NOT grow just by waiting longer, because nothing else changes
    either CSN afterwards. Waiting was therefore never going to help; this
    is what caused ~29/30 automated divergence runs to time out before this
    fix. The real fix is to keep the peer's contextCSN advancing during the
    isolation window (multiple writes, spaced apart) so a genuine, growing
    gap opens up relative to the isolated node's frozen value, matching how
    divergence actually accumulates in a real missed-replication-window
    failure.

    The full write sequence (and the short settle time after the last
    write) is included in t_inject's elapsed time, same as it would be for
    a real, naturally-occurring divergence — this is an honest model of
    detection time, not a shortcut.

    IMPORTANT (recovery, not just detection): the iptables isolation added
    below is only a LAB TECHNIQUE to freeze the node's contextCSN long
    enough to create a real gap — it is not part of the fault being
    modeled. A genuine replication divergence (missed replication window,
    consumer stall) does not usually involve an ongoing network partition;
    the node stays reachable, it has just fallen behind. If the isolation
    were left in place, trigger_resync's slapd restart could never succeed
    (a restarted slapd still can't complete a syncrepl handshake with
    peers it's iptables-blocked from reaching) — that would be a pure
    artifact of the simulation, not a real constraint the remediation
    needs to overcome. So once the divergence gap has been established,
    this function reconnects the node to its peers (simulating the
    transient blip clearing) before returning, leaving it reachable but
    still holding stale replication state — exactly the condition
    trigger_resync is designed to fix."""
    for peer in peers:
        ssh_exec(node.host, "sudo", "iptables", "-A", "INPUT", "-s", peer.host,
                  "-p", "tcp", "--dport", "389", "-j", "REJECT")
        ssh_exec(node.host, "sudo", "iptables", "-A", "OUTPUT", "-d", peer.host,
                  "-p", "tcp", "--dport", "389", "-j", "REJECT")
    writer = peers[0]
    remote_cmd = f"ldapadd -x -D cn=admin,{config.BASE_DN} -w admin"
    run_tag = f"{run_id}-{uuid.uuid4().hex[:8]}"  # unique per invocation, even
    # if run_id (e.g. "divergence1") repeats across separate script runs —
    # otherwise a re-run reuses the same DN and every ldapadd fails with
    # "already exists" (looks identical to a real write failure).
    for i in range(num_writes):
        dn = f"cn=exp-divergence-{run_tag}-{i},{config.BASE_DN}"
        ldif = f"dn: {dn}\nobjectClass: organizationalRole\ncn: exp-divergence-{run_tag}-{i}\n"
        ok = ssh_exec_stdin(writer.host, remote_cmd, ldif, verbose=True)
        if not ok:
            # Surface write failures immediately rather than silently
            # continuing — a failed write here means the peer's contextCSN
            # will NOT advance for this iteration, undermining the whole
            # premise of the scenario.
            print(f"    [warn] divergence write {i} to {writer.host} failed")
        if i < num_writes - 1:
            time.sleep(write_interval_s)
    # Short settle time after the final write so the peer's contextCSN has
    # definitely registered it before the poll loop starts checking.
    time.sleep(5)
    # Reconnect the node to its peers now that the gap is established (see
    # docstring) — the network isolation was only scaffolding to freeze
    # the CSN during setup, not part of the fault itself. Without this,
    # trigger_resync's slapd restart can never succeed later, since the
    # node would still be iptables-blocked from reaching the peers it
    # needs to resync with.
    ssh_exec(node.host, "sudo", "iptables", "-F")


def cleanup_divergence(node):
    ssh_exec(node.host, "sudo", "iptables", "-F")
    ssh_exec(node.host, "sudo", "systemctl", "restart", "slapd")


SCENARIOS = {
    "crash": {"inject": inject_crash, "cleanup": cleanup_crash, "needs_peers": False},
    "partition": {"inject": inject_partition, "cleanup": cleanup_partition, "needs_peers": True},
    "divergence": {"inject": inject_divergence, "cleanup": cleanup_divergence, "needs_peers": True},
}


def wait_for_baseline_healthy(ctrl, timeout=SETTLE_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshots, _ = ctrl.poll_once()
        if all(t.state == NodeState.HEALTHY for t in ctrl.trackers.values()):
            return True
        time.sleep(config.POLL_INTERVAL_SECONDS)
    return False


def run_once(scenario_name, target, peers, run_id, verbose=False):
    spec = SCENARIOS[scenario_name]
    ctrl = Controller()  # fresh trackers each run — clean hysteresis state

    if not wait_for_baseline_healthy(ctrl):
        return {"outcome": "baseline_never_healthy"}

    t_inject = datetime.now(timezone.utc)
    if spec["needs_peers"]:
        if scenario_name == "divergence":
            spec["inject"](target, peers, run_id)
        else:
            spec["inject"](target, peers)
    else:
        spec["inject"](target)

    # Replication-consistency marker: write one tagged entry to a peer
    # right after injection (see write_consistency_marker docstring). If
    # this write itself fails, the run still proceeds — consistency is
    # just recorded as unknown for this row rather than aborting the run.
    marker_dn = write_consistency_marker(peers[0], run_id) if peers else None

    # --- wait for FENCE ---
    cause = None
    t_fence = None
    timeout = DIVERGENCE_DETECTION_TIMEOUT_S if scenario_name == "divergence" else DETECTION_TIMEOUT_S
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshots, transitions = ctrl.poll_once()
        tr = ctrl.trackers[target.name]
        if verbose:
            snap = snapshots[target.name]
            peer_csns = [snapshots[p.name]["context_csn"] for p in peers
                         if snapshots[p.name]["context_csn"]]
            drift = None
            if snap["context_csn"] and peer_csns:
                drift = abs((max(peer_csns) - snap["context_csn"]).total_seconds())
            print(f"\n    [verbose] reachable={snap['reachable']} "
                  f"bind_ms={snap['bind_latency_ms']} "
                  f"own_csn={snap['context_csn']} "
                  f"max_peer_csn={max(peer_csns) if peer_csns else None} "
                  f"drift_s={drift} "
                  f"consecutive_bad={tr.consecutive_bad} "
                  f"last_bad_raw_state={tr.last_bad_raw_state} "
                  f"state={tr.state}", end="")
        if tr.state == NodeState.FENCED:
            t_fence = datetime.now(timezone.utc)
            cause = tr.last_bad_raw_state.value if tr.last_bad_raw_state else "unknown"
            break
        time.sleep(config.POLL_INTERVAL_SECONDS)

    if t_fence is None:
        spec["cleanup"](target)
        return {"outcome": "detection_timeout", "target": target.name,
                "t_inject": t_inject.isoformat()}

    detect_latency = (t_fence - t_inject).total_seconds()

    # --- wait for REINTEGRATED / back to Healthy ---
    t_recovered = None
    deadline = time.monotonic() + RECOVERY_TIMEOUT_S
    while time.monotonic() < deadline:
        snapshots, transitions = ctrl.poll_once()
        tr = ctrl.trackers[target.name]
        if tr.state in (NodeState.REINTEGRATED, NodeState.HEALTHY):
            t_recovered = datetime.now(timezone.utc)
            break
        time.sleep(config.POLL_INTERVAL_SECONDS)

    # Consistency check runs BEFORE cleanup — cleanup restarts slapd on
    # some paths, which would be a confound (a fresh restart could mask a
    # real propagation gap by triggering another resync of its own).
    consistency = None
    if t_recovered is not None:
        time.sleep(3)  # small settle margin before reading entry counts
        consistency = check_replication_consistency(target, peers, marker_dn)

    spec["cleanup"](target)  # safety net regardless of outcome

    if t_recovered is None:
        return {"outcome": "recovery_timeout", "target": target.name, "cause": cause,
                "t_inject": t_inject.isoformat(), "t_fence": t_fence.isoformat(),
                "detect_latency_s": round(detect_latency, 2)}

    recover_latency = (t_recovered - t_fence).total_seconds()
    total_mttr = (t_recovered - t_inject).total_seconds()

    result = {"outcome": "success", "target": target.name, "cause": cause,
              "t_inject": t_inject.isoformat(), "t_fence": t_fence.isoformat(),
              "t_recovered": t_recovered.isoformat(),
              "detect_latency_s": round(detect_latency, 2),
              "recover_latency_s": round(recover_latency, 2),
              "total_mttr_s": round(total_mttr, 2)}
    result.update(consistency)
    return result


def summarize(rows, scenario_name):
    successes = [r for r in rows if r["outcome"] == "success"]
    n = len(rows)
    n_ok = len(successes)
    print(f"\n=== {scenario_name} ({n_ok}/{n} succeeded) ===")
    if not successes:
        print("  No successful runs to summarize.")
        return
    for metric in ("detect_latency_s", "recover_latency_s", "total_mttr_s"):
        values = [r[metric] for r in successes]
        print(f"  {metric}: mean={statistics.mean(values):.2f}s  "
              f"median={statistics.median(values):.2f}s  "
              f"stdev={statistics.stdev(values):.2f}s" if len(values) > 1 else
              f"  {metric}: {values[0]:.2f}s (only 1 sample)")

    consistent = sum(1 for r in successes if r.get("replication_consistent") is True)
    unknown = sum(1 for r in successes if r.get("replication_consistent") == "unknown")
    print(f"  replication_consistent: {consistent}/{n_ok} confirmed"
          + (f" ({unknown} unknown — marker write failed)" if unknown else ""))


def main():
    parser = argparse.ArgumentParser(description="Automated failure-scenario experiment runner")
    parser.add_argument("--runs", type=int, default=30, help="repetitions per scenario (default 30)")
    parser.add_argument("--scenarios", type=str, default="crash,partition,divergence",
                         help="comma-separated list of scenarios to run")
    parser.add_argument("--output", type=str, default="/home/vagrant/experiment_results.csv")
    parser.add_argument("--verbose", action="store_true",
                         help="print per-poll diagnostics (reachability, bind latency, "
                              "contextCSN drift, hysteresis counter) during the FENCE-wait "
                              "phase of each run — use for troubleshooting flaky/timeout runs, "
                              "not for the full statistical batch (very noisy over 30 runs)")
    args = parser.parse_args()

    scenario_names = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    nodes = config.NODES

    fieldnames = ["scenario", "run", "target", "cause", "outcome",
                  "t_inject", "t_fence", "t_recovered",
                  "detect_latency_s", "recover_latency_s", "total_mttr_s",
                  "marker_present_on_target", "entry_counts_match",
                  "target_entry_count", "replication_consistent"]

    all_rows_by_scenario = {s: [] for s in scenario_names}

    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for scenario_name in scenario_names:
            print(f"\n### Starting scenario: {scenario_name} ({args.runs} runs) ###")
            for run_id in range(1, args.runs + 1):
                target = nodes[(run_id - 1) % len(nodes)]
                peers = [n for n in nodes if n.name != target.name]

                print(f"[{scenario_name} {run_id}/{args.runs}] target={target.name} ...",
                      end=" ", flush=True)
                result = run_once(scenario_name, target, peers, f"{scenario_name}{run_id}",
                                   verbose=args.verbose)
                result["scenario"] = scenario_name
                result["run"] = run_id
                print(result["outcome"])

                writer.writerow({k: result.get(k, "") for k in fieldnames})
                f.flush()
                all_rows_by_scenario[scenario_name].append(result)

                time.sleep(3)  # brief gap between runs

    print(f"\nFull results written to {args.output}")
    for scenario_name in scenario_names:
        summarize(all_rows_by_scenario[scenario_name], scenario_name)


if __name__ == "__main__":
    main()
