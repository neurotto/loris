#!/usr/bin/env bash
set -euo pipefail
umask 077

action=${1:-up}
if [[ "$action" == --help || "$action" == -h ]]; then
    printf '%s\n' \
        'Usage: bash run-local.sh [up|down|reset|logs|status]' \
        'Requires Docker with the Compose v2 plugin and OpenSSL.' \
        'LORIS_IMAGE defaults to scilus/loris:29.0.0-test (build it first).' \
        'LORIS_PORT defaults to 8080 on first startup; later runs reuse the saved port.' \
        'Only 127.0.0.1 is exposed. Changing the port requires reset.' \
        'down preserves local state; reset deletes this project and its volumes.'
    exit 0
fi
case "$action" in
    up|down|reset|logs|status) ;;
    *) printf 'Unknown action: %s\n' "$action" >&2; exit 1 ;;
esac

for tool in docker openssl; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'Missing prerequisite: %s\n' "$tool" >&2; exit 1; }
done
docker compose version >/dev/null
docker info >/dev/null

export LORIS_IMAGE=${LORIS_IMAGE:-scilus/loris:29.0.0-test}
project=scilus-loris-local
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/scilusnetes/loris-local"
credentials="$state_dir/credentials.env"
port_file="$state_dir/port"
saved_port=8080
if [[ -f "$port_file" ]]; then
  saved_port=$(< "$port_file")
fi
export LORIS_PORT=${LORIS_PORT:-$saved_port}
if [[ ! "$LORIS_PORT" =~ ^[0-9]{1,5}$ ]] || ((10#$LORIS_PORT < 1 || 10#$LORIS_PORT > 65535)); then
    printf 'LORIS_PORT must be between 1 and 65535.\n' >&2
    exit 1
fi
LORIS_PORT=$((10#$LORIS_PORT))
if [[ -f "$port_file" && "$action" == up && "$LORIS_PORT" != "$saved_port" ]]; then
  printf 'This deployment uses port %s. Reuse it or reset before changing the public URL.\n' "$saved_port" >&2
  exit 1
fi

if [[ "$action" == up ]]; then
    if ! docker image inspect "$LORIS_IMAGE" >/dev/null 2>&1; then
        printf 'Build the local image first:\n  docker build -t %s %s/image\n' \
            "$LORIS_IMAGE" "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" >&2
        exit 1
    fi
    mkdir -p "$state_dir"
    chmod 0700 "$state_dir"
    if [[ ! -f "$credentials" ]]; then
        printf 'MARIADB_ROOT_PASSWORD=%s\nLORIS_DB_PASSWORD=%s\nLORIS_FRONTEND_ADMIN_PASSWORD=%s\n' \
            "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" > "$credentials"
    fi
    chmod 0600 "$credentials"
    printf '%s\n' "$LORIS_PORT" > "$port_file"
elif [[ ! -f "$credentials" ]]; then
    printf 'No local deployment configured. Run this script with up first.\n' >&2
    exit 1
fi

compose() {
  env -u MARIADB_ROOT_PASSWORD -u LORIS_DB_PASSWORD -u LORIS_FRONTEND_ADMIN_PASSWORD \
    docker compose --project-name "$project" --env-file "$credentials" -f - "$@" <<'YAML'
services:
  database:
    image: mariadb:10.11.10
    environment:
      MARIADB_ROOT_PASSWORD: ${MARIADB_ROOT_PASSWORD:?}
      MARIADB_DATABASE: loris
      MARIADB_ROOT_HOST: "%"
    volumes:
      - database:/var/lib/mysql
    healthcheck:
      test: [CMD, healthcheck.sh, --connect, --innodb_initialized]
      interval: 2s
      timeout: 5s
      retries: 60
      start_period: 10s
  prepare:
    image: ${LORIS_IMAGE:?}
    pull_policy: never
    command:
      - sh
      - -ec
      - |
        if [ ! -f /project-state/.loris-seeded ]; then
          cp -a /var/www/loris/project/. /project-state/
          touch /project-state/.loris-seeded
        fi
        mkdir -p /data/loris/data/assembly_bids
        chown -R www-data:www-data /project-state /data/loris
    volumes:
      - project:/project-state
      - data:/data/loris
  bootstrap:
    image: ${LORIS_IMAGE:?}
    pull_policy: never
    user: "33:33"
    depends_on:
      prepare:
        condition: service_completed_successfully
      database:
        condition: service_healthy
    environment:
      LORIS_DB_HOST: database
      LORIS_DB_NAME: loris
      LORIS_DB_USER: loris
      LORIS_ADMIN_USER: admin
      LORIS_PUBLIC_URL: http://localhost:${LORIS_PORT:?}
      MARIADB_ROOT_PASSWORD: ${MARIADB_ROOT_PASSWORD:?}
      LORIS_DB_PASSWORD: ${LORIS_DB_PASSWORD:?}
      LORIS_FRONTEND_ADMIN_PASSWORD: ${LORIS_FRONTEND_ADMIN_PASSWORD:?}
    command: [php, /usr/local/bin/bootstrap-loris.php]
    volumes:
      - project:/var/www/loris/project
      - data:/data/loris
  web:
    image: ${LORIS_IMAGE:?}
    pull_policy: never
    depends_on:
      bootstrap:
        condition: service_completed_successfully
    ports:
      - "127.0.0.1:${LORIS_PORT:?}:80"
    volumes:
      - project:/var/www/loris/project
      - data:/data/loris
    healthcheck:
      test: [CMD-SHELL, 'curl --fail --silent http://127.0.0.1/login | grep -q "loginIndex.js"']
      interval: 5s
      timeout: 5s
      retries: 12
      start_period: 10s
volumes:
  database:
  project:
  data:
YAML
}

case "$action" in
    up)
        if ! compose up -d --wait --wait-timeout 180; then
            printf 'Local startup failed. Inspect it with: bash %s logs\n' "${BASH_SOURCE[0]}" >&2
            exit 1
        fi
        printf 'LORIS: http://localhost:%s\nUsername: admin\nCredentials file: %s\n' "$LORIS_PORT" "$credentials"
        ;;
    down) compose down ;;
    reset)
        compose down --volumes
        rm -f -- "$credentials" "$port_file"
        ;;
    logs) compose logs --tail=100 ;;
    status) compose ps --all ;;
esac