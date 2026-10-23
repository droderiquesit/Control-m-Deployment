#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Golden image: final generalization step. Resets cloud-init so every VM/VMSS
# instance booted from this image runs its own bootstrap (customData), then
# deprovisions the Azure agent and removes the build user. Must run LAST.
# ---------------------------------------------------------------------------
set -euo pipefail

# Shrink the image and drop package caches.
dnf clean all
rm -rf /var/cache/dnf

# Reset cloud-init completely: instances booted from the image must treat
# first boot as a fresh provisioning run and execute their customData.
cloud-init clean --logs --seed || cloud-init clean --logs

# Truncate logs and shell history left over from the build.
find /var/log -type f -name '*.log' -exec truncate -s 0 {} + 2>/dev/null || true
rm -f /root/.bash_history
export HISTSIZE=0

# Generalize: remove machine-specific state and the temporary build user.
# (waagent exits after deprovisioning; keep it the last command.)
sync
waagent -force -deprovision+user
