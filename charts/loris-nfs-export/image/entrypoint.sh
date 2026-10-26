#!/bin/sh
set -eu

if [ -z "${NFS_ALLOWED_CLIENTS:-}" ]; then
    echo "ERROR: NFS_ALLOWED_CLIENTS must contain approved client CIDRs" >&2
    exit 1
fi

nfs_port=${NFS_PORT:-20490}
case "$nfs_port" in
    ''|*[!0-9]*) echo "ERROR: NFS_PORT must be an integer from 1024 to 65535" >&2; exit 1 ;;
esac
if [ "$nfs_port" -lt 1024 ] || [ "$nfs_port" -gt 65535 ]; then
    echo "ERROR: NFS_PORT must be an integer from 1024 to 65535" >&2
    exit 1
fi

export_uid=${EXPORT_UID:-10001}
export_gid=${EXPORT_GID:-}
for id in "$export_uid" "$export_gid"; do
    case "$id" in
        ''|*[!0-9]*|0) echo "ERROR: EXPORT_UID and EXPORT_GID must be non-zero integers" >&2; exit 1 ;;
    esac
done

cat > /run/ganesha/ganesha.conf <<EOF
NFS_Core_Param {
    NFS_Port = ${nfs_port};
    NFS_Protocols = 4;
    Enable_UDP = false;
    Enable_NLM = false;
    Enable_RQUOTA = false;
}
NFSv4 {
    RecoveryBackend = fs;
    RecoveryRoot = /var/lib/ganesha;
    RecoveryDir = v4recov;
}
EXPORT {
    Export_Id = 1;
    Path = /export;
    Pseudo = /bids;
    Protocols = 4;
    Transports = TCP;
    SecType = sys;
    Access_Type = RO;
    Squash = all_squash;
    Anonymous_uid = ${export_uid};
    Anonymous_gid = ${export_gid};
    CLIENT {
        Clients = ${NFS_ALLOWED_CLIENTS};
        Access_Type = RO;
    }
    FSAL {
        Name = VFS;
    }
}
EOF

exec ganesha.nfsd -F -L STDOUT -f /run/ganesha/ganesha.conf