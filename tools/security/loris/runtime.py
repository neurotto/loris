"""Explicitly opted-in, owned kind-cluster checks using synthetic inputs only."""

import hashlib
import json
from pathlib import Path
import secrets
import shutil
import urllib.request

import yaml

from audit import CHART, generated_worker, parse_json
from common import CheckError


NODE = "kindest/node:v1.33.1@sha256:050072256b9a903bd914c0b2866828150cb229cea0efe5892e2b644d5dd3b34f"
CILIUM_URL = "https://helm.cilium.io/cilium-1.18.2.tgz"
CILIUM_SHA256 = "39b62a72f0892dd16548bd08ee97d5db2e975f7c1f57155578a144186e22992f"
KIND_VERSION = "0.33.0"


def validate(audit) -> None:
    if shutil.disk_usage(audit.scratch).free < 12 * 1024 ** 3:
        raise CheckError("Runtime requires at least 12GiB free disk; no cluster was created")
    available = next(line for line in Path("/proc/meminfo").read_text().splitlines()
                     if line.startswith("MemAvailable:"))
    if int(available.split()[1]) < 3 * 1024 ** 2:
        raise CheckError("Runtime requires at least 3GiB available memory; no cluster was created")
    images = {item["name"]: item["identity"] for item in audit.images}
    if "application" not in images or "database" not in images:
        raise CheckError("Runtime requires successful immutable application and database image scans")
    if any("@" not in images[key] for key in ("application", "database")):
        raise CheckError("Runtime requires pullable repository digests, not local-only image IDs")
    image = images["application"]
    name = "loris-audit-" + secrets.token_hex(4)
    kubeconfig = audit.scratch / "runtime-kubeconfig"
    runtime = audit.scratch / "runtime"
    runtime.mkdir()
    kube = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", "kind-" + name]

    def kubectl(*args: str) -> str:
        return audit.run([*kube, *args], timeout=600)

    def apply(objects: list[dict], filename: str) -> None:
        path = runtime / filename
        path.write_text(yaml.safe_dump_all(objects))
        path.chmod(0o600)
        kubectl("apply", "-f", str(path))

    if KIND_VERSION not in audit.run(["kind", "version"], timeout=30):
        raise CheckError(f"Runtime requires kind {KIND_VERSION}")
    audit.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
    audit.tools["kind"] = KIND_VERSION
    audit.tools["cilium-chart"] = "1.18.2"
    audit.tools["runtime-node"] = NODE
    configuration = runtime / "kind.yaml"
    configuration.write_text(yaml.safe_dump({
        "kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4",
        "networking": {"disableDefaultCNI": True},
        "nodes": [{"role": "control-plane"}],
    }))
    with urllib.request.urlopen(CILIUM_URL, timeout=120) as response:
        chart = response.read()
    if hashlib.sha256(chart).hexdigest() != CILIUM_SHA256:
        raise CheckError("Runtime Cilium chart checksum mismatch")
    cilium = runtime / "cilium.tgz"
    cilium.write_bytes(chart)
    try:
        audit.run(["kind", "create", "cluster", "--name", name, "--image", NODE,
                   "--config", str(configuration), "--kubeconfig", str(kubeconfig), "--wait", "180s"], timeout=600)
        kubeconfig.chmod(0o600)
        audit.run(["helm", "install", "cilium", str(cilium), "--namespace", "kube-system",
                   "--kubeconfig", str(kubeconfig), "--kube-context", "kind-" + name,
                   "--set", "ipam.mode=kubernetes", "--set", "kubeProxyReplacement=false",
                   "--set", "operator.replicas=1", "--wait", "--timeout", "5m"], timeout=420)
        kubectl("wait", "--for=condition=Ready", "nodes", "--all", "--timeout=180s")
        kubectl("create", "namespace", "loris-audit")
        namespace = "loris-audit"
        credentials = {key: secrets.token_hex(24) for key in (
            "MARIADB_ROOT_PASSWORD", "LORIS_DB_PASSWORD", "LORIS_FRONTEND_ADMIN_PASSWORD")}
        apply([{"apiVersion": "v1", "kind": "Secret", "metadata": {
            "name": "loris-credentials", "namespace": namespace},
            "type": "Opaque", "stringData": credentials}], "secret.yaml")
        # Use the same full reference as the static audit without ambiguous tag parsing.
        manifest = audit.run(["helm", "template", "runtime", str(CHART), "--namespace", namespace,
                              "--set", "enabled=true", "--set", "loris.data.persistence.enabled=true"])
        documents = [item for item in yaml.safe_load_all(manifest) if item]
        for item in documents:
            item.setdefault("metadata", {})["namespace"] = namespace
            if item["kind"] in ("Deployment", "StatefulSet"):
                pod = item["spec"]["template"]["spec"]
                for container in [*pod.get("initContainers", []), *pod["containers"]]:
                    if container["image"].startswith("docker.io/scilus/loris:"):
                        container["image"] = image
                    elif item["kind"] == "StatefulSet":
                        container["image"] = images["database"]
                if item["kind"] == "Deployment":
                    for container in pod["initContainers"]:
                        for variable in container.get("env", []):
                            if variable["name"] == "LORIS_PUBLIC_URL":
                                variable["value"] = "http://loris"
        # No dynamic storage provisioner is required in this disposable single-node cluster.
        apply([{
            "apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": name + "-data"},
            "spec": {"capacity": {"storage": "6Gi"}, "accessModes": ["ReadWriteOnce"],
                     "storageClassName": "", "persistentVolumeReclaimPolicy": "Retain",
                     "hostPath": {"path": "/var/local/loris-audit-data", "type": "DirectoryOrCreate"},
                     "claimRef": {"namespace": namespace, "name": "loris-data"}},
        }], "data-pv.yaml")
        for item in documents:
            if item["kind"] == "PersistentVolumeClaim":
                item["spec"]["storageClassName"] = ""
                item["spec"]["volumeName"] = name + "-data"
        apply(documents, "portal.yaml")
        kubectl("-n", namespace, "rollout", "status", "deployment/loris", "--timeout=600s")
        kubectl("-n", namespace, "exec", "deployment/loris", "-c", "loris", "--",
                "curl", "--fail", "--silent", "http://localhost/login")
        kubectl("-n", namespace, "exec", "deployment/loris", "-c", "loris", "--",
                "sh", "-ec", "mkdir -p /data/loris/data/logs; chown www-data:www-data /data/loris/data/logs")

        synthetic = {
            "dataset_description.json": json.dumps({"Name": "Synthetic Audit", "BIDSVersion": "1.9.0"}),
            "participants.tsv": "participant_id\nsub-audit01\n",
            "placeholder.txt": "Synthetic fixture; not research data.\n",
        }
        script = (
            "from pathlib import Path; import json; "
            "p=Path('/data/loris/synthetic-bids/study-a'); "
            "(p/'sub-audit01').mkdir(parents=True,exist_ok=True); "
            f"d=json.loads({json.dumps(json.dumps(synthetic))}); "
            "[(p.joinpath('sub-audit01' if k=='placeholder.txt' else '.',k)).write_text(v) "
            "for k,v in d.items()]"
        )
        kubectl("-n", namespace, "exec", "deployment/loris", "-c", "loris", "--", "python3", "-c", script)
        imports = audit.run(["helm", "template", "runtime", str(CHART), "--namespace", namespace,
                             "-f", str(Path(__file__).parent / "profiles/imports.yaml")])
        imports_docs = [item for item in yaml.safe_load_all(imports) if item]
        profile = next(item for item in imports_docs if item["kind"] == "ConfigMap")
        config = json.loads(profile["data"]["runtime.json"])
        config["site"]["bidsImport"]["datasets"] = {}
        profile["data"]["runtime.json"] = json.dumps(config)
        # The synthetic source is a read-only PVC subPath, not real NFS.
        job = generated_worker(imports_docs)
        pod = job["spec"]["template"]["spec"]
        for container in pod["containers"]:
            container["image"] = image
            for env in container.get("env", []):
                if env["name"] == "BIDS_DATASET_ID":
                    env["value"] = hashlib.sha256(b"audit-site/study-a").hexdigest()
            for mount in container["volumeMounts"]:
                if mount["name"] == "site-data":
                    mount["subPath"] = "synthetic-bids"
        for volume in pod["volumes"]:
            if volume["name"] == "site-data":
                volume["persistentVolumeClaim"] = {"claimName": "loris-data"}
        # Exercise real initialize/enroll/preflight code with a synthetic reserved ledger attempt.
        initializer = {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": "audit-ledger", "namespace": namespace},
            "spec": {"backoffLimit": 0, "template": {"spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                "containers": [{
                    "name": "initialize", "image": image,
                    "command": ["python3", "-c", (
                                "import sys,json; sys.path.insert(0,'/import-profile'); "
                                "import bids_runtime as b; from lib.config_file import load_config; "
                                "from lib.db.connect import get_database_engine; "
                                "e=get_database_engine(load_config('config.py').mysql); "
                                "c=json.load(open('/import-profile/runtime.json')); "
                                "b.initialize(e); s=b.resolve_settings(c,'study-a','Synthetic Audit'); "
                                "i=b.enroll(e,'audit-site','study-a',s,'Synthetic Audit'); "
                                "b.reserve(e,i,s)")],
                    "env": copy_env(pod["containers"][0]["env"], root=True),
                    "volumeMounts": pod["containers"][0]["volumeMounts"],
                }], "volumes": pod["volumes"],
            }}},
        }
        # Worker references its immutable snapshot profile; provide the same contents.
        profile["metadata"] = {"name": "audit-worker", "namespace": namespace}
        job["metadata"] = {"name": "audit-worker", "namespace": namespace}
        apply([profile, initializer], "ledger.yaml")
        kubectl("-n", namespace, "wait", "--for=condition=complete", "job/audit-ledger", "--timeout=180s")
        apply([job], "worker.yaml")
        kubectl("-n", namespace, "wait", "--for=condition=complete", "job/audit-worker", "--timeout=180s")
        # Source write attempt must fail, not merely show a readOnly flag.
        write_test = copy_probe(image, "readonly-probe", namespace)
        write_test["spec"]["containers"][0].update({
            "command": ["python3", "-c",
                        "import os,errno\nfrom pathlib import Path\n"
                        "p=Path('/source')\n"
                        "assert (p/'study-a/dataset_description.json').is_file()\n"
                        "assert os.statvfs(p).f_flag & os.ST_RDONLY\n"
                        "try:\n (p/'prohibited').touch()\n"
                        "except OSError as e:\n assert e.errno == errno.EROFS\n"
                        "else:\n raise SystemExit(1)\n"],
            "volumeMounts": [{"name": "source", "mountPath": "/source", "subPath": "synthetic-bids",
                             "readOnly": True}],
        })
        write_test["spec"]["volumes"] = [{"name": "source", "persistentVolumeClaim": {"claimName": "loris-data"}}]
        apply([write_test], "readonly.yaml")
        kubectl("-n", namespace, "wait", "--for=jsonpath={.status.phase}=Succeeded",
                "pod/readonly-probe", "--timeout=90s")

        # API checks use explicit impersonation, never a host user's ambient permissions.
        rbac = [item for item in imports_docs if item["kind"] in ("ServiceAccount", "Role", "RoleBinding")]
        for item in rbac:
            item.setdefault("metadata", {})["namespace"] = namespace
        apply(rbac, "rbac.yaml")
        for verb, resource, expected in (
            ("create", "jobs", "yes"), ("delete", "jobs", "no"), ("get", "secrets", "no"),
        ):
            answer = audit.run([*kube, "-n", namespace, "auth", "can-i", verb, resource,
                                "--as=system:serviceaccount:loris-audit:loris-bids-audit-site"],
                               allowed=(0, 1)).strip()
            if answer != expected:
                raise CheckError("Runtime discovery RBAC assertion failed")
        worker_state = parse_json(kubectl("-n", namespace, "get", "pods", "-l", "job-name=audit-worker",
                                         "-o", "json"))
        for worker in worker_state["items"]:
            spec = worker["spec"]
            if spec.get("automountServiceAccountToken") is not False:
                raise CheckError("Admitted worker unexpectedly has API-token automount enabled")
            if any("kube-api-access" in volume["name"] for volume in spec["volumes"]):
                raise CheckError("Admitted worker unexpectedly has an API-token volume")
        for allowed in (True, False):
            probe = copy_probe(image, "network-allowed" if allowed else "network-denied", namespace)
            if allowed:
                probe["metadata"]["labels"] = {"app.kubernetes.io/name": "loris"}
            script = ("import socket,time; time.sleep(3); s=socket.socket(); s.settimeout(5); "
                      "r=s.connect_ex(('loris-mariadb',3306)); "
                      f"raise SystemExit(0 if {'r == 0' if allowed else 'r != 0'} else 1)")
            probe["spec"]["containers"][0]["command"] = ["python3", "-c", script]
            apply([probe], probe["metadata"]["name"] + ".yaml")
            kubectl("-n", namespace, "wait", "--for=jsonpath={.status.phase}=Succeeded",
                    "pod/" + probe["metadata"]["name"], "--timeout=90s")
        # No logs, kubeconfig, source metadata, or disposable credentials are persisted.
    finally:
        audit.run(["kind", "delete", "cluster", "--name", name, "--kubeconfig", str(kubeconfig)], timeout=180)


def copy_env(environment: list[dict], root: bool = False) -> list[dict]:
    output = json.loads(json.dumps(environment))
    if root:
        for variable in output:
            if variable["name"] == "LORIS_DB_USER":
                variable["value"] = "root"
            if variable["name"] == "LORIS_DB_PASSWORD":
                variable["valueFrom"]["secretKeyRef"]["key"] = "MARIADB_ROOT_PASSWORD"
    return output


def copy_probe(image: str, name: str, namespace: str) -> dict:
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": namespace},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                     "containers": [{"name": "probe", "image": image}]}}
