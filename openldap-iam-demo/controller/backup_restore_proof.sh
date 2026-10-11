#!/bin/bash
# Proves directory-level BACKUP and RESTORE using OpenLDAP's own tools
# (slapcat / slapadd) -- a DIFFERENT resilience property than
# replication: this protects against total data loss/corruption on a
# node, even if no healthy peer exists to sync from.
#
# The proof is deliberately POINT-IN-TIME, not just "data survived":
#   - A "before-backup" canary entry is added, then the backup is taken.
#   - An "after-backup" canary entry is added AFTER the backup.
#   - The database is then swapped out for an empty one (simulating
#     catastrophic loss) and restored from the backup file.
#   - The before-backup canary MUST reappear (backup/restore works).
#   - The after-backup canary MUST NOT reappear (correctly absent --
#     this is what proves it's a real point-in-time restore, not just
#     a lucky data copy).
#
# SAFETY: the original database directory is RENAMED, never deleted, so
# this is fully reversible if anything looks wrong (see ROLLBACK note
# at the end).
#
# The target node is ISOLATED from its peers for the duration of this
# demo (same iptables technique used elsewhere in this project) so that
# multi-master replication doesn't quietly refill the missing
# after-backup entry mid-demo before you get to observe it. Reconnected
# at the end.
#
# Run directly on ctrl:
#   bash /vagrant/controller/backup_restore_proof.sh <node-ip>
# e.g.
#   bash /vagrant/controller/backup_restore_proof.sh 192.168.56.12   (ldap2, the default)

set -u
NODE_IP="${1:-192.168.56.12}"
ALL_IPS=(192.168.56.11 192.168.56.12 192.168.56.13)
KEY="/home/vagrant/.ssh/id_rsa"
BASE_DN="dc=research,dc=lab"
ADMIN_DN="cn=admin,$BASE_DN"
ADMIN_PW="admin"
DB_DIR="/var/lib/ldap"            # standard Debian/Ubuntu slapd data path
BACKUP_FILE="/tmp/directory_backup_$(date +%s).ldif"
SSH="ssh -o StrictHostKeyChecking=no -i $KEY"
BEFORE_CN="backup-canary-before-$(date +%s)"
AFTER_CN="backup-canary-after-$(date +%s)"

add_entry() {
    local cn=$1
    $SSH vagrant@$NODE_IP "ldapadd -x -D $ADMIN_DN -w $ADMIN_PW" <<EOF
dn: cn=$cn,$BASE_DN
objectClass: organizationalRole
cn: $cn
EOF
}

check_entry() {
    local cn=$1
    $SSH vagrant@$NODE_IP "ldapsearch -x -D $ADMIN_DN -w $ADMIN_PW -b cn=$cn,$BASE_DN -s base -LLL '(objectClass=*)'" 2>/dev/null
}

echo "=== STEP 1: Isolating $NODE_IP from its peers (clean demo, no replication interference) ==="
for peer in "${ALL_IPS[@]}"; do
    if [ "$peer" != "$NODE_IP" ]; then
        $SSH vagrant@$NODE_IP "sudo iptables -A INPUT -s $peer -p tcp --dport 389 -j REJECT"
        $SSH vagrant@$NODE_IP "sudo iptables -A OUTPUT -d $peer -p tcp --dport 389 -j REJECT"
    fi
done

echo ""
echo "=== STEP 2: Adding a 'before-backup' canary entry: $BEFORE_CN ==="
add_entry "$BEFORE_CN"

echo ""
echo "=== STEP 3: Counting live entries, then taking the backup (slapcat) ==="
$SSH vagrant@$NODE_IP "ldapsearch -x -D $ADMIN_DN -w $ADMIN_PW -b $BASE_DN -LLL '(objectClass=*)' dn" > /tmp/live_before.txt
BEFORE_COUNT=$(grep -c "^dn:" /tmp/live_before.txt)
echo "Live entries before backup: $BEFORE_COUNT"

$SSH vagrant@$NODE_IP "sudo systemctl stop slapd"
$SSH vagrant@$NODE_IP "sudo slapcat -b '$BASE_DN' -l /tmp/backup.ldif"
scp -o StrictHostKeyChecking=no -i $KEY vagrant@$NODE_IP:/tmp/backup.ldif "$BACKUP_FILE" 2>/dev/null
BACKUP_COUNT=$(grep -c "^dn:" "$BACKUP_FILE")
echo "Entries captured in backup file ($BACKUP_FILE): $BACKUP_COUNT"
$SSH vagrant@$NODE_IP "sudo systemctl start slapd"
sleep 2

