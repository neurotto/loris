# Generic LORIS Forgejo export image: git, git-annex (the p2phttp client needs >= 10.20240731), Python 3, and the
# LORIS electrophysiology chunker (LORIS-MRI, with MNE, NumPy, SciPy and protobuf from Debian) plus its metadata
# patch. The chart's scripts are not part of the image; they are mounted from ConfigMaps at /opt/loris-forgejo-export.
# Runs as www-data (33), the LORIS data owner.
FROM docker.io/library/debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad

ARG LORIS_MRI_COMMIT=7576da14ec84cedcee2565326544c624e089d2fc
ARG VERSION=0.1.0

LABEL org.opencontainers.image.source="https://github.com/neurotto/loris" \
      org.opencontainers.image.title="loris-forgejo-export" \
      org.opencontainers.image.version="${VERSION}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends git git-annex python3 ca-certificates \
      python3-mne python3-numpy python3-scipy python3-protobuf python3-pip python3-hatchling \
 && pip install --no-deps --no-build-isolation --no-cache-dir --break-system-packages \
      "git+https://github.com/aces/Loris-MRI@${LORIS_MRI_COMMIT}#subdirectory=python/loris_ephys_chunker" \
 && apt-get purge -y python3-pip python3-hatchling \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && annex=$(git annex version --raw) \
 && echo "git-annex $annex" \
 && dpkg --compare-versions "${annex%%-*}" ge 10.20240731 \
 && python3 -c 'import mne, loris_ephys_chunker.chunking' \
 && edf-to-chunks --help >/dev/null && eeglab-to-chunks --help >/dev/null

COPY --chmod=0444 ephys-chunker-metadata.patch /tmp/ephys-chunker-metadata.patch
RUN cd /tmp \
 && library=$(python3 -c 'import loris_ephys_chunker.chunking as c, pathlib; print(pathlib.Path(c.__file__).parent.parent)') \
 && git apply --unsafe-paths --directory="$library" --check ephys-chunker-metadata.patch \
 && git apply --unsafe-paths --directory="$library" ephys-chunker-metadata.patch \
 && rm ephys-chunker-metadata.patch

ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR=/tmp
USER 33:33
