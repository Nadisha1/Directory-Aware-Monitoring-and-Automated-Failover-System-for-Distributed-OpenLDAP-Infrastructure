#!/usr/bin/env bash
# Scenario 2: network partition. Uses iptables on the target node to drop
# all LDAP-port traffic to/from its peers, simulating a network split while
# the process itself keeps running (distinguishes PARTITIONED from a
# simple service crash — the point of monitoring reachability separately
# from bind latency).
#
# Usage: ./partition_node.sh ldap2 "192.168.56.11 192.168.56.13"
set -euo pipefail
NODE="${1:?usage: partition_node.sh <node-name> \"<peer-ip-1> <peer-ip-2>\"}"
PEERS="${2:?must supply peer IPs to block}"

for peer in $PEERS; do
  vagrant ssh "$NODE" -c "sudo iptables -A INPUT -s ${peer} -p tcp --dport 389 -j DROP"
  vagrant ssh "$NODE" -c "sudo iptables -A OUTPUT -d ${peer} -p tcp --dport 389 -j DROP"
done

echo "[scenario] ${NODE} partitioned from: ${PEERS}"
echo "[scenario] restore with: restore_node.sh ${NODE}"
