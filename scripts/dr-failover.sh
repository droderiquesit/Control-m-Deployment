#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Control-M DR failover (primary region -> DR region). Run from a workstation
# with az CLI logged in. Companion to docs/DR-RUNBOOK.md.
#
#   ./scripts/dr-failover.sh <env> [--test]
#
# Steps:
#   1. Promote the PostgreSQL cross-region replica to a standalone server.
#   2. Fail over EM/Server VMs with Azure Site Recovery (latest recovery point).
#   3. Point ctm-db.<env>.ctm.internal in the DR private zone at the promoted DB.
#   4. Scale the DR agent scale set to the production minimum.
# ---------------------------------------------------------------------------
set -euo pipefail

ENV="${1:?usage: $0 <prod|nonprod> [--test]}"
MODE="${2:-}"
cd "$(dirname "$0")/../environments/$ENV"

tf_out() { terraform output -json | jq -r "$1"; }

DR_RG=$(tf_out '.dr.value.resource_group')
REPLICA_FQDN=$(tf_out '.dr.value.replica_fqdn')
REPLICA_NAME="${REPLICA_FQDN%%.*}"
ASR_VAULT=$(tf_out '.dr.value.site_recovery_vault')
ZONE="$ENV.ctm.internal"

echo "== Control-M DR failover: env=$ENV dr_rg=$DR_RG replica=$REPLICA_NAME mode=${MODE:-real}"

if [ "$MODE" = "--test" ]; then
  echo "== TEST: running Site Recovery test failover only (no DB promotion, no DNS change)"
  for vm in $(tf_out '.dr.value.replicated_vms[]'); do
    az site-recovery replicated-vm test-failover --resource-group "$DR_RG" --vault-name "$ASR_VAULT" \
      --fabric-name "ctm-$ENV-fabric-primary" --protection-container "ctm-$ENV-container-primary" \
      --name "$vm" --failover-direction PrimaryToRecovery || true
  done
  exit 0
fi

read -r -p "This promotes the DR database and breaks replication. Continue? [yes/NO] " ans
[ "$ans" = "yes" ] || exit 1

echo "== 1/4 promoting PostgreSQL replica"
az postgres flexible-server replica promote --resource-group "$DR_RG" --name "$REPLICA_NAME" \
  --promote-mode standalone --promote-option forced

echo "== 2/4 Site Recovery unplanned failover of EM/Server VMs"
for vm in $(tf_out '.dr.value.replicated_vms[]'); do
  az site-recovery replicated-vm unplanned-failover --resource-group "$DR_RG" --vault-name "$ASR_VAULT" \
    --fabric-name "ctm-$ENV-fabric-primary" --protection-container "ctm-$ENV-container-primary" \
    --name "$vm" --failover-direction PrimaryToRecovery
done

echo "== 3/4 ensuring ctm-db.$ZONE -> $REPLICA_FQDN in the DR private zone"
az network private-dns record-set cname set-record --resource-group "$DR_RG" --zone-name "$ZONE" \
  --record-set-name ctm-db --cname "$REPLICA_FQDN" >/dev/null

echo "== 4/4 scaling DR agent pools (one per Control-M/Server)"
for key in $(tf_out '.servers.value | keys[]'); do
  MIN=$(python3 - "$key" <<'PY2'
import re,sys
key=sys.argv[1]; txt=open("terraform.tfvars").read()
blk=re.search(r'\b'+key+r'\s*=\s*\{.*?agents\s*=\s*\{(.*?)\}', txt, re.S)
m=re.search(r'\bmin\s*=\s*(\d+)', blk.group(1)) if blk else None
print(m.group(1) if m else 1)
PY2
)
  echo "   $key -> $MIN"
  az vmss scale --resource-group "$DR_RG" --name "ctm-$ENV-dr-agents-$key" --new-capacity "$MIN" >/dev/null
done

cat <<MSG

Failover issued. Next (see docs/DR-RUNBOOK.md):
  * wait for the ASR jobs, then 'systemctl status controlm-em controlm-server' on the failed-over VMs
  * commit the failover in Site Recovery once validated
  * re-point Traffic Manager / users to the DR load balancer if not automatic
  * run 'terraform apply' for $ENV later to reconcile state (replica is now standalone)
MSG
