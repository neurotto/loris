# loris-forgejo-export image

Image `docker.io/scilus/loris-forgejo-export`, used by every component of the
`loris-forgejo-export` chart. It is Debian trixie-slim (pinned by digest) with:

- `git` and `git-annex` (at least 10.20240731, checked at build time);
- `python3`, with `mne`, `numpy` and `scipy` from Debian;
- the LORIS-MRI electrophysiology chunker (`loris_ephys_chunker`, commands
  `edf-to-chunks` and `eeglab-to-chunks`) at a pinned commit, with
  `ephys-chunker-metadata.patch` applied.

The chart's Python scripts are not in the image: they are mounted from a
ConfigMap at `/opt/loris-forgejo-export`. The image runs as `33:33` with
`HOME=/tmp`, `PYTHONDONTWRITEBYTECODE=1` and `MPLCONFIGDIR=/tmp`, so it works
with a read-only root filesystem and an `emptyDir` at `/tmp`.

Build:

```sh
docker build -t docker.io/scilus/loris-forgejo-export:0.1.0 charts/loris-forgejo-export/image
```

`LORIS_MRI_COMMIT` and `VERSION` (the OCI version label) are build arguments.
