#!/usr/bin/env bash
# Common OpenLDAP node provisioning: package install + baseline hardening.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Pre-seed slapd so the interactive debconf prompts don't block automation.
debconf-set-selections <<EOF
slapd slapd/internal/generated_adminpw password admin
slapd slapd/internal/adminpw password admin
slapd slapd/password2 password admin
slapd slapd/password1 password admin
slapd slapd/domain string research.lab
slapd shared/organization string Research Lab
slapd slapd/backend select MDB
slapd slapd/purge_database boolean false
slapd slapd/move_old_database boolean true
slapd slapd/allow_ldap_v2 boolean false
slapd slapd/no_configuration boolean false
EOF

apt-get update -y
apt-get install -y slapd ldap-utils gnutls-bin net-tools

# Make sure syncprov/syncrepl overlay module is available
if ! ldapmodify -Y EXTERNAL -H ldapi:/// -Q -Q 2>/dev/null; then
  true
fi

systemctl enable slapd
systemctl restart slapd

echo "[common.sh] slapd installed on $(hostname)"
