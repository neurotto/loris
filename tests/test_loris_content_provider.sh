#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
image=${LORIS_IMAGE:-docker.io/scilus/loris:29.0.0-chart0.5.0}
fixtures="$ROOT/tests/fixtures/content_provider"

docker run --rm --network none -v "$fixtures:/fixtures:ro" \
    -e PROVIDER_URL=http://127.0.0.1:8099 \
    -e REQUEST_LOG=/tmp/loris-content-provider-requests.jsonl \
    -e LORIS_DATA_ROOT=/tmp/loris-content-provider \
    -e LORIS_CONTENT_ROOT=/tmp/loris-content-provider/assembly_bids \
    --entrypoint sh "$image" -ec '
        mkdir -p /tmp/loris-content-provider /tmp/loris-content-provider-state
        php -S 127.0.0.1:8099 /fixtures/provider.php >/dev/null 2>&1 &
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            php -r "exit(@fsockopen(\"127.0.0.1\", 8099) ? 0 : 1);" && break
            sleep 0.5
        done
        php /fixtures/file_reader.php
        php /fixtures/check.php
    '
