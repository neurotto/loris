"""Install pinned audit tools in a repository-local directory, without sudo."""

import argparse
import hashlib
import io
import json
import platform
from pathlib import Path
import subprocess
import tarfile
import urllib.request
import venv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="store_true", help="Also install pinned kind (does not create clusters)")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    target = source.parents[2] / ".audit-tools"
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise SystemExit("Automated setup supports Linux x86_64 only; install the pinned versions manually.")
    binary = target / "bin"
    binary.mkdir(parents=True, exist_ok=True)
    for name, spec in json.loads((source / "tools.json").read_text()).items():
        print(f"Installing {name} {spec['version']}", flush=True)
        with urllib.request.urlopen(spec["url"], timeout=120) as response:
            archive = response.read()
        if hashlib.sha256(archive).hexdigest() != spec["sha256"]:
            raise SystemExit(f"Checksum mismatch: {name}")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            member = bundle.getmember(spec["member"])
            if not member.isfile():
                raise SystemExit(f"Not a regular binary: {name}")
            stream = bundle.extractfile(member)
            if stream is None:
                raise SystemExit(f"Missing binary: {name}")
            (binary / name).write_bytes(stream.read())
        (binary / name).chmod(0o755)
    if args.runtime:
        url = "https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64"
        with urllib.request.urlopen(url, timeout=120) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != "aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d":
            raise SystemExit("Checksum mismatch: kind")
        (binary / "kind").write_bytes(content)
        (binary / "kind").chmod(0o755)
    environment = target / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run(
        [str(environment / "bin/python"), "-m", "pip", "install", "--disable-pip-version-check",
         "-r", str(source / "requirements.txt")], check=True,
    )
    print(f"Use PATH={binary}:{environment / 'bin'}:$PATH")


if __name__ == "__main__":
    main()
