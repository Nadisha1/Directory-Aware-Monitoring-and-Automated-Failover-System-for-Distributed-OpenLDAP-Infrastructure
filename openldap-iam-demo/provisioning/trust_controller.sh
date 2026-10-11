#!/usr/bin/env bash
# Adds the controller VM's SSH public key to this node's authorized_keys,
# so the remediation controller can SSH in to restart slapd or clear
# self-inflicted iptables rules as part of automated recovery actions.
#
# This depends on the ctrl VM having already generated its keypair and
# published the public half to the shared /vagrant folder (see the key
# generation step added to provisioning/controller_host.sh). Because
# ldap1/ldap2/ldap3 provision before ctrl on a first-time `vagrant up`,
# this script may need to be re-run once ctrl exists:
#
#   vagrant provision ctrl
#   vagrant provision ldap1 ldap2 ldap3
#
set -euo pipefail

PUBKEY_FILE="/vagrant/ctrl_pubkey.pub"

if [ ! -f "$PUBKEY_FILE" ]; then
  echo "[trust_controller.sh] $PUBKEY_FILE not found yet — the ctrl VM"
  echo "  hasn't generated its key yet. Run 'vagrant provision ctrl' first,"
  echo "  then re-run 'vagrant provision $(hostname)' (or all ldap nodes)."
  exit 0
fi

mkdir -p /home/vagrant/.ssh
touch /home/vagrant/.ssh/authorized_keys

if ! grep -qF "$(cat "$PUBKEY_FILE")" /home/vagrant/.ssh/authorized_keys 2>/dev/null; then
  cat "$PUBKEY_FILE" >> /home/vagrant/.ssh/authorized_keys
  echo "[trust_controller.sh] Added controller's public key to authorized_keys"
else
  echo "[trust_controller.sh] Controller's public key already trusted"
fi

chmod 700 /home/vagrant/.ssh
chmod 600 /home/vagrant/.ssh/authorized_keys
chown -R vagrant:vagrant /home/vagrant/.ssh
