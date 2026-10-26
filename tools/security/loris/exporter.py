"""Exporter-specific profiles and safety invariants, sharing the audit/report core."""

from pathlib import Path

from audit import Audit, HERE, ROOT, finding
from common import CheckError


class ExporterAudit(Audit):
    def __init__(self, output: Path, scratch: Path, kubernetes: str, image_source: str,
                 chart: Path = ROOT / "charts/loris-nfs-export") -> None:
        super().__init__(output, scratch, kubernetes, image_source, chart)
        self.limitations = [
            "Automated checks cannot certify absence of vulnerabilities; alerts require manual triage.",
            "Synthetic profiles do not validate actual source permissions, node firewalls, routing, or admission policy.",
            "NFS sec=sys and client CIDRs are not cryptographic authentication or encrypted transport.",
            "Root plus DAC_READ_SEARCH/SETUID/SETGID, hostPath and hostPort require explicit site-owner risk review.",
            "The chart does not validate CIDR syntax; configuration values are trusted operator input.",
            "The legacy prepare-source.sh is syntax-checked but never executed or used to alter source permissions.",
            "Image scans inventory shipped dependencies, not all build-only dependencies or image provenance.",
            "Optional runtime tests use synthetic data only and require a suitable isolated Linux Docker host.",
        ]

    def profiles(self) -> None:
        for name in ("disabled", "path-only", "clients-only", "enabled", "custom-port"):
            self.check(f"profile/{name}", lambda name=name: self.profile(name))
        self.check("profile/negative-missing-group", self.negative)

    def profile(self, name: str) -> None:
        if name == "disabled":
            values = []
        elif name == "path-only":
            values = ["--set", "dataPath=/var/local/loris-audit-source"]
        elif name == "clients-only":
            values = ["--set", "allowedClientCIDRs[0]=192.0.2.10/32"]
        else:
            values = ["-f", str(HERE / "profiles/exporter-enabled.yaml")]
            if name == "custom-port":
                values.extend(["--set", "nfsPort=20500", "--set", "exportUid=12001",
                               "--set", "exportGid=3000", "--set", "allowedClientCIDRs[1]=192.0.2.11/32"])
        documents = self.render(values)
        dormant = name in ("disabled", "path-only", "clients-only")
        if not dormant:
            self.invariants(documents, 20500 if name == "custom-port" else 20490)
        self.validate_profile(name, documents, dormant)

    def invariants(self, documents: list[dict], port: int) -> None:
        if len(documents) != 1 or documents[0].get("kind") != "Deployment":
            raise CheckError("Exporter must render exactly one Deployment")
        deployment = documents[0]
        if deployment["spec"].get("replicas") != 1 or deployment["spec"]["strategy"].get("type") != "Recreate":
            raise CheckError("Exporter hostPort requires one replica with Recreate strategy")
        pod = deployment["spec"]["template"]["spec"]
        if pod.get("hostNetwork") or pod["nodeSelector"].get("kubernetes.io/hostname") != "audit-node":
            raise CheckError("Exporter must remain node-pinned without host networking")
        container = pod["containers"][0]
        context = container["securityContext"]
        if (context.get("allowPrivilegeEscalation") is not False
                or context.get("readOnlyRootFilesystem") is not True
                or context.get("privileged", False)):
            raise CheckError("Exporter lost required container isolation settings")
        capabilities = context["capabilities"]
        if (capabilities.get("drop") != ["ALL"]
                or set(capabilities.get("add", [])) != {"DAC_READ_SEARCH", "SETUID", "SETGID"}):
            raise CheckError("Exporter capabilities changed; review the access boundary")
        mount = next(item for item in container["volumeMounts"] if item["name"] == "site-data")
        if mount.get("readOnly") is not True or mount.get("mountPath") != "/export":
            raise CheckError("Exporter source must remain read-only at /export")
        volume = next(item for item in pod["volumes"] if item["name"] == "site-data")
        if volume["hostPath"]["path"] != "/var/local/loris-audit-source":
            raise CheckError("Exporter source path does not match the synthetic fixture")
        nfs = container["ports"][0]
        env = {item["name"]: item["value"] for item in container["env"]}
        if (nfs.get("hostPort") != port or nfs.get("containerPort") != port
                or nfs.get("protocol") != "TCP" or env.get("NFS_PORT") != str(port)):
            raise CheckError("Exporter NFS port settings are inconsistent")
        if not env.get("NFS_ALLOWED_CLIENTS") or env.get("EXPORT_UID") in ("", "0") or env.get("EXPORT_GID") in ("", "0"):
            raise CheckError("Exporter allowlist or squashed identity is missing")

    def negative(self) -> None:
        command = ["helm", "template", "audit", str(self.chart),
                   "-f", str(HERE / "profiles/exporter-enabled.yaml"), "--set", "exportGid="]
        self.run(command, expected_error="exportGid must be the source data's owning group ID")

    def sources(self) -> None:
        super().sources()
        for path in sorted(self.chart.rglob("*.sh")):
            shell = "bash" if path.read_text().splitlines()[0].endswith("bash") else "sh"
            self.check(f"source/syntax/{path.relative_to(self.chart)}",
                       lambda path=path, shell=shell: self.run([shell, "-n", str(path)]))
        self.check("source/entrypoint-contract", self.entrypoint_contract)

    def entrypoint_contract(self) -> None:
        text = (self.chart / "image/entrypoint.sh").read_text()
        for fragment in ("Access_Type = RO;", "Squash = all_squash;", "SecType = sys;",
                         "Pseudo = /bids;", "Protocols = 4;", "Transports = TCP;",
                         "Clients = ${NFS_ALLOWED_CLIENTS};"):
            if fragment not in text:
                raise CheckError("Exporter NFS configuration contract changed; review runtime behavior")
        # This is an explicit limitation, not a claim that the literal check proves enforcement.
        self.findings.append(finding(
            "exporter-plaintext-sys-auth", "MEDIUM",
            str(self.chart / "image/entrypoint.sh"), "configuration",
            text[:text.index("SecType = sys;")].count("\n") + 1,
            "Use approved private routing/firewalls and an encrypted transport where required; sec=sys is not strong authentication.",
        ))
