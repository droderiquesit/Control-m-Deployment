# identity/ — the platform deployer role

One file: `deployer-role.bicep`, a **custom subscription role** named
**Control-M Platform Deployer** that lets one identity (the Azure DevOps WIF
service connection, a workstation principal, …) deploy **everything this
repository ships except the network**. VNets, subnets, NSGs and peerings are
deliberately not writable — the network is owned by the cloud team through
the `network/` folder, with its own identity.

The complete per-phase permission matrix (management plane, the data-plane
roles this role cannot replace, the cloud team's identity, and the Azure
DevOps elastic-pool note) lives in `docs/IDENTITY-PERMISSIONS.md`.

## Deploy the role, assign it

```bash
# 1. Create/refresh the role definition (idempotent — deterministic guid name):
az deployment sub create -l eastus2 -f identity/deployer-role.bicep

# 2. Assign it to the deploying identity (the WIF service connection's
#    service principal, or whoever runs ctmdeploy):
SP_OBJECT_ID=$(az ad sp show --id <appId-or-spn-name> --query id -o tsv)
az role assignment create \
  --assignee-object-id "$SP_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Control-M Platform Deployer" \
  --scope "/subscriptions/$(az account show --query id -o tsv)"
```

Deploying the role definition itself requires
`Microsoft.Authorization/roleDefinitions/write` (built-in **User Access
Administrator** or Owner) — a one-time, human-run step; the pipeline never
needs it.

## What the role can and cannot do

| | Covered |
|---|---|
| **Can** | resource groups + locks; deployments/what-if; VMs, VM scale sets, disks, galleries/images; storage accounts (management plane, object replication included); Key Vault management; PostgreSQL Flexible Server; Recovery Services (backup + Site Recovery); Log Analytics/VMInsights/DCRs/alerts/autoscale/action groups; App Service plans + Function Apps (scm publish included); managed identities; load balancers, Application Gateway, Bastion, public IPs, NICs, private endpoints, private DNS zones (links + records), Traffic Manager; **join** existing subnets/VNets; role assignments (scoped use — see below) |
| **Cannot** | create or modify **VNets, subnets, NSGs, peerings** (no `virtualNetworks/write`, `subnets/write`, `networkSecurityGroups/*` or `virtualNetworkPeerings/*` action); read or write **secret values** (data plane); upload **blobs** (data plane) |

Two data-plane grants complete the deployer (management actions cannot
substitute for them):

| Data-plane role | Scope | Why |
|---|---|---|
| **Key Vault Secrets Officer** | the platform vaults | `--phase secrets` seeds them. Granted *automatically* by the foundation deployment to `CTM_DEPLOYER_OBJECT_ID` — this role's `roleAssignments/write` is what allows that grant. |
| **Storage Blob Data Contributor** | the platform storage accounts (prod: primary **and** DR) | `ctmdeploy media upload` and the compute phase's node-script upload are data-plane blob writes. |

`roleAssignments/write` is subscription-visible but its **documented use is
scoped**: the templates only create assignments at Key Vault / storage /
container scope with deterministic GUID names (Secrets User/Officer, Blob
Data Reader/Contributor). ARM cannot express that restriction inside an
action list; the review gate is the pipeline's what-if artifact, which shows
every role assignment before an approver lets the apply run.

## Who needs what instead

* **Cloud team (`network/` folder)**: built-in **Network Contributor** on the
  `rg-network-*` resource groups (plus the right to create those groups at
  subscription scope). The platform deployer only *joins* their subnets.
* **Azure DevOps elastic pool**: after the `access` phase creates the
  `ctm-build-agents` VMSS, Azure DevOps' own service principal needs
  **Virtual Machine Contributor** on `ctm-build-rg` to manage pool capacity
  — documented in `docs/AZURE-DEVOPS.md`, deliberately not granted by any
  template here.

## Built-in fallback

No custom roles allowed in your tenant? **Contributor + User Access
Administrator** on the subscription (the combination `docs/SETUP.md` lists as
the generic prerequisite) runs every phase too — but it can write VNets,
NSGs and peerings and assign any role anywhere. The custom role exists
precisely to remove that surface; prefer it wherever custom roles are
permitted.
