#!/usr/bin/env bash
# Configures N-way multi-master replication (mirrormode) via syncrepl.
# Args: $1 = this node's server_id (1/2/3)
#       $2 = this node's IP
#       $3 = comma-separated list of ALL node IPs in the cluster (in server_id order)
set -euo pipefail

SERVER_ID="$1"
OWN_IP="$2"
IFS=',' read -r -a ALL_IPS <<< "$3"

BASE_DN="dc=research,dc=lab"
ADMIN_PW="admin"
REPL_PW="replicator-secret"
WORKDIR="/tmp/ldap-config"
mkdir -p "$WORKDIR"

# --- 1. Load syncprov + syncrepl support modules -----------------------------
cat > "$WORKDIR/modules.ldif" <<EOF
dn: cn=module{0},cn=config
changetype: modify
add: olcModuleLoad
olcModuleLoad: syncprov.la
EOF
ldapmodify -Y EXTERNAL -H ldapi:/// -f "$WORKDIR/modules.ldif" || true

# --- 2. Set this node's unique server ID -------------------------------------
cat > "$WORKDIR/serverid.ldif" <<EOF
dn: cn=config
changetype: modify
replace: olcServerID
olcServerID: ${SERVER_ID}
EOF
ldapmodify -Y EXTERNAL -H ldapi:/// -f "$WORKDIR/serverid.ldif"

# --- 3. Add syncprov overlay on the main database ----------------------------
cat > "$WORKDIR/syncprov.ldif" <<EOF
dn: olcOverlay=syncprov,olcDatabase={1}mdb,cn=config
changetype: add
objectClass: olcOverlayConfig
objectClass: olcSyncProvConfig
olcOverlay: syncprov
olcSpCheckpoint: 100 10
olcSpSessionLog: 100
EOF
ldapmodify -Y EXTERNAL -H ldapi:/// -f "$WORKDIR/syncprov.ldif" || true

# --- 4. First: add one olcSyncRepl entry per PEER (not self), with NO
#      mirrormode/multiprovider setting yet. (olcMirrorMode and
#      olcMultiProvider are two names for the SAME underlying attribute in
#      OpenLDAP — setting it before syncrepl exists, or setting it twice in
#      one request, both trigger "database is not a shadow". So: add
#      syncrepl alone first, then set the mirror flag once, separately.)
{
  echo "dn: olcDatabase={1}mdb,cn=config"
  echo "changetype: modify"

  id=1
  first=true
  for peer_ip in "${ALL_IPS[@]}"; do
    if [ "$peer_ip" != "$OWN_IP" ]; then
      if [ "$first" = true ]; then
        echo "add: olcSyncRepl"
        first=false
      else
        echo "-"
        echo "add: olcSyncRepl"
      fi
      cat <<RID
olcSyncRepl: rid=00${id} provider=ldap://${peer_ip}:389/ binddn="cn=admin,${BASE_DN}" bindmethod=simple credentials=${ADMIN_PW} searchbase="${BASE_DN}" type=refreshAndPersist retry="5 5 60 +" timeout=1 interval=00:00:05:00
RID
    fi
    id=$((id+1))
  done
} > "$WORKDIR/syncrepl.ldif"

ldapmodify -Y EXTERNAL -H ldapi:/// -f "$WORKDIR/syncrepl.ldif"

# --- 4b. NOW that the database has syncrepl configured (is a "shadow"),
#      turn on mirror mode in a SEPARATE follow-up request.
cat > "$WORKDIR/mirrormode.ldif" <<EOF
dn: olcDatabase={1}mdb,cn=config
changetype: modify
replace: olcMirrorMode
olcMirrorMode: TRUE
EOF
ldapmodify -Y EXTERNAL -H ldapi:/// -f "$WORKDIR/mirrormode.ldif"

# --- 5. Allow the mirrormode nodes to write (mirrormode needs a real ACL,
#         a load balancer/controller decides which one accepts writes) -------
cat > "$WORKDIR/limits.ldif" <<EOF
dn: olcDatabase={1}mdb,cn=config
changetype: modify
replace: olcLimits
olcLimits: dn.exact="cn=admin,${BASE_DN}" time.soft=unlimited time.hard=unlimited size.soft=unlimited size.hard=unlimited
EOF
ldapmodify -Y EXTERNAL -H ldapi:/// -f "$WORKDIR/limits.ldif" || true

# --- 6. Seed base DN structure once (idempotent, ignore "already exists") ----
cat > "$WORKDIR/base.ldif" <<EOF
dn: ${BASE_DN}
objectClass: top
objectClass: dcObject
objectClass: organization
o: Research Lab
dc: research

dn: ou=people,${BASE_DN}
objectClass: organizationalUnit
ou: people
EOF
ldapadd -x -D "cn=admin,${BASE_DN}" -w "$ADMIN_PW" -f "$WORKDIR/base.ldif" || true

systemctl restart slapd
echo "[configure_node.sh] node $(hostname) configured as server-id=${SERVER_ID}, mirrormode peers: ${ALL_IPS[*]}"
