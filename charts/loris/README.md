# LORIS Helm chart

Deploy [LORIS](https://loris.ca/), a web-based platform to manage neuroimaging
and research data, with a bundled MariaDB database and optional read-only BIDS
dataset mounts.

This chart packages a **development deployment**, not a production-certified
LORIS installation. It uses a custom image containing LORIS and LORIS-MRI
**29.0.0**, an automated installer, and viewer, Module Manager and
provider-neutral content patches.
Chart version: **0.5.0**.

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
- Optional read-only BIDS site mounts from NFSv4.1 exports or existing claims.
- Optional scheduled dataset discovery and ledger-backed import Jobs using the
  LORIS-MRI `--no-copy` importer.
- A database ingress NetworkPolicy and Argo CD sync-wave annotations.

## Requirements and limitations

- Helm 3 and a Kubernetes cluster supporting `apps/v1`, `batch/v1`, and
  `networking.k8s.io/v1`. Scheduled imports use CronJob `spec.timeZone`; Kubernetes
  1.27 or later is recommended when enabling them.
- Pull access to `docker.io/scilus/loris:29.0.0-chart0.5.0` and `mariadb:10.11.10`.
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
- For NFS site mounts: NFS client support on eligible Kubernetes nodes, network
  access from those nodes to each server, and permission to create cluster-scoped
  PVs. PVC site mounts only need an existing claim the portal and import Pods can
  mount.
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
the portal and import Pods at the same path:

- `source: nfs` (default): an existing NFSv4.1 export. The chart creates the PV/PVC;
  it does **not** deploy an NFS server or configure a site's firewall.
- `source: pvc`: a directory on an existing PersistentVolumeClaim in the release
  namespace, for example a copy of site data that another workload synchronizes
  inside the cluster. The chart creates no PV/PVC for it and never writes to it.

`source: pvc` lets you keep the central cluster from opening connections into site
networks: sites push data to a service you operate, which writes a copy to a
claim that LORIS mounts read-only. Content that is not yet present (for example a
dangling symlink in a partial copy) can be fetched on demand through
[`contentProvider.url`](#provider-neutral-on-demand-content).

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
  - name: synced-site
    enabled: true
    source: pvc
    claimName: site-copies
    subPath: sites/synced-site
    siteAlias: SYNC
```

Both forms expose the same layout: dataset folders directly below the site root.

| Site field     | Default                            | Description                                                                                               |
| -------------- | ---------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `name`       | Required                           | Unique lowercase DNS label, at most 40 characters; also used for the LORIS site name and mount directory. |
| `enabled`    | Set explicitly                     | Mount the site in the portal (and create its PV/PVC for NFS sources).                                     |
| `source`     | `nfs`                              | `nfs` or `pvc`.                                                                                           |
| `server`     | Required for enabled NFS sites     | NFS server address reachable from eligible cluster nodes. NFS only.                                       |
| `nfsPort`    | `20490`                          | NFS TCP port. NFS only.                                                                                   |
| `capacity`   | `1Ti`                            | Declared PV/PVC capacity, not an enforced NFS quota. NFS only.                                            |
| `claimName`  | Required for enabled PVC sites     | Existing claim in the release namespace. PVC only.                                                        |
| `subPath`    | Claim root                         | Relative directory inside the claim, without `..`. PVC only.                                              |
| `siteAlias`  | Required for imports               | Unique 1–4 character LORIS site alias.                                                                   |
| `bidsImport` | Disabled unless enabled explicitly | Site-specific discovery and registration settings.                                                        |

Volumes use `ReadOnlyMany`, a `Retain` reclaim policy, and NFS mount options
`nfsvers=4.1,proto=tcp,port=<nfsPort>,soft,timeo=100,retrans=2`. The source must
grant UID/GID 33, or the server's mapped identity, read and directory-traversal
access. Verify node-to-server reachability and read-only behavior before enabling
the site. An unavailable NFS mount can prevent the portal Pod from starting;
soft-mount read failures also need operational monitoring.

For PVC sites, the claim's access mode and scheduling must let the portal and
import Pods mount it alongside its writer; the mount is read-only in LORIS
containers, so write protection of the source is the writer's responsibility.
A claim may be the LORIS data claim itself, with `subPath` pointing outside the
directories LORIS writes.

Network isolation, source authorization, and encrypted transport must be
provided separately. The chart does not configure NFS transport encryption or
per-site authentication. Only export approved, appropriately de-identified data.

Mounting a dataset does not register it in LORIS. Read-only site storage must not
be used for writable upload staging, temporary files, or pipeline outputs.

### Provider-neutral on-demand content

Set `contentProvider.url` to opt in to an external content provider. The chart
does not deploy that service or depend on a particular backend.

The provider implements two JSON POST endpoints:

| Endpoint | Request body | Ready | Pending | Terminal |
| -------- | ------------ | ----- | ------- | -------- |
| `/content` | `{"path":"<site>/<dataset>/<relative-file>"}` | HTTP 200 `{"state":"ready"}` | HTTP 202 `{"state":"pending","message":"...","retryAfter":1}` | HTTP 404 `{"state":"unavailable","message":"..."}` |
| `/chunks` | `{"path":"<site>/<dataset>/<relative-file>","chunkPath":"content-provider-chunks/<site>/<dataset>/<sha256>.chunks"}` | HTTP 200 `{"state":"ready"}` | HTTP 202 `{"state":"pending","message":"...","retryAfter":1}` | HTTP 404 `{"state":"unavailable","message":"..."}` |

`retryAfter` is clamped to 1–60 seconds. Any malformed or unexpected response,
or a provider timeout, is treated as terminal unavailable; LORIS does not retry
that request. The provider must make ready content visible at its existing path
under `/data/loris/data/assembly_bids` and ready EEG chunks visible at
`chunkPath/index.json`. It therefore needs access to the corresponding storage;
the chart only supplies the URL and creates the chunk directory. Imaging and EEG
viewers stop retrying after five minutes, and their waits can be cancelled when
the page or viewer is closed.

If the provider reports ready before the shared filesystem exposes the file,
LORIS continues returning pending with a one-second retry interval. This wait
is subject to the same viewer deadline.

For EEG, give the external provider write access to the same persistent
directory LORIS reads at `/data/loris/data/content-provider-chunks`. For example,
when using `loris.data.persistence.existingClaim`, mount that claim in the
provider with `subPath: data/content-provider-chunks`. The provider's mount path
is its own choice; its output must appear at the requested `chunkPath` inside
LORIS. The chart does not attach a provider's separate volume automatically.
Choose storage access modes and scheduling that permit both workloads to mount
the claim.

The chunk path is derived from the SHA-256 digest of the recording's path below
its site and dataset. It contains no provider-specific identifiers.

| Value                 | Default | Description                                  |
| --------------------- | ------- | -------------------------------------------- |
| `contentProvider.url` | `""`    | `http(s)://host[:port]` for the generic provider contract. |

### Migration from chart 0.3.x

Chart 0.5.0 removes `sites[].source: clone`, `siteClones.*`, the legacy
clone-cache image patch, clone-specific BIDS import behavior, and the bundled
maintenance tools for those integrations. Existing values using these settings
must not be carried forward unchanged: the Helm templates reject
`source: clone` and `siteClones`.

Before upgrading, pause site discovery and any external clone synchronization,
and back up the LORIS database and persistent data. Then choose one of:

- **Keep the in-cluster copies:** set each former clone site to `source: pvc`
  with the claim that holds the copies and `subPath: <siteClones.subPath>/<site>`
  (for example `claimName: loris-data`, `subPath: re-move/clones/<site>` with the
  former defaults). The mount path is unchanged and no site network access is
  added. To keep on-demand fetching of absent content, point
  `contentProvider.url` at a service implementing the provider contract instead
  of `siteClones.cacheURL`. If the copies keep file contents behind in-dataset
  symlinks that may be dropped, set `bidsImport.symlinkedContent: identity`;
  if they grow through synchronization, set `bidsImport.updates: additive`.
- **Switch to NFS:** provision and verify a replacement NFS export with the same
  site/dataset layout and read-only access, then set `source: nfs` with its
  server configuration.

`contentProvider.url` only implements the provider-neutral on-demand content
contract; it does not provide site storage or migrate clone data.

Existing import views may contain links into clone-specific object paths, and
their registrations may refer to those paths. The chart does not migrate those
database records or views. Keep the prior deployment and clone data available
until you have a separately reviewed migration or restore strategy. Source
fingerprints now hash file paths and either contents or, with
`symlinkedContent: identity`, symlink targets. The first discovery after the
upgrade therefore sees a changed fingerprint: it is rejected for review unless
`updates: additive` is set, in which case the dataset is re-armed, already
registered files are skipped, and legacy view links to the same final file are
upgraded in place. Legacy links that reach another file are rejected. Because
no per-file digests exist yet, each such dataset is first rejected with its new
fingerprint; verify that its imported files are unchanged before setting
`acceptFingerprint` as described in
[Symlinked content and additive updates](#symlinked-content-and-additive-updates).
Rollback requires restoring the prior chart version, values, image and clone
storage together. Do not remove the old storage until the upgraded portal and
all affected registered files have been verified.

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
rows matching its subject directories. Subject PSCIDs are global in LORIS, not
automatically namespaced by site. Numeric-only labels require an explicit
`subjectPrefix`, preferably configured per dataset:

```yaml
sites:
  - name: nandou
    # Other required site settings omitted.
    bidsImport:
      datasets:
        sigmo:
          subjectPrefix: nandousigmo
```

With that opt-in mapping, BIDS subject `001` registers as PSCID
`nandousigmo001`; LORIS generates its CandID normally. All subjects in that
dataset use the prefix. The prefix must start with a letter, contain only
letters/digits, and leave the full PSCID within 255 characters.
Source files, published labels, import-view paths and EEG references are unchanged.
The pinned importer adapter applies the mapping consistently during candidate
creation, session validation and acquisition lookup, then restores its hooks.
Changing the prefix of an enrolled dataset is an identity migration and is
refused, not an ordinary retry. Do not enable a prefix for participants already
registered under another identity without reviewing that migration.
Setup also ensures the BrainVision `eeg`, `vhdr`, and `vmrk` imaging-file types
exist; existing descriptions are preserved.
Those are shared file-type lookup rows, not MRI scan registrations:
EEG uses the upstream EEG importer, `physiological_file`/`physiological_archive`
records and the separate `electrophysiology_browser` module. That browser is
kept active alongside the API and imaging viewers. Automatic BIDS imports do not
require enabling the manual `electrophysiology_uploader`.

Without a content provider, BrainVision and BDF recordings are chunked locally
using MNE and LORIS's chunk writer. Other supported formats retain upstream
chunk generation. Generated chunks live on writable application storage, never
on the read-only site export.
The chart-compatible image fixes the pinned chunk writer's swapped sample-count
and downsampling metadata fields. Use `29.0.0-chart0.5.0` or a verified derivative,
not the older `29.0.0` image tag.

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

Dataset identity is the site/folder pair. Changes to dataset names,
registration identity, or source fingerprints are rejected rather than silently
reimported, unless a site opts in to additive updates (below); investigate and
plan an explicit migration. Imports commit
incrementally: a failed attempt does not roll back previously registered scans.
Fingerprinting reads the included dataset contents, even during discovery;
account for source size and network throughput when choosing the schedule and
Job deadline.

| Scheduling value                       | Default         | Description                                               |
| -------------------------------------- | --------------- | --------------------------------------------------------- |
| `bidsImport.enabled`                 | `false`       | Global import resource switch.                            |
| `bidsImport.schedule`                | `"0 2 * * *"` | Five-field cron expression or supported cron shorthand.   |
| `bidsImport.timeZone`                | `Etc/UTC`     | CronJob time zone.                                        |
| `bidsImport.suspend`                 | `false`       | Suspend scheduled discovery; does not stop existing Jobs. |
| `bidsImport.activeDeadlineSeconds`   | `3600`        | Deadline for each discovery or worker Job.                |
| `bidsImport.ttlSecondsAfterFinished` | `604800`      | Finished Job retention in seconds (seven days).           |

Sites can override `schedule`, `timeZone`, and `suspend` inside their
`bidsImport` section. Dataset overrides can set registration fields, `enabled`, and
`acceptFingerprint` (below), but not scheduling fields. Jobs use `backoffLimit: 0`; the discovery CronJob uses
`concurrencyPolicy: Forbid`, retains one successful and three failed discovery
Jobs, and uses a dedicated ServiceAccount with namespace-scoped Job/ConfigMap
permissions. Its ledger initializer uses the database root credential; workers
have application credentials and no mounted service-account token.

### Symlinked content and additive updates

Two site-level `bidsImport` options support sources whose files are symlinks
into a content store inside the dataset and whose datasets grow over time. Both
are opt-in and cannot be set per dataset.

| Site value | Default | Description |
| ---------- | ------- | ----------- |
| `sites[].bidsImport.symlinkedContent` | `resolve` | `resolve` follows file symlinks and requires their content. `identity` fingerprints a file symlink by its target path and links import views to the source path, so content may be absent. |
| `sites[].bidsImport.updates` | `reject` | `reject` sends changed fingerprints to review. `additive` lets an idle dataset re-import after files are added; changed or removed imported files are still rejected. |

With `identity`, a symlink target must stay inside the dataset and must not
pass through further symlinks, including symlinked directories; if the target
exists, it must be a regular file. A store that replaces file contents without
changing the target path is not detected. Pair absent content with
`contentProvider.url` so viewers can request it. New acquisitions still need
their content present when they are imported; files already registered are not
read again. Metadata such as `dataset_description.json`, `participants.tsv`, and
sidecars must always be present.

With `additive`, the import ledger records a digest for every dataset file
(the content, or the symlink target with `identity`). A changed fingerprint
re-arms the dataset only if every recorded file except `participants.tsv` is
still present and unchanged; otherwise discovery rejects it for review. The
re-arm resets completed modes and sets the status to `updated`, and the next
discovery creates an import attempt that registers only the additions. An
active import still blocks the update. Import views are extended in place;
existing view links that already point to the same final file are rewritten to
the current link form.

Datasets enrolled before `additive` was enabled, or by an earlier chart
version, receive their digests at the first discovery where their fingerprint
is unchanged. If the fingerprint has already changed by then, discovery
rejects the dataset and reports its new fingerprint. After verifying that the
files already imported are unchanged, accept exactly that state:

```yaml
sites:
  - name: example-site
    bidsImport:
      updates: additive
      datasets:
        example-dataset:
          acceptFingerprint: <64-character fingerprint from the discovery error>
```

The value only applies while no digests are recorded and only to that exact
fingerprint; any later change needs no new value and is checked against the
recorded digests. It requires `updates: additive`.

## Configuration reference

The tables above cover persistence, sites, and imports. The remaining chart
values are listed below.

| Value                                 | Default                                               | Description                                                                                  |
| ------------------------------------- | ----------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `enabled`                           | `false`                                             | Render chart resources. With the default, no workloads are created.                          |
| `image.repository`                  | `docker.io/scilus/loris`                            | Custom LORIS image repository.                                                               |
| `image.tag`                         | `29.0.0-chart0.5.0`                                 | Chart-compatible image tag; the LORIS application remains 29.0.0.                              |
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
| `contentProvider.url`              | `""`                                                | Optional provider-neutral on-demand content service URL.                                    |
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

The `api`, `imaging_browser`, `brainbrowser`, and separate
`electrophysiology_browser` modules are always required.
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

For a focused real-import run, use
`BIDS_TEST_PATTERN=test_loris_bids_importer.py bash tests/test_loris_bids_integration.sh`.

Tests live in the repository's `tests/` directory:

| Command                                              | Needs                                                  | Covers                                          |
| ---------------------------------------------------- | ------------------------------------------------------ | ----------------------------------------------- |
| `python3 -m pytest tests --ignore tests/test_loris_bids_setup.py` | Python 3.12+, `sqlalchemy>=2`, `pytest` | Discovery ledger, content fingerprints, review-gated import views, and provider chunk paths. |
| `bash tests/test_loris_chart.sh`                     | `helm`, jq-compatible `yq` (Debian package `yq`)        | Rendered resources and values validation.       |
| `bash tests/test_loris_bids_integration.sh`          | Docker, the `scilus/loris:29.0.0-chart0.5.0` and `mariadb:10.11.10` images | All `test_loris_bids_*` suites, including real LORIS-MRI imports, in the image. |
| `node tests/test_loris_brainbrowser.js`              | Node.js, Docker                                        | BrainBrowser React mount and on-demand content retry patches. |
| `bash tests/test_loris_content_provider.sh`          | Docker, an image built with `content-provider.patch` | Provider contract: ready, pending, unavailable, malformed responses, timeout, path validation, and EEG chunks. |
| `bash tests/test_loris_diagnostics.sh`, `bash tests/test_loris_module_manager.sh` | A running local portal (`run-local.sh`) | Diagnostics and Module Manager guards.          |

## Upstream documentation

- [LORIS project](https://github.com/aces/Loris)
- [LORIS 29.0.0 installation guide](https://github.com/aces/Loris/blob/v29.0.0/docs/wiki/00_SERVER_INSTALL_AND_CONFIGURATION/01_LORIS_Install/Ubuntu/README.md)
- [LORIS-MRI 29.0.0](https://github.com/aces/Loris-MRI/tree/v29.0.0)
- [LORIS-MRI 29.0.0 BIDS importer](https://github.com/aces/Loris-MRI/blob/v29.0.0/python/loris_bids_importer/README.md)
