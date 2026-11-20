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
from forgejo import DIGEST, PROFILE, ForgejoExportAudit, pod_spec


CHART = ROOT / "charts/loris-forgejo-export"


class ForgejoExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        command = ["helm", "template", "audit", str(CHART), "--namespace", "loris-audit", "-f", str(PROFILE)]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        cls.documents = [item for item in yaml.safe_load_all(result.stdout) if item]

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.audit = ForgejoExportAudit(self.output, self.output, "1.33.0", "remote", CHART)

    def find(self, documents, kind, suffix):
        return next(item for item in documents
                    if item["kind"] == kind and item["metadata"]["name"].endswith(suffix))

    def rules(self):
        return {(item["rule"], item["resource"].split("/")[0]) for item in self.audit.findings}

    def test_real_profiles_and_validation_failures(self):
        scanned = []
        with patch.object(self.audit, "validate_profile",
                          side_effect=lambda name, documents, dormant=False:
                          scanned.append((name, len(documents) > 0, dormant))):
            self.audit.profiles()
        self.assertEqual([(c["name"], c["status"]) for c in self.audit.checks], [
            ("profile/disabled", "passed"), ("profile/two-site", "passed"), ("profile/pinned-digest", "passed"),
            ("profile/negative-triggers-exclusive", "passed"), ("profile/negative-nfs-forgejo-site", "passed"),
        ])
        self.assertEqual(scanned, [("disabled", False, True), ("two-site", True, False),
                                   ("pinned-digest", True, False)])

    def test_hardened_render_has_no_invariant_findings(self):
        self.audit.invariants(copy.deepcopy(self.documents), "two-site")
        self.assertEqual(self.audit.findings, [])

    def test_digest_must_pin_every_container(self):
        self.audit.invariants(copy.deepcopy(self.documents), "pinned-digest", DIGEST)
        self.assertIn(("forgejo-export-unpinned-image", "Deployment"), self.rules())

    def test_container_and_pod_hardening_regressions_are_reported(self):
        documents = copy.deepcopy(self.documents)
        sync = pod_spec(self.find(documents, "CronJob", "-sync"))
        sync["hostNetwork"] = True
        sync["volumes"].append({"name": "host", "hostPath": {"path": "/"}})
        sync["securityContext"]["runAsUser"] = 0
        sync["securityContext"].pop("seccompProfile")
        context = sync["containers"][0]["securityContext"]
        context.update(privileged=True, allowPrivilegeEscalation=True, readOnlyRootFilesystem=False,
                       capabilities={"drop": ["ALL"], "add": ["NET_ADMIN"]})
        sync["containers"][0]["resources"].pop("limits")
        self.audit.invariants(documents, "two-site")
        self.assertEqual({rule for rule, kind in self.rules() if kind == "CronJob"}, {
            "forgejo-export-host-namespace", "forgejo-export-hostpath", "forgejo-export-root-user",
            "forgejo-export-seccomp", "forgejo-export-privileged", "forgejo-export-privilege-escalation",
            "forgejo-export-writable-root", "forgejo-export-capabilities", "forgejo-export-resource-limits",
        })

    def test_provider_token_rbac_and_network_policy_regressions_are_reported(self):
        documents = copy.deepcopy(self.documents)
        pod_spec(self.find(documents, "Deployment", "-provider"))["automountServiceAccountToken"] = True
        self.find(documents, "Role", "-sync")["rules"][0]["verbs"] = ["*"]
        documents.remove(self.find(documents, "NetworkPolicy", "-webhook"))
        documents.append({"kind": "ClusterRoleBinding", "metadata": {"name": "escalate"}})
        self.audit.invariants(documents, "two-site")
        self.assertEqual(self.rules(), {
            ("forgejo-export-provider-token", "Deployment"), ("forgejo-export-rbac-wildcard", "Role"),
            ("forgejo-export-missing-networkpolicy", "Deployment"), ("forgejo-export-cluster-rbac", "ClusterRoleBinding"),
        })
        missing = next(item for item in self.audit.findings if item["rule"] == "forgejo-export-missing-networkpolicy")
        self.assertTrue(missing["resource"].endswith("-webhook"))
        self.audit.report()
        jsonschema.validate(json.loads((self.output / "report.json").read_text()),
                            json.loads((HERE / "report.schema.json").read_text()))

    def test_missing_component_is_an_error_not_a_pass(self):
        documents = [item for item in self.documents if not item["metadata"]["name"].endswith("-provider")]
        with self.assertRaisesRegex(CheckError, "provider"):
            self.audit.invariants(documents, "two-site")

    def test_report_identifies_chart_and_its_own_source(self):
        self.audit.report()
        data = json.loads((self.output / "report.json").read_text())
        self.assertEqual(data["chart"], "loris-forgejo-export")
        self.assertEqual(data["source_sha256"], source_hash(CHART))
        jsonschema.validate(data, json.loads((HERE / "report.schema.json").read_text()))

    def test_sources_cover_shell_and_python_syntax(self):
        def run(command, **kwargs):
            if command[0] == "shellcheck":
                return "[]"
            if command[0] == "bash":
                return ""
            if command[0] == "semgrep":
                # Semgrep's default ignore skips the unpackaged tests/ harness.
                scanned = [str(p) for p in (CHART / "files").rglob("*.py")]
                return json.dumps({"paths": {"scanned": scanned}, "results": []})
            return '{"Results":[]}'

        with patch.object(self.audit, "scan_config"), patch.object(self.audit, "workflows"), \
             patch.object(self.audit, "run", side_effect=run):
            self.audit.sources()
        statuses = {item["name"]: item["status"] for item in self.audit.checks}
        self.assertEqual(statuses["source/python-syntax"], "passed")
        self.assertEqual(statuses["source/semgrep"], "passed")
        self.assertEqual(statuses["source/syntax/tests/render.sh"], "passed")

    def test_cli_selects_only_the_forgejo_export_image(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        argv = ["audit.py", "--chart", "loris-forgejo-export", "--output", str(self.output)]
        with patch("sys.argv", argv), patch.object(ForgejoExportAudit, "versions"), \
             patch.object(ForgejoExportAudit, "profiles"), patch.object(ForgejoExportAudit, "sources"), \
             patch.object(ForgejoExportAudit, "image") as image, \
             patch.object(ForgejoExportAudit, "run", return_value=json.dumps({
                 "VulnerabilityDB": {"NextUpdate": future},
             })):
            self.assertEqual(audit.main(), 0)
        image.assert_called_once_with("forgejo-export", "docker.io/scilus/loris-forgejo-export:0.1.0", "linux/amd64")
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(report["chart"], "loris-forgejo-export")
        self.assertFalse(any(check["name"] == "image/database" for check in report["checks"]))

    def test_cli_rejects_runtime_and_database_override(self):
        for extra in (["--runtime"], ["--database-image", "wrong:1"]):
            with self.subTest(extra=extra), \
                 patch("sys.argv", ["audit.py", "--chart", "loris-forgejo-export", *extra]):
                with self.assertRaises(SystemExit) as error:
                    audit.main()
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
