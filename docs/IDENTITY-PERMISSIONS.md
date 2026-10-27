# Identity permissions — who needs what to deploy and run the platform

Three deploy-time identities and three runtime identities touch this
repository. This page is the authoritative matrix: which identity needs which
role, at which scope, for which phase. The custom role itself lives in
`identity/deployer-role.bicep` (quickstart: `identity/README.md`).

| Identity | Who it is | Gets |
|---|---|---|
| **Platform deployer** | the pipeline's WIF service connection (`sc-ctm-platform-wif`), or whoever runs `ctmdeploy` | custom role **Control-M Platform Deployer** (subscription) + the two data-plane roles below |
| **Cloud team** | whoever deploys the `network/` folder | **Network Contributor** on the `rg-network-*` groups + resource-group create |
| **Azure DevOps** | ADO's own service principal managing the elastic agent pool | **Virtual Machine Contributor** on `ctm-build-rg` |
| *(runtime)* node identity `ctm-<env>[-dr]-nodes-id` | every EM/Server VM + agent VMSS instance | granted by the foundation templates (below) |
| *(runtime)* kvsync function MI | the `<name>-kvsync` Function App | granted by the access phase (below) |
| *(runtime)* build identity `ctm-build-id` | the `ctm-build-agents` VMSS | **nothing by default** — pipeline deployments authenticate through the WIF service connection, not this identity |

## The platform deployer: per-phase matrix

Management plane = covered by the **Control-M Platform Deployer** custom role
(`identity/deployer-role.bicep`). Data plane = the extra built-in roles that
no management action can replace.

