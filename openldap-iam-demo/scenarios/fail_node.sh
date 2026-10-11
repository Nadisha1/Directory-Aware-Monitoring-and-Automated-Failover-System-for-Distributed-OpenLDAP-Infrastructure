#!/usr/bin/env bash
# Scenario 1: hard node failure. Stops slapd on the target node to simulate
# a crashed service while the VM/host itself stays up.
#
# Usage: ./fail_node.sh ldap2
set -euo pipefail
NODE="${1:?usage: fail_node.sh <node-name>}"

echo "[scenario] stopping slapd on ${NODE}"
vagrant ssh "$NODE" -c "sudo systemctl stop slapd"
echo "[scenario] slapd stopped on ${NODE}. Recover later with restore_node.sh ${NODE}"
