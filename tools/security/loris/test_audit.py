import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import runpy
import tempfile
import unittest
from unittest.mock import patch

import jsonschema
import yaml

from audit import (
    Audit, CHART, CheckError, HERE, execute, finding, generated_worker,
    parse_json, safe_identifier, severity, trivy_findings,
)
import runtime


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.audit = Audit(self.output, self.output, "1.33.0", "remote")

    def test_exit_codes_and_schema(self):
        for level, expected in (("LOW", 0), ("MEDIUM", 0), ("HIGH", 1), ("CRITICAL", 1)):
            with self.subTest(level=level):
                self.audit.findings = [finding("test-rule", level, "test.yaml", "configuration")]
                self.assertEqual(self.audit.report(), expected)
                report = json.loads((self.output / "report.json").read_text())
                jsonschema.validate(report, json.loads((HERE / "report.schema.json").read_text()))
        self.audit.checks = [{"name": "broken", "required": True, "status": "error", "error": "Unavailable"}]
        self.assertEqual(self.audit.report(), 2)

    def test_redaction_and_secret_evidence(self):
        secret = "DO-NOT-PUBLISH-this-secret"
        raw = {"Results": [{
            "Target": "config.yaml",
            "Secrets": [{"RuleID": "example-secret", "Severity": "HIGH", "StartLine": 8,
                         "Match": secret, "Code": {"Lines": [{"Content": secret}]}}],
            "Misconfigurations": [{"ID": "AVD-TEST", "Severity": "LOW", "Message": secret,
                                    "CauseMetadata": {"Resource": "Deployment.loris", "StartLine": 2}}],
        }]}
        self.audit.findings = trivy_findings(raw, "configuration")
        self.audit.report()
        for path in (self.output / "report.json", self.output / "report.md"):
            self.assertNotIn(secret, path.read_text())
        self.assertIn("example-secret", (self.output / "report.md").read_text())

    def test_stable_sorted_deduplicated_findings(self):
        high = finding("a", "HIGH", "x.yaml", "source")
        low = finding("z", "LOW", "y.yaml", "source")
        self.audit.findings = [low, high, high]
        self.audit.report()
        first = json.loads((self.output / "report.json").read_text())["findings"]
        self.audit.findings = [high, low]
        self.audit.report()
        second = json.loads((self.output / "report.json").read_text())["findings"]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        self.assertEqual(first[0]["severity"], "HIGH")

    def test_check_errors_are_explicit(self):
        self.audit.check("broken", lambda: execute(["this-tool-does-not-exist-loris"]))
        self.assertEqual(self.audit.checks[0]["status"], "error")
        self.assertEqual(self.audit.report(), 2)

    def test_scanner_failure_does_not_leak_output(self):
        response = subprocess.CompletedProcess(["scanner"], 2, "sensitive stdout", "sensitive stderr")
        with patch("audit.subprocess.run", return_value=response):
            with self.assertRaisesRegex(CheckError, "output withheld") as error:
                execute(["scanner"])
        self.assertNotIn("sensitive", str(error.exception))

    def test_timeout(self):
        with patch("audit.subprocess.run", side_effect=subprocess.TimeoutExpired("scanner", 1)):
            with self.assertRaisesRegex(CheckError, "deadline"):
                execute(["scanner"], timeout=1)

    def test_negative_fixture_requires_exact_expected_failure(self):
        for response, passes in (
            (subprocess.CompletedProcess(["helm"], 1, "", "bidsImport requires persistent LORIS data shared with the portal"), True),
            (subprocess.CompletedProcess(["helm"], 1, "", "Unrelated invalid YAML"), False),
            (subprocess.CompletedProcess(["helm"], 0, "", ""), False),
        ):
            with patch("audit.subprocess.run", return_value=response):
                if passes:
                    execute(["helm"], expected_error="bidsImport requires persistent LORIS data shared with the portal")
                else:
                    with self.assertRaises(CheckError):
                        execute(["helm"], expected_error="bidsImport requires persistent LORIS data shared with the portal")

    def test_invalid_json(self):
        with self.assertRaises(CheckError):
            parse_json("not json")

    def test_missing_image_is_error(self):
        with patch.object(self.audit, "run", side_effect=CheckError("Registry denied access")):
            self.audit.check("image/application", lambda: self.audit.image("application", "missing:1", "linux/amd64"))
        self.assertEqual(self.audit.checks[0]["status"], "error")

    def test_image_must_have_immutable_identity(self):
        with patch.object(self.audit, "run", return_value=json.dumps({"Metadata": {}, "Results": []})):
            with self.assertRaisesRegex(CheckError, "immutable"):
                self.audit.image("application", "example:1", "linux/amd64")

    def test_sbom_and_digest_scanning(self):
        digest = "example/image@sha256:" + "a" * 64
        image = {"Metadata": {"RepoDigests": [digest]},
                 "Results": [{"Target": "os", "Packages": [{"Name": "example"}]}]}
        with patch.object(self.audit, "run", side_effect=[json.dumps(image), json.dumps(image),
                                                         '{"bomFormat":"CycloneDX"}']) as run:
            self.audit.image("application", "example/image:latest", "linux/amd64")
        self.assertEqual(run.call_args_list[1].args[0][-1], digest)
        self.assertEqual(self.audit.images[0]["identity"], digest)
        self.assertTrue((self.output / "application.cdx.json").exists())

    def test_empty_enabled_render_is_error(self):
        with patch.object(self.audit, "run", side_effect=["lint passed", ""]):
            with self.assertRaisesRegex(CheckError, "no resources"):
                self.audit.profile("development")

    def test_invalid_schema_is_error(self):
        with patch.object(self.audit, "run", side_effect=[
            "lint passed", "apiVersion: v1\nkind: Service\nmetadata:\n  name: example\n",
            '{"summary":{"valid":0,"invalid":1,"errors":0}}',
        ]):
            with self.assertRaisesRegex(CheckError, "schema"):
                self.audit.profile("development")

    def test_semgrep_error_is_not_clean(self):
        with patch.object(self.audit, "scan_config"), patch.object(self.audit, "run") as run:
            run.side_effect = [
                '{"Results":[]}', '{"errors":[{"message":"unparseable"}],"results":[]}', "[]", "[]",
            ]
            self.audit.sources()
        semgrep = next(check for check in self.audit.checks if check["name"] == "source/semgrep")
        self.assertEqual(semgrep["status"], "error")

    def test_normalization_and_identifier_guard(self):
        self.assertEqual(severity("warning"), "MEDIUM")
        self.assertEqual(severity("error"), "HIGH")
        self.assertEqual(severity("new-level"), "UNKNOWN")
        self.assertEqual(safe_identifier("unsafe\n| markdown"), "redacted-identifier")

    def test_source_changes_make_report_incomplete(self):
        with patch("audit.source_hash", return_value="b" * 64):
            self.assertEqual(self.audit.report(), 2)
        self.assertEqual(self.audit.checks[-1]["name"], "source/snapshot")


