#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
image=${LORIS_IMAGE:-scilus/loris:29.0.0}
database_image=${LORIS_DATABASE_IMAGE:-mariadb:10.11.10}
for tool in docker openssl; do
    command -v "$tool" >/dev/null || { printf 'Missing prerequisite: %s\n' "$tool" >&2; exit 1; }
done
docker image inspect "$image" "$database_image" >/dev/null
project="scilus-bids-check-$(openssl rand -hex 8)"
database="$project-db"
network="$project-net"
volume="$project-data"
database_created=false
network_created=false
volume_created=false

cleanup() {
    local result=$?
    if [[ "$database_created" == true ]] && ! docker rm -f "$database" >/dev/null; then
        printf 'Failed to clean up test container %s\n' "$database" >&2
        result=1
    fi
    if [[ "$volume_created" == true ]] && ! docker volume rm "$volume" >/dev/null; then
        printf 'Failed to clean up test volume %s\n' "$volume" >&2
        result=1
    fi
    if [[ "$network_created" == true ]] && ! docker network rm "$network" >/dev/null; then
        printf 'Failed to clean up test network %s\n' "$network" >&2
        result=1
    fi
    return "$result"
}
trap cleanup EXIT
export MARIADB_ROOT_PASSWORD LORIS_DB_PASSWORD LORIS_FRONTEND_ADMIN_PASSWORD
MARIADB_ROOT_PASSWORD=$(openssl rand -hex 24)
LORIS_DB_PASSWORD=$(openssl rand -hex 24)
LORIS_FRONTEND_ADMIN_PASSWORD=$(openssl rand -hex 24)
docker network create "$network" >/dev/null
network_created=true
docker volume create "$volume" >/dev/null
volume_created=true
docker run -d --name "$database" --network "$network" \
    -e MARIADB_ROOT_PASSWORD -e MARIADB_ROOT_HOST=% -e MARIADB_DATABASE=loris \
    "$database_image" >/dev/null
database_created=true
ready=false
for _ in $(seq 1 90); do
    if docker exec "$database" healthcheck.sh --connect --innodb_initialized >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [[ "$ready" != true ]]; then
    printf 'Isolated MariaDB did not become ready.\n' >&2
    exit 1
fi
docker run --rm --network "$network" -v "$volume:/data/loris" \
    -e LORIS_DB_HOST="$database" -e LORIS_DB_NAME=loris -e LORIS_DB_USER=loris \
    -e LORIS_ADMIN_USER=admin -e LORIS_PUBLIC_URL=http://localhost \
    -e MARIADB_ROOT_PASSWORD -e LORIS_DB_PASSWORD -e LORIS_FRONTEND_ADMIN_PASSWORD \
    "$image" php /usr/local/bin/bootstrap-loris.php
export BIDS_TEST_DATABASE_URL="mysql+mysqldb://loris:$LORIS_DB_PASSWORD@$database/loris"
export BIDS_TEST_ADMIN_DATABASE_URL="mysql+mysqldb://root:$MARIADB_ROOT_PASSWORD@$database/loris"
docker run --rm --network "$network" -v "$volume:/data/loris" -v "$ROOT:/workspace:ro" \
    -e BIDS_TEST_DATABASE_URL -e BIDS_TEST_ADMIN_DATABASE_URL -e PYTHONDONTWRITEBYTECODE=1 \
    -e LORIS_DB_HOST="$database" -e LORIS_DB_NAME=loris -e LORIS_DB_USER=loris \
    -e LORIS_DB_PASSWORD -e LORIS_FRONTEND_ADMIN_PASSWORD \
    "$image" python3 -m unittest discover -s /workspace/tests -p 'test_loris_bids_*.py' -v
