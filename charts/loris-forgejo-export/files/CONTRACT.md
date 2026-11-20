# loris-forgejo-export internal contract

This is the frozen interface between the chart templates, the image, and the
Python components. Every component must follow it exactly. Change it only
together with every consumer.

`<fullname>` is the `loris-forgejo-export.fullname` helper (at most 40
characters). `<settings.*>` refers to `registry.json` settings (see
`registry.py`).

## Image

`docker.io/scilus/loris-forgejo-export` (built from `image/`), Debian trixie-slim.
It contains:

- `git`, `git-annex` (at least 10.20240731, the p2phttp client) and `python3`.
- The LORIS-MRI electrophysiology chunker with the metadata patch. Its commands
  are `edf-to-chunks` and `eeglab-to-chunks`; the Python package is
  `loris_ephys_chunker`.
- `mne`, `numpy` and `scipy`.

The image does not include the chart's Python scripts. Its environment is
`HOME=/tmp PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR=/tmp` and it runs as
`USER 33:33`. It has no entrypoint requirements: the chart runs
`python3 /opt/loris-forgejo-export/<component>.py`. Components use the Python
standard library only, plus the CLI tools above; they do not use PyYAML.

## Shared files and mounts

| Path in pod | Source | Used by |
|---|---|---|
| `/opt/loris-forgejo-export/*.py` | ConfigMap `<fullname>-scripts`, one key per file in `files/*.py`: `registry.py`, `reconcile.py`, `sync.py`, `provider.py`, `ephys_chunks.py`, `webhook.py` | all |
| `/etc/loris-forgejo-export/registry.json` | ConfigMap `<fullname>-registry`, key `registry.json` (`loris-forgejo-export.registry` helper) | all |
| `/etc/loris-forgejo-export/worker-template/` | ConfigMap `<fullname>-worker-template`. A key is the repository path with `/` encoded as `__`, for example `.forgejo__workflows__probe.yml` | reconciler |
| `/var/run/secrets/loris-forgejo-export/bot/{username,token}` | Secret `<fullname>-bot`, mounted with `optional: true` | sync, provider |
| `/var/run/secrets/loris-forgejo-export/webhook/secret` | Secret `loris-forgejo-export.webhookSecret`, key `secret` | webhook |
| `/clones` | LORIS data claim, `subPath: <loris.clonesSubPath>`, read-write | sync, provider |
| `/provider-chunks` | LORIS data claim, `subPath: <loris.chunksSubPath>`, read-write | provider |
| `/tmp` | `emptyDir` | all |

Pod template annotations hold `checksum/scripts` and `checksum/registry`, so a
script or registry change rolls the provider and webhook Deployments.

### Paths on the claim

- Dataset copy: `/clones/<site>/<dataset>`. LORIS sees it at
  `<clonesSubPath>/<site>/<dataset>` through the site's `subPath`.
- Per-dataset lock: `/clones/<site>/.<dataset>.lock`. It is an exclusive
  `fcntl.flock`, held by sync for a whole dataset sync and by the provider
  while it gets or drops content of that dataset.
- Shared expiry ledger: `/clones/.cache-ledger.sqlite` (SQLite, WAL,
  `busy_timeout` 30 s). The provider owns and initializes it; sync only reads
  it, read-only, and treats a missing file as empty. The DDL is frozen:

  ```sql
  CREATE TABLE IF NOT EXISTS content (site TEXT, dataset TEXT, key TEXT, size INTEGER,
      fetched REAL, accessed REAL, PRIMARY KEY (site, dataset, key));
  CREATE TABLE IF NOT EXISTS provider_chunks (site TEXT, dataset TEXT, key TEXT, size INTEGER,
      fetched REAL, accessed REAL, PRIMARY KEY (site, dataset, key));
  ```

  In `provider_chunks`, `key` is the chunk directory's SHA-256 name, and
  `size` is the directory size.
- Ledger rules:
  - The provider inserts a `content` row (`accessed=now`), under the dataset
    lock, before it gets a key.
  - A key whose content was fetched only to build EEG chunks stays pinned
    until the conversion finishes or `chunkTimeout` expires. The pin is its
    row with `accessed` refreshed when the conversion starts and ends.
  - The sweeper takes the dataset lock, then drops content and deletes the row
    in that order. It skips datasets whose lock is busy, and never drops a row
    accessed within `chunkTimeout` while a conversion of that key is active.
  - Sync never drops keys that have a `content` row.
