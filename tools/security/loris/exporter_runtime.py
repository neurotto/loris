"""Opt-in NFS export checks confined to an owned three-node kind cluster."""

import ipaddress
import os
from pathlib import Path
import secrets
import shutil

import yaml

from audit import Audit, HERE, parse_json
from common import CheckError
from runtime import KIND_VERSION, NODE


def validate(audit: Audit) -> None:
    if shutil.disk_usage(audit.scratch).free < 16 * 1024 ** 3:
        raise CheckError("Exporter runtime requires at least 16GiB free disk; no cluster was created")
    available = next(line for line in Path("/proc/meminfo").read_text().splitlines()
                     if line.startswith("MemAvailable:"))
    if int(available.split()[1]) < 4 * 1024 ** 2:
        raise CheckError("Exporter runtime requires at least 4GiB available memory; no cluster was created")
    image = next((item["identity"] for item in audit.images if item["name"] == "exporter"), "")
    if "@" not in image:
        raise CheckError("Exporter runtime requires a successfully scanned, pullable repository digest")
    if KIND_VERSION not in audit.run(["kind", "version"], timeout=30):
        raise CheckError(f"Runtime requires kind {KIND_VERSION}")
    host = os.environ.get("DOCKER_HOST", "")
    if not host:
        context = parse_json(audit.run(["docker", "context", "inspect"], timeout=30))
        host = context[0]["Endpoints"]["docker"]["Host"]
    if not host.startswith("unix://"):
        raise CheckError("Exporter runtime requires an explicitly local Unix-socket Docker daemon")
    audit.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)

    name = "loris-nfs-audit-" + secrets.token_hex(4)
    namespace = "loris-nfs-audit"
    kubeconfig = audit.scratch / "exporter-kubeconfig"
    configuration = audit.scratch / "exporter-kind.yaml"
    configuration.write_text(yaml.safe_dump({
        "kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4",
        "nodes": [{"role": "control-plane"}, {"role": "worker"}, {"role": "worker"}],
    }))
    kube = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", "kind-" + name]

    def kubectl(*args: str) -> str:
        return audit.run([*kube, *args], timeout=600)

    def node_exec(node: str, command: list[str], timeout: int = 180) -> str:
        if node not in owned:
            raise CheckError("Refusing Docker execution outside owned kind nodes")
        return audit.run(["docker", "exec", node, *command], timeout=timeout)

    owned: set[str] = set()
    audit.tools["kind"] = KIND_VERSION
    audit.tools["runtime-node"] = NODE
    try:
        audit.run(["kind", "create", "cluster", "--name", name, "--image", NODE,
                   "--config", str(configuration), "--kubeconfig", str(kubeconfig),
                   "--wait", "180s"], timeout=900)
        kubeconfig.chmod(0o600)
        owned = set(audit.run(["kind", "get", "nodes", "--name", name], timeout=30).split())
        expected = {name + "-control-plane", name + "-worker", name + "-worker2"}
        if owned != expected:
            raise CheckError("Kind node inventory does not match the owned exporter cluster")
        for node in sorted(owned):
            label = audit.run(["docker", "inspect", "--format",
                               '{{index .Config.Labels "io.x-k8s.kind.cluster"}}', node], timeout=30).strip()
            if label != name:
                raise CheckError("Kind container ownership label does not match")
        nodes = parse_json(kubectl("get", "nodes", "-o", "json"))["items"]
        addresses = {}
        hostnames = {}
        for node in nodes:
            node_name = node["metadata"]["name"]
            addresses[node_name] = str(ipaddress.ip_address(next(
                item["address"] for item in node["status"]["addresses"] if item["type"] == "InternalIP")))
            hostnames[node_name] = node["metadata"]["labels"]["kubernetes.io/hostname"]
        if set(addresses) != owned:
            raise CheckError("Kubernetes node inventory does not match kind ownership")
        server = name + "-control-plane"
        client = name + "-worker"
        denied = name + "-worker2"
        # Preparation affects only the named kind container, never a host source mount.
        node_exec(server, ["sh", "-ec",
                           "mkdir -p /var/local/loris-audit-source; "
                           "printf 'synthetic-nfs-audit\\n' > /var/local/loris-audit-source/readable.txt; "
                           "printf 'root-only-fixture\\n' > /var/local/loris-audit-source/root-only.txt; "
                           "chown 0:2000 /var/local/loris-audit-source /var/local/loris-audit-source/readable.txt; "
                           "chown 0:0 /var/local/loris-audit-source/root-only.txt; "
                           "chmod 0550 /var/local/loris-audit-source; "
                           "chmod 0440 /var/local/loris-audit-source/readable.txt; "
                           "chmod 0400 /var/local/loris-audit-source/root-only.txt"])
        values = yaml.safe_load((HERE / "profiles/exporter-enabled.yaml").read_text())
        values["nodeHostname"] = hostnames[server]
        values["allowedClientCIDRs"] = [addresses[client] + ("/32" if ":" not in addresses[client] else "/128")]
        path = audit.scratch / "exporter-runtime-values.yaml"
        path.write_text(yaml.safe_dump(values))
        manifest = audit.run(["helm", "template", "audit", str(audit.chart), "--namespace", namespace,
                              "-f", str(path)])
        documents = [item for item in yaml.safe_load_all(manifest) if item]
        if len(documents) != 1 or documents[0]["kind"] != "Deployment":
            raise CheckError("Unexpected exporter runtime render")
        deployment = documents[0]
        pod = deployment["spec"]["template"]["spec"]
        pod["containers"][0]["image"] = image
        # The source node is the control plane only in this disposable fixture.
        pod["tolerations"] = [{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists",
                               "effect": "NoSchedule"}]
        kubectl("create", "namespace", namespace)
        deployment["metadata"]["namespace"] = namespace
        rendered = audit.scratch / "exporter-runtime.yaml"
        rendered.write_text(yaml.safe_dump(deployment))
        kubectl("apply", "-f", str(rendered))
        kubectl("-n", namespace, "rollout", "status", "deployment/loris-nfs-export-audit-site", "--timeout=300s")
        for node in (client, denied):
            node_exec(node, ["sh", "-ec",
                             "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; "
                             "apt-get install -y --no-install-recommends nfs-common; "
                             "mkdir -p /mnt/loris-audit"], timeout=600)
        source = ("[" + addresses[server] + "]" if ":" in addresses[server] else addresses[server]) + ":/bids"
        # Request a writable client mount so EROFS tests server enforcement, not just a client 'ro' flag.
        options = "rw,nfsvers=4.1,proto=tcp,port=20490,soft,timeo=20,retrans=1"
        node_exec(client, ["timeout", "45", "mount", "-t", "nfs", "-o", options, source, "/mnt/loris-audit"])
        node_exec(client, ["sh", "-ec",
                           "test \"$(cat /mnt/loris-audit/readable.txt)\" = synthetic-nfs-audit; "
                           "if touch /mnt/loris-audit/prohibited 2>/tmp/loris-write-error; then exit 1; fi; "
                           "grep -qi 'read-only file system' /tmp/loris-write-error; "
                           "if cat /mnt/loris-audit/root-only.txt >/dev/null 2>/tmp/loris-root-error; then exit 1; fi; "
                           "grep -qi 'permission denied' /tmp/loris-root-error"])
        # A network outage is not accepted as proof that the CIDR rule denied access.
        node_exec(denied, ["sh", "-ec",
                           'if timeout 45 mount -t nfs -o "$1" "$2" /mnt/loris-audit '
                           '2>/tmp/loris-denied-error; then umount /mnt/loris-audit; exit 1; fi; '
                           "grep -qi 'access denied' /tmp/loris-denied-error",
                           "sh", options, source], timeout=60)
        node_exec(server, ["sh", "-ec",
                           "test ! -e /var/local/loris-audit-source/prohibited; "
                           "test \"$(cat /var/local/loris-audit-source/readable.txt)\" = synthetic-nfs-audit"])
        node_exec(client, ["umount", "/mnt/loris-audit"])
    finally:
        audit.run(["kind", "delete", "cluster", "--name", name, "--kubeconfig", str(kubeconfig)], timeout=180)
