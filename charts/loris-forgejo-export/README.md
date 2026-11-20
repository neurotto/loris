# loris-forgejo-export

Central companion chart that publishes site datasets from an existing
[Forgejo-Aneksajo](https://codeberg.org/forgejo-aneksajo/forgejo-aneksajo)
server into a LORIS installation. Install it **once on the central node**, in
the LORIS namespace. One release serves every site.

Each site has one Forgejo **organization**, with one **repository per
dataset**, plus a `.worker` repository. Sites run their own worker, a
Forgejo Actions runner and request command, installed separately with
Podman. This chart never runs on a site.

The LORIS chart is unchanged and knows nothing about Forgejo:

- LORIS sees the datasets as ordinary `source: pvc` sites.
- LORIS fetches missing content through its generic `contentProvider.url`.

This chart neither installs nor modifies Forgejo.

```
site worker (Podman, not in this chart)     central namespace (LORIS namespace)
  runner polls Forgejo  ─────────────▶  Forgejo-Aneksajo (existing)
  pushes dataset repos ──push webhook─▶  <release>-webhook ──Job──▶ <release>-sync
                                          sync: fetch → stage via .worker → get → checkout
                                                → LORIS import (loris-bids-<site>) → drop
  .worker stage-content ◀──dispatch────  <release>-provider (/content, /chunks)
                                          ◀── LORIS contentProvider.url
                                          <release>-reconcile: orgs, teams, repos, .worker,
                                            bots, webhooks, enrollment Secrets
  shared LORIS data PVC: <clonesSubPath>/<site>/<dataset>, <chunksSubPath>
```

## Requirements

- **LORIS chart 0.5.0 or later** in the same namespace, with these settings:
  - its data PVC (`loris.dataClaim`) is reachable from this chart's pods;
  - `contentProvider.url` set to `http://<release>-loris-forgejo-export-provider:8080`;
  - BIDS import enabled for the sites that import.
- **Forgejo-Aneksajo** with git-annex p2phttp and Actions enabled. The image
  needs git-annex 10.20240731 or later and ships 10.20250416.
- **A Forgejo administrator account**, in a Secret given by
  `forgejo.adminSecret`. Only the reconciler uses it.
- **Network access:**
  - the chart's pods can reach `forgejo.url`;
  - site runners can reach `forgejo.publicUrl`.
- **Node placement:** a node that can mount the LORIS data volume. Use
  `nodeSelector` or `affinity` when the volume is node-local.

## Forgejo prerequisites

The built-in push receiver is
`http://<fullname>-webhook.<namespace>.svc.cluster.local:8080/forgejo`. Forgejo
refuses webhooks to private addresses unless they are allowed. Add the host,
or a matching pattern, to the Forgejo configuration:

```ini
[webhook]
ALLOWED_HOST_LIST = loopback,private   ; or: <fullname>-webhook.<namespace>.svc.cluster.local
```

Without this setting, pushes are still picked up by the hourly sync, only
later.

## Shared sites values

Use **one** `sites` list for both LORIS and this chart; LORIS ignores the
`forgejo:` keys. This chart manages only sites with `forgejo.enabled: true`;
other sites, such as NFS sites, are ignored.

```yaml
# sites.yaml (passed to both charts with -f)
bidsImport:
  enabled: true
sites:
  - name: alpha
    source: pvc
    claimName: loris-data                 # == loris.dataClaim
    subPath: forgejo/clones/alpha         # == <loris.clonesSubPath>/<name>
    bidsImport:
      enabled: true
      symlinkedContent: identity          # required for importing Forgejo sites
      updates: additive                   # required for importing Forgejo sites
    forgejo:
      enabled: true
      datasets: [Study_A, Study_B]        # one repository each
      visibility: {Study_A: signed-in}    # optional; default private
  - name: beta
    source: nfs                           # not managed by this chart
    server: 192.0.2.10
```

```yaml
# forgejo-export.yaml
forgejo:
  url: http://forgejo-http.forgejo.svc.cluster.local:3000
  publicUrl: https://forge.example.org
  adminSecret: {name: forgejo-admin}
image:
  digest: sha256:...                      # pin the published image
```

```sh
helm repo add neurotto-loris https://neurotto.github.io/loris
helm upgrade --install loris neurotto-loris/loris -n loris -f loris.yaml -f sites.yaml \
  --set contentProvider.url=http://forgejo-export-loris-forgejo-export-provider:8080
helm upgrade --install forgejo-export neurotto-loris/loris-forgejo-export -n loris \
  -f forgejo-export.yaml -f sites.yaml
```

Render-time validation rejects:

- a managed site whose `claimName`/`subPath` does not match the convention;
- a missing `symlinkedContent: identity` or `updates: additive` on an
  importing site;
- invalid dataset names, and organization, bot, team or repository names that
  collide (case-insensitive);
- incompatible trigger settings.

## What gets created in Forgejo

The reconciler runs as a Job on every install or upgrade, and also daily. All
of its operations are idempotent. It never deletes organizations or
repositories; it only reports unmanaged ones as `drift:` lines.

For each site, `<organizationPrefix><site>`, it creates:

- **The organization.** Its visibility is `private`, or `limited` when a
  dataset is `signed-in`.
- **Team `datasets`** (`publicationTeam`): write access to the dataset
  repositories. Add the site's users to it.
- **Team `loris-export`** (`exportTeam`): the export bot, with code and Actions
  access to every repository of the organization.
- **One private repository per dataset**, with Actions disabled.
- **`.worker`**, with Actions enabled, holding the worker workflows.
- **Publication bot `<org>-worker`.** Its token is stored as the org Actions
  secret `<variablePrefix>WORKER_TOKEN`.
- **Org variables** `<variablePrefix>FORGE_URL` and, optionally,
  `<variablePrefix>FORGE_IP`.
- **An org push webhook** to the receiver, signed with the generated HMAC
  secret.

Sync and the provider never use the administrator. They act as the restricted
export bot (`botUser`), whose token is kept in Secret `<fullname>-bot`.

> **Trust level.** The export team gives the export bot the same control over
> `.worker` workflows as the central administrators: it can dispatch the
> site's worker workflows. Protect Secret `<fullname>-bot` accordingly.

## Site enrollment

When `reconciler.enrollment.enabled` is set (the default), the reconciler
creates a Secret `<fullname>-enrollment-<site>` for each site. It registers the
site's Actions runner and creates the publication token. Hand this Secret to
the site's administrator over a secure channel, as input for the site worker
installer:

```sh
kubectl -n loris get secret forgejo-export-loris-forgejo-export-enrollment-alpha \
  -o go-template='{{range $k, $v := .data}}{{$k}}={{$v | base64decode}}{{"\n"}}{{end}}'
```

| Key | Purpose |
|---|---|
| `forgeUrl`, `forgeIP` | Forgejo address for the site |
| `organization`, `workerRepository`, `repositories` | Where to publish |
| `runnerName`, `runnerUUID`, `runnerToken`, `runnerLabel` | Runner registration |
| `requestCommand` | Command name the workflows call |
| `publicationUser`, `publicationToken` | Push credentials for dataset repositories |

Once written, a key is never regenerated.

- **Rotating credentials:** delete the Secret, or one key pair, and run the
  reconciler again. It replaces the runner or token.
- **Uninstalling:** enrollment Secrets are not Helm-managed and survive
  uninstall.

## Site worker contract

The `.worker` repository receives the bundled workflows from
[`files/worker-template`](files/worker-template), or your own files from
`forgejo.worker.templateFiles` (repository path → content), which fully
replace them.

The workflows run on `runs-on: <runnerLabel>` and call the site command:

| Workflow | Site command |
|---|---|
| `probe.yml` | `<requestCommand> status`, then `probe <dataset>` |
| `refresh.yml` (`dataset`) | `<requestCommand> refresh "$DATASET"` |
| `stage-content.yml` (`dataset`, `commit`, `keys`) | `<requestCommand> stage "$DATASET"` (`COMMIT`, `KEYS` in env) |

`stage-content` must make the requested git-annex keys available from the
dataset repository on Forgejo. The provider and sync then fetch them over
p2phttp.

The bundled workflows run on the runner host and need `bash` and `jq` there
(`probe.yml` parses the `status` output with `jq`).

The names are configurable to match an existing site worker:

- `runnerLabel`
- `requestCommand`
- `variablePrefix`
- `repository`

Full details are in [`files/CONTRACT.md`](files/CONTRACT.md).

## Triggers

| Mode | Settings | Behavior |
|---|---|---|
| Built-in receiver (default) | `triggers.webhook.enabled: true` | Verifies the HMAC signature and accepts pushes to the default branch of declared datasets only. Each accepted push starts a Job from the `<fullname>-sync` CronJob |
| Argo Events | `webhook.enabled: false`, `argoEvents.enabled: true`, `eventSourceName`, `eventName`, `webhookUrl` | A Sensor in `argoEvents.namespace` filters pushes and creates sync Jobs |
| Scheduled only | both disabled | No hook is created; datasets sync on `sync.schedule` |

All modes keep the hourly sync (`sync.schedule`) as a catch-up.

Webhook-created Jobs are owned by the sync CronJob. With
`concurrencyPolicy: Forbid`, a scheduled run is skipped while a push sync is
still running.

**Notes on each mode:**

- **Built-in receiver:** reachable on port 8080 by any pod in the cluster.
  Only signed requests are acted on.
- **Argo Events:**
  - Argo cannot verify Forgejo signatures. Anyone who can reach the
    EventSource can trigger a sync of a declared dataset.
  - A sync is harmless and idempotent, but restrict access to the EventSource.
  - `webhookUrl` must also be in Forgejo's `ALLOWED_HOST_LIST`.

## Content provider

`<fullname>-provider` implements the LORIS generic content-provider contract:

- **`/content`:** returns `200` when the content is present. Missing content
  returns `503` with `Retry-After` while the provider asks the site to stage it
  and fetches it.
- **`/chunks`:** EEG chunks are generated with the bundled LORIS-MRI chunker
  into `<loris.chunksSubPath>`.

**Cache limits:**

- Content fetched on demand is dropped after `provider.cache.idleTTL` or
  `maxAge`.
- Total size is capped at `maxBytes`.
- When the LORIS data volume is RAM-backed, `provider.resources.limits.memory`
  must cover `maxBytes`.

**Access and permissions:**

- A NetworkPolicy admits only the pods selected by `loris.clientPodSelectors`.
- The provider has no Kubernetes service-account token.
- Every pod runs as `loris.runAsUser` (33), with a read-only root filesystem.

## Operations

```sh
# Re-run the reconciler now
kubectl -n loris create job --from=cronjob/<fullname>-reconcile reconcile-manual
# Sync every dataset now (pushes already trigger a per-dataset sync)
kubectl -n loris create job --from=cronjob/<fullname>-sync sync-manual
```

`<fullname>` is `<release>-loris-forgejo-export` (truncated to 40 characters).
For example, release `forgejo-export` gives `forgejo-export-loris-forgejo-export`.

**Reconciler:** a second run with no changes reports
`0 Forgejo writes, 0 Secret writes`. It also reports webhook-secret refreshes,
because Forgejo never returns the secret.

**History rewrites.** Sync refuses a non-fast-forward dataset history. To
accept one rewrite, run a one-off Job from `<fullname>-sync` with these env
vars:

- `LFX_HISTORY_REPOSITORY=<org>/<dataset>`
- `LFX_HISTORY_OLD=<old 40-hex commit>`
- `LFX_HISTORY_NEW=<new 40-hex commit>`

The old head is kept under `refs/loris-forgejo-export/history-backups/<old>`.

**Locking and imports:**

- Runs are serialized per dataset with a lock, and the reconciler with a Lease
  `<fullname>-reconcile`.
- Imports run through LORIS's own discovery CronJob
  `<importCronJobPrefix><site>`. Sync waits for LORIS's result line before
  dropping fetched content.

## Removing a site

Set `forgejo.enabled: false`, or remove the site from `sites`. The chart then
stops syncing and serving it.

The Forgejo organization, the repositories, the copies on the LORIS volume
and the enrollment Secret are left in place. Remove them manually.

## Values

See [`values.yaml`](values.yaml) for every option and its default.
