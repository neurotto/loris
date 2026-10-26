# LORIS NFS export Helm chart

Expose an approved site directory as a **read-only NFSv4 export** for a central
[LORIS](https://loris.ca/) deployment. The chart runs the custom
`docker.io/scilus/loris-nfs-export:0.3.0` image, based on NFS-Ganesha with its VFS
backend. Chart latest version: **0.1.0**.

> **Disabled by default:** no resources are rendered unless both `dataPath` and
> a nonempty `allowedClientCIDRs` are provided. Enabling the exporter also
> requires the source's owning group ID (`exportGid`) and a valid node hostname.
> This is a site-side development integration, not a production security
> certification or a complete LORIS deployment.

## What this chart does

- Runs one node-pinned Deployment with a fixed TCP `hostPort` (default `20490`).
- Mounts an existing host directory read-only at `/export`.
- Serves it at the NFSv4 pseudo-path **`/bids`**, using TCP and `sec=sys`.
- Restricts clients using an operator-supplied CIDR list.
- Squashes **all** client identities to the configured nonzero UID/GID.
- Uses `Recreate` upgrades so the previous Pod releases the host port first.
- Runs filesystem readiness checks as the squashed identity and a TCP liveness
  check.

It does not create a Service, Ingress, PVC, storage copy, firewall rule,
NetworkPolicy, authentication Secret, encrypted tunnel, or central LORIS mount.
It does not import datasets. The site operator remains responsible for the source,
access policy, network protection, and availability.

## Requirements

- Helm 3 and Kubernetes supporting `apps/v1` Deployments. The audit's default
  schema target is Kubernetes 1.33.0; this is not a tested minimum version.
- A Linux node with the approved source directory and a known
  `kubernetes.io/hostname` label.
- Registry access to the exporter image. Confirm the selected tag exists before
  deployment, or build and publish the image separately from this repository.
- A free TCP port on the selected node, reachable from approved NFS client nodes.
  This chart defaults to `20490` to avoid an existing kernel NFS server on `2049`.
- Permission under cluster admission policy to use `hostPath`, `hostPort`, root,
  and capabilities `DAC_READ_SEARCH`, `SETUID`, and `SETGID`. This workload is
  **not compatible with a Restricted Pod Security policy** as configured.
- Source filesystem group read/traverse permissions for `exportGid` across the
  approved tree. Client nodes also need NFSv4.1 client support.

The Pod deliberately does **not** use host networking. Switching to
`hostNetwork` can allow Ganesha to change the node's rpcbind registrations and
interfere with an existing NFS service.

## Source layout and permissions

For scheduled LORIS BIDS discovery, export an approved parent directory containing
immediate dataset folders:

```text
/srv/approved-bids/
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

The exporter itself does not validate BIDS contents. The central importer expects
this shared-root layout, not a dataset directly at `/bids`. Existing single-dataset
exports require a coordinated path/record migration before their root changes.

Choose the source's **existing owning group** for `exportGid`, and a nonzero
`exportUid` (default `10001`). Ganesha's VFS permission checks use mode bits, not
named POSIX ACLs. A named-user ACL alone is not sufficient; the intended group
must have read permission on files and read/traverse permission on directories.
Account for ancestor access, mixed ownership, and any filesystem-specific
restrictions. Obtain site-owner approval before changing permissions; the chart
does not change source ownership or permissions.

The repository's legacy `prepare-source.sh` helper is **not a supported setup
step**: it currently has a shell syntax error and describes ACL-based preparation
that does not replace the group-mode requirement above. The audit scans it and
reports the defect. Do not run it against research data.

## Installation

These commands use the chart from a repository checkout; they do not assume a
published Helm repository URL.

### 1. Verify the target node and source

```sh
kubectl get nodes -L kubernetes.io/hostname
```

Confirm source access, the approved client-node addresses, firewall rules, and
the node port with the site administrator. Prefer narrow client CIDRs such as
individual `/32` IPv4 addresses. NFS mounts normally originate from Kubernetes
**nodes**, not the portal's Pod address; account for routing/NAT before choosing
the allowlist.

### 2. Create site values

Save the following as `export-values.yaml` and replace every site-specific value:

```yaml
siteName: example-site
nodeHostname: example-site-node
dataPath: /srv/approved-bids
allowedClientCIDRs:
  - 192.0.2.10/32
nfsPort: 20490
exportUid: 10001
exportGid: 2000
```

`192.0.2.10` is a documentation address, not a usable client in your environment.
Use trusted operator values only: the chart joins the CIDR strings into Ganesha
configuration without validating CIDR syntax. Use a nonzero group ID; quote it
if necessary to distinguish it from an unset value.

### 3. Install

```sh
helm upgrade --install loris-nfs-export ./charts/loris-nfs-export \
  --namespace loris-export --create-namespace \
  --values export-values.yaml \
  --wait --timeout 5m

kubectl -n loris-export get pods -o wide
kubectl -n loris-export logs deployment/loris-nfs-export-example-site \
  -c nfs-ganesha
```

A missing/unreadable source or rejected identity/port should be investigated
through events and logs. A blank hostname leaves the enabled Pod unschedulable.
The hostPath has no Kubernetes type constraint; operators must verify that
`dataPath` exists and is the intended directory.

Only one instance can bind a given node/port. Resource names are based on
`siteName`, not the Helm release name; use unique site names within a namespace.
Changing names does not avoid host-port collisions.

## Connect the central LORIS chart

After exporter readiness and node-to-node reachability have been verified,
configure the central chart separately:

```yaml
sites:
  - name: example-site
    enabled: true
    server: 192.0.2.20
    nfsPort: 20490
    capacity: 1Ti
    siteAlias: EX
    bidsImport:
      enabled: false
```

Use the actual exporter node address for `server`, not its Pod address or a
Service name. The central chart mounts `/bids` read-only under
`/data/loris/data/assembly_bids/example-site`. The declared PV capacity is not
an NFS quota.

Keep import disabled until a synthetic dataset has been tested and registration
policy is approved. Central scheduled imports additionally require persistent
writable LORIS data, registration settings, and the global import switch.
Mounting files does not register candidates or scans in LORIS.

## Configuration reference

| Value                         | Default                               | Description                                                                                     |
| ----------------------------- | ------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `siteName`                  | `site`                              | Deployment/label suffix. Use a short, unique lowercase DNS label.                               |
| `nodeHostname`              | `""`                                | Exact `kubernetes.io/hostname` label of the source node; required operationally when enabled. |
| `dataPath`                  | `""`                                | Existing approved absolute host directory. Empty prevents resource creation.                    |
| `allowedClientCIDRs`        | `[]`                                | Approved NFS client-node CIDRs. Empty prevents resource creation.                               |
| `nfsPort`                   | `20490`                             | Container and node TCP port. Entrypoint accepts `1024`–`65535`.                            |
| `exportUid`                 | `10001`                             | Nonzero anonymous UID for all client requests.                                                  |
| `exportGid`                 | `""`                                | Required nonzero owning group ID granting source read/traverse.                                 |
| `image.repository`          | `docker.io/scilus/loris-nfs-export` | Custom image repository; must provide its entrypoint and Ganesha VFS dependencies.              |
| `image.tag`                 | `0.3.0`                             | Image tag; chart has no separate digest value.                                                  |
| `image.pullPolicy`          | `IfNotPresent`                      | Image pull policy.                                                                              |
| `resources.requests.cpu`    | `100m`                              | Exporter CPU request.                                                                           |
| `resources.requests.memory` | `128Mi`                             | Exporter memory request.                                                                        |
| `resources.limits.memory`   | `512Mi`                             | Exporter memory limit.                                                                          |

The replica count (`1`), `Recreate` strategy, hostname selector, hostPath mount,
security context, and probe settings are fixed in the template. There are no
values for affinity, tolerations, image-pull Secrets, TLS, or persistent Ganesha
state. Changing supported settings requires a reviewed chart change, not an
undocumented values key.

## Access model and limitations

| Boundary      | Configured behavior                                                                      | What operators must still provide                                              |
| ------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Source        | Read-only container mount and read-only Ganesha export                                   | Approved stable source, correct mode bits, de-identification, and backups      |
| Identity      | `all_squash` to `exportUid:exportGid`                                                | A least-privilege nonzero source group; no per-user identity is preserved      |
| Network       | TCP NFSv4, CIDR allowlist, fixed host port                                               | Private routing, narrow firewall rules, and encrypted transport where required |
| Container     | UID 0, only three added capabilities, no privilege escalation, read-only root filesystem | Explicit admission approval and image vulnerability/provenance review          |
| Runtime state | `emptyDir` for `/run/ganesha`, `/var/lib/ganesha`, and `/tmp`                    | Outage/restart testing; recovery state is not durable                          |

`sec=sys` is **not cryptographic client authentication**. The chart does not
configure Kerberos or transport encryption. A client within an approved network
identity can read the approved export; do not expose this service to the public
internet. Read-only mounts protect against client writes, not against disclosure
to an allowed client or source changes made by the site.

No explicit ServiceAccount/token opt-out or NetworkPolicy is rendered. Review
default service-account permissions, admission policy, egress, and host-port
firewall enforcement. Readiness checks only the export root and its immediate
listing, not every file or an end-to-end NFS request. Liveness checks only the
TCP listener.

## Operations

```sh
kubectl -n loris-export describe deployment loris-nfs-export-example-site
kubectl -n loris-export get events --sort-by=.metadata.creationTimestamp
kubectl -n loris-export logs deployment/loris-nfs-export-example-site \
  -c nfs-ganesha
```

- **No resources:** ensure both `dataPath` and `allowedClientCIDRs` are nonempty.
- **Helm render fails:** provide `exportGid`; inspect types and rendered values.
- **Pending:** inspect the hostname label, admission decisions, and host-port
  conflicts.
- **Unready:** inspect root access as the squashed identity and source permissions.
  A ready Pod is not proof that the full dataset can be read.
- **Client timeout/access denied:** inspect node-origin addresses, firewall/NAT,
  CIDRs, NFS version, `/bids` pseudo-path, and the nonstandard port.
- **Permission denied within the export:** check owning groups and mode bits,
  not only POSIX ACLs.

For upgrades, preserve a copy of reviewed values and the exact image identity,
then repeat `helm upgrade --install`. `Recreate` causes an interruption; the chart
does not provide high availability or persistent NFS recovery state. Verify
client reconnection and data reads before enabling central imports again.

Uninstall:

```sh
helm uninstall loris-nfs-export --namespace loris-export
```

Removing the release stops export access and removes ephemeral Ganesha state.
It does **not** delete the host source directory. Disable or unmount central
consumers first and coordinate any path changes with existing LORIS registrations.

## Security audit

From the repository root, on Linux x86_64 with Python 3.12:

```sh
python3 tools/security/loris/setup.py
export PATH="$PWD/.audit-tools/bin:$PWD/.audit-tools/venv/bin:$PATH"
python tools/security/loris/audit.py \
  --chart loris-nfs-export \
  --output audit-results/loris-nfs-export
```

The shared runner checks dormant/enabled/custom-port profiles, Kubernetes
schemas/policies, shell scripts including the image entrypoint, Dockerfile and
workflow configuration, secrets, and the actual exporter image's dependencies.
It generates Markdown/JSON reports and a CycloneDX SBOM. High/critical scanner
alerts fail the gate; required scanner failures produce an explicitly incomplete
report. Alerts are not confirmed vulnerabilities until reviewed.

Use `--app-image` to select an exporter image, or `--image-source docker` for a
local image. No image is automatically built or pushed. Optional `--runtime`
validation uses synthetic data in an owned disposable kind cluster, not real site
paths. It requires separate runtime tool setup and suitable host resources.
At the initial audit, the default public `0.3.0` image tag was unavailable
(`MANIFEST_UNKNOWN`). Until the image is published or an explicit image override
is supplied, dependency scanning is incomplete and no exporter SBOM can be
produced. The audit reports this as an error, never a clean scan.

See the [audit guide](https://github.com/neurotto/loris/tree/main/tools/security/loris)
for report formats, Mermaid flow/trust-boundary diagrams with textual equivalents,
CI integration, runtime prerequisites, and limitations. These repository audit
tools are not packaged in the chart. No automated scan certifies production
readiness; review source permissions, network isolation, image provenance, and
site-owner approval before exposing research data.

## References

- [NFS-Ganesha](https://github.com/nfs-ganesha/nfs-ganesha)
- [Kubernetes hostPath volumes](https://kubernetes.io/docs/concepts/storage/volumes/#hostpath)
- [LORIS](https://github.com/aces/Loris)
