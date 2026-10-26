FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    nfs-ganesha nfs-ganesha-vfs \
    && mkdir -p /run/ganesha /var/lib/ganesha \
    && ln -sf /proc/self/mounts /etc/mtab \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/loris-nfs-export-entrypoint
RUN chmod 0555 /usr/local/bin/loris-nfs-export-entrypoint

# Ganesha's VFS backend needs root plus DAC_READ_SEARCH/SETUID/SETGID; clients are squashed to the export UID.
EXPOSE 20490/tcp
ENTRYPOINT ["/usr/local/bin/loris-nfs-export-entrypoint"]