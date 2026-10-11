# Directory-State-Aware Automated Remediation — Lab Deployment

Companion implementation for *"Design and Evaluation of a
Directory-State-Aware Automated Remediation Framework for Distributed
OpenLDAP Identity and Access Management Clusters."*

Implements proposal objectives 1–4: a 3-node OpenLDAP test cluster, a
controller monitoring service health / replication freshness / bind
latency / reachability, a multi-indicator remediation policy, and
controlled fencing + routing updates + safe reintegration.

## 0. Prerequisites (Windows host)

- VirtualBox 7.x for Windows
- Vagrant 2.4+ for Windows
- Virtualization enabled in your BIOS/UEFI (usually on by default)
- ~2 GB RAM free for the 4 VMs (512MB each)
- Run all commands below from Command Prompt, PowerShell, or Git Bash

## 1. Bring up the lab

```bash
cd openldap-iam-demo
vagrant up          # provisions ldap1, ldap2, ldap3, ctrl (~10-15 min first run)
```

This will:
1. Install `slapd` on `ldap1`/`ldap2`/`ldap3` (`provisioning/common.sh`).
2. Configure N-way multi-master replication via syncrepl/mirrormode, with
   unique `olcServerID`s and a seeded `dc=research,dc=lab` tree
   (`provisioning/configure_node.sh`).
3. Install Python + HAProxy on `ctrl` and generate an HAProxy config that
   fronts all three nodes on port 389 (`provisioning/controller_host.sh`).

## 2. Verify replication is working

```bash
vagrant ssh ldap1 -c "ldapsearch -x -D cn=admin,dc=research,dc=lab -w admin -b dc=research,dc=lab '(objectClass=*)' contextCSN"
```

Run the same on `ldap2` and `ldap3` — `contextCSN` values should match (or
be within a few seconds of each other) after a write propagates.

## 3. Run the controller

```bash
vagrant ssh ctrl
sudo /opt/remediation-venv/bin/python3 /opt/remediation-controller/cli.py status
sudo /opt/remediation-venv/bin/python3 /opt/remediation-controller/cli.py monitor
```

`status` gives a one-shot table; `monitor` runs the continuous
poll → classify → remediate loop and prints state transitions live. Leave
`monitor` running in one terminal while you inject failures from another.

## 4. Run experimental scenarios (proposal section 6: 30–50 repetitions each)

From the host, in `openldap-iam-demo/`:

```bash
./scenarios/fail_node.sh ldap2                 # hard failure: stop slapd
./scenarios/restore_node.sh ldap2              # recover

./scenarios/partition_node.sh ldap2 "192.168.56.11 192.168.56.13"
./scenarios/restore_node.sh ldap2              # clears iptables + restarts slapd

./scenarios/induce_divergence.sh ldap3         # replication divergence
```

For each run, capture the `monitor` output (state transition timestamps)
to compute:
- **MTTR** (primary metric): time between the `FENCE` and `REINTEGRATE`
  audit-log lines for that node.
- **Detection latency**: time between the scenario script's action and the
  first non-Healthy classification in the log.
- **False positives/negatives**: cross-reference against what you actually
  injected.

A simple way to batch this for 30–50 repetitions: wrap the fail/restore
pair in a shell loop with a sleep, tee `monitor`'s stdout to a timestamped
file per run, and parse the audit log (`/var/log/remediation-controller/audit.log`
on `ctrl`) afterward.

## 5. Manual operator actions (for comparison against automated remediation)

```bash
sudo /opt/remediation-venv/bin/python3 cli.py fence ldap2 --reason "manual test"
sudo /opt/remediation-venv/bin/python3 cli.py reintegrate ldap2 --reason "manual test"
```

Use these to produce the "manual recovery" baseline the proposal compares
the controller against (section 6).

## 6. Tear down / reset

```bash
vagrant destroy -f     # full reset
vagrant up              # fresh cluster
```

## Project layout

```
Vagrantfile                    # 4-VM topology (3 LDAP nodes + controller VM)
provisioning/
  common.sh                    # slapd install (all LDAP nodes)
  configure_node.sh             # syncrepl/mirrormode config (per node)
  controller_host.sh            # Python + HAProxy setup (ctrl VM)
controller/
  config.py                     # topology + policy thresholds — edit here first
  health.py                     # reachability / bind latency / contextCSN collectors
  state_machine.py               # Healthy/Degraded/Partitioned/Divergent/Fenced/Recovering/Reintegrated
  remediation.py                  # fencing, routing updates, resync trigger, audit log
  controller.py                  # poll-classify-remediate loop, MTTR tracking
  cli.py                          # status / monitor / fence / reintegrate commands
scenarios/
  fail_node.sh                  # stop slapd on a target node
  partition_node.sh              # iptables-based network partition
  induce_divergence.sh           # direct write bypassing replication
  restore_node.sh                 # undo any of the above
docs/
  architecture.md                # component/deployment/state-machine/sequence diagrams + pseudocode
```

## Known lab limitations (for the dissertation's "Threats to Validity" section)

- 3 nodes on one hypervisor share the host's clock/scheduler — detection
  latency numbers won't transfer directly to a geographically distributed
  production deployment.
- HAProxy fencing is routing-level, not a true STONITH — the fenced node's
  slapd process keeps running, which is deliberate (keeps it diagnosable)
  but means it can still be reached directly on its own IP, not just via
  the controller's front-end.
- `induce_divergence.sh` creates divergence by disabling syncrepl
  temporarily; this is a reasonable proxy for a missed-replication-window
  failure but is not identical to every real divergence cause (e.g.
  clock skew, conflicting concurrent writes on two masters simultaneously).
