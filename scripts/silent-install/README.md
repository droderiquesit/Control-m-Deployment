# Silent-install XML files

Control-M/EM and Control-M/Server are installed unattended from an XML answer
file (`setup.sh -silent <file>`). **The schema of that file is owned by BMC and
changes between releases**, so the files in this directory are *examples of the
placeholder contract*, not drop-in answer files.

How to produce the real files for your release:

1. Download the EM and Server media for your version from BMC EPD.
2. On any Linux box, run the installer once interactively (or with
   `./setup.sh -silent_xml_only`, where supported) and keep the generated XML.
3. Replace the environment-specific values in that XML with the placeholders
   below. The bootstrap script substitutes them at first boot.
4. Upload the four files to the `installers` container of the platform blob
   account under the names referenced by `installer_blobs` (see
   `modules/platform/variables.tf`).

| Placeholder             | EM | Server | Value injected at boot                              |
|-------------------------|----|--------|-----------------------------------------------------|
| `__DB_HOST__`           | ✔  | ✔      | `ctm-db.<env>.ctm.internal` (CNAME to PostgreSQL)   |
| `__DB_PORT__`           | ✔  | ✔      | 5432                                                |
| `__DB_NAME__`           | ✔  | ✔      | `emdb` / `ctmdb`                                    |
| `__DB_ADMIN_USER__`     | ✔  | ✔      | PostgreSQL administrator login                      |
| `__DB_ADMIN_PASSWORD__` | ✔  | ✔      | Key Vault secret `db-admin-password`                |
| `__EM_DB_USER__`        | ✔  |        | EM database owner                                   |
| `__EM_DB_PASSWORD__`    | ✔  |        | Key Vault secret `em-db-password`                   |
| `__CTM_DB_USER__`       |    | ✔      | Server database owner                               |
| `__CTM_DB_PASSWORD__`   |    | ✔      | Key Vault secret `ctm-db-password`                  |
| `__EM_HOME__`           | ✔  |        | `/ctm/emuser`                                       |
| `__EM_USER__`           | ✔  |        | `emuser`                                            |
| `__EM_ADMIN_PASSWORD__` | ✔  |        | Key Vault secret `em-admin-password`                |
| `__CTM_HOME__`          |    | ✔      | `/ctm/controlm`                                     |
| `__CTM_USER__`          |    | ✔      | `controlm`                                          |
| `__CTM_SERVER_NAME__`   |    | ✔      | Logical Control-M/Server name, e.g. `CTM_PROD`      |
| `__HOSTNAME__`          | ✔  | ✔      | Short hostname of the node                          |
| `__PRIMARY_HOST__`      | ✔  | ✔      | `ctm-em-a` / `ctm-srv-a`                            |
| `__SECONDARY_HOST__`    | ✔  | ✔      | `ctm-em-b` / `ctm-srv-b` (empty when HA is off)     |
| `__WEB_PORT__`          | ✔  |        | 8443                                                |
| `__EM_HOST__`           |    | ✔      | `ctm-em-a`                                          |

Secondary (HA) files must select the "secondary / high availability" install
type so the installer attaches to the existing database instead of creating a
new one.