- Imported checkpoint: git ref `refs/loris-forgejo-export/imported` in each
  dataset copy.
  - Sync moves it only after a verified import acknowledgement (see Sync).
  - The keys wanted for a target commit are those of imaging files added or
    changed since that ref. Before the first import, that means every imaging
    file. Content dropped after an earlier import is therefore not fetched
    again.
- EEG chunks: LORIS sends
  `chunkPath = content-provider-chunks/<site>/<dataset>/<sha256>.chunks`. The
  provider writes `/provider-chunks/<site>/<dataset>/<sha256>.chunks/index.json`
  and the chunk files, after stripping the leading `content-provider-chunks/`.

## Environment (prefix `LFX_`)

The `loris-forgejo-export.commonEnv` helper sets these in every container:

- `LFX_REGISTRY`: the path of `registry.json`.
- `LFX_FORGEJO_URL`: the in-cluster base URL, with no `/api/v1`.
- `LFX_FORGEJO_PUBLIC_URL`.
- `HOME=/tmp`.

| Variable | Component | Default | Meaning |
|---|---|---|---|
| `LFX_NAMESPACE` | reconciler, sync, webhook | downward API `metadata.namespace` | Kubernetes namespace for API calls |
| `LFX_FULLNAME` | reconciler, webhook | `<fullname>` | Prefix of the Secret, CronJob and Job names |
| `LFX_ADMIN_USER`, `LFX_ADMIN_PASS` | reconciler only | `forgejo.adminSecret` | Forgejo admin, basic auth. No admin token is ever created |
| `LFX_FORGEJO_PUBLIC_IP` | reconciler, sync | `forgejo.publicIP` | Optional |
| `LFX_WEBHOOK_URL` | reconciler | built-in receiver URL, `triggers.argoEvents.webhookUrl`, or empty when both triggers are disabled (scheduled sync only: no hook is created; existing hooks are only reported) | Target of the org push webhook |
| `LFX_WEBHOOK_SECRET` | reconciler | webhook Secret `secret`; unset in Argo or scheduled-only mode | HMAC secret configured on the org hooks |
| `LFX_WORKER_TEMPLATE` | reconciler | `/etc/loris-forgejo-export/worker-template` | `.worker` template directory |
| `LFX_ENROLLMENT` | reconciler | `1` when `reconciler.enrollment.enabled` | Register runners and publication tokens |
| `LFX_DRY_RUN` | reconciler | `0` | `1` logs writes without performing them |
| `LFX_WAIT_SECONDS` | reconciler, sync | `300` | How long to wait for the Forgejo API |
| `LFX_ANNEX_URL` | sync, provider | `forgejo.annexUrl` | Optional p2phttp URL; if empty it is derived from `LFX_FORGEJO_URL` + `/git-annex-p2phttp` |
| `LFX_SYNC_REPOSITORY` | sync | empty | `<org>/<dataset>`. Empty syncs every managed dataset |
| `LFX_STAGE_TIMEOUT`, `LFX_IMPORT_TIMEOUT` | sync | `sync.stageTimeout` / `importTimeout` | Seconds |
| `LFX_POLL_SECONDS` | sync | `5` | |
| `LFX_PORT` | provider, webhook | `provider.port` / `8080` | HTTP listen port |
| `LFX_CACHE_IDLE_TTL`, `LFX_CACHE_MAX_AGE`, `LFX_CACHE_MAX_BYTES`, `LFX_CACHE_SWEEP_SECONDS`, `LFX_CACHE_OFFLINE_AFTER`, `LFX_CACHE_BATCH_WINDOW`, `LFX_CACHE_RETRY_AFTER`, `LFX_CACHE_CHUNK_TIMEOUT` | provider | `provider.cache.*` | |
| `LFX_SYNC_CRONJOB` | webhook | `<fullname>-sync` | Template for push-triggered Jobs |
| `LFX_RELEASE` | reconciler | `.Release.Name` | `app.kubernetes.io/instance` label on generated Secrets |
| `LFX_HISTORY_REPOSITORY`, `LFX_HISTORY_OLD`, `LFX_HISTORY_NEW` | sync | unset | One-off approved history transition (operator sets them on a manual Job; full 40-hex commit IDs). The old head is kept at `refs/loris-forgejo-export/history-backups/<old>` |
| `LFX_BOT_DIR`, `LFX_WEBHOOK_SECRET_FILE` | sync, provider / webhook | mounted paths below | Test overrides only; the chart never sets them |

