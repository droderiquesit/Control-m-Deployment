# Silent-install XML files

Control-M/EM, Control-M/Server and Control-M/Agent are installed unattended
from an XML answer file (`setup.sh -silent <file>`). **The schema of that file
is owned by BMC and changes between releases**, so the files in this directory
are *examples of the placeholder contract*, not drop-in answer files.

How to produce the real files for your release:

1. Download the EM and Server media for your version from BMC EPD.
2. On any Linux box, run the installer once interactively (or with
   `./setup.sh -silent_xml_only`, where supported) and keep the generated XML.
3. Replace the environment-specific values in that XML with the placeholders
   below. The bootstrap script substitutes them at first boot.
4. EM/Server files: upload them to the `installers` container of the
   platform blob account under the names referenced by `installer_blobs`
   (see `environments/<env>/config.yaml`; defaults in
   `deploy/ctmdeploy/config.py`) — the pipeline / `ctmdeploy media upload`
   does this automatically from the Azure Artifacts packages.
   `silent-agent.xml` is different: it never goes to blob — it is consumed
   by the **Packer ctm-agent image build** (v4.1; see `packer/README.md`).

| Placeholder                | EM | Server | Agent | Value injected at boot                              |
|----------------------------|----|--------|-------|-----------------------------------------------------|
| `__DB_HOST__`              | ✔  | ✔      |       | `ctm-db.<platform>.ctm.internal` (CNAME to PostgreSQL) |
| `__DB_PORT__`              | ✔  | ✔      |       | 5432                                                |
| `__DB_NAME__`              | ✔  | ✔      |       | `emdb` / `ctm_<stage>`                              |
| `__DB_ADMIN_USER__`        | ✔  | ✔      |       | PostgreSQL administrator login                      |
| `__DB_ADMIN_PASSWORD__`    | ✔  | ✔      |       | Key Vault secret `db-admin-password`                |
| `__EM_DB_USER__`           | ✔  |        |       | EM database owner                                   |
| `__EM_DB_PASSWORD__`       | ✔  |        |       | Key Vault secret `em-db-password`                   |
| `__CTM_DB_USER__`          |    | ✔      |       | Server database owner                               |
| `__CTM_DB_PASSWORD__`      |    | ✔      |       | Key Vault secret `ctm-db-password-<stage>`          |
| `__EM_HOME__`              | ✔  |        |       | `/ctm/emuser`                                       |
| `__EM_USER__`              | ✔  |        |       | `emuser`                                            |
| `__EM_ADMIN_PASSWORD__`    | ✔  |        |       | Key Vault secret `em-admin-password`                |
| `__CTM_HOME__`             |    | ✔      |       | `/ctm/controlm`                                     |
| `__CTM_USER__`             |    | ✔      |       | `controlm`                                          |
| `__CTM_SERVER_NAME__`      |    | ✔      | ✔     | Logical Control-M/Server name, e.g. `CTM_PROD`      |
| `__HOSTNAME__`             | ✔  | ✔      | ✔     | Short hostname of the node                          |
| `__PRIMARY_HOST__`         | ✔  | ✔      | ✔     | `ctm-em-a` / `ctm-srv-<stage>-a`                    |
| `__SECONDARY_HOST__`       | ✔  | ✔      |       | `ctm-em-b` / `ctm-srv-<stage>-b` (empty when HA is off) |
| `__WEB_PORT__`             | ✔  |        |       | 8443                                                |
| `__EM_HOST__`              |    | ✔      |       | `ctm-em-a`                                          |
| `__AGENT_HOME__`           |    |        | ✔     | `/home/ctmagent/ctm` (INSTALL_DIR) — at image-BUILD time |
| `__AGENT_USER__`           |    |        | ✔     | `ctmagent` (OS_USER) — at image-build time          |
| `__AGENT_TO_SERVER_PORT__` |    |        | ✔     | 7005 (BMC default) — at image-build time            |
| `__SERVER_TO_AGENT_PORT__` |    |        | ✔     | 7006 — placeholder at build; the REAL `servers.<stage>.agents.agent_port` is written into CONFIG.dat at boot |

Secondary (HA) files must select the "secondary / high availability" install
type so the installer attaches to the existing database instead of creating a
new one.

`silent-agent.xml` is the Control-M/Agent answer file consumed by the
**Packer ctm-agent image build** (`packer/scripts/30-agent-install.sh`,
v4.1): its placeholders are substituted at image-build time with PLACEHOLDER
server settings (and `START_AGENT` forced to `no` — the baked agent is never
started or registered during the build). At boot,
`node-scripts/ctm_node_install.py` writes the real server name/ports/
hostname into the baked agent's `CONFIG.dat`, starts it and registers it
with the EM Automation API. The agent file carries NO secrets.
