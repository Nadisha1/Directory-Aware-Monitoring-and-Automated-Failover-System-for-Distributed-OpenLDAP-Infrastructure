"""
Manual recovery baseline for the dissertation's Objective #5:
"Compare automated remediation against manual recovery using quantitative
metrics." The automated MTTR numbers (experiment_runner.py) are meaningless
as a "the automation was worth it" argument without something to compare
them against — this script produces that something.

SCOPE, STATED HONESTLY (put this in your dissertation, don't hide it):
This measures manual EXECUTION time — diagnosis + fix + confirmation — by
a trained operator who already knows a fault exists and has runbook
commands on hand. It does NOT measure passive-monitoring detection time
(how long a human would take to *notice* a problem nobody told them
about), because with one person both injecting the fault and fixing it,
that phase can't be measured honestly — you already know the fault
exists the moment you inject it. This is a legitimate, common baseline
framing (it's what an on-call runbook execution time would look like
once paged) and should be described as such, not conflated with the
automated system's end-to-end MTTR, which DOES include real detection
time. If asked "what did you actually compare", the honest answer is:
automated total MTTR (detection + remediation) vs. manual EXECUTION time
(diagnosis + fix + confirmation), and the writeup should say exactly that.

Reuses the SAME inject_crash/inject_partition/cleanup_* functions as
experiment_runner.py, so the fault itself is byte-for-byte identical to
what the automated system was tested against — only the remediation is
manual.

Usage:
    sudo /opt/remediation-venv/bin/python3 manual_baseline.py --scenario crash
    sudo /opt/remediation-venv/bin/python3 manual_baseline.py --scenario partition

Run this SEPARATELY from experiment_runner.py and false_positive_test.py
(never at the same time) — confirm baseline health with
`cli.py status` before each trial. Recommended: 5 trials per scenario;
that's enough for a mean/stdev comparison without turning your whole
afternoon into stopwatch-pressing.
"""

import argparse
import csv
import os
import random
import time
from datetime import datetime, timezone

import config
from experiment_runner import inject_crash, inject_partition, cleanup_crash, cleanup_partition

RUNBOOK = {
    "crash": """
  Suggested manual runbook for a crashed/unreachable node (from README.md):
    1. Diagnose:   sudo /opt/remediation-venv/bin/python3 cli.py status
                   (identifies which node is unreachable)
    2. Fix:        ssh into the affected node, then:
                       sudo systemctl restart slapd
    3. Confirm:    re-run cli.py status until the node shows Healthy
""",
    "partition": """
  Suggested manual runbook for a partitioned node (from README.md):
    1. Diagnose:   sudo /opt/remediation-venv/bin/python3 cli.py status
                   (identifies which node is unreachable — a partition and
                   a crash look IDENTICAL to this status check; you may
                   need to ssh in and check `systemctl status slapd`
                   directly to tell them apart, same ambiguity the
                   automated controller has to resolve)
    2. Fix:        ssh into the affected node, then:
                       sudo iptables -F
                       sudo systemctl restart slapd
    3. Confirm:    re-run cli.py status until the node shows Healthy
""",
}


def wait_for_enter(prompt):
    input(f"\n>>> {prompt}\n>>> Press ENTER the moment you've done this: ")
    return datetime.now(timezone.utc)


def run_trial(scenario, target):
    peers = [n for n in config.NODES if n.name != target.name]

    print(f"\n{'='*70}\nManual baseline trial — scenario={scenario} target={target.name}\n{'='*70}")
    input("Press ENTER when you are ready for the fault to be injected "
          "(you will NOT be told exactly when, only that it's about to happen): ")

    time.sleep(random.uniform(1, 4))  # small unpredictable gap, closer to a real page
    t_inject = datetime.now(timezone.utc)
    if scenario == "crash":
        inject_crash(target)
    else:
        inject_partition(target, peers)
    print(f"\n[FAULT INJECTED at {t_inject.isoformat()}] — go.")
    print(RUNBOOK[scenario])

    t_diagnosed = wait_for_enter(
        "You have identified WHICH node is affected and WHAT kind of fault it is "
        "(checked status, know it's down/partitioned).")
    t_fixed = wait_for_enter(
        "You have applied the fix (restarted slapd / cleared iptables as appropriate).")
    t_confirmed = wait_for_enter(
        "You have re-checked status and confirmed the node is back to Healthy.")

    if scenario == "crash":
        cleanup_crash(target)
    else:
        cleanup_partition(target)

    diagnosis_s = (t_diagnosed - t_inject).total_seconds()
    fix_s = (t_fixed - t_diagnosed).total_seconds()
    confirm_s = (t_confirmed - t_fixed).total_seconds()
    total_s = (t_confirmed - t_inject).total_seconds()

    print(f"\nDiagnosis: {diagnosis_s:.1f}s | Fix: {fix_s:.1f}s | "
          f"Confirmation: {confirm_s:.1f}s | TOTAL manual execution time: {total_s:.1f}s")

    return {
        "mode": "manual", "scenario": scenario, "target": target.name,
        "t_inject": t_inject.isoformat(), "t_diagnosed": t_diagnosed.isoformat(),
        "t_fixed": t_fixed.isoformat(), "t_confirmed": t_confirmed.isoformat(),
        "diagnosis_s": round(diagnosis_s, 2), "fix_s": round(fix_s, 2),
        "confirm_s": round(confirm_s, 2), "total_manual_mttr_s": round(total_s, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Manual recovery baseline timing")
    parser.add_argument("--scenario", choices=["crash", "partition"], required=True)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--output", type=str, default="/home/vagrant/manual_baseline.csv")
    args = parser.parse_args()

    fieldnames = ["mode", "scenario", "target", "t_inject", "t_diagnosed", "t_fixed",
                  "t_confirmed", "diagnosis_s", "fix_s", "confirm_s", "total_manual_mttr_s"]
    file_exists = os.path.exists(args.output)

    print(f"### Manual baseline: {args.scenario}, {args.trials} trial(s) ###")
    print("Make sure the cluster is confirmed healthy before each trial "
          "(cli.py status) — this script does NOT auto-check baseline health "
          "the way experiment_runner.py does.")

    with open(args.output, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()

        for i in range(args.trials):
            target = config.NODES[i % len(config.NODES)]
            input(f"\n\n### Trial {i+1}/{args.trials} — confirm cluster is Healthy "
                  f"(run cli.py status yourself), then press ENTER to continue ###")
            result = run_trial(args.scenario, target)
            writer.writerow(result)
            f.flush()

    print(f"\nAll trials complete. Results appended to {args.output}")


if __name__ == "__main__":
    main()