Sync and the provider read the bot username and token from the mounted files
every time they need them. If a file is missing or empty, the bot has not been
created yet:

- **sync** exits 1 with the message `bot credentials not provisioned yet; run the reconciler`.
- **provider** answers `/healthz` with 200, and answers content requests as
  `unavailable` with the same message.

Git and the HTTP API authenticate as the bot with HTTP basic auth (username and
token) against `LFX_FORGEJO_URL`. Credentials are never written to
`.git/config` or to logs.

## Forgejo objects (reconciler)

All operations are idempotent, and nothing is ever deleted except stale tokens
the reconciler itself owns and duplicate hooks pointing at `LFX_WEBHOOK_URL`.
Organisations and repositories that the registry does not manage, under
`settings.organizationPrefix`, are only reported as `drift:` lines. When the
prefix is empty, the drift report is limited to organisations named after
known sites.

**Global**

- The export bot `settings.botUser` is restricted, private, and has a random
  password. Its token `loris-forgejo-export` has scopes `["write:repository"]`.
- That token is stored in Secret `<fullname>-bot`, with keys `username` and
  `token`.
  - If the Secret is missing or empty while the token exists, the reconciler
    deletes the token and creates it again.
  - If the Secret write fails, it deletes the new token.

**Per managed site**, in this order:

1. **Organisation `site.org`.** `full_name` is `LORIS site <siteAlias or name>`.
   - Visibility is `limited` if any dataset is `signed-in`, otherwise
     `private`.
   - Any other existing visibility fails the site.
2. **Team `settings.publicationTeam`.** Permission `write`, units
   `["repo.code"]`, `includes_all_repositories: false`, holding exactly the
   dataset repositories. Operators add site users to it.
3. **Team `settings.exportTeam`.** Permission `write`, units
   `["repo.code", "repo.actions"]`, `includes_all_repositories: true`, with
   member `settings.botUser`.
   - This gives the export bot the same control over `.worker` workflows as
     the central administrators. Document this.
4. **Dataset repositories.** One per dataset, with `has_actions: false`.
   - They are private, except that `signed-in` datasets are not private (the
     organisation is limited).
   - Each is added to the publication team.
5. **`settings.workerRepository`.** Private, `has_actions: true`, `auto_init`,
   default branch `main`, and not in the publication team.
   - Its files are made identical to the template on `main`.
   - Extra files are reported, never deleted.
6. **Publication bot `site.bot`.** Restricted, and a member of the publication
   team only.
   - Its token `loris-forgejo-worker` has scopes `["write:repository"]` and is
     stored as the org Actions secret `<settings.variablePrefix>WORKER_TOKEN`.
   - If the secret is missing, the token is replaced.
7. **Org Actions variables.**
   - `<variablePrefix>FORGE_URL` is set to `LFX_FORGEJO_PUBLIC_URL`.
   - `<variablePrefix>FORGE_IP` is set to `LFX_FORGEJO_PUBLIC_IP`, only when
     that value is non-empty.
8. **Org push webhook.**
   - Exactly one hook of type `forgejo`, `content_type: json`,
     `events: ["push"]`, active, pointing at `LFX_WEBHOOK_URL`.
   - Its `secret` is `LFX_WEBHOOK_SECRET` when set. Forgejo never returns the
     secret, so the reconciler PATCHes it on every run.
   - Duplicates are deleted.
9. **Enrollment** (only when `LFX_ENROLLMENT=1`). Secret
   `<fullname>-enrollment-<site>`, of type `Opaque`. It carries labels
   `app.kubernetes.io/name: loris-forgejo-export`,
   `app.kubernetes.io/instance: <release>`,
   `app.kubernetes.io/component: enrollment` and
   `loris-forgejo-export/site: <site>`.

   | Key | Value |
   |---|---|
   | `forgeUrl` | public URL |
   | `forgeIP` | public IP, or empty |
   | `organization` | `site.org` |
   | `workerRepository` | `settings.workerRepository` |
   | `runnerLabel` | `settings.runnerLabel` |
   | `requestCommand` | `settings.requestCommand` |
   | `runnerName` | `<site>-worker` |
   | `runnerUUID`, `runnerToken` | from `POST /orgs/{org}/actions/runners` `{"name": "<site>-worker"}` |
   | `publicationUser` | `site.bot` |
   | `publicationToken` | token `loris-forgejo-publication`, scopes `["write:repository"]` |
   | `repositories` | JSON list of datasets, refreshed every run |

   - The reconciler writes the Secret right after each identity is created:
     first the runner keys, then the publication keys.
   - Keys that are already present are never regenerated.
   - **Rotation:** delete the Secret, or one key pair.
     - **Runner.** If the runner keys are missing while a runner named
       `<site>-worker` exists, the reconciler deletes that runner through
       `DELETE /orgs/{org}/actions/runners/{id}` and registers a new one. If
       deletion is unsupported, the site fails with instructions.
     - **Publication token.** If the token keys are missing while the token
       exists, the reconciler deletes the token and creates a new one.
   - Secrets are not Helm-managed and survive uninstall.