class ChartTests(unittest.TestCase):
    def test_generated_worker_privileges_and_schema(self):
        result = subprocess.run(
            ["helm", "template", "audit", str(CHART), "--namespace", "loris-audit",
             "-f", str(HERE / "profiles/imports.yaml")], capture_output=True, text=True, check=True,
        )
        documents = [item for item in yaml.safe_load_all(result.stdout) if item]
        original = copy.deepcopy(documents)
        worker = generated_worker(documents)
        self.assertEqual(documents, original)
        pod = worker["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("initContainers", pod)
        self.assertNotIn("serviceAccountName", pod)
        self.assertEqual(pod["containers"][0]["args"][0], "worker")
        self.assertNotIn("MARIADB_ROOT_PASSWORD", json.dumps(pod))
        self.assertTrue(next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "site-data")["readOnly"])

    def test_runtime_checks_capacity_before_creating_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Audit(Path(directory), Path(directory), "1.33.0", "remote")
            with patch("runtime.shutil.disk_usage") as usage, patch.object(audit, "run") as run:
                usage.return_value.free = 0
                with self.assertRaisesRegex(CheckError, "no cluster was created"):
                    runtime.validate(audit)
                run.assert_not_called()

    def test_cli_and_runtime_share_error_contract(self):
        cli = runpy.run_path(str(HERE / "audit.py"), run_name="audit_cli_test")
        self.assertIs(cli["CheckError"], runtime.CheckError)

    def test_runtime_probe_does_not_mount_api_token(self):
        probe = runtime.copy_probe("example@sha256:" + "a" * 64, "probe", "loris-audit")
        self.assertFalse(probe["spec"]["automountServiceAccountToken"])
        self.assertEqual(probe["metadata"]["namespace"], "loris-audit")

    def test_root_environment_copy_does_not_change_worker(self):
        environment = [
            {"name": "LORIS_DB_USER", "value": "loris"},
            {"name": "LORIS_DB_PASSWORD", "valueFrom": {"secretKeyRef": {
                "name": "loris-credentials", "key": "LORIS_DB_PASSWORD"}}},
        ]
        root = runtime.copy_env(environment, root=True)
        self.assertEqual(root[0]["value"], "root")
        self.assertEqual(environment[0]["value"], "loris")
        self.assertEqual(root[1]["valueFrom"]["secretKeyRef"]["key"], "MARIADB_ROOT_PASSWORD")

    def simulate_runtime(self, directory: str, fail_install: bool = False) -> list[list[str]]:
        audit = Audit(Path(directory), Path(directory), "1.33.0", "remote")
        audit.images = [
            {"name": "application", "identity": "example/app@sha256:" + "a" * 64},
            {"name": "database", "identity": "example/db@sha256:" + "b" * 64},
        ]
        commands = []
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                return "MemAvailable: 8000000 kB\n"
            return original_read(path, *args, **kwargs)

        def run(command, **kwargs):
            commands.append(command)
            if command[:2] == ["kind", "version"]:
                return "kind v0.33.0"
            if command[:3] == ["kind", "create", "cluster"]:
                Path(command[command.index("--kubeconfig") + 1]).write_text("synthetic kubeconfig")
            if command[:2] == ["helm", "install"] and fail_install:
                raise CheckError("Synthetic CNI installation failure")
            if command[:2] == ["helm", "template"]:
                return subprocess.run(command, capture_output=True, text=True, check=True).stdout
            if "can-i" in command:
                return "yes" if command[command.index("can-i") + 1] == "create" else "no"
            if "get" in command and "pods" in command:
                return '{"items":[{"spec":{"automountServiceAccountToken":false,"volumes":[]}}]}'
            if "-f" in command and command[0] == "kubectl":
                documents = list(yaml.safe_load_all(Path(command[command.index("-f") + 1]).read_text()))
                for document in documents:
                    spec = document.get("spec", {})
                    pod = spec.get("template", {}).get("spec", spec)
                    for container in pod.get("containers", []):
                        invocation = container.get("command", [])
                        if invocation[:2] == ["python3", "-c"]:
                            compile(invocation[2], "<runtime-synthetic-code>", "exec")
            return ""

        with patch("runtime.shutil.disk_usage") as disk, \
             patch.object(Path, "read_text", read), \
             patch("runtime.urllib.request.urlopen", return_value=io.BytesIO(b"synthetic chart")), \
             patch("runtime.CILIUM_SHA256", hashlib.sha256(b"synthetic chart").hexdigest()), \
             patch.object(audit, "run", side_effect=run):
            disk.return_value.free = 20 * 1024 ** 3
            if fail_install:
                with self.assertRaisesRegex(CheckError, "installation failure"):
                    runtime.validate(audit)
            else:
                runtime.validate(audit)
        return commands

    def test_runtime_orchestration_uses_owned_context_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = self.simulate_runtime(directory)
        self.assertEqual(commands[-1][:3], ["kind", "delete", "cluster"])
        name = commands[-1][commands[-1].index("--name") + 1]
        self.assertTrue(name.startswith("loris-audit-"))
        for command in commands:
            if command[0] == "kubectl":
                self.assertEqual(command[command.index("--context") + 1], "kind-" + name)
                self.assertIn("--kubeconfig", command)

    def test_runtime_cleans_up_after_installation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = self.simulate_runtime(directory, fail_install=True)
        self.assertEqual(commands[-1][:3], ["kind", "delete", "cluster"])


if __name__ == "__main__":
    unittest.main()
