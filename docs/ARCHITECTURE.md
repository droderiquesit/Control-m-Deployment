# Architecture

```mermaid
flowchart TB
  subgraph users[Users / schedulers]
    U[Control-M client, Web, Automation API]
  end

  subgraph primary["Primary region (existing VNet)"]
    LB["Standard Load Balancer<br/>ctm-web.&lt;env&gt;.ctm.internal :8443"]
    subgraph app[app subnet]
      EMA["EM primary<br/>ctm-em-a (zone 1)"]
      EMB["EM secondary<br/>ctm-em-b (zone 2)"]
      SRVA["Control-M/Server primary<br/>ctm-srv-a (zone 1)"]
      SRVB["Control-M/Server secondary<br/>ctm-srv-b (zone 2)"]
      VMSS["Agent VM Scale Set<br/>zones 1-3, autoscale, host group"]
    end
    subgraph data[data subnet]
      PG["PostgreSQL Flexible Server<br/>zone-redundant HA<br/>ctm-db.&lt;env&gt;.ctm.internal"]
    end
    subgraph pe[private endpoints]
      KV[(Key Vault)]
      BLOB[(Blob: installers / backups)]
      NFS[(Premium Files NFS<br/>/mnt/ctm-shared)]
    end
    LAW[Log Analytics + alerts]
    RSV[Recovery Services vault<br/>VM + file share backup]
  end

  subgraph dr["DR region (existing VNet)"]
    LBD[Standard Load Balancer]
    PGR["PostgreSQL read replica<br/>(promote on failover)"]
    ASR["Site Recovery replicas<br/>ctm-em-a/b, ctm-srv-a/b"]
    VMSSD[Warm agent scale set]
    KVD[(Key Vault replica secrets)]
    BLOBD[(Blob - object replication)]
  end

  U --> LB
  LB --> EMA
  LB -. standby .-> EMB
  EMA <--> SRVA
  EMB <--> SRVB
  SRVA <--> VMSS
  EMA & EMB & SRVA & SRVB --> PG
  EMA & EMB & SRVA & SRVB & VMSS --> KV & BLOB & NFS
  EMA & EMB & SRVA & SRVB & VMSS --> LAW
  EMA & EMB & SRVA & SRVB --> RSV
  PG == async replication ==> PGR
  EMA & EMB & SRVA & SRVB == disk replication ==> ASR
  BLOB == object replication ==> BLOBD
  U -. Traffic Manager / DNS switch .-> LBD
  LBD --> ASR
  ASR --> PGR
  VMSSD --> ASR
```

## Design decisions

**Why VMs and not Kubernetes for EM/Server.** BMC supports the EM and Server
components on Linux hosts with a shared external PostgreSQL. Their HA model is
an active/standby pair bound to stable hostnames and a shared database, which
maps directly onto two zonal VMs with persistent data disks. Agents, which are
stateless from the platform's point of view, go into a scale set.

**Why one composition module with a `role`.** The DR region needs the same
foundation (identity, secrets, storage, LB, monitoring, DNS aliases) but not
the database or VMs, which arrive through replication. Reusing `modules/platform`
with `role = "dr"` keeps the two regions drift-free.

**Why aliases instead of real hostnames in Control-M config.** Nodes connect
to `ctm-db.<env>.ctm.internal` and `ctm-web.<env>.ctm.internal`. The same zone
name exists in both regions, linked to each region's VNet. After failover the
CNAME in the DR zone points at the promoted replica and Site Recovery brings
the VMs up with their original hostnames, so Control-M starts unchanged.

**Why PostgreSQL Flexible Server.** Zone-redundant HA with automatic failover,
geo-redundant backups and cross-region read replicas cover HA and DR without
database administration on the VMs. Control-M supports external PostgreSQL.

**Why Premium Files NFS.** Installer media cache, EM/Server backup exports and
shared logs need a POSIX filesystem reachable from every node in every zone.
Premium ZRS Files over NFS 4.1 through a private endpoint provides that.

**Why Site Recovery for VMs.** The Control-M installation is tied to the host
(home directory, hostname, local config). Replicating the OS and data disks
gives an identical host in the DR region in minutes, versus re-installing and
re-registering a secondary EM/Server across regions.

**Scaling.** Agents scale horizontally on CPU with min/max per environment and
deregister gracefully on scale-in via Azure Scheduled Events. EM, Server and
database scale vertically by changing one tfvars value; the data disks and
database storage auto-grow.

## Identity and secrets

* One user-assigned managed identity per region for all nodes. Rights:
  Key Vault Secrets User, Storage Blob Data Reader (installers), Storage Blob
  Data Contributor (backups container).
* All passwords and the SSH key are generated once by Terraform in the
  environment root (`secrets.tf`) and written to the Key Vault in every region.
  The SSH private key is available as the `ssh-private-key` secret.
* No secrets are embedded in cloud-init; nodes fetch them with the IMDS token.
