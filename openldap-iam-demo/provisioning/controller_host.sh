#!/usr/bin/env bash
# Provisions the controller/orchestration VM: Python, HAProxy (for routing
# updates), dependencies for the remediation controller, and an SSH
# keypair so the controller can automatically SSH into fenced nodes to
# attempt repair actions (restart slapd, clear self-inflicted firewall
# rules) as part of automated remediation.
# Args: $1 = comma-separated list of LDAP node IPs
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
IFS=',' read -r -a NODE_IPS <<< "$1"

apt-get update -y
apt-get install -y python3 python3-pip python3-venv ldap-utils haproxy iptables libldap2-dev libsasl2-dev

python3 -m venv /opt/remediation-venv
/opt/remediation-venv/bin/pip install --upgrade pip
/opt/remediation-venv/bin/pip install click python-ldap prettytable pyyaml

# --- Generate an SSH keypair for the vagrant user (idempotent) and
#     publish the public key via the shared /vagrant folder so each LDAP
#     node's trust_controller.sh provisioner can pick it up and add it to
#     its own authorized_keys. This is what makes automated remediation
#     actions (SSH-based restart / iptables clear) actually work, instead
#     of failing with a permission error.
if [ ! -f /home/vagrant/.ssh/id_rsa ]; then
  sudo -u vagrant ssh-keygen -t rsa -b 2048 -N "" -f /home/vagrant/.ssh/id_rsa
fi
cp /home/vagrant/.ssh/id_rsa.pub /vagrant/ctrl_pubkey.pub

# Baseline HAProxy config: routes client LDAP traffic to whichever nodes the
# controller currently considers healthy. The controller rewrites the
# backend server 'disabled' state via the HAProxy runtime socket.
cat > /etc/haproxy/haproxy.cfg <<'EOF'
global
    log /dev/log local0
    maxconn 2000
    stats socket /run/haproxy/admin.sock mode 660 level admin expose-fd listeners
    stats timeout 30s

defaults
    log     global
    mode    tcp
    timeout connect 5s
    timeout client  30s
    timeout server  30s

frontend ldap_front
    bind *:389
    default_backend ldap_nodes

backend ldap_nodes
    balance roundrobin
EOF

id=1
for ip in "${NODE_IPS[@]}"; do
  echo "    server ldap${id} ${ip}:389 check inter 3s fall 2 rise 2" >> /etc/haproxy/haproxy.cfg
  id=$((id+1))
done

mkdir -p /run/haproxy
chmod 666 /run/haproxy/admin.sock 2>/dev/null || true
systemctl enable haproxy
systemctl restart haproxy

mkdir -p /var/log/remediation-controller
chmod 777 /var/log/remediation-controller

echo "[controller_host.sh] controller VM ready; node IPs: ${NODE_IPS[*]}"
echo "[controller_host.sh] SSH public key published to /vagrant/ctrl_pubkey.pub"