| Phase / command | What it touches | Management-plane needs (in the custom role) | Data-plane needs (grant separately) |
|---|---|---|---|
| `--phase network` | **NOT COVERED — by design.** `network/main.bicep` writes VNets/subnets/NSGs/peerings | none in this role; the deployment is refused. Run it with the cloud-team identity (or the built-in fallback) when `network_infrastructure.create: true` | — |
| `--phase rg` | platform resource groups + `CanNotDelete` locks | `Resources/subscriptions/resourceGroups/*`, `Authorization/locks/*` | — |
| `--phase foundation` | managed identity, Key Vault (+PE), storage (+PEs), PostgreSQL, LB, private DNS zones/links/records, Log Analytics, DCR, alerts, role assignments to the node identity and the deployer | `ManagedIdentity/*`, `KeyVault/vaults/*`, `Storage/storageAccounts/*`, `DBforPostgreSQL/*`, `Network/loadBalancers/*`, `Network/privateEndpoints/*`, `Network/privateDnsZones/*`, `Network/virtualNetworks/subnets/join/action` + `virtualNetworks/join/action` (**on the cloud team's VNets** — join, never write), `OperationalInsights/*`, `Insights/*`, `Authorization/roleAssignments/read+write` | — |
| `--phase secrets` | seeds the six platform secrets into every region vault | (vault reachability only) | **Key Vault Secrets Officer** on each platform vault — the foundation grants it automatically to `CTM_DEPLOYER_OBJECT_ID` / `--deployer-object-id` |
| `--phase compute` | uploads `node-scripts/ctm_node_install.py` to the installers container **first**, then VMs/NICs/disks, agent VMSS, autoscale, Recovery Services backup, VM alerts | `Compute/*` (pinned set), `Network/networkInterfaces/*`, `Network/virtualNetworks/subnets/join/action`, `RecoveryServices/*`, `Insights/*` | **Storage Blob Data Contributor** on the platform blob accounts (prod: primary **and** DR) for the script upload |
| `--phase access` | Bastion (+PIP), private Application Gateway + `ctm-global` A record, kvsync Function infra (plan/storage/app + its Key Vault Secrets User grant), `ctm-build-rg` + build-agent VMSS + `ctm-build-id` | `Network/bastionHosts/*`, `Network/publicIPAddresses/*`, `Network/applicationGateways/*`, `Network/privateDnsZones/*`, `Web/serverfarms/*` + `Web/sites/*`, `Storage/storageAccounts/*`, `ManagedIdentity/*`, `Compute/virtualMachineScaleSets/*`, `Authorization/roleAssignments/write` (the function's Secrets User grant), subnet joins | — |
| `--phase dr` | PostgreSQL read replica, ASR vault + cache storage, blob object-replication policies, optional Traffic Manager | `DBforPostgreSQL/*`, `RecoveryServices/*`, `Storage/storageAccounts/*` (object replication is management plane), `Network/trafficmanagerprofiles/*` | — |
| `--phase replication` | SDK-driven ASR fabrics/containers/policy/mappings/protected items + EM NIC → DR LB pool | `RecoveryServices/*`, `Network/*/read`, `Network/loadBalancers/*` | — |
| `ctmdeploy image gallery/build` | `ctm-images-rg`, Compute Gallery, image definition; Packer build VM + managed image + gallery version | `Resources/subscriptions/resourceGroups/write`, `Compute/galleries/*`, `Compute/images/*`, `Compute/virtualMachines/*`, `Network/networkInterfaces/*` + `publicIPAddresses/*` (Packer's temporary build VM NIC/PIP) | — |
| `ctmdeploy media upload` | blob uploads: `em/`, `server/` media + `scripts/ctm_node_install.py` (v4.1: no agent media — it feeds the Packer ctm-agent build) | `Storage/storageAccounts/read` (endpoint resolution) | **Storage Blob Data Contributor** on the platform blob accounts (prod: primary **and** DR) |
| `ctmdeploy kvsync deploy` | zip-deploy of `functions/kvsync` to the `<name>-kvsync` scm endpoint with an ARM bearer token | `Web/sites/*` (includes `Microsoft.Web/sites/publish/action`, which the scm zipdeploy auth checks) | — |
| `ctmdeploy failover` | ASR failover + replica promotion + DNS flips | `RecoveryServices/*`, `DBforPostgreSQL/*`, `Network/privateDnsZones/*` | — |

**The data-plane pair, spelled out** (assign once, to the same principal that
holds the custom role):

```bash
SUB=$(az account show --query id -o tsv)
# secrets seeding (also granted automatically by the foundation to
# CTM_DEPLOYER_OBJECT_ID — this explicit grant covers the FIRST foundation run
# racing the secrets phase, and workstation use):
az role assignment create --assignee-object-id "$SP_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets Officer" --scope "/subscriptions/$SUB/resourceGroups/ctm-prod-rg"
# media + node-script uploads (repeat for ctm-prod-dr-rg and ctm-nonprod-rg):
az role assignment create --assignee-object-id "$SP_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" --scope "/subscriptions/$SUB/resourceGroups/ctm-prod-rg"
```

## What the deployer can NEVER do (the network boundary)

The custom role's `Microsoft.Network` surface is: **read everything**, write
only `loadBalancers`, `applicationGateways`, `bastionHosts`,
`publicIPAddresses`, `networkInterfaces`, `privateEndpoints`,
`privateDnsZones` (links and records included) and `trafficmanagerprofiles`,
plus two join actions. There is **no** `virtualNetworks/write`,
`virtualNetworks/subnets/write`, `networkSecurityGroups/*` or
`virtualNetworkPeerings/*` action — a compromised pipeline cannot open an NSG,
re-peer a VNet or widen an address space.

### The subnets/join requirement on the network resource groups

Placing resources into the cloud team's subnets is the action
`Microsoft.Network/virtualNetworks/subnets/join/action` (NIC/PE/appgw/VNet
integration placement), and linking private DNS zones to their VNets is
`Microsoft.Network/virtualNetworks/join/action`. The custom role carries both
**at subscription scope**, which covers the `rg-network-*` groups. If you
instead build the deployer from narrower assignments (e.g. built-in roles
scoped to the `ctm-*` platform RGs only), you must additionally grant those
two actions on the `rg-network-*` resource groups or every foundation,
compute and access deployment fails with *LinkedAuthorizationFailed*.

## The cloud team identity (network/ folder)

```bash
az role assignment create --assignee-object-id "$CLOUD_TEAM_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Network Contributor" --scope "/subscriptions/$SUB"
```

* **Network Contributor** (built-in) covers every resource `network/main.bicep`
  creates: VNets, subnets, NSGs, delegations and peerings.
* The deployment also **creates the `rg-network-*` resource groups** at
  subscription scope, which Network Contributor does not include — grant
  `Microsoft.Resources/subscriptions/resourceGroups/write` alongside it (a
  tiny custom role, or pre-create the groups and scope Network Contributor to
  them).
* Deployment command and plan: `network/README.md`. The platform deployer
  role deliberately cannot run this deployment.

## Azure DevOps elastic pool (build agents)

The `access` phase creates the `ctm-build-agents` VMSS but **does not grant
Azure DevOps anything**. To register the scale set as an ADO elastic pool,
Azure DevOps' own service principal (the one the elastic-pool configuration
runs as) needs **Virtual Machine Contributor** on the `ctm-build-rg` resource
group, so the pool can create/delete/reimage instances. Setup steps:
`docs/AZURE-DEVOPS.md` §6 and `docs/SETUP.md`.

## Runtime identities (granted by the templates — nothing to do manually)

| Identity | Role | Scope | Granted by |
|---|---|---|---|
| `ctm-<env>[-dr]-nodes-id` (all EM/Server VMs + agent VMSS instances) | Key Vault Secrets User | region vault | foundation |
| 〃 | Storage Blob Data Reader | region blob account (**this is what lets every node — agent VMSS instances included — fetch `scripts/ctm_node_install.py`, and the EM/Server nodes their installer media, from blob**) | foundation |
| 〃 | Storage Blob Data Contributor | `backups` container only | foundation |
| `<name>-kvsync` system-assigned MI | Key Vault Secrets User (**read-only** — the function reads tagged secrets and `em-admin-password`; it can never write the vault) | platform vault | access phase (`bicep/modules/function-kvsync.bicep`) |
| `ctm-build-id` (build-agent VMSS) | — none — | — | access phase creates the identity only; deployments from the agents authenticate via the WIF service connection |

## Built-in-roles fallback

Where custom roles are not allowed, the platform deployer can run on
built-ins — the original `docs/SETUP.md` prerequisite:

| Built-in role | Scope | Replaces |
|---|---|---|
| **Contributor** | subscription | every management-plane action above — *including* network writes the custom role withholds |
| **User Access Administrator** | subscription | `roleAssignments/write` — *any* role, *any* scope, versus the custom role's same action reviewed through what-if |
| Key Vault Secrets Officer + Storage Blob Data Contributor | as above | unchanged — data plane is identical in both models |

The fallback works everywhere but is strictly broader: it can modify the
cloud team's VNets/NSGs/peerings and grant arbitrary roles. Prefer the custom
role; keep the fallback for tenants that prohibit custom role definitions.