**Mutual exclusion.** Before writing anything, the reconciler acquires
Lease `<fullname>-reconcile` (`coordination.k8s.io/v1`):

- `holderIdentity` is the pod name.
- `leaseDurationSeconds` is 300, renewed every 60 s by a background thread.
- An unexpired Lease held by someone else makes the run wait up to
  `LFX_WAIT_SECONDS`, then exit 1 without writing.
- If renewal fails, the run aborts before the next identity creation.
- `LFX_DRY_RUN=1` skips the Lease.

This covers the CronJob, revision Jobs and manual runs.

The reconciler exits 1 if any site failed. It continues with the other sites
after a failure.

### Worker template

The `files/worker-template/**` files, or `forgejo.worker.templateFiles`
(path to content, which fully replaces the bundled files), may contain these
placeholders:

- `@RUNNER_LABEL@`
- `@REQUEST_COMMAND@`
- `@VARIABLE_PREFIX@`
- `@WORKER_REPOSITORY@`

The reconciler substitutes them from the registry settings. Helm never
templates these files, so `${{ }}` stays verbatim.

Required workflows on the `main` branch of `.worker`:

| File | Inputs | Site command |
|---|---|---|
| `.forgejo/workflows/probe.yml` | none | `@REQUEST_COMMAND@ status`, then `@REQUEST_COMMAND@ probe <dataset>` for each dataset |
| `.forgejo/workflows/refresh.yml` | `dataset` | `@REQUEST_COMMAND@ refresh "$DATASET"` |
| `.forgejo/workflows/stage-content.yml` | `dataset`, `commit`, `keys` (space-separated SHA256E keys, at most 1000) | `@REQUEST_COMMAND@ stage "$DATASET"` with `COMMIT` and `KEYS` in env |

All of them use `runs-on: @RUNNER_LABEL@`. Sync dispatches through
`POST /repos/{org}/{workerRepository}/actions/workflows/stage-content.yml/dispatches`
`{"ref": "main", "inputs": {...}, "return_run_info": true}`. It follows
`GET .../actions/runs/{id}` and cancels on timeout.

## Kubernetes objects and RBAC

Every object carries the `loris-forgejo-export.labels` labels. Pods add
`app.kubernetes.io/component`, which is one of `reconciler`, `sync`,
`provider` or `webhook`.

| Object | Notes |
|---|---|
| ConfigMaps | `<fullname>-scripts`, `<fullname>-registry`, `<fullname>-worker-template` |
| Secret `<fullname>-webhook` | Rendered when the webhook is enabled and `existingSecret` is empty. Its key `secret` holds 48 random characters, kept across upgrades through `lookup` |
| ServiceAccounts | `<fullname>-reconciler`, `<fullname>-sync`, `<fullname>-webhook`. The provider uses `automountServiceAccountToken: false` |
| Role `<fullname>-reconciler` | secrets: `create`; `get`, `update`, `patch` with `resourceNames` set to `<fullname>-bot` and the enrollment names (enrollment ones only when enabled). leases: `create`; `get`, `update` with `resourceNames: [<fullname>-reconcile]` |
| Role `<fullname>-sync` | cronjobs: `get` with `resourceNames: [<importCronJobPrefix><site> …]`, only for importing sites; jobs: `create`, `get`, `list`; pods: `list`; pods/log: `get` (to read LORIS discovery and import results) |
| Role `<fullname>-webhook` | cronjobs: `get` with `resourceNames: [<fullname>-sync]`; jobs: `create` |
| CronJob `<fullname>-reconcile` | `reconciler.schedule`, `concurrencyPolicy: Forbid` |
| Job `<fullname>-reconcile-<revision>` | Not a Helm hook. Rendered for every release revision, with `ttlSecondsAfterFinished: 86400`. Gives the first reconcile at install |
| CronJob `<fullname>-sync` | `sync.schedule`, `concurrencyPolicy: Forbid`, `suspend: sync.suspend`. Its pod spec comes from the `loris-forgejo-export.syncPodSpec` helper |
| Deployment and Service `<fullname>-provider` | Port `provider.port`. Probes `GET /healthz`. `strategy: Recreate` |
| Deployment and Service `<fullname>-webhook` | Port 8080. Path `POST /forgejo`, probes `GET /healthz` |
| NetworkPolicy `<fullname>-provider` | Ingress only from pods matching any `loris.clientPodSelectors`, on the provider port |
| NetworkPolicy `<fullname>-webhook` | Ingress on 8080 from any source; it is HMAC-protected |
| Argo Sensor `<fullname>-sync` (optional) | Lives in `triggers.argoEvents.namespace`, with ServiceAccount `<fullname>-sensor` in that namespace and a RoleBinding to Role `<fullname>-webhook` in the release namespace. Its k8s trigger creates a Job using `syncPodSpec`, with `LFX_SYNC_REPOSITORY` taken from `body.repository.full_name` |

