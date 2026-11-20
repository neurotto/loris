"""Forgejo-export profiles and workload/RBAC/network invariants, sharing the audit/report core."""

import ast
import hashlib
from pathlib import Path

from audit import Audit, HERE, ROOT, finding
from common import CheckError

PROFILE = HERE / "profiles/forgejo-export.yaml"
DIGEST = "sha256:" + "0123456789abcdef" * 4
WORKLOADS = {"Pod", "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob"}
# Rule -> (severity, remediation). Every violation is reported per resource, never suppressed.
RULES = {
    "forgejo-export-host-namespace": ("HIGH", "Remove hostNetwork/hostPID/hostIPC from the pod."),
    "forgejo-export-hostpath": ("HIGH", "Replace hostPath volumes with PVC, ConfigMap, Secret or emptyDir volumes."),
    "forgejo-export-root-user": ("HIGH", "Run every pod and container with runAsNonRoot and the LORIS data uid."),
    "forgejo-export-privileged": ("HIGH", "Remove privileged: true from the container."),
    "forgejo-export-privilege-escalation": ("HIGH", "Set allowPrivilegeEscalation: false on every container."),
    "forgejo-export-writable-root": ("MEDIUM", "Set readOnlyRootFilesystem: true and mount emptyDir for scratch paths."),
    "forgejo-export-capabilities": ("MEDIUM", "Drop ALL capabilities and add none."),
    "forgejo-export-seccomp": ("MEDIUM", "Set seccompProfile type RuntimeDefault on the pod or every container."),
    "forgejo-export-resource-limits": ("MEDIUM", "Set at least a memory limit on every container."),
    "forgejo-export-unpinned-image": ("HIGH", "Render image.repository@image.digest whenever a digest is supplied."),
    "forgejo-export-provider-token": ("HIGH", "Set automountServiceAccountToken: false on the provider pod."),
    "forgejo-export-cluster-rbac": ("HIGH", "Use namespaced Roles/RoleBindings only; never bind ClusterRoles."),
    "forgejo-export-rbac-wildcard": ("HIGH", "List exact apiGroups, resources, verbs and resourceNames."),
    "forgejo-export-missing-networkpolicy": ("HIGH", "Render an Ingress NetworkPolicy selecting the component's pods."),
}


