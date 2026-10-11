#!/bin/bash
# Demonstrates OpenLDAP's OWN multi-master replication catch-up -- NOT
# anything the controller does. Stops ldap1, adds a user directly to
# ldap2 while ldap1 is offline, brings ldap1 back up, then polls until
# the same user appears on ldap1 too. This proves the underlying
# syncrepl data layer works correctly on its own, independent of the
# controller's fencing/reintegration logic.
#
# Run directly on ctrl:
#   bash /vagrant/controller/replication_proof.sh

set -u
NODE1=192.168.56.11   # ldap1
NODE2=192.168.56.12   # ldap2
KEY="/home/vagrant/.ssh/id_rsa"
BASE_DN="dc=research,dc=lab"
ADMIN_DN="cn=admin,$BASE_DN"
ADMIN_PW="admin"
USER_CN="demo-user-$(date +%s)"   # unique each run, avoids "already exists" on repeat demos
SSH="ssh -o StrictHostKeyChecking=no -i $KEY"

echo "=== STEP 1: Stopping ldap1 (simulating it being down) ==="
$SSH vagrant@$NODE1 "sudo systemctl stop slapd"

echo "=== STEP 2: ldap1 is now down. Adding user '$USER_CN' to ldap2 while ldap1 is offline ==="
$SSH vagrant@$NODE2 "ldapadd -x -D $ADMIN_DN -w $ADMIN_PW" <<EOF
dn: cn=$USER_CN,$BASE_DN
objectClass: organizationalRole
cn: $USER_CN
EOF

echo "=== STEP 3: Confirming the new user exists on ldap2 ==="
$SSH vagrant@$NODE2 "ldapsearch -x -D $ADMIN_DN -w $ADMIN_PW -b cn=$USER_CN,$BASE_DN -s base -LLL '(objectClass=*)'"

echo "=== STEP 4: Bringing ldap1 back up ==="
$SSH vagrant@$NODE1 "sudo systemctl start slapd"

echo "=== STEP 5: Polling ldap1 until the same user replicates over (the actual proof) ==="
FOUND=0
for i in $(seq 1 15); do
  OUT=$($SSH vagrant@$NODE1 "ldapsearch -x -D $ADMIN_DN -w $ADMIN_PW -b cn=$USER_CN,$BASE_DN -s base -LLL '(objectClass=*)'" 2>/dev/null)
  if echo "$OUT" | grep -q "dn: cn=$USER_CN"; then
    echo "=== SUCCESS: replicated to ldap1 after ~$((i*2)) seconds ==="
    echo "$OUT"
    FOUND=1
    break
  fi
  echo "  ...not yet replicated, waiting (check $i/15)"
  sleep 2
done

if [ "$FOUND" -eq 0 ]; then
  echo "=== FAILED: user did not replicate to ldap1 within 30 seconds ==="
  exit 1
fi
echo "=== DEMO COMPLETE ==="
