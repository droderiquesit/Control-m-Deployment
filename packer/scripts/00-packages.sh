#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Golden image: Control-M OS package prerequisites (RHEL 9 family, per BMC
# docs). Previously installed by cloud-init on every first boot; baking them
# here makes node boot faster and independent of repo availability.
# ---------------------------------------------------------------------------
set -euo pipefail

dnf -y install jq curl unzip tar nfs-utils bind-utils net-tools which hostname \
  libnsl libnsl2 ncurses-libs ncurses-compat-libs glibc-langpack-en xfsprogs \
  policycoreutils-python-utils chrony

echo "== 00-packages: done"
