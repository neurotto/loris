"""Local-only LORIS audit orchestration. Scanner failures are not clean results."""

import argparse
import ast
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml

from common import CheckError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CHART = ROOT / "charts/loris"
SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"}
LIMITATIONS = [
    "Automated checks cannot certify absence of vulnerabilities.",
    "Synthetic profiles do not represent deployment-specific ingress, CNI, admission, or storage policy.",
    "Local Semgrep rules are a bounded heuristic set, not a full manual application audit.",
    "Image scanners inventory shipped dependencies; build-only dependencies and upstream source need manual review.",
    "Dataset path/SQL/subprocess trust boundaries and image patches still require manual exploitability review.",
    "No exceptions are suppressed; findings are unverified until triaged.",
]
SCHEMA_REVISION = "f7d855e49c34b039c9ef2196444910275e31e09e"


def execute(command: list[str], timeout: int = 1200, allowed: tuple[int, ...] = (0,),
            expected_error: str | None = None) -> str:
    environment = dict(os.environ, SEMGREP_SEND_METRICS="off", SEMGREP_ENABLE_VERSION_CHECK="0",
                       DO_NOT_TRACK="1")
    try:
        result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                                text=True, timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise CheckError(f"Required executable unavailable: {Path(command[0]).name}") from error
    except subprocess.TimeoutExpired as error:
        raise CheckError(f"{Path(command[0]).name} exceeded its {timeout}s deadline") from error
    if expected_error is not None:
        if result.returncode != 1 or expected_error not in result.stderr:
            raise CheckError("The exact expected validation failure was not observed")
        return result.stdout
    if result.returncode not in allowed:
        # Tool output can contain source snippets, credentials or sensitive paths.
        if Path(command[0]).name == "trivy" and "MANIFEST_UNKNOWN" in result.stderr:
            raise CheckError("Image registry manifest/tag unavailable (MANIFEST_UNKNOWN); no dependency evidence produced")
        raise CheckError(f"{Path(command[0]).name} exited {result.returncode}; output withheld for safety")
    return result.stdout


def parse_json(text: str) -> dict | list:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise CheckError("Scanner returned invalid JSON") from error


def severity(value: str) -> str:
    normalized = {"WARNING": "MEDIUM", "ERROR": "HIGH", "INFO": "LOW"}.get(value.upper(), value.upper())
    return normalized if normalized in SEVERITIES else "UNKNOWN"


def safe_identifier(value: object) -> str:
    # Scanner rule identifiers only: never preserve messages or matched source.
    text = str(value)
    return text if re.fullmatch(r"[A-Za-z0-9_.:/@+-]{1,240}", text) else "redacted-identifier"


def location(value: object) -> str:
    path = Path(str(value))
    if path.is_absolute():
        try:
            path = path.relative_to(ROOT)
        except ValueError:
            return "<external>"
    if ".." in path.parts:
        return "<external>"
    return str(path)


def finding(rule: str, level: str, target: str, category: str,
            line: int = 0, remediation: str = "Review the referenced scanner rule and validate applicability.") -> dict:
    identity = f"{rule}|{target}|{line}|{category}"
    return {
        "id": hashlib.sha256(identity.encode()).hexdigest()[:16],
        "rule": safe_identifier(rule), "severity": severity(level), "target": location(target),
        "line": line, "category": category, "verification": "unverified",
        "remediation": remediation,
    }


