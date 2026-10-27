#!/usr/bin/env bash
set -euo pipefail
umask 077

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
collector="$root/charts/loris/diagnose-local.sh"
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
jq -n '{log:{entries:[
    {request:{method:"GET",url:"https://dummy:top-secret@example.org/module_manager/?token=top-secret#top-secret",headers:[{name:"Cookie",value:"top-secret"}]},
     response:{status:200,content:{mimeType:"text/html",text:"top-secret"}},time:5000,timings:{wait:40,blocked:4500,receive:460}},
    {request:{method:"GET",url:"https://example.org/broken.js"},response:{status:500,content:{mimeType:"text/javascript"}},time:50,timings:{wait:45}}
]}}' > "$temporary/input.har"
bash "$collector" --no-browser --timeout 1 --url http://127.0.0.1:19999 \
    --har "$temporary/input.har" --output-dir "$temporary/report" > "$temporary/console"
grep -q '\[FAIL\] Local admin authentication failed' "$temporary/report/report.txt"
grep -q '\[WARN\] Your browser: 5000ms' "$temporary/report/report.txt"
grep -q '\[FAIL\] Your browser: HTTP 500' "$temporary/report/report.txt"
if grep -Rq top-secret "$temporary/report"; then
    printf 'FAIL: HAR secrets leaked into diagnostic output.\n' >&2
    exit 1
fi
jq -e '.[0].url == "https://[redacted]@example.org/module_manager/" and .[0].wait_ms == 40 and .[0].blocked_ms == 4500' \
    "$temporary/report/har-summary.json" >/dev/null
printf 'PASS: failures and slow HAR requests are highlighted without copying secrets.\n'

if bash "$collector" --no-browser --url 'http://localhost:18080@evil.invalid' > "$temporary/rejected" 2>&1; then
    printf 'FAIL: non-loopback credential destination was accepted.\n' >&2
    exit 1
fi
printf 'PASS: non-loopback authentication is rejected.\n'
if bash "$collector" --no-browser --url http://127.0.0.1:19999 --output-dir "$temporary/report" > "$temporary/reused" 2>&1; then
    printf 'FAIL: an old report directory was reused.\n' >&2
    exit 1
fi
grep -q 'already contains diagnostic results' "$temporary/reused"
printf 'PASS: previous diagnostic results cannot be confused with a new run.\n'