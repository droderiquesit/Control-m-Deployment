#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Golden image: OS tuning for Control-M (same values the platform always ran
# with). Host firewall off (NSGs are the enforced boundary; Control-M uses
# many dynamic ports between EM, Server and Agents), SELinux permissive per
# BMC documentation, file/process limits and kernel parameters.
# ---------------------------------------------------------------------------
set -euo pipefail

# NSGs are the enforced boundary; host firewall would otherwise block the many
# dynamic ports Control-M uses between EM, Server and Agents.
systemctl disable --now firewalld >/dev/null 2>&1 || true

# BMC documents Control-M with SELinux permissive/disabled.
setenforce 0 || true
sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config || true

cat > /etc/security/limits.d/90-controlm.conf <<'LIMITS'
*    soft nofile 65536
*    hard nofile 65536
*    soft nproc  32768
*    hard nproc  32768
LIMITS

cat > /etc/sysctl.d/90-controlm.conf <<'SYSCTL'
fs.file-max = 2097152
net.core.somaxconn = 4096
net.ipv4.tcp_keepalive_time = 300
kernel.shmmax = 68719476736
SYSCTL
sysctl --system >/dev/null

echo "== 10-os-tuning: done"