The Sensor applies the receiver's filters as dependency data filters:

- the header `X-Forgejo-Event` is `push`;
- `body.ref` matches `refs/heads/.*`, but not `refs/heads/git-annex`;
- `body.repository.full_name` matches the regex of the managed
  `<org>/<dataset>` names, rendered from the registry.

Argo's webhook EventSource does not verify Forgejo signatures. Sync therefore
re-validates the repository against the registry and is idempotent: an
unauthenticated caller can at most start no-op sync Jobs of managed datasets.
The README documents this, and recommends the built-in receiver or an
EventSource behind an authenticating ingress.

### Validation rules

- `triggers.webhook.enabled` and `triggers.argoEvents.enabled` are mutually
  exclusive. Both disabled means scheduled sync only.
- Forgejo user and organization names share one case-insensitive namespace.
  The export bot, every site organization and every publication bot must not
  collide, and `publicationTeam` must differ from `exportTeam`. Datasets are
  compared case-insensitively with `workerRepository`. `registry.py` and the
  Helm validation helper enforce this.
- Argo requires `eventSourceName`, `eventName` and `webhookUrl`.
- The webhook URL that the reconciler registers is
  `http://<fullname>-webhook.<namespace>.svc.cluster.local:8080/forgejo`.
  Operators must allow that host in Forgejo's `[webhook] ALLOWED_HOST_LIST`.

All pods use the `loris-forgejo-export.podSecurityContext` (uid/gid 33) and
`containerSecurityContext` (read-only root, no capabilities) helpers.
Scheduling comes from the `loris-forgejo-export.scheduling` helper.

## Sync (`sync.py`)

`main()` loads the registry and processes each target `(site, dataset)`.

- **Targets.** `LFX_SYNC_REPOSITORY` alone, which must resolve through
  `registry.by_repository`; otherwise sync exits 1. Without it, every dataset
  of every enabled managed site.
- **Lock.** Each target runs under the per-dataset lock. The flow is the same
  as RE-MOVE `sync.py`.

**Steps**

1. **Fetch.** Clone or fetch `/clones/<site>/<dataset>` from
   `LFX_FORGEJO_URL/<org>/<dataset>.git`.
   - Refresh the origin annex UUID from `/<org>/<dataset>.git/config`.
   - Resolve the target commit (the remote default branch), but do not check
     it out yet.
   - Refuse non-descending history relative to the imported ref, except for
     the one-off approved transition carried over from RE-MOVE.
2. **Hydrate.** Compute the wanted keys (see "Imported checkpoint"). Stage the
   keys the site must upload through `stage-content`. Run `git annex get
   --key` from origin.
   - Steps 1–2 never change the working tree LORIS sees.
3. **Publish.** Check out the target commit with `git merge --ff-only`, or the
   approved transition. If import is disabled, stop here (status `fetched`).