def source_hash(chart: Path = CHART) -> str:
    digest = hashlib.sha256()
    paths = [*chart.rglob("*"), *(ROOT / ".github/workflows").glob("*"), *HERE.rglob("*")]
    for path in sorted(set(paths)):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(str(path.relative_to(ROOT)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def generated_worker(documents: list[dict]) -> dict:
    """Run only the chart's dispatch function against a fake namespaced API."""
    tree = ast.parse((CHART / "files/bids_runtime.py").read_text())
    names = {"encoded", "owner", "dispatch"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in nodes} != names:
        raise CheckError("Worker dispatch functions changed; update the audit harness")

    class FakeApi:
        def __init__(self) -> None:
            self.objects: dict[tuple[str, str], dict] = {}

        def get(self, kind: str, name: str) -> dict | None:
            return self.objects.get((kind, name))

        def request(self, kind: str, name: str = "", method: str = "GET", body: dict | None = None) -> dict:
            if body is None:
                raise CheckError("Worker harness received an empty request")
            if method == "POST":
                item = copy.deepcopy(body)
                item["metadata"]["uid"] = "synthetic-uid"
                self.objects[kind, item["metadata"]["name"]] = item
                return item
            if method == "PATCH":
                item = self.objects[kind, name]
                item["metadata"].update(body["metadata"])
                return item
            raise CheckError("Unexpected API operation in worker harness")

    namespace = {"copy": copy, "json": json, "ApiError": CheckError}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<chart-worker-harness>", "exec"), namespace)
    cron = copy.deepcopy(next(item for item in documents if item["kind"] == "CronJob"))
    cron["metadata"]["uid"] = "synthetic-cron"
    profile = next(item for item in documents if item["kind"] == "ConfigMap")
    api = FakeApi()
    namespace["dispatch"](api, cron, profile, {"job_name": "audit-worker", "id": "a" * 64, "attempt": 1})
    job = api.objects["jobs", "audit-worker"]
    pod = job["spec"]["template"]["spec"]
    if pod.get("initContainers") or pod.get("serviceAccountName") or pod.get("automountServiceAccountToken") is not False:
        raise CheckError("Generated worker retained discovery privileges or API token")
    keys = json.dumps(pod["containers"])
    if "MARIADB_ROOT_PASSWORD" in keys or '"value": "root"' in keys:
        raise CheckError("Generated worker retained root database credentials")
    return job


def trivy_findings(data: dict, category: str, base: Path | None = None) -> list[dict]:
    output = []
    for result in data.get("Results", []):
        raw_target = str(result.get("Target", "image"))
        if base is not None:
            path = Path(raw_target)
            if path.is_absolute() and path.is_relative_to(base):
                raw_target = str(path.relative_to(base))
        target = location(raw_target)
        for issue in result.get("Misconfigurations", []):
            if issue.get("Status", "FAIL") == "PASS":
                continue
            cause = issue.get("CauseMetadata", {})
            item = finding(issue.get("ID", "unknown"), issue.get("Severity", "UNKNOWN"),
                           target, category, cause.get("StartLine", 0))
            item["resource"] = safe_identifier(cause.get("Resource", "unknown"))
            output.append(item)
        for issue in result.get("Secrets", []):
            output.append(finding(issue.get("RuleID", "secret"), issue.get("Severity", "HIGH"),
                                  target, "secret", issue.get("StartLine", 0),
                                  "Investigate locally without publishing the match; rotate exposed credentials."))
        for issue in result.get("Vulnerabilities", []):
            item = finding(issue.get("VulnerabilityID", "unknown"), issue.get("Severity", "UNKNOWN"),
                           target, "dependency",
                           remediation="Review the advisory and upgrade the affected package/image; retest compatibility.")
            item.update(package=safe_identifier(issue.get("PkgName", "unknown")),
                        installed=safe_identifier(issue.get("InstalledVersion", "unknown")),
                        fixed=safe_identifier(issue.get("FixedVersion") or "not-published"))
            # A CVE may affect several packages in one image target.
            item["id"] = hashlib.sha256((item["id"] + item["package"] + item["installed"]).encode()).hexdigest()[:16]
            output.append(item)
    return output


class Audit:
    def __init__(self, output: Path, scratch: Path, kubernetes: str, image_source: str,
                 chart: Path = CHART) -> None:
        self.chart = chart
        self.output = output
        self.scratch = scratch
        self.kubernetes = kubernetes
        self.image_source = image_source
        self.checks: list[dict] = []
        self.findings: list[dict] = []
        self.images: list[dict] = []
        self.tools: dict[str, str] = {}
        self.commands: list[list[str]] = []
        self.database: dict = {}
        self.source = source_hash(self.chart)
        self.limitations = list(LIMITATIONS)

    def run(self, command: list[str], **kwargs) -> str:
        self.commands.append([part.replace(str(ROOT), "<repo>").replace(str(self.scratch), "<scratch>")
                              for part in command])
        return execute(command, **kwargs)

    def check(self, name: str, action) -> None:
        before = len(self.findings)
        try:
            action()
            self.checks.append({"name": name, "required": True,
                                "status": "findings" if len(self.findings) > before else "passed"})
        except (CheckError, OSError, ValueError, KeyError, StopIteration, TypeError,
                AttributeError, IndexError, yaml.YAMLError) as error:
            message = str(error) if isinstance(error, CheckError) else f"{type(error).__name__}; inspect inputs locally"
            self.checks.append({"name": name, "required": True, "status": "error", "error": message})
        print(f"{name}: {self.checks[-1]['status']}", flush=True)

    def versions(self) -> None:
        pins = json.loads((HERE / "tools.json").read_text())
        pins["semgrep"] = {"version": "1.182.1"}
        for tool, spec in pins.items():
            def verify(tool=tool, spec=spec):
                flag = "-v" if tool == "kubeconform" else "--version"
                command = ["helm", "version", "--short"] if tool == "helm" else [tool, flag]
                text = self.run(command, timeout=60)
                if spec["version"] not in text:
                    raise CheckError(f"{tool} does not match pinned version {spec['version']}")
                self.tools[tool] = spec["version"]
            self.check(f"tool/{tool}", verify)

    def scan_config(self, path: Path) -> None:
        data = parse_json(self.run(["trivy", "config", "--format", "json", "--exit-code", "0",
                                    "--skip-check-update", "--cache-dir",
                                    str(self.scratch / "embedded-policy"), str(path)]))
        issues = trivy_findings(data, "configuration", path)
        for issue in issues:
            issue["profile"] = path.name if path != self.chart else "source"
            issue["id"] = hashlib.sha256((issue["id"] + issue["profile"]).encode()).hexdigest()[:16]
        self.findings.extend(issues)

    def profiles(self) -> None:
        for name in ("disabled", "development", "persistent-tls", "imports"):
            self.check(f"profile/{name}", lambda name=name: self.profile(name))
        self.check("profile/negative-import-storage", self.negative)

    def profile(self, name: str) -> None:
        values = [] if name == "disabled" else ["-f", str(HERE / "profiles" / f"{name}.yaml")]
        documents = self.render(values)
        self.validate_profile(name, documents, dormant=name == "disabled")

    def render(self, values: list[str]) -> list[dict]:
        self.run(["helm", "lint", str(self.chart), "--strict", *values])
        text = self.run(["helm", "template", "audit", str(self.chart), "--namespace", "loris-audit", *values])
        documents = [item for item in yaml.safe_load_all(text) if item]
        return documents

    def validate_profile(self, name: str, documents: list[dict], dormant: bool = False) -> None:
        if dormant:
            if documents:
                raise CheckError("Disabled profile unexpectedly renders resources")
            return
        if not documents:
            raise CheckError("Enabled profile rendered no resources")
        folder = self.scratch / name
        folder.mkdir()
        if name == "imports":
            documents.append(generated_worker(documents))
        manifest = folder / "resources.yaml"
        manifest.write_text(yaml.safe_dump_all(documents))
        schema = parse_json(self.run(["kubeconform", "-strict", "-summary", "-output", "json",
                                      "-kubernetes-version", self.kubernetes,
                                      "-schema-location", "https://raw.githubusercontent.com/yannh/"
                                      f"kubernetes-json-schema/{SCHEMA_REVISION}/"
                                      "{{.NormalizedKubernetesVersion}}-standalone-strict/"
                                      "{{.ResourceKind}}{{.KindSuffix}}.json", str(manifest)]))
        summary = schema.get("summary", {})
        if (summary.get("invalid", 0) or summary.get("errors", 0) or summary.get("skipped", 0)
                or summary.get("valid") != len(documents)):
            raise CheckError("Kubernetes schema validation did not validate every resource")
        self.scan_config(folder)

    def negative(self) -> None:
        command = ["helm", "template", "audit", str(CHART), "-f", str(HERE / "profiles/imports.yaml"),
                   "--set", "loris.data.persistence.enabled=false"]
        self.run(command, expected_error="bidsImport requires persistent LORIS data shared with the portal")

    def sources(self) -> None:
        self.check("source/configuration", lambda: self.scan_config(self.chart))

        def secrets():
            data = parse_json(self.run(["trivy", "fs", "--scanners", "secret", "--format", "json",
                                        "--exit-code", "0", str(self.chart)]))
            self.findings.extend(trivy_findings(data, "secret", self.chart))
        self.check("source/secrets", secrets)

        def semgrep():
            data = parse_json(self.run(["semgrep", "scan", "--config", str(HERE / "rules.yaml"),
                                        "--metrics", "off", "--disable-version-check", "--no-git-ignore",
                                        "--json", str(self.chart)]))
            if data.get("errors"):
                raise CheckError("Semgrep reported scan/parse errors")
            scanned = data.get("paths", {}).get("scanned", [])
            expected = {path.resolve() for path in self.chart.rglob("*") if path.suffix in {".py", ".php", ".mjs"}}
            if not expected or not expected.issubset({Path(path).resolve() for path in scanned}):
                raise CheckError("Semgrep did not cover every chart Python/PHP/JavaScript source")
            for issue in data.get("results", []):
                self.findings.append(finding(issue["check_id"], issue["extra"]["severity"],
                                             issue["path"], "source", issue["start"]["line"]))
        if any(path.suffix in {".py", ".php", ".mjs"} for path in self.chart.rglob("*")):
            self.check("source/semgrep", semgrep)
        else:
            self.checks.append({"name": "source/semgrep", "required": False, "status": "skipped"})

        def shellcheck():
            files = sorted(str(path) for path in self.chart.rglob("*.sh"))
            if not files:
                raise CheckError("No shell scripts found")
            data = parse_json(self.run(["shellcheck", "--format=json", *files], allowed=(0, 1)))
            for issue in data:
                self.findings.append(finding(f"SC{issue['code']}", issue["level"], issue["file"],
                                             "source", issue["line"]))
        self.check("source/shellcheck", shellcheck)
        self.check("workflow/actionlint", self.workflows)

    def workflows(self) -> None:
        files = sorted((ROOT / ".github/workflows").glob("*.y*ml"))
        if not files:
            raise CheckError("No release/audit workflows found")
        output = self.run(["actionlint", "-shellcheck=", "-format",
                           "{{json .}}", *map(str, files)], allowed=(0, 1))
        if parse_json(output.strip() or "[]"):
            # actionlint's template JSON contains source snippets; never persist it.
            self.findings.append(finding("actionlint-validation", "MEDIUM", ".github/workflows",
                                         "workflow", remediation="Run actionlint locally and correct workflow errors."))
        for path in files:
            data = yaml.safe_load(path.read_text())
            for job in data.get("jobs", {}).values():
                for step in job.get("steps", []):
                    action = step.get("uses", "")
                    if action and not action.startswith(("./", "docker://")) and not re.search(r"@[0-9a-f]{40}$", action):
                        self.findings.append(finding("workflow-unpinned-action", "MEDIUM",
                                                     str(path), "workflow",
                                                     remediation="Pin external actions to reviewed full commit hashes."))
            trigger = data.get("on", data.get(True, {}))
            if "pull_request_target" in trigger:
                self.findings.append(finding("workflow-privileged-pr-trigger", "HIGH", str(path), "workflow",
                                             remediation="Avoid privileged execution of untrusted pull-request code."))
            if not data.get("permissions") and not all("permissions" in job for job in data.get("jobs", {}).values()):
                self.findings.append(finding("workflow-implicit-permissions", "MEDIUM", str(path), "workflow"))

    def image(self, name: str, reference: str, platform: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_./:@-]+", reference):
            raise CheckError("Image reference contains unsupported characters")
        base = ["trivy", "image", "--scanners", "vuln", "--format", "json", "--list-all-pkgs",
                "--image-src", self.image_source, "--platform", platform, "--exit-code", "0"]
        initial = parse_json(self.run([*base, reference]))
        metadata = initial.get("Metadata", {})
        digests = metadata.get("RepoDigests", [])
        identity = digests[0] if digests else metadata.get("ImageID", "")
        if not re.search(r"(?:@|^)sha256:[0-9a-f]{64}$", identity):
            raise CheckError("Scanner did not return an immutable image identity")
        # Remote repositories resolve by manifest digest. Local-only images use content IDs.
        pinned = parse_json(self.run([*base, identity]))
        pinned_meta = pinned.get("Metadata", {})
        if digests and identity not in pinned_meta.get("RepoDigests", []):
            raise CheckError("Pinned image identity was not verified")
        if not digests and metadata.get("ImageID") != pinned_meta.get("ImageID"):
            raise CheckError("Local image identity changed during scanning")
        if "Results" not in pinned:
            raise CheckError("Image scan returned no package results")
        if not any(result.get("Packages") for result in pinned["Results"]):
            raise CheckError("Image scan returned no package inventory")
        self.images.append({"name": name, "requested": reference, "identity": identity, "platform": platform})
        issues = trivy_findings(pinned, "dependency")
        for issue in issues:
            issue["image"] = name
            issue["id"] = hashlib.sha256((name + issue["id"]).encode()).hexdigest()[:16]
        self.findings.extend(issues)
        raw = self.scratch / f"{name}-image.json"
        raw.write_text(json.dumps(pinned))
        sbom = parse_json(self.run(["trivy", "convert", "--format", "cyclonedx", str(raw)]))
        (self.output / f"{name}.cdx.json").write_text(json.dumps(sbom, indent=2) + "\n")

    def report(self) -> int:
        if source_hash(self.chart) != self.source and not any(check["name"] == "source/snapshot" for check in self.checks):
            self.checks.append({"name": "source/snapshot", "required": True, "status": "error",
                                "error": "Source changed while auditing; rerun from a stable checkout"})
        unique = {item["id"]: item for item in self.findings}
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
        findings = sorted(unique.values(), key=lambda item: (order[item["severity"]], item["id"]))
        errors = any(item["required"] and item["status"] == "error" for item in self.checks)
        blocked = any(item["severity"] in {"HIGH", "CRITICAL"} for item in findings)
        code = 2 if errors else 1 if blocked else 0
        data = {
            "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "chart": self.chart.name,
            "source_sha256": self.source, "kubernetes_version": self.kubernetes,
            "kubernetes_schema_revision": SCHEMA_REVISION,
            "tools": self.tools, "vulnerability_database": self.database,
            "checks": self.checks, "findings": findings, "images": self.images,
            "commands": self.commands, "limitations": self.limitations,
            "exit_code": code,
        }
        (self.output / "report.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        rows = [
            f"# {self.chart.name} security audit", "",
            f"Outcome: **{'INCOMPLETE' if errors else 'FINDINGS' if blocked else 'PASSED AUTOMATED GATES'}**",
            f"Exit code: `{code}`. Source SHA-256: `{data['source_sha256']}`.", "",
            "Scanner findings are **unverified**, not automatically confirmed vulnerabilities.",
            "No secret matches, source snippets, or raw scanner stderr are included.", "",
            "```mermaid", "flowchart LR", " A[Source and synthetic profiles] --> B[Static checks]",
            " C[Immutable images] --> D[Dependency scans]", " B --> E[Redacted evidence]",
            " D --> E", " F[Optional isolated runtime] --> E", " E --> G[Report and exit policy]", "```", "",
            "Text equivalent: source/profile checks, image scans, and optional runtime checks feed",
            "redacted evidence into this report. Required errors fail with code 2; high/critical",
            "findings fail with code 1; other completed scans exit 0.", "",
            "## Coverage", "", "| Check | Required | Status | Detail |",
            "|---|---|---|---|",
        ]
        for check in self.checks:
            rows.append(f"| {check['name']} | {check['required']} | {check['status']} | {check.get('error', '')} |")
        rows.extend(["",         "## Findings", "", "| ID | Severity | Category | Rule | Profile/image | Target/resource | Line | Verification |",
                 "|---|---|---|---|---|---|---|---|"])
        for item in findings:
            rows.append(f"| {item['id']} | {item['severity']} | {item['category']} | {item['rule']} | "
                        f"{item.get('profile', item.get('image', '-'))} | "
                        f"{item['target']} / {item.get('resource', item.get('package', '-'))} | "
                        f"{item['line'] or '-'} | {item['verification']} |")
        if not findings:
            rows.append("| - | - | - | No findings from completed checks | - | - | - | Not a security guarantee |")
        rows.extend(["", "## Remediation and retest", ""])
        for item in findings:
            dependency = (f" `{item['package']}`: installed `{item['installed']}`, "
                          f"fixed `{item['fixed']}`.") if "package" in item else ""
            rows.append(f"- `{item['id']}`:{dependency} {item['remediation']}")
        rows.extend(["", "Fix or triage each finding, then rerun the same profiles and immutable images.",
                     "No baselines or exemptions are silently applied.", "",
                     "## Limitations and manual review", *[f"- {text}" for text in self.limitations], "",
                     "## Evidence", "", "See `report.json` for tool versions, normalized scanner evidence,",
                     "commands, image identities, database metadata, and check errors.",
                     "Image SBOMs, when generated, are `<image>.cdx.json`.", ""])
        (self.output / "report.md").write_text("\n".join(rows))
        return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", choices=("loris", "loris-nfs-export"), default="loris")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--kubernetes-version", default="1.33.0")
    parser.add_argument("--app-image")
    parser.add_argument("--database-image")
    parser.add_argument("--image-source", choices=("remote", "docker"), default="remote")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--runtime", action="store_true", help="Opt in to an owned disposable kind cluster")
    args = parser.parse_args()
    chart = ROOT / "charts" / args.chart
    if args.chart == "loris-nfs-export" and args.database_image:
        parser.error("--database-image does not apply to the exporter chart")
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.kubernetes_version):
        parser.error("--kubernetes-version must be an explicit x.y.z version")
    if not re.fullmatch(r"linux/(amd64|arm64)", args.platform):
        parser.error("--platform must be linux/amd64 or linux/arm64")
    if args.runtime and args.platform != "linux/amd64":
        parser.error("Runtime validation currently supports linux/amd64 only")
    output = (args.output or ROOT / "audit-results" / args.chart).resolve()
    if output == ROOT or output.is_relative_to(ROOT / "charts"):
        parser.error("Audit output must be outside all charts and not the repository root")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(output.iterdir()):
        parser.error("Output directory must be empty; choose a fresh path to avoid stale evidence")
    with tempfile.TemporaryDirectory(prefix="loris-audit-") as temporary:
        if args.chart == "loris-nfs-export":
            from exporter import ExporterAudit
            audit = ExporterAudit(output, Path(temporary), args.kubernetes_version, args.image_source, chart)
        else:
            audit = Audit(output, Path(temporary), args.kubernetes_version, args.image_source)
        audit.versions()
        audit.profiles()
        audit.sources()
        values = yaml.safe_load((chart / "values.yaml").read_text())
        app = args.app_image or f"{values['image']['repository']}:{values['image']['tag']}"
        if not args.app_image and values["image"].get("digest"):
            app += "@" + values["image"]["digest"]
        image_name = "exporter" if args.chart == "loris-nfs-export" else "application"
        audit.check(f"image/{image_name}", lambda: audit.image(image_name, app, args.platform))
        if args.chart == "loris":
            audit.check("image/database", lambda: audit.image(
                "database", args.database_image or values["database"]["image"], args.platform))

        def database():
            version = parse_json(audit.run(["trivy", "--version", "--format", "json"]))
            metadata = version.get("VulnerabilityDB")
            if not metadata:
                raise CheckError("Vulnerability database metadata unavailable")
            next_update = metadata.get("NextUpdate")
            if not next_update or datetime.fromisoformat(next_update.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                raise CheckError("Vulnerability database is stale or has no freshness metadata")
            audit.database = metadata
        audit.check("image/database-metadata", database)
        if args.runtime:
            if args.chart == "loris-nfs-export":
                from exporter_runtime import validate
            else:
                from runtime import validate
            audit.check("runtime/isolated-cluster", lambda: validate(audit))
        else:
            audit.checks.append({"name": "runtime/isolated-cluster", "required": False, "status": "skipped"})
        code = audit.report()
    print(f"Report: {output / 'report.md'} (exit {code})")
    return code


if __name__ == "__main__":
    sys.exit(main())
