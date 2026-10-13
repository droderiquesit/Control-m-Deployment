# Network requirements (bring-your-own VNet)

The platform never creates or modifies a VNet, subnet, NSG, route table or
firewall. Each environment (and each DR region) needs the following to exist
before `terraform apply`.

## Subnets

| tfvars key | Purpose | Size | Must have |
|------------|---------|------|-----------|
| `app_subnet_name` | EM and Server VMs, agent scale set, LB frontend | /24 minimum, /22 recommended for prod agents | outbound internet (NAT Gateway, Azure Firewall or proxy) for OS packages and BMC media mirrors |
| `data_subnet_name` | PostgreSQL Flexible Server | /28 minimum (/27 if DR replica shares the layout) | **delegation to `Microsoft.DBforPostgreSQL/flexibleServers`**, no other resources |
| `pe_subnet_name` (optional) | Private endpoints for Key Vault, Blob and Files | /27 | private endpoint network policies disabled (default) |

## NSG rules on the app subnet

Control-M components talk over many fixed and dynamic TCP ports (CORBA naming
service, GUI server, gateway, agent/server 7005-7006, Configuration Agent 2369,
Web 8443). Keep it simple and secure at the subnet boundary:

| Priority | Direction | Source | Destination | Ports | Note |
|----------|-----------|--------|-------------|-------|------|
| 100 | Inbound | app subnet | app subnet | * | EM ↔ Server ↔ Agents |
| 110 | Inbound | `AzureLoadBalancer` | * | 8443 | health probes |
| 120 | Inbound | user / admin CIDRs | * | 443, 8443 | Control-M Web, Automation API, thick client |
| 130 | Inbound | Bastion / jump subnet | * | 22 | operator SSH |
| 4000 | Inbound | * | * | * | deny |
| — | Outbound | app subnet | data subnet | 5432 | PostgreSQL |
| — | Outbound | app subnet | Internet | 443 | packages, BMC support, Azure APIs (or use private endpoints + service tags) |

Jobs that agents run may need additional outbound rules (SFTP, databases,
SaaS APIs) — those belong to the agent host group's own NSG policy.

## DNS

* The platform creates one private DNS zone per region named
  `<env>.ctm.internal` with **auto-registration** linked to the VNet. VM
  hostnames (`ctm-em-a`, `ctm-srv-a`, `ctm-agent-xxxxxx`) resolve without
  manual records; `ctm-web` and `ctm-db` are managed aliases.
* Private endpoints need the standard zones `privatelink.blob.core.windows.net`,
  `privatelink.file.core.windows.net`, `privatelink.vaultcore.azure.net` and a
  `<x>.postgres.database.azure.com` zone linked to the VNet. Pass hub-managed
  zone ids through `existing_private_dns_zone_ids`; anything omitted is created
  in the platform resource group and linked to the VNet.
* If the VNet uses custom DNS servers (hub resolver / AD DNS), they must forward
  `*.ctm.internal`, the privatelink zones and `*.postgres.database.azure.com` to
  Azure DNS (`168.63.129.16`) or a DNS Private Resolver.

## Cross-region (DR)

* Primary and DR VNets must not overlap if you ever peer them for failback
  copies; otherwise no connectivity between them is required (replication is
  over Azure backbone services).
* The DR VNet needs the same three subnet roles. Site Recovery recreates the
  EM/Server NICs in the DR `app` subnet; NSG rules must mirror the primary.
* Platform secrets, DNS aliases and hostnames are identical in both regions by
  design, so no application reconfiguration is needed on failover.