4. **Import**, when `site.import_enabled`. This step produces a verified
   acknowledgement. Because LORIS discovery does not take this chart's lock,
   publication happens only after hydration, and LORIS's own fingerprint
   check rejects a tree that changes during an import.
   1. **Wait for earlier imports.** List the Jobs labelled
      `scilus.net/bids-dataset=<sha256("<site>/<dataset>")[:32]>`. Wait for
      any that are unfinished.
   2. **Start discovery.** Create Job `<importCronJobPrefix><site>-lfx-<8
      random hex>` (at most 53 characters) from that CronJob's `jobTemplate`,
      with annotation `loris-forgejo-export/triggered-by: sync`. Wait for it
      to complete.
   3. **Read the discovery log.** Read its pod log. LORIS prints one JSON
      object per line:
      - `{"discovery": "already-active"}` means not acknowledged; retry
        later.
      - `{"dataset": "<sha256 id>", "attempt": n, "job": "<name>"}` means
        LORIS dispatched an import for that dataset.
   4. **Read the import log.** If an import was dispatched, wait for that Job.
      It is acknowledged only when it is Complete **and** its pod log contains
      `{"dataset": "<id>", ..., "result": {"mode": "import", ...}}`. A
      Complete Job without that line means LORIS deferred the import: not
      acknowledged.
   5. **No dispatch line.** If discovery dispatched nothing for the dataset,
      and no import Job for it is unfinished, LORIS already holds this
      fingerprint: acknowledged.
   6. **On acknowledgement**, move `refs/loris-forgejo-export/imported` to the
      target commit. Otherwise log `import-pending` and leave the ref
      unchanged; the next sync retries.
5. **Drop.** This always runs, even after a failure.
   - Git ref `refs/loris-forgejo-export/dropped` marks the imported commit
     whose local drop completed. It only moves after a successful local drop.
     Every run first retries the drop of keys between it and the imported ref,
     so a failed drop is not lost when the imported ref is already current.
   - Drop Forgejo transit copies whose site copy is recorded in the location
     log.
   - Drop local imaging content only for keys at or below the imported ref
     whose site copy is recorded and that have no ledger `content` row.
   - Metadata files are never dropped. Without an acknowledgement, local
     content is kept.

Sync logs one JSON object per line on stdout, with keys `event`, `site` and
`dataset`. It exits 1 if any dataset failed. A pending import is not a
failure.

## Provider (`provider.py`)

The provider serves LORIS's provider-neutral contract (see the LORIS chart
README, "Provider-neutral on-demand content"). There is no legacy key API.

| Endpoint | Body | 200 | 202 | 404 |
|---|---|---|---|---|
| `POST /content` | `{"path": "<site>/<dataset>/<relative-file>"}` | `{"state":"ready"}` | `{"state":"pending","message":...,"retryAfter":n}` | `{"state":"unavailable","message":...}` |
| `POST /chunks` | `{"path": ..., "chunkPath": "content-provider-chunks/<site>/<dataset>/<sha256>.chunks"}` | same | same | same |

- Requests larger than 4096 bytes, malformed requests, and paths outside a
  managed dataset get 400.
- `GET /healthz` returns 200.
- Behavior carried over from RE-MOVE `cache.py`:
  - per-dataset batching within `batchWindow`;
  - `unavailable` after `offlineAfter` seconds without a runner pickup;
  - the ledger, with an idle/age/byte-cap sweeper every `sweepSeconds`;
  - EEG chunker selection by extension: `.edf` uses `edf-to-chunks`, `.set`
    uses `eeglab-to-chunks`, and `.vhdr`/`.bdf` use `ephys_chunks.py`.

## Webhook (`webhook.py`)

- **`POST /forgejo`**
  - The body is at most 1 MiB.
  - The event must be `push`, from `X-Forgejo-Event`, or `X-Gitea-Event` as a
    fallback.
  - The signature is the `X-Forgejo-Signature` header (fallback
    `X-Gitea-Signature`): the hex HMAC-SHA256 of the raw body under the secret
    file. It is compared in constant time. A wrong signature returns 401.
  - The request is ignored with 204 when:
    - `repository.full_name` is not `registry.by_repository` (this also
      covers `.worker`);
    - or `ref` is not `refs/heads/<repository.default_branch>`.
- **On accept**, it creates Job `<fullname>-push-<8 random hex>` from CronJob
  `LFX_SYNC_CRONJOB`'s `jobTemplate`, and returns 202
  `{"job": name}`.
  - It sets `LFX_SYNC_REPOSITORY=<org>/<dataset>` in the first container.
  - Labels: `loris-forgejo-export/repository=<sha256(full_name)[:32]>`.
  - Annotations: `loris-forgejo-export/repository: <full_name>` and
    `loris-forgejo-export/triggered-by: webhook`.
  - Concurrent Jobs are safe because sync serializes on the dataset lock.
- **`GET /healthz`** returns 200.
