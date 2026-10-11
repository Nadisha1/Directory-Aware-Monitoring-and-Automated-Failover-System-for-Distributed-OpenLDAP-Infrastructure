# Architecture Notes

Maps directly to proposal section 11 ("Recommended Architecture"). Use
these as the basis for the dissertation's diagrams — draw them properly
(e.g. draw.io / PlantUML) rather than submitting ASCII, but the structure
below is accurate to what's actually deployed.

## Component diagram (logical)

```
                    ┌─────────────────────────┐
                    │      ctrl VM             │
                    │  ┌───────────────────┐   │
                    │  │ Remediation        │   │
                    │  │ Controller (Python)│   │
                    │  │  - health.py       │   │
                    │  │  - state_machine.py│   │
                    │  │  - remediation.py  │   │
                    │  └─────────┬──────────┘   │
                    │            │ runtime API   │
                    │  ┌─────────▼──────────┐   │
                    │  │      HAProxy        │   │
                    │  └─────────┬──────────┘   │
                    └────────────┼──────────────┘
                                 │ LDAP :389 (client traffic)
              ┌──────────────────┼──────────────────┐
              │                  │                  │
        ┌─────▼─────┐      ┌─────▼─────┐      ┌─────▼─────┐
        │  ldap1     │◄────►│  ldap2     │◄────►│  ldap3     │
        │  slapd     │      │  slapd     │      │  slapd     │
        │  mirrormode│      │  mirrormode│      │  mirrormode│
        └────────────┘      └────────────┘      └────────────┘
              ▲___________________│___________________▲
                   syncrepl (N-way multi-master)
```

The controller polls each node **directly** (bypassing HAProxy) for health
data, but only manipulates **routing** via HAProxy — it never talks to
slapd's replication engine directly except to trigger a restart for resync.

## Deployment diagram

4 VMs on a private VirtualBox network (192.168.56.0/24):

| VM    | IP             | Role                                  |
|-------|----------------|----------------------------------------|
| ldap1 | 192.168.56.11  | OpenLDAP provider/consumer, server-id=1 |
| ldap2 | 192.168.56.12  | OpenLDAP provider/consumer, server-id=2 |
| ldap3 | 192.168.56.13  | OpenLDAP provider/consumer, server-id=3 |
| ctrl  | 192.168.56.10  | Controller + HAProxy front-end          |

## Remediation state machine

```
   Healthy ──(bind slow)──► Degraded ──(recovers)──► Healthy
      │                          │
      │(TCP unreachable /        │(contextCSN drift > threshold)
      │ bind fails)               │
      ▼                          ▼
  Partitioned                Divergent
      │                          │
      └──────────┬───────────────┘
                  │ (N consecutive bad samples: hysteresis)
                  ▼
               Fenced ──(HAProxy: disable server)
                  │
                  │ (node/network comes back)
                  ▼
             Recovering
                  │
                  │ (N consecutive good samples: hysteresis)
                  ▼
             Reintegrated ──(HAProxy: enable server)──► Healthy
```

Hysteresis constants (`config.CONSECUTIVE_FAILURES_TO_FENCE`,
`CONSECUTIVE_SUCCESSES_TO_REINTEGRATE`) exist specifically to avoid
flapping a node in and out of rotation on a single transient sample —
this is worth a paragraph in the dissertation's design-rationale section.

## Sequence: node failure scenario

```
operator          fail_node.sh          ldap2          controller          haproxy
   │──run fail_node.sh ldap2──►│            │                 │                 │
   │                           │──stop slapd─►│                │                 │
   │                           │            (down)              │                 │
   │                                                             │──poll ldap2────►│
   │                                                             │◄──unreachable───│
   │                                                     (consecutive_bad++)       │
   │                                              (threshold reached: FENCED)      │
   │                                                             │──disable ldap2─►│
   │                           │◄── run restore_node.sh ldap2 ───────────────────  │
   │                           │──start slapd──►│               │                 │
   │                                                             │──poll ldap2────►│
   │                                                             │◄──reachable─────│
   │                                                  (consecutive_good++, RECOVERING)
   │                                              (threshold reached: REINTEGRATED)│
   │                                                             │──enable ldap2──►│
```

Repeat the equivalent sequence diagrams for the partition and divergence
scenarios using `scenarios/partition_node.sh` and
`scenarios/induce_divergence.sh` as the trigger step.

## Remediation algorithm (pseudocode, for the dissertation)

```
for each poll cycle (every POLL_INTERVAL_SECONDS):
    for each node in cluster:
        snapshot = collect_health_snapshot(node)   # reachability, bind latency, contextCSN
        raw_state = classify(snapshot, peer_contextCSNs)
        tracker[node].apply_hysteresis(raw_state)

        if tracker[node].transitioned_to(FENCED):
            haproxy.disable(node)
            audit_log("FENCE", node, reason)
            record_fence_time(node)

        elif tracker[node].transitioned_to(REINTEGRATED):
            haproxy.enable(node)
            audit_log("REINTEGRATE", node, reason)
            record_recovery_time(node)   # used for MTTR

        elif tracker[node].transitioned_to(DIVERGENT):
            audit_log("DIVERGENCE_DETECTED", node, reason)
            trigger_resync(node)
```
