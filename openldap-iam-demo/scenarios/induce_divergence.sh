#!/usr/bin/env bash
# Scenario 3: replication divergence. Temporarily disables syncrepl on the
# target node, writes an entry directly to it (bypassing replication), then
# re-enables syncrepl — leaving contextCSN out of step with peers until the
# controller detects and triggers a resync.
#
# Usage: ./induce_divergence.sh ldap3
set -euo pipefail
NODE="${1:?usage: induce_divergence.sh <node-name>}"
BASE_DN="dc=research,dc=lab"

echo "[scenario] disabling syncrepl on ${NODE}"
vagrant ssh "$NODE" -c "sudo ldapmodify -Y EXTERNAL -H ldapi:/// <<'EOF'
dn: olcDatabase={1}mdb,cn=config
changetype: modify
delete: olcSyncRepl
EOF" || true

echo "[scenario] writing a direct (non-replicated) entry to ${NODE}"
vagrant ssh "$NODE" -c "ldapadd -x -D cn=admin,${BASE_DN} -w admin <<EOF
dn: uid=divergence-test,ou=people,${BASE_DN}
objectClass: inetOrgPerson
sn: Test
cn: Divergence Test
uid: divergence-test
EOF" || true

echo "[scenario] ${NODE} now has a local write its peers don't know about."
echo "[scenario] the controller should classify it DIVERGENT via contextCSN comparison and trigger a resync."
echo "[scenario] NOTE: re-adding olcSyncRepl requires re-running provisioning/configure_node.sh on that node."
