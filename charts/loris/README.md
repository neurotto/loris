# LORIS Helm chart

Deploy [LORIS](https://loris.ca/), a web-based platform to manage neuroimaging
and research data, with a bundled MariaDB database and optional read-only BIDS
dataset mounts.

This chart packages a **development deployment**, not a production-certified
LORIS installation. It uses a custom image containing LORIS and LORIS-MRI
**29.0.0**, an automated installer, and viewer, Module Manager and on-demand
site content patches.
Chart version: **0.3.1**.

> **Important:** The chart is disabled by default (`enabled: false`). Enabling it
> without configuring persistence uses ephemeral storage for the database,
> project configuration, and application data. Do not use those defaults for
> data you need to retain.

## Features

- LORIS web portal and single-instance MariaDB 10.11.10 database.
- Automatic initial schema, database account, frontend administrator, and project
  configuration setup.
- Configurable persistent storage or existing claims for all writable state.
- Optional ingress at the root of a dedicated hostname.
- Optional read-only BIDS site mounts: NFSv4.1 exports, or central git-annex
  clones kept on the LORIS data volume by an external sync job.
- Optional scheduled dataset discovery and ledger-backed import Jobs using the
  LORIS-MRI `--no-copy` importer.
- A database ingress NetworkPolicy and Argo CD sync-wave annotations.

## Requirements and limitations

- Helm 3 and a Kubernetes cluster supporting `apps/v1`, `batch/v1`, and
  `networking.k8s.io/v1`. Scheduled imports use CronJob `spec.timeZone`; Kubernetes
  1.27 or later is recommended when enabling them.
- Pull access to `docker.io/scilus/loris:29.0.0` and `mariadb:10.11.10`.
  Confirm image availability before installation. A replacement LORIS image must
  retain the chart's bootstrap scripts, runtime dependencies, and patches; an
  arbitrary upstream image is not interchangeable.
- A pre-created Kubernetes Secret containing the three required passwords.
  The chart does not create or generate credentials.
- A suitable default StorageClass, explicit StorageClasses, or pre-provisioned
  claims when persistence is enabled. Persistence alone does not guarantee
  durability: RAM-backed storage is still lost on a node reboot.
- Permission to run the image's root-based preparation and Apache containers,
  including ownership changes on writable volumes. The chart is not configured
  for a restricted Pod Security profile. Installer and import processes run as
  UID/GID 33 (`www-data`).
- For ingress: an ingress controller, DNS, and a TLS Secret or certificate
  provisioning mechanism.
- For site mounts: NFS client support on eligible Kubernetes nodes, network access
  from those nodes to each server, and permission to create cluster-scoped PVs.
- A CNI that enforces NetworkPolicy for the database policy to take effect.

Resource names such as `loris` and `loris-mariadb` are fixed rather than derived
from the Helm release name. Install only one instance per namespace. Site PV names
are also fixed (`loris-bids-<site>`), so site names must avoid collisions across
releases in the cluster.

Keep `loris.replicas: 1` unless you have independently validated shared storage,
concurrent initialization, and application behavior. This chart does not provide
database high availability, external database configuration, backups, automatic
LORIS schema upgrades, or a complete MRI processing service.

## Installation

The following commands run from a checkout of this repository. They use the local
chart path rather than assuming a published Helm repository or OCI registry.

### 1. Provision credentials

Create the target namespace if it does not already exist:

```sh
kubectl create namespace loris
```

Provision a Secret named `loris-credentials` in that namespace using your secret
manager, Sealed Secrets, or another approved workflow. Its default keys are:

| Secret key                        | Purpose                                            |
| --------------------------------- | -------------------------------------------------- |
| `MARIADB_ROOT_PASSWORD`         | MariaDB root account and privileged initialization |
| `LORIS_DB_PASSWORD`             | LORIS application database account                 |
| `LORIS_FRONTEND_ADMIN_PASSWORD` | Initial frontend administrator password            |

Use distinct, strong, nonempty passwords. Do not commit them to Git or put them
in a Helm values file. For a manual installation, an existing, access-restricted
environment file containing those three `KEY=value` entries can be loaded with:

```sh
kubectl -n loris create secret generic loris-credentials \
  --from-env-file=/secure/path/loris-credentials.env
```

Protect and manage that file according to your credential-handling policy. If you
use another Secret name or key names, configure the corresponding `database.*`
values before installing.

### 2. Configure storage and the public URL

Save the following as `loris-values.yaml`. This example enables persistent storage
using the cluster's default StorageClass and keeps ingress and BIDS imports off.

```yaml
enabled: true

database:
  persistence:
    enabled: true
    size: 5Gi

loris:
  publicUrl: http://localhost:8080
  project:
    persistence:
      enabled: true
      size: 1Gi
  data:
    persistence:
      enabled: true
      size: 6Gi
```

These capacities are starting points, not production sizing recommendations.
Writable data includes import views, generated previews, logs, and other LORIS
outputs, even when source scans remain on read-only site storage.

### 3. Install and connect

```sh
helm upgrade --install loris ./charts/loris \
  --namespace loris \
  --values loris-values.yaml \
  --wait --timeout 10m

kubectl -n loris port-forward service/loris 8080:80
```

Open [http://localhost:8080](http://localhost:8080) and sign in as `admin` using the frontend password
you provisioned. If you change `loris.servicePort`, adjust the port-forward's
remote port accordingly.

The portal's init containers seed the project directory, initialize a new
database using the LORIS installer, write project configuration, and activate
required modules. No manual SQL or browser-based installation is needed.
Existing initialized databases are checked rather than reinstalled; a partial
bootstrap causes an explicit failure.

## Storage

| State                 | Mount path                 | Default when persistence is disabled   | Default persistent size | Generated claim              |
| --------------------- | -------------------------- | -------------------------------------- | ----------------------- | ---------------------------- |
| MariaDB               | `/var/lib/mysql`         | RAM-backed `emptyDir`, limit `5Gi` | `5Gi`                 | `database-loris-mariadb-0` |
| Project/configuration | `/var/www/loris/project` | `emptyDir`, limit `1Gi`            | `1Gi`                 | `loris-project`            |
| Application data      | `/data/loris`            | `emptyDir`, limit `6Gi`            | `6Gi`                 | `loris-data`               |

All three persistence sections share these settings:

| Setting suffix                   | Default             | Description                                                                                          |
| -------------------------------- | ------------------- | ---------------------------------------------------------------------------------------------------- |
| `persistence.enabled`          | `false`           | Use a PVC instead of `emptyDir`.                                                                   |
| `persistence.existingClaim`    | `""`              | Use an existing claim instead of creating one; requires persistence enabled.                         |
| `persistence.storageClassName` | `""`              | StorageClass for a generated claim. Empty means the field is omitted and cluster defaulting applies. |
| `persistence.size`             | See above           | Requested capacity for a generated claim.                                                            |
| `persistence.accessModes`      | `[ReadWriteOnce]` | Access modes for a generated claim.                                                                  |

Apply these suffixes under `database`, `loris.project`, or `loris.data`.
For example, set `loris.data.persistence.existingClaim` to reuse an application
data claim. The chart does not create, resize, or change the storage configuration
of an existing claim.

`emptyDir` survives a container restart but not deletion or replacement of its
Pod. The database's memory-backed volume also consumes memory. Do not mix durable
database state with disposable application state for a deployment you need to
recover reliably. Back up the database, project configuration, and writable data
together, and preserve source mounts and paths referenced by imported records.

Scheduled imports require `loris.data.persistence.enabled: true` because worker
Jobs and the portal share application data. Choose access modes and scheduling
that allow every consumer to mount it: `ReadWriteOnce` generally requires those
Pods to run on the same node, while multi-node consumers need suitable shared
storage. The same constraint applies to each site's static PVC.

## Ingress and TLS

LORIS 29 must be served at `/` on a dedicated hostname. A URL prefix such as
`https://research.example.org/loris` is not supported.

Merge the following into your values, replacing the hostname, ingress class, and
TLS Secret with values appropriate for your cluster:

```yaml
loris:
  publicUrl: https://loris.example.org

ingress:
  enabled: true
  definitions:
    - name: loris
      className: nginx
      annotations: {}
      tls:
        - secretName: loris-tls
          hosts:
            - loris.example.org
      hosts:
        - host: loris.example.org
          paths:
            - path: /
              pathType: Prefix
```

Configure DNS to resolve to your ingress address and provision the certificate
separately. The chart creates Ingress resources, not certificates. Keep ingress
disabled until initialization and login work through port-forwarding.

Changing `loris.publicUrl` after initialization is not an automatic migration of
persisted LORIS configuration; review the stored configuration when moving an
existing deployment to another URL.

## Read-only BIDS site mounts

Site storage is optional (`sites: []`). Each enabled site is mounted read-only in
the portal and import Pods at the same path, whatever its `source`:

- `source: nfs` (default): an existing NFSv4.1 export. The chart creates the PV/PVC;
  it does **not** deploy an NFS server or configure a site's firewall.
- `source: clone`: a central git-annex (DataLad) clone directory on the LORIS data
  volume. See [Clone-sourced sites](#clone-sourced-sites).

Each enabled site is mounted at:

```text
/data/loris/data/assembly_bids/<site-name>
```

The NFS export path is fixed at `/bids`. The default server port is `20490`,
not the usual NFS port `2049`; override `nfsPort` to match your server.

```yaml
sites:
  - name: example-site
    enabled: true
    server: nfs.example.internal
    nfsPort: 20490
    capacity: 1Ti
    siteAlias: EX
    bidsImport:
      enabled: false
```

| Site field     | Default                            | Description                                                                                               |
| -------------- | ---------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `name`       | Required                           | Unique lowercase DNS label, at most 40 characters; also used for the LORIS site name and mount directory. |
| `enabled`    | Set explicitly                     | Mount the site in the portal (and create its PV/PVC for NFS sources).                                     |
| `source`     | `nfs`                              | `nfs` or `clone`.                                                                                         |
| `server`     | Required for enabled NFS sites     | NFS server address reachable from eligible cluster nodes.                                                 |
| `nfsPort`    | `20490`                          | NFS TCP port.                                                                                             |
| `capacity`   | `1Ti`                            | Declared PV/PVC capacity, not an enforced NFS quota.                                                      |
| `siteAlias`  | Required for imports               | Unique 1–4 character LORIS site alias.                                                                   |
| `bidsImport` | Disabled unless enabled explicitly | Site-specific discovery and registration settings.                                                        |

Volumes use `ReadOnlyMany`, a `Retain` reclaim policy, and NFS mount options
`nfsvers=4.1,proto=tcp,port=<nfsPort>,soft,timeo=100,retrans=2`. The source must
grant UID/GID 33, or the server's mapped identity, read and directory-traversal
access. Verify node-to-server reachability and read-only behavior before enabling
the site. An unavailable NFS mount can prevent the portal Pod from starting;
soft-mount read failures also need operational monitoring.

Network isolation, source authorization, and encrypted transport must be
provided separately. The chart does not configure NFS transport encryption or
per-site authentication. Only export approved, appropriately de-identified data.

Mounting a dataset does not register it in LORIS. Read-only site storage must not
be used for writable upload staging, temporary files, or pipeline outputs.

### Clone-sourced sites

With `source: clone`, the site directory is
`<siteClones.subPath>/<site-name>` on the LORIS data PVC (default
`re-move/clones/<site-name>`), outside the LORIS `data/` tree. It is mounted
read-only (a `subPath` of the data volume) at the usual site path, so no PV, NFS
client or site network access is needed and imports keep their read-only check.
Clone sources require `loris.data.persistence.enabled`.

The chart only prepares the directory: the portal init container creates it, owned
by `www-data` (UID 33). An external job, such as the RE-MOVE sync job, keeps one
git-annex clone per dataset in it (`<site>/<dataset>/`, with locked annexed files)
and must write as UID 33. Annexed content may be absent: dataset metadata
(`dataset_description.json`, `participants.tsv`, sidecars) must stay present, and
imaging content only needs to be present while it is imported or viewed.

For clone sources:

- fingerprints hash git-annex keys (already content hashes) instead of reading
  content, and skip `.git`, so discovery works with content dropped;
- import views link annexed files to their annex object paths, which do not
  depend on content presence;
- a dataset whose fingerprint changed is re-armed and imported again. Additions
  (new subjects, sessions or files) are added to the existing import view and
  imported; files already registered are skipped without reading them. Removed or
  changed files that LORIS already registered stop the attempt with a review error.

#### On-demand imaging content

Images of clone-sourced sites stay at the site. Set `siteClones.cacheURL` to the
base URL of a content cache service (the RE-MOVE `re-move-cache`, for example
`http://re-move-cache.re-move-central.svc.cluster.local:8080`) to fetch them when
they are viewed. The portal then receives `RE_MOVE_CACHE_URL` and
`RE_MOVE_CLONE_ROOT=/data/loris/data/assembly_bids`, and the image patch
(`image/re-move-annex-content.patch`) changes three endpoints:

- the API image endpoint (`/api/v0.0.3/candidates/<id>/<visit>/images/<file>`) and
  BrainBrowser's `imageinfo` follow the file's symlinks. When the target is a
  git-annex object of a clone below `RE_MOVE_CLONE_ROOT` and its content is absent,
  they `POST /content` `{site, dataset, key}` to the cache and answer
  **HTTP 503** with a `Retry-After` header and a JSON body
  `{error, state, retryAfter}` instead of 404. `state` is `fetching` (try again) or
  `unavailable` (for example, the site is offline or has no copy);
- serving present content sends a best-effort access notice (1 s timeout) so the
  cache keeps recently viewed content longer;
- BrainBrowser shows the cache message with the elapsed time and retries after
  `Retry-After` while the state is `fetching`, then loads the volumes; other errors
  are shown in place of the viewer.

The site, dataset and key are derived from the symlink targets, never from the
request. Files that are not annexed in a site clone, and portals without
`cacheURL` (the default), behave as before. Thumbnails are generated at import and
kept, so session pages do not need the content. The cache, not the chart, decides
how long content stays; the portal Pod needs network access to it.

| Value                   | Default          | Description                                                       |
| ----------------------- | ---------------- | ----------------------------------------------------------------- |
| `siteClones.subPath`  | `re-move/clones` | Clone directory on the LORIS data PVC.                            |
| `siteClones.cacheURL` | `""`             | `http(s)://host[:port]` of the content cache; empty disables it.   |

#### Moving an imported NFS site to a clone source

LORIS registrations point at files in the site's import view
(`assembly_bids/import-<...>`), and the view links point at the mounted
dataset. After switching a site from `source: nfs` to `source: clone`, annexed
files resolve to `.git/annex/objects/...` in the clone instead of plain dataset
paths. The importer then refuses the view with "removed or changed source files
need review". `bids_clone_migrate.py`, shipped in the runtime profile ConfigMap,
relinks an existing view without changing the database or site data:

```bash
# While the NFS mount is still present, record every view link and its hashes.
python3 /import-profile/bids_clone_migrate.py prepare \
  --view /data/loris/data/assembly_bids/import-<view> \
  --source /data/loris/data/assembly_bids/<site>/<dataset> \
  --journal /data/loris/data/clone-migration-<site>.json

# After switching to clone source and checking out the clone, verify first.
python3 /import-profile/bids_clone_migrate.py apply \
  --journal /data/loris/data/clone-migration-<site>.json

# Relink only after reviewing a clean verification report.
python3 /import-profile/bids_clone_migrate.py apply \
  --journal /data/loris/data/clone-migration-<site>.json \
  --write --clone /data/loris/re-move/clones/<site>/<dataset>

# To undo, restore the NFS mount before restoring the original links.
python3 /import-profile/bids_clone_migrate.py rollback \
  --journal /data/loris/data/clone-migration-<site>.json --write
```

`prepare` writes a mode-0600 journal and refuses to overwrite an existing one.
`apply` verifies each target against the recorded size and hashes. Annexed files
are verified against their git-annex key without fetching their content; other
files are hashed. Missing, changed, or unverifiable files stop the operation
before any links are changed. `--write` atomically replaces links and records
the new targets, so interrupted runs can be resumed. With `--clone`, it also
sets `refs/re-move/imported` under the dataset lock only if every annexed file
in the clone has been verified. This lets the sync job stage later additions
without re-staging the existing dataset. The next discovery re-arms the dataset
because its fingerprint changed; registered files are skipped by the importer.

## Scheduled BIDS discovery and import

Imports are opt-in at **both** `bidsImport.enabled` and
`sites[].bidsImport.enabled`; the site mount must also be enabled.

### Expected source layout

Export a shared root containing immediate dataset directories, not a dataset
directly at the export root:

```text
/bids/
  study-a/
    dataset_description.json
    participants.tsv
    sub-example01/
      ...
  study-b/
    dataset_description.json
    participants.tsv
    sub-example02/
      ...
```

Each dataset needs a nonempty `dataset_description.json` `Name` and participant
rows matching its subject directories. Numeric-only subject labels are rejected
because they require an explicit candidate identity mapping. Subject PSCIDs are
global in LORIS, not automatically namespaced by site.

Discovery ignores hidden directories and the root `derivatives` directory.
Symlinked dataset directories are rejected. Dataset overrides are **not an
allowlist**: every eligible immediate dataset directory is discovered unless
explicitly disabled.

### Configure an initial preflight

With persistent application data enabled, extend your values as follows:

```yaml
bidsImport:
  enabled: true
  schedule: "0 2 * * *"
  timeZone: Etc/UTC
  defaults:
    mode: preflight
    project: Example Research
    projectAlias: EXR
    createCandidates: false
    createSessions: false

sites:
  - name: example-site
    enabled: true
    server: nfs.example.internal
    nfsPort: 20490
    siteAlias: EX
    bidsImport:
      enabled: true
      datasets:
        study-b:
          enabled: false
```

Registration settings resolve in this order, with the last taking precedence:

1. `bidsImport.defaults`
2. `sites[].bidsImport`
3. `sites[].bidsImport.datasets.<immediate-folder-name>`

Lists such as `sites` replace the previous list when Helm merges values; include
all sites you intend to keep.

| Registration setting  | Default          | Description                                                                           |
| --------------------- | ---------------- | ------------------------------------------------------------------------------------- |
| `mode`              | `import`       | `preflight`, `setup`, or `import`; start with `preflight`.                    |
| `project`           | `""`           | Required nonempty LORIS project name when importing is enabled.                       |
| `projectAlias`      | `""`           | Required 1–4 character project alias.                                                |
| `cohort`            | Dataset `Name` | Optional cohort override. Empty or absent uses `dataset_description.json` `Name`. |
| `recruitmentTarget` | `null`         | Positive integer or `null`.                                                         |
| `defaultVisit`      | `"000"`        | Nonempty visit label used for subjects without session directories; keep it quoted.   |
| `datasetType`       | `raw`          | `raw` or `derivative`.                                                            |
| `createCandidates`  | `false`        | Allow the importer to create candidates. Otherwise, provision them first.             |
| `createSessions`    | `false`        | Allow the importer to create sessions. Otherwise, provision them first.               |

`preflight` checks source metadata, read-only mounting, and data/log directory
configuration without registering scans or project/site/cohort metadata. Discovery
still initializes and writes its import ledger. These checks are not a full BIDS
compliance validator. The configured LORIS data base must be
`/data/loris/data`, with an accessible `logs` directory.

For an initial preflight on a fresh deployment, create the required log directory
on the writable data volume if it is not already present:

```sh
kubectl -n loris exec deployment/loris -c loris -- sh -ec \
  'mkdir -p /data/loris/data/logs; chown www-data:www-data /data/loris/data/logs'
```

`setup` configures registration metadata, output directories, and viewer modules,
then runs the preflight checks. `import` performs setup and imports through a stable generated
view using LORIS-MRI `--no-copy`. Source files remain read-only, but metadata,
previews, logs, and importer artifacts use MariaDB and writable application data.

After preflight succeeds, deliberately change `mode` to `setup` or `import`.
Enable candidate/session creation only if that matches your registration policy.
The import adapter requires LORIS-MRI 29.0.0; changing the image to another release
requires revalidation.

### Scheduling and retries

One CronJob named `loris-bids-<site>` discovers datasets for each import-enabled
site. It creates attempt-specific worker Jobs and ConfigMaps at runtime. MariaDB
records dataset identity, completion, errors, and attempts independently of
Kubernetes Job history. Imports use database locking to serialize shared-state
changes; failed or deferred attempts can be retried on subsequent discovery.
Already completed modes are skipped.

Dataset identity is the site/folder pair. Changes to dataset names or
registration identity are rejected rather than silently reimported; investigate
and plan an explicit migration. Changed NFS source fingerprints are rejected the
same way; clone sources import additive updates instead (see
[Clone-sourced sites](#clone-sourced-sites)). Imports commit incrementally: a
failed attempt does not roll back previously registered scans. NFS fingerprinting
reads the included dataset contents, even during discovery; account for source
size and network throughput when choosing the schedule and Job deadline.

| Scheduling value                       | Default         | Description                                               |
| -------------------------------------- | --------------- | --------------------------------------------------------- |
| `bidsImport.enabled`                 | `false`       | Global import resource switch.                            |
| `bidsImport.schedule`                | `"0 2 * * *"` | Five-field cron expression or supported cron shorthand.   |
| `bidsImport.timeZone`                | `Etc/UTC`     | CronJob time zone.                                        |
| `bidsImport.suspend`                 | `false`       | Suspend scheduled discovery; does not stop existing Jobs. |
| `bidsImport.activeDeadlineSeconds`   | `3600`        | Deadline for each discovery or worker Job.                |
| `bidsImport.ttlSecondsAfterFinished` | `604800`      | Finished Job retention in seconds (seven days).           |

Sites can override `schedule`, `timeZone`, and `suspend` inside their
`bidsImport` section. Dataset overrides can set registration fields and `enabled`,
but not scheduling fields. Jobs use `backoffLimit: 0`; the discovery CronJob uses
`concurrencyPolicy: Forbid`, retains one successful and three failed discovery
Jobs, and uses a dedicated ServiceAccount with namespace-scoped Job/ConfigMap
permissions. Its ledger initializer uses the database root credential; workers
have application credentials and no mounted service-account token.

## Configuration reference

The tables above cover persistence, sites, and imports. The remaining chart
values are listed below.

| Value                                 | Default                                               | Description                                                                                  |
| ------------------------------------- | ----------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `enabled`                           | `false`                                             | Render chart resources. With the default, no workloads are created.                          |
| `image.repository`                  | `docker.io/scilus/loris`                            | Custom LORIS image repository.                                                               |
| `image.tag`                         | `29.0.0`                                            | Image tag.                                                                                   |
| `image.digest`                      | `""`                                                | Optional `sha256:` digest with 64 lowercase hexadecimal characters; pins content when set. |
| `image.pullPolicy`                  | `IfNotPresent`                                      | Pull policy for portal and import containers.                                                |
| `database.image`                    | `mariadb:10.11.10`                                  | Bundled database image; pulled with `IfNotPresent`.                                        |
| `database.name`                     | `loris`                                             | Database name; must start with a letter and contain only letters, digits, or underscores.    |
| `database.secretName`               | `loris-credentials`                                 | Existing credentials Secret.                                                                 |
| `database.rootPasswordKey`          | `MARIADB_ROOT_PASSWORD`                             | Root password key in the Secret.                                                             |
| `database.appPasswordKey`           | `LORIS_DB_PASSWORD`                                 | Application database password key.                                                           |
| `database.adminPasswordKey`         | `LORIS_FRONTEND_ADMIN_PASSWORD`                     | Initial frontend administrator password key.                                                 |
| `database.emptyDir.medium`          | `Memory`                                            | Ephemeral database volume medium.                                                            |
| `database.emptyDir.sizeLimit`       | `5Gi`                                               | Ephemeral database volume size limit.                                                        |
| `loris.databaseUser`                | `loris`                                             | Application database username.                                                               |
| `loris.adminUser`                   | `admin`                                             | Initial frontend administrator username.                                                     |
| `loris.publicUrl`                   | `http://loris.local`                                | Absolute public URL used during bootstrap; use a dedicated hostname with no sub-path.        |
| `siteClones.subPath`                | `re-move/clones`                                    | Relative LORIS data-PVC directory holding clone-sourced sites; must not start with `data`.   |
| `loris.additionalRequiredModules`   | `[]`                                                | Additional module names to keep active.                                                      |
| `loris.servicePort`                 | `80`                                                | ClusterIP Service port; container HTTP port remains 80.                                      |
| `loris.replicas`                    | `1`                                                 | Portal replicas; one is recommended.                                                         |
| `loris.project.emptyDir.sizeLimit`  | `1Gi`                                               | Ephemeral project volume size limit.                                                         |
| `loris.data.emptyDir.sizeLimit`     | `6Gi`                                               | Ephemeral data volume size limit.                                                            |
| `loris.resources.requests.cpu`      | `500m`                                              | Portal and import-container CPU request.                                                     |
| `loris.resources.requests.memory`   | `1Gi`                                               | Portal and import-container memory request.                                                  |
| `loris.resources.limits.memory`     | `4Gi`                                               | Portal and import-container memory limit.                                                    |
| `nodeSelector`                      | `{}`                                                | Node selection for database, portal, and import Pods.                                        |
| `affinity`                          | `{}`                                                | Affinity for database, portal, and import Pods.                                              |
| `tolerations`                       | `[]`                                                | Tolerations for database, portal, and import Pods.                                           |
| `ingress.enabled`                   | `false`                                             | Create configured Ingress resources.                                                         |
| `ingress.definitions`               | One `loris` definition for `loris.local` at `/` | List of ingress definitions; see the TLS example above.                                      |
| `ingress.definitions[].name`        | `loris`                                             | Required Ingress resource name.                                                              |
| `ingress.definitions[].className`   | `""`                                                | Optional ingress class.                                                                      |
| `ingress.definitions[].annotations` | `{}`                                                | Controller-specific annotations.                                                             |
| `ingress.definitions[].tls`         | `[]`                                                | Kubernetes Ingress TLS entries.                                                              |
| `ingress.definitions[].hosts`       | `loris.local`, `/`, `Prefix`                    | Host rules with `host` and `paths` (`path`, `pathType`).                             |
| `sites`                             | `[]`                                                | Optional read-only site mounts.                                                              |

`loris.resources` does not configure MariaDB or the portal's init containers.
Account for those processes separately when sizing the deployment.

The `api`, `imaging_browser`, and `brainbrowser` modules are always required.
They are re-enabled on each portal start and protected from deactivation in the
patched Module Manager. Additional required modules must exist in the installed
LORIS release or startup fails. Other modules remain administrator-managed;
portal restarts and GitOps syncs do not reset their activation state.

## Operations and troubleshooting

Inspect rollout, storage, and initialization:

```sh
kubectl -n loris get pods,pvc
kubectl -n loris describe deployment loris
kubectl -n loris logs deployment/loris -c prepare-loris-state
kubectl -n loris logs deployment/loris -c install-loris
kubectl -n loris logs deployment/loris -c activate-loris-modules
kubectl -n loris logs deployment/loris -c loris
kubectl -n loris logs statefulset/loris-mariadb
```

- **Pending or `ContainerCreating`:** inspect Pod events, PVC binding, volume
  access modes, NFS node connectivity, and export permissions.
- **Installer failure:** verify Secret keys, database health, volume ownership,
  and the initialization logs. An incomplete database is deliberately not
  overwritten; recover from a consistent backup or investigate before retrying.
- **Login or assets fail behind ingress:** use a dedicated hostname at `/`,
  inspect TLS/proxy behavior, and check persisted public URL configuration.
- **Missing previews or BrainBrowser data:** inspect required-module activation
  and confirm source paths remain readable from the portal.
- **Import failure:** inspect both discovery and worker Jobs. A successful
  discovery Job only indicates dispatch/reconciliation, not completed imports.

Inspect scheduled imports or trigger discovery manually:

```sh
kubectl -n loris get cronjobs,jobs -l app.kubernetes.io/name=loris-bids
kubectl -n loris create job --from=cronjob/loris-bids-example-site \
  "loris-discover-$(date +%s)"
```

Inspect the created Job's Pod logs and any worker Jobs it dispatches. If the
`initialize-import-ledger` init container fails, inspect its logs separately.
Manually triggered discovery is not blocked by CronJob suspension.

### Upgrades and credential changes

Back up and test restoration of all persistent state before upgrading. Review
upstream LORIS/LORIS-MRI migration requirements and validate the exact image and
import behavior in a non-production environment. Reapply your reviewed values:

```sh
helm upgrade loris ./charts/loris \
  --namespace loris \
  --values loris-values.yaml \
  --wait --timeout 10m
```

The bootstrap detects existing data; it is not a schema migration tool.
Helm rollback cannot reverse database migrations or import side effects.
Changing Secret values alone does not rotate existing MariaDB accounts or reset
an existing frontend administrator password. Coordinate account changes, Secret
updates, persisted configuration, and workload restarts.

### Uninstallation

```sh
helm uninstall loris --namespace loris
```

Ephemeral state is lost when Pods are removed. Chart-created project/data PVCs
are normally deleted with the release, and their backing storage follows its
reclaim policy. MariaDB's StatefulSet-generated PVC is normally retained;
existing claims are not owned by this chart. Site PVs declare `Retain`, but
release removal is not a backup strategy. Review PVC/PV ownership and reclaim
policies and take backups before uninstalling. Do not delete source site data.

## Before exposing real research data

Treat a production rollout as a separate validation and operational exercise:

- Obtain site-owner approval for source paths, access identities, metadata
  handling, and retention. BIDS filenames, sidecars, and participant tables can
  contain identifying information.
- Validate browsing and no-copy import with synthetic, non-sensitive datasets,
  including read-only enforcement, previews, concurrent access, and outage
  recovery.
- Use durable storage and test coordinated database/configuration/data restores.
- Configure TLS, network restrictions, secret rotation, dependency/image review,
  monitoring, alerting, and an operator-approved recovery procedure.
- Restrict portal access until these checks pass. The included NetworkPolicy
  restricts database ingress to portal/import Pods in the namespace; it is not
  a complete portal, NFS, or egress security policy.

## Reproducible security audit

This repository includes a local audit runner and GitHub Actions workflow.
From a repository checkout on Linux x86_64 with Python 3.12:

```sh
python3 tools/security/loris/setup.py
export PATH="$PWD/.audit-tools/bin:$PWD/.audit-tools/venv/bin:$PATH"
python tools/security/loris/audit.py --output audit-results/local
```

The audit covers disabled/enabled chart profiles, persistence/TLS, NFS/import
resources, generated worker Jobs, chart-owned source, release workflows, and
immutable application/database image dependencies. It produces Markdown/JSON
reports and image SBOMs. High/critical scanner findings fail the gate; missing
required checks also fail explicitly. Scanner alerts remain unverified until
triaged, and passing does not certify a production deployment.

An explicit `--runtime` mode adds disposable-cluster checks with synthetic data,
not real site mounts or production credentials. It requires separate runtime
tool setup and adequate host resources.
See the [audit guide](https://github.com/neurotto/loris/tree/main/tools/security/loris)
for prerequisites, diagrams, coverage limitations, report formats, and runtime
safety. These repository tools are not included in the Helm chart package.

## Tests

Tests live in the repository's `tests/` directory:

| Command                                              | Needs                                                  | Covers                                          |
| ---------------------------------------------------- | ------------------------------------------------------ | ----------------------------------------------- |
| `python3 -m pytest tests --ignore tests/test_loris_bids_setup.py` | Python 3.12+, `sqlalchemy>=2`, `pytest`, `git-annex` | Discovery ledger, fingerprints, import views, NFS-to-clone view migration. |
| `bash tests/test_loris_chart.sh`                     | `helm`, jq-compatible `yq` (Debian package `yq`)        | Rendered resources and values validation.       |
| `bash tests/test_loris_bids_integration.sh`          | Docker, the `scilus/loris:29.0.0` and `mariadb:10.11.10` images | All `test_loris_bids_*` suites, including real LORIS-MRI imports, in the image. |
| `node tests/test_loris_brainbrowser.js`              | Node.js, Docker                                        | BrainBrowser React mount and on-demand content retry patches. |
| `bash tests/test_loris_annex_content.sh`             | Docker, an image built with `re-move-annex-content.patch` | On-demand content lookup, cache requests and 503 responses.   |
| `bash tests/test_loris_diagnostics.sh`, `bash tests/test_loris_module_manager.sh` | A running local portal (`run-local.sh`) | Diagnostics and Module Manager guards.          |

## Upstream documentation

- [LORIS project](https://github.com/aces/Loris)
- [LORIS 29.0.0 installation guide](https://github.com/aces/Loris/blob/v29.0.0/docs/wiki/00_SERVER_INSTALL_AND_CONFIGURATION/01_LORIS_Install/Ubuntu/README.md)
- [LORIS-MRI 29.0.0](https://github.com/aces/Loris-MRI/tree/v29.0.0)
- [LORIS-MRI 29.0.0 BIDS importer](https://github.com/aces/Loris-MRI/blob/v29.0.0/python/loris_bids_importer/README.md)
