#!/usr/bin/env bash
# Restores a node previously taken down by fail_node.sh or partition_node.sh.
# Usage: ./restore_node.sh ldap2
set -euo pipefail
NODE="${1:?usage: restore_node.sh <node-name>}"

echo "[scenario] restoring network + service on ${NODE}"
vagrant ssh "$NODE" -c "sudo iptables -F || true"
vagrant ssh "$NODE" -c "sudo systemctl start slapd"
echo "[scenario] ${NODE} restored. The controller should move it Fenced -> Recovering -> Reintegrated."