def pod_spec(document: dict) -> dict:
    spec = document["spec"]
    if document["kind"] == "Pod":
        return spec
    if document["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


def pod_labels(document: dict) -> dict:
    if document["kind"] == "Pod":
        return document["metadata"].get("labels", {})
    spec = document["spec"]
    if document["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"].get("metadata", {}).get("labels", {})


class ForgejoExportAudit(Audit):
    # Unit-test harnesses are excluded from the packaged chart by .helmignore.
    semgrep_unscanned = ("tests",)

    def __init__(self, output: Path, scratch: Path, kubernetes: str, image_source: str,
                 chart: Path = ROOT / "charts/loris-forgejo-export") -> None:
        super().__init__(output, scratch, kubernetes, image_source, chart)
        self.limitations = [
            "Automated checks cannot certify absence of vulnerabilities; alerts require manual triage.",
            "Synthetic two-site profiles do not validate the real Forgejo, LORIS claim, CNI enforcement or admission policy.",
            "NetworkPolicies are checked for presence and pod selection only; the webhook port is intentionally reachable "
            "and relies on HMAC signatures that need manual review.",
            "Reconciler/sync tokens, Forgejo administrator credentials and site enrollment Secrets require operator review.",
            "Image scans inventory shipped dependencies of the published image, which may differ from local image/ changes.",
            "Runtime cluster validation is not implemented for this chart.",
        ]

    def profiles(self) -> None:
        for name in ("disabled", "two-site", "pinned-digest"):
            self.check(f"profile/{name}", lambda name=name: self.profile(name))
        for name, values, message in (
            ("triggers-exclusive", ["--set", "triggers.argoEvents.enabled=true"],
             "triggers.webhook and triggers.argoEvents are mutually exclusive"),
            ("nfs-forgejo-site", ["--set", "sites[0].forgejo.enabled=true"],
             "Forgejo sites require source pvc"),
        ):
            self.check(f"profile/negative-{name}", lambda values=values, message=message: self.run(
                ["helm", "template", "audit", str(self.chart), "-f", str(PROFILE), *values],
                expected_error=message))

    def profile(self, name: str) -> None:
        values = [] if name == "disabled" else ["-f", str(PROFILE)]
        if name == "pinned-digest":
            values.extend(["--set", f"image.digest={DIGEST}"])
        documents = self.render(values)
        dormant = name == "disabled"
        if not dormant:
            self.invariants(documents, name, DIGEST if name == "pinned-digest" else None)
        self.validate_profile(name, documents, dormant)

    def flag(self, rule: str, document: dict, profile: str) -> None:
        level, remediation = RULES[rule]
        resource = f"{document['kind']}/{document['metadata']['name']}"
        item = finding(rule, level, str(self.chart / "templates"), "configuration", remediation=remediation)
        item.update(profile=profile, resource=resource)
        item["id"] = hashlib.sha256((item["id"] + profile + resource).encode()).hexdigest()[:16]
        self.findings.append(item)

    def invariants(self, documents: list[dict], profile: str, digest: str | None = None, uid: int = 33) -> None:
        workloads = [item for item in documents if item.get("kind") in WORKLOADS]
        components = {}
        for item in workloads:
            component = pod_labels(item).get("app.kubernetes.io/component")
            if item["kind"] == "Deployment" and component:
                components[component] = item
        if not {"provider", "webhook", "sync", "reconciler"} <= {
                pod_labels(item).get("app.kubernetes.io/component") for item in workloads}:
            raise CheckError("Forgejo export profile must render reconciler, sync, provider and webhook workloads")
        for item in workloads:
            self.workload(item, profile, digest, uid)
        provider = components.get("provider")
        if provider is None:
            raise CheckError("Forgejo export profile must render the provider Deployment")
        if pod_spec(provider).get("automountServiceAccountToken") is not False:
            self.flag("forgejo-export-provider-token", provider, profile)
        policies = [item for item in documents if item.get("kind") == "NetworkPolicy"]
        for component, deployment in components.items():
            labels = pod_labels(deployment)
            if not any("Ingress" in policy["spec"].get("policyTypes", ["Ingress"])
                       and policy["spec"].get("podSelector", {}).get("matchLabels")
                       and policy["spec"]["podSelector"]["matchLabels"].items() <= labels.items()
                       for policy in policies):
                self.flag("forgejo-export-missing-networkpolicy", deployment, profile)
        for item in documents:
            kind = item.get("kind")
            if kind in ("ClusterRole", "ClusterRoleBinding") or (
                    kind == "RoleBinding" and item.get("roleRef", {}).get("kind") == "ClusterRole"):
                self.flag("forgejo-export-cluster-rbac", item, profile)
            if kind in ("Role", "ClusterRole") and any(
                    "*" in rule.get(field, []) for rule in item.get("rules", [])
                    for field in ("apiGroups", "resources", "verbs", "resourceNames", "nonResourceURLs")):
                self.flag("forgejo-export-rbac-wildcard", item, profile)

    def workload(self, document: dict, profile: str, digest: str | None, uid: int) -> None:
        pod = pod_spec(document)
        flagged = set()

        def flag(rule: str) -> None:
            if rule not in flagged:
                flagged.add(rule)
                self.flag(rule, document, profile)

        if any(pod.get(key) for key in ("hostNetwork", "hostPID", "hostIPC")):
            flag("forgejo-export-host-namespace")
        if any("hostPath" in volume for volume in pod.get("volumes", [])):
            flag("forgejo-export-hostpath")
        context = pod.get("securityContext", {})
        for container in [*pod.get("initContainers", []), *pod.get("containers", []),
                          *pod.get("ephemeralContainers", [])]:
            own = container.get("securityContext", {})
            if (own.get("runAsNonRoot", context.get("runAsNonRoot")) is not True
                    or own.get("runAsUser", context.get("runAsUser")) != uid):
                flag("forgejo-export-root-user")
            seccomp = own.get("seccompProfile", context.get("seccompProfile", {}))
            if seccomp.get("type") != "RuntimeDefault":
                flag("forgejo-export-seccomp")
            if own.get("privileged", False):
                flag("forgejo-export-privileged")
            if own.get("allowPrivilegeEscalation") is not False:
                flag("forgejo-export-privilege-escalation")
            if own.get("readOnlyRootFilesystem") is not True:
                flag("forgejo-export-writable-root")
            capabilities = own.get("capabilities", {})
            if "ALL" not in capabilities.get("drop", []) or capabilities.get("add"):
                flag("forgejo-export-capabilities")
            if not container.get("resources", {}).get("limits", {}).get("memory"):
                flag("forgejo-export-resource-limits")
            if digest and not container.get("image", "").endswith("@" + digest):
                flag("forgejo-export-unpinned-image")

    def sources(self) -> None:
        super().sources()
        for path in sorted(self.chart.rglob("*.sh")):
            self.check(f"source/syntax/{path.relative_to(self.chart)}",
                       lambda path=path: self.run(["bash", "-n", str(path)]))
        self.check("source/python-syntax", self.python_syntax)

    def python_syntax(self) -> None:
        # Parse only; chart code is never imported or executed by the audit.
        files = sorted(path for path in (self.chart / "files").rglob("*.py") if "__pycache__" not in path.parts)
        if not files:
            raise CheckError("No chart Python sources found")
        for path in files:
            try:
                ast.parse(path.read_text(), str(path))
            except SyntaxError as error:
                raise CheckError(f"Python syntax error in {path.relative_to(self.chart)}") from error