echo ""
echo "=== STEP 4: Adding an 'after-backup' canary entry: $AFTER_CN (deliberately NOT in the backup) ==="
add_entry "$AFTER_CN"
echo "Confirming it exists right now, before we touch the database:"
check_entry "$AFTER_CN"

echo ""
echo "=== STEP 5: Simulating catastrophic data loss (rename, NOT delete, the DB directory) ==="
$SSH vagrant@$NODE_IP "sudo systemctl stop slapd"
$SSH vagrant@$NODE_IP "sudo mv $DB_DIR ${DB_DIR}.bak_$(date +%s)"
$SSH vagrant@$NODE_IP "sudo mkdir -p $DB_DIR && sudo chown openldap:openldap $DB_DIR"
$SSH vagrant@$NODE_IP "sudo systemctl start slapd"
sleep 2
echo "Confirming the directory is now genuinely empty:"
$SSH vagrant@$NODE_IP "ldapsearch -x -D $ADMIN_DN -w $ADMIN_PW -b $BASE_DN -LLL '(objectClass=*)' dn" 2>&1 | head -5

echo ""
echo "=== STEP 6: RESTORING from the backup taken in Step 3 ==="
$SSH vagrant@$NODE_IP "sudo systemctl stop slapd"
scp -o StrictHostKeyChecking=no -i $KEY "$BACKUP_FILE" vagrant@$NODE_IP:/tmp/restore.ldif 2>/dev/null
$SSH vagrant@$NODE_IP "sudo slapadd -b '$BASE_DN' -l /tmp/restore.ldif"
$SSH vagrant@$NODE_IP "sudo chown -R openldap:openldap $DB_DIR"
$SSH vagrant@$NODE_IP "sudo systemctl start slapd"
sleep 3

echo ""
echo "=== STEP 7: THE PROOF ==="
AFTER_COUNT=$($SSH vagrant@$NODE_IP "ldapsearch -x -D $ADMIN_DN -w $ADMIN_PW -b $BASE_DN -LLL '(objectClass=*)' dn" | grep -c "^dn:")
echo "Entry count -- before backup: $BEFORE_COUNT | in backup file: $BACKUP_COUNT | after restore: $AFTER_COUNT"

echo ""
echo "--- Checking BEFORE-backup canary (MUST be present -- it was in the backup) ---"
BEFORE_RESULT=$(check_entry "$BEFORE_CN")
if echo "$BEFORE_RESULT" | grep -q "dn: cn=$BEFORE_CN"; then
    echo "  PRESENT (correct) -- backup and restore worked."
else
    echo "  *** MISSING -- something went wrong with the backup/restore itself ***"
fi

echo ""
echo "--- Checking AFTER-backup canary (MUST be ABSENT -- it was never in the backup) ---"
AFTER_RESULT=$(check_entry "$AFTER_CN")
if echo "$AFTER_RESULT" | grep -q "dn: cn=$AFTER_CN"; then
    echo "  *** UNEXPECTEDLY PRESENT -- would mean the restore pulled in data that"
    echo "      wasn't in the backup file, which would need investigating ***"
else
    echo "  ABSENT (correct) -- confirms this is a true point-in-time restore:"
    echo "  only what existed AT BACKUP TIME came back."
fi

echo ""
echo "=== STEP 8: Reconnecting $NODE_IP to its peers ==="
$SSH vagrant@$NODE_IP "sudo iptables -F"
echo "(Multi-master replication will now resync this node with its peers in the"
echo " background -- if you check again in ~30-60s, the after-backup canary"
echo " WILL reappear via replication, since peers still have it. That's a"
echo " separate mechanism from this backup/restore proof, worth noting as an"
echo " additional resilience layer, not a contradiction of Step 7.)"

echo ""
echo "ROLLBACK, if anything looks wrong: the pre-wipe database is still on disk"
echo "at ${DB_DIR}.bak_<timestamp> on the node -- stop slapd, move it back to"
echo "$DB_DIR, fix ownership, restart slapd."
echo ""
echo "=== DEMO COMPLETE ==="
