import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import jsonschema
import yaml

import audit
from audit import HERE, ROOT, CheckError, source_hash
from exporter import ExporterAudit
import exporter_runtime


CHART = ROOT / "charts/loris-nfs-export"


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.audit = ExporterAudit(self.output, self.output, "1.33.0", "remote", CHART)

    def render(self, *values):
        command = ["helm", "template", "audit", str(CHART), "--namespace", "loris-audit", *values]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return [item for item in yaml.safe_load_all(result.stdout) if item]

    def test_real_profiles_and_missing_group_validation(self):
        scanned = []
        with patch.object(self.audit, "validate_profile",
                          side_effect=lambda name, documents, dormant=False:
                          scanned.append((name, len(documents), dormant))):
            self.audit.profiles()
        self.assertFalse(any(check["status"] == "error" for check in self.audit.checks))
        self.assertEqual(scanned, [
            ("disabled", 0, True), ("path-only", 0, True), ("clients-only", 0, True),
            ("enabled", 1, False), ("custom-port", 1, False),
        ])
        self.assertEqual(self.audit.checks[-1]["name"], "profile/negative-missing-group")
        self.assertEqual(self.audit.checks[-1]["status"], "passed")

    def test_invariants_reject_writable_mount_and_host_network(self):
        documents = self.render("-f", str(HERE / "profiles/exporter-enabled.yaml"))
        self.audit.invariants(documents, 20490)
        writable = copy.deepcopy(documents)
        pod = writable[0]["spec"]["template"]["spec"]
        next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "site-data")["readOnly"] = False
        with self.assertRaisesRegex(CheckError, "read-only"):
            self.audit.invariants(writable, 20490)
        network = copy.deepcopy(documents)
        network[0]["spec"]["template"]["spec"]["hostNetwork"] = True
        with self.assertRaisesRegex(CheckError, "host networking"):
            self.audit.invariants(network, 20490)

    def test_report_identifies_exporter_and_its_own_source(self):
        self.audit.report()
        data = json.loads((self.output / "report.json").read_text())
        self.assertEqual(data["chart"], "loris-nfs-export")
        self.assertEqual(data["source_sha256"], source_hash(CHART))
        self.assertNotEqual(data["source_sha256"], source_hash())
        jsonschema.validate(data, json.loads((HERE / "report.schema.json").read_text()))
        self.assertIn("# loris-nfs-export security audit", (self.output / "report.md").read_text())
        self.assertTrue(any("sec=sys" in text for text in data["limitations"]))

    def test_recursive_shell_scan_includes_entrypoint_and_semgrep_is_not_applicable(self):
        scanned = []

        def run(command, **kwargs):
            if command[0] == "shellcheck":
                scanned.extend(command[2:])
                return "[]"
            if command[0] in ("bash", "sh"):
                return ""
            return '{"Results":[]}'

        with patch.object(self.audit, "scan_config"), patch.object(self.audit, "workflows"), \
             patch.object(self.audit, "run", side_effect=run):
            self.audit.sources()
        self.assertIn(str(CHART / "image/entrypoint.sh"), scanned)
        self.assertIn(str(CHART / "prepare-source.sh"), scanned)
        check = next(item for item in self.audit.checks if item["name"] == "source/semgrep")
        self.assertEqual(check, {"name": "source/semgrep", "required": False, "status": "skipped"})

    def test_shell_syntax_failure_names_the_affected_file(self):
        def run(command, **kwargs):
            if command[0] == "bash":
                raise CheckError("bash exited 2; output withheld for safety")
            return "[]" if command[0] == "shellcheck" else '{"Results":[]}'

        with patch.object(self.audit, "scan_config"), patch.object(self.audit, "workflows"), \
             patch.object(self.audit, "run", side_effect=run):
            self.audit.sources()
        syntax = {item["name"]: item["status"] for item in self.audit.checks if "/syntax/" in item["name"]}
        self.assertEqual(syntax["source/syntax/image/entrypoint.sh"], "passed")
        self.assertEqual(syntax["source/syntax/prepare-source.sh"], "error")
        self.assertEqual(self.audit.report(), 2)

    def test_entrypoint_contract_finding_is_explicit_and_unverified(self):
        self.audit.entrypoint_contract()
        issue = self.audit.findings[-1]
        self.assertEqual(issue["rule"], "exporter-plaintext-sys-auth")
        self.assertEqual(issue["verification"], "unverified")
        lines = (CHART / "image/entrypoint.sh").read_text().splitlines()
        self.assertIn("SecType = sys;", lines[issue["line"] - 1])

    def test_cli_selects_only_the_exporter_image(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        argv = ["audit.py", "--chart", "loris-nfs-export", "--output", str(self.output)]
        with patch("sys.argv", argv), patch.object(ExporterAudit, "versions"), \
             patch.object(ExporterAudit, "profiles"), patch.object(ExporterAudit, "sources"), \
             patch.object(ExporterAudit, "image") as image, \
             patch.object(ExporterAudit, "run", return_value=json.dumps({
                 "VulnerabilityDB": {"NextUpdate": future},
             })):
            self.assertEqual(audit.main(), 0)
        image.assert_called_once_with("exporter", "docker.io/scilus/loris-nfs-export:0.3.0", "linux/amd64")
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(report["chart"], "loris-nfs-export")
        self.assertFalse(any(check["name"] == "image/database" for check in report["checks"]))

    def test_cli_rejects_database_override_for_exporter(self):
        with patch("sys.argv", ["audit.py", "--chart", "loris-nfs-export", "--database-image", "wrong:1"]):
            with self.assertRaises(SystemExit) as error:
                audit.main()
        self.assertEqual(error.exception.code, 2)

    def test_missing_manifest_has_safe_actionable_error(self):
        response = subprocess.CompletedProcess(["trivy"], 1, "", "MANIFEST_UNKNOWN with private details")
        with patch("audit.subprocess.run", return_value=response):
            with self.assertRaisesRegex(CheckError, "manifest/tag unavailable") as error:
                audit.execute(["trivy", "image", "example:missing"])
        self.assertNotIn("private details", str(error.exception))

    def test_runtime_cli_uses_exporter_validator_not_portal_validator(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        argv = ["audit.py", "--chart", "loris-nfs-export", "--runtime", "--output", str(self.output)]
        with patch("sys.argv", argv), patch.object(ExporterAudit, "versions"), \
             patch.object(ExporterAudit, "profiles"), patch.object(ExporterAudit, "sources"), \
             patch.object(ExporterAudit, "image"), \
             patch.object(ExporterAudit, "run", return_value=json.dumps({
                 "VulnerabilityDB": {"NextUpdate": future},
             })), patch("exporter_runtime.validate") as validate, patch("runtime.validate") as portal:
            self.assertEqual(audit.main(), 0)
        validate.assert_called_once()
        portal.assert_not_called()

    def test_runtime_blocks_low_disk_before_touching_docker(self):
        with patch("exporter_runtime.shutil.disk_usage") as disk, patch.object(self.audit, "run") as run:
            disk.return_value.free = 0
            with self.assertRaisesRegex(CheckError, "no cluster was created"):
                exporter_runtime.validate(self.audit)
        run.assert_not_called()

    def test_entrypoint_rejects_invalid_inputs_before_starting_ganesha(self):
        base = {"NFS_ALLOWED_CLIENTS": "192.0.2.10/32", "NFS_PORT": "20490",
                "EXPORT_UID": "10001", "EXPORT_GID": "2000", "PATH": "/usr/bin:/bin"}
        for override, message in (
            ({"NFS_ALLOWED_CLIENTS": ""}, "approved client CIDRs"),
            ({"NFS_PORT": "0"}, "1024 to 65535"),
            ({"NFS_PORT": "65536"}, "1024 to 65535"),
            ({"NFS_PORT": "not-a-port"}, "1024 to 65535"),
            ({"EXPORT_UID": "0"}, "non-zero integers"),
            ({"EXPORT_GID": ""}, "non-zero integers"),
            ({"EXPORT_GID": "invalid"}, "non-zero integers"),
        ):
            with self.subTest(override=override):
                result = subprocess.run(
                    ["sh", str(CHART / "image/entrypoint.sh")],
                    env={**base, **override}, capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)


class ExporterRuntimeTests(unittest.TestCase):
    def simulate(self, directory, fail=None):
        path = Path(directory)
        runner = ExporterAudit(path, path, "1.33.0", "remote", CHART)
        runner.images = [{"name": "exporter", "identity": "example/exporter@sha256:" + "a" * 64}]
        commands = []
        cluster = ""
        original_read = Path.read_text

        def read(filename, *args, **kwargs):
            if str(filename) == "/proc/meminfo":
                return "MemAvailable: 8000000 kB\n"
            return original_read(filename, *args, **kwargs)

        def run(command, **kwargs):
            nonlocal cluster
            commands.append(command)
            if command[:2] == ["kind", "version"]:
                return "kind v0.33.0"
            if command[:3] == ["docker", "context", "inspect"]:
                return '[{"Endpoints":{"docker":{"Host":"unix:///var/run/docker.sock"}}}]'
            if command[:3] == ["kind", "create", "cluster"]:
                cluster = command[command.index("--name") + 1]
                Path(command[command.index("--kubeconfig") + 1]).write_text("synthetic")
            if command[:3] == ["kind", "get", "nodes"]:
                return "\n".join(cluster + suffix for suffix in ("-control-plane", "-worker", "-worker2"))
            if command[:2] == ["docker", "inspect"]:
                return "someone-elses-cluster" if fail == "ownership" else cluster
            if command[0] == "kubectl" and "get" in command and "nodes" in command:
                return json.dumps({"items": [
                    {"metadata": {"name": cluster + suffix, "labels": {
                        "kubernetes.io/hostname": cluster + suffix}},
                     "status": {"addresses": [{"type": "InternalIP", "address": f"172.18.0.{index}"}]}}
                    for index, suffix in enumerate(("-control-plane", "-worker", "-worker2"), 2)
                ]})
            if command[:2] == ["helm", "template"]:
                return subprocess.run(command, capture_output=True, text=True, check=True).stdout
            if command[0] == "kubectl" and "apply" in command:
                deployment = yaml.safe_load(Path(command[command.index("-f") + 1]).read_text())
                pod = deployment["spec"]["template"]["spec"]
                self.assertEqual(pod["containers"][0]["image"], runner.images[0]["identity"])
                self.assertTrue(pod["containers"][0]["volumeMounts"][0]["readOnly"])
                self.assertEqual(pod["nodeSelector"]["kubernetes.io/hostname"], cluster + "-control-plane")
            if command[:2] == ["docker", "exec"]:
                if fail == "mount" and "mount" in command:
                    raise CheckError("Synthetic mount failure")
                if command[3:5] == ["sh", "-ec"]:
                    subprocess.run(["sh", "-n", "-c", command[5]], check=True)
            return ""

        with patch("exporter_runtime.shutil.disk_usage") as disk, \
             patch.object(Path, "read_text", read), \
             patch.dict("os.environ", {"DOCKER_HOST": ""}), \
             patch.object(runner, "run", side_effect=run):
            disk.return_value.free = 20 * 1024 ** 3
            if fail:
                with self.assertRaises(CheckError):
                    exporter_runtime.validate(runner)
            else:
                exporter_runtime.validate(runner)
        return commands, cluster

    def test_owned_nodes_context_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            commands, cluster = self.simulate(directory)
        self.assertEqual(commands[-1][:3], ["kind", "delete", "cluster"])
        self.assertEqual(commands[-1][commands[-1].index("--name") + 1], cluster)
        for command in commands:
            if command[0] == "kubectl":
                self.assertEqual(command[command.index("--context") + 1], "kind-" + cluster)
                self.assertIn("--kubeconfig", command)
            if command[:2] == ["docker", "exec"]:
                self.assertIn(command[2], {cluster + suffix for suffix in ("-control-plane", "-worker", "-worker2")})
        commands_text = json.dumps(commands)
        self.assertIn("read-only file system", commands_text)
        self.assertIn("access denied", commands_text)
        self.assertIn("permission denied", commands_text)
        self.assertIn("rw,nfsvers=4.1", commands_text)

    def test_cleans_up_after_mount_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            commands, _ = self.simulate(directory, fail="mount")
        self.assertEqual(commands[-1][:3], ["kind", "delete", "cluster"])

    def test_ownership_mismatch_prevents_all_node_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            commands, _ = self.simulate(directory, fail="ownership")
        self.assertFalse(any(command[:2] == ["docker", "exec"] for command in commands))
        self.assertEqual(commands[-1][:3], ["kind", "delete", "cluster"])


if __name__ == "__main__":
    unittest.main()
