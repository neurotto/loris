#!/usr/bin/env bash
# Checks the RE-MOVE on-demand content patch (LORIS\ReMove\AnnexContent) inside the LORIS image.
set -euo pipefail

image=${LORIS_IMAGE:-docker.io/scilus/loris:29.0.0}
fixtures=$(cd "$(dirname "$0")/fixtures/annex_content" && pwd)

docker run --rm --network none --user 33:33 -v "$fixtures:/fixtures:ro" \
    -e CACHE_URL=http://127.0.0.1:8099 -e REQUEST_LOG=/tmp/requests.log \
    --entrypoint sh "$image" -ec '
        php -S 127.0.0.1:8099 /fixtures/cache.php >/dev/null 2>&1 &
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            php -r "exit(@fsockopen(\"127.0.0.1\", 8099) ? 0 : 1);" && break
            sleep 0.5
        done
        php /fixtures/check.php
        php /fixtures/imageinfo.php
    '
