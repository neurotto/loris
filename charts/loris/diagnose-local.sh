#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/scilusnetes/loris-local"
port=8080
if [[ -f "$state_dir/port" ]]; then port=$(< "$state_dir/port"); fi
url="http://localhost:$port"
public_url=
output_dir=
browser=true
timeout=20
har=
while [[ $# -gt 0 ]]; do
    case "$1" in
        --url|--public-url|--output-dir|--timeout|--har)
            [[ $# -ge 2 && -n "$2" ]] || { printf 'Missing value for %s\n' "$1" >&2; exit 1; }
            case "$1" in
                --url) url=$2 ;;
                --public-url) public_url=$2 ;;
                --output-dir) output_dir=$2 ;;
                --timeout) timeout=$2 ;;
                --har) har=$2 ;;
            esac
            shift 2
            ;;
        --no-browser) browser=false; shift ;;
        --help|-h)
            printf '%s\n' 'Usage: bash diagnose-local.sh [--url URL] [--public-url URL] [--output-dir DIR] [--timeout SECONDS] [--no-browser] [--har FILE]' \
                'Requires Bash, curl, jq. Docker collects optional service health.' \
                'Optional browser checks require Node.js >=22 and Google Chrome/Chromium.' \
                'Uses generated local admin credentials. No module settings are changed.' \
                'The public URL is tested without sending credentials or session cookies.'
            exit 0
            ;;
        *) printf 'Unknown argument: %s\n' "$1" >&2; exit 1 ;;
    esac
done
for tool in curl jq; do
    command -v "$tool" >/dev/null 2>&1 || { printf 'Missing prerequisite: %s\n' "$tool" >&2; exit 1; }
done
[[ "$timeout" =~ ^[1-9][0-9]*$ ]] && ((timeout <= 120)) || { printf 'Timeout must be 1..120 seconds.\n' >&2; exit 1; }
url=${url%/}
public_url=${public_url%/}
if [[ ! "$url" =~ ^http://(localhost|127\.0\.0\.1)(:[0-9]{1,5})?$ ]]; then
    printf 'Authenticated diagnostics accept only a loopback HTTP URL; use --public-url for a tunnel.\n' >&2
    exit 1
fi
if [[ -n "$public_url" ]]; then
    case "$public_url" in https://*) ;; *) printf 'Public URL must use HTTPS.\n' >&2; exit 1 ;; esac
fi
if [[ -n "$har" ]] && ! jq -e '.log.entries | type == "array"' "$har" >/dev/null 2>&1; then
    printf 'HAR must be readable JSON with a log.entries array.\n' >&2
    exit 1
fi
output_dir=${output_dir:-"$PWD/audit-output/loris-$(date -u +%Y%m%dT%H%M%SZ)"}
mkdir -p "$output_dir"
output_dir=$(cd -- "$output_dir" && pwd)
if [[ -e "$output_dir/report.txt" || -e "$output_dir/http.jsonl" || -e "$output_dir/browser.json" ]]; then
    printf 'Report directory already contains diagnostic results; choose a new --output-dir.\n' >&2
    exit 1
fi
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
report="$output_dir/report.txt"
metrics="$output_dir/http.jsonl"
findings="$temporary/findings"
touch "$metrics" "$findings"

finding() {
    printf '[%s] %s\n' "$1" "$2" >> "$findings"
}

probe() {
    local label=$1 path=$2 base=$3 authenticated=$4
    local result status=0
    local args=(--silent --show-error --connect-timeout 5 --max-time "$timeout" --compressed)
    if [[ "$authenticated" == true ]]; then args+=(-b "$temporary/cookies"); fi
    result=$(curl "${args[@]}" -o "$temporary/body" -w \
        '{"status":%{http_code},"dns":%{time_namelookup},"connect":%{time_connect},"ttfb":%{time_starttransfer},"total":%{time_total},"bytes":%{size_download}}' \
        "$base$path" 2>/dev/null) || status=$?
    if ! jq -e . >/dev/null 2>&1 <<< "$result"; then result='{"status":0,"ttfb":0,"total":0,"bytes":0}'; fi
    jq -c --arg label "$label" --arg path "$path" --argjson error "$status" \
        '. + {label:$label,path:$path,curl_error:$error}' <<< "$result" >> "$metrics"
    if [[ "$status" -ne 0 ]]; then
        finding FAIL "$label $path: transport error $status (28 means timeout); request bounded to ${timeout}s."
    elif jq -e '.status >= 400' >/dev/null <<< "$result"; then
        finding FAIL "$label $path: HTTP $(jq -r .status <<< "$result")."
    elif jq -e '.status >= 300' >/dev/null <<< "$result"; then
        finding WARN "$label $path: HTTP redirect; timing is not an authenticated LORIS page result."
    elif grep -Eq 'Fatal error|Uncaught .*Exception|Uncaught Error' "$temporary/body"; then
        finding FAIL "$label $path: PHP error text in the body despite successful HTTP status."
    elif jq -e '.total > 2' >/dev/null <<< "$result"; then
        finding WARN "$label $path: HTTP response exceeded 2 seconds."
    fi
}

printf 'Collecting HTTP timings and service health...\n'
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker ps -a --filter label=com.docker.compose.project=scilus-loris-local \
        --format '{{.Names}} | {{.Status}} | {{.Ports}}' > "$output_dir/services.txt"
    docker stats --no-stream --format '{{.Name}} | CPU {{.CPUPerc}} | memory {{.MemUsage}} | {{.PIDs}} processes' \
        scilus-loris-local-web-1 scilus-loris-local-database-1 > "$output_dir/resources.txt" 2>/dev/null || finding WARN 'Could not collect Docker resource usage.'
    for service in web database; do
        name="scilus-loris-local-$service-1"
        health=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} OOM={{.State.OOMKilled}} restarts={{.RestartCount}}' "$name" 2>/dev/null) || health=unavailable
        printf '%s: %s\n' "$service" "$health" >> "$output_dir/health.txt"
        if [[ "$health" == *unhealthy* || "$health" == *OOM=true* || "$health" != running* ]]; then finding FAIL "$service: $health"; fi
    done
    docker logs --since 15m --tail 1500 scilus-loris-local-web-1 > "$temporary/logs" 2>&1 || true
    error_count=$(grep -Ec 'PHP Fatal|Uncaught|Permission denied|\[.*:error\]' "$temporary/logs" || true)
    printf 'Recent web runtime error lines: %s\n' "$error_count" > "$output_dir/log-summary.txt"
    if [[ "$error_count" -gt 0 ]]; then finding WARN "$error_count recent web error lines; inspect live container logs for details (raw logs omitted for privacy)."; fi
else
    finding WARN 'Docker unavailable; service health and resources were not collected.'
fi

admin_password=
if [[ -r "$state_dir/credentials.env" ]]; then
    while IFS='=' read -r key value; do
        if [[ "$key" == LORIS_FRONTEND_ADMIN_PASSWORD ]]; then admin_password=$value; fi
    done < "$state_dir/credentials.env"
    unset value
fi
authenticated=false
if [[ -n "$admin_password" ]]; then
    status=$(printf '%s' "$admin_password" | jq -Rs '{login:"true",command:"login",username:"admin",password:.}' |
        curl -sS --max-time "$timeout" -c "$temporary/cookies" -H 'Content-Type: application/json' --data-binary @- \
            -o "$temporary/auth" -w '%{http_code}' "$url/login/Authentication" 2>/dev/null) || status=000
    if [[ "$status" == 200 ]] && jq -e . "$temporary/auth" >/dev/null 2>&1; then authenticated=true; fi
fi
unset admin_password
if [[ "$authenticated" != true ]]; then
    finding FAIL 'Local admin authentication failed; protected route/browser checks skipped rather than reporting login redirects as healthy.'
    probe local /login "$url" false
else
    probe local '/module_manager/?format=json' "$url" true
    if jq -e '.Data | type == "array"' "$temporary/body" >/dev/null 2>&1; then
        jq '[.Data[] | {module:.[0],active:.[2]}]' "$temporary/body" > "$output_dir/modules.json"
    else
        finding FAIL 'Module list response is not valid expected JSON.'
    fi
    for path in / /configuration/ /module_manager/ /user_accounts/ /imaging_browser/; do
        probe local "$path" "$url" true
        if [[ "$path" == /module_manager/ || "$path" == /user_accounts/ || "$path" == /imaging_browser/ ]]; then
            probe local "${path}?format=json" "$url" true
            if ! jq -e . "$temporary/body" >/dev/null 2>&1; then finding FAIL "$path JSON response is HTML or invalid JSON; client tables cannot load."; fi
        fi
    done
fi
if [[ -n "$public_url" ]]; then
    for path in /login /module_manager/ /imaging_browser/; do probe tunnel "$path" "$public_url" false; done
    finding INFO 'Tunnel probes are unauthenticated and do not follow redirects; tunnel sign-in pages cannot establish application latency.'
else
    finding INFO 'Tunnel not measured. Pass --public-url with your actual HTTPS forwarding URL to compare transport timings.'
fi

if [[ -n "$har" ]]; then
    jq '[.log.entries[] | {
        url: (.request.url | sub("://[^/@]*@"; "://[redacted]@") | split("?")[0] | split("#")[0]),
        method: .request.method,
        status: .response.status,
        total_ms: .time,
        wait_ms: (.timings.wait // 0),
        blocked_ms: (.timings.blocked // 0),
        receive_ms: (.timings.receive // 0),
        mime: .response.content.mimeType
    }]' "$har" > "$output_dir/har-summary.json"
    while IFS=$'\t' read -r severity problem; do finding "$severity" "$problem"; done < <(
        jq -r '.[] | if .status == 0 or .status >= 400 then
            ["FAIL", ("Your browser: HTTP " + (.status|tostring) + " " + .url)]
            elif .total_ms > 2000 then
            ["WARN", ("Your browser: " + (.total_ms|tostring) + "ms (server wait " + (.wait_ms|tostring) + "ms, blocked " + (.blocked_ms|tostring) + "ms) " + .url)]
            else empty end | @tsv' "$output_dir/har-summary.json"
    )
    finding INFO 'HAR summaries represent the supplied browser session; original headers, cookies, bodies, and query strings are not copied.'
else
    finding INFO 'For browser-specific lag, export a Network HAR while reproducing it and pass --har FILE. Local Chrome does not measure your Firefox/tunnel session.'
fi

if [[ "$browser" == true && "$authenticated" == true ]]; then
    chrome=$(command -v google-chrome || command -v chromium || command -v chromium-browser || true)
    if [[ -n "$chrome" ]] && command -v node >/dev/null 2>&1 && [[ -f "$script_dir/diagnose-browser.mjs" ]]; then
        printf 'Collecting isolated browser timings...\n'
        if ! node "$script_dir/diagnose-browser.mjs" "$url" "$chrome" "$state_dir/credentials.env" "$output_dir/browser.json"; then
            finding WARN 'Browser diagnostics did not finish; see browser.json if available.'
        fi
        if [[ -f "$output_dir/browser.json" ]]; then
            while IFS=$'\t' read -r severity problem; do finding "$severity" "$problem"; done < <(
                jq -r '.pages[]? | select(.verdict != "OK") | .path as $path | .verdict as $severity | .problems[]? | [$severity, ("Browser " + $path + ": " + .)] | @tsv' "$output_dir/browser.json"
            )
        fi
    else
        finding WARN 'Browser checks unavailable (requires Node >=22, Chrome/Chromium, and diagnose-browser.mjs). HTTP timings alone do not prove browser responsiveness.'
    fi
fi

{
    printf 'LORIS DIAGNOSTIC REPORT\nGenerated: %s\nLocal target: %s\n\n' "$(date -u +%FT%TZ)" "$url"
    printf 'SUMMARY: %s failures, %s warnings\n\n' "$(grep -c '^\[FAIL\]' "$findings" || true)" "$(grep -c '^\[WARN\]' "$findings" || true)"
    printf 'PROBLEMS AND LIMITATIONS\n'
    if ! grep -Eq '^\[(FAIL|WARN)\]' "$findings"; then printf '[OK] No problems detected in the measured direct checks.\n'; fi
    cat "$findings"
    printf '\nHTTP TIMINGS (seconds; >2s is flagged)\n'
    printf '%-8s %-40s %5s %8s %8s %9s\n' TARGET PATH HTTP TTFB TOTAL BYTES
    jq -r '[.label,.path,(.status|tostring),(.ttfb|tostring),(.total|tostring),(.bytes|tostring)] | @tsv' "$metrics" |
        while IFS=$'\t' read -r target path status ttfb total bytes; do
            printf '%-8s %-40s %5s %8s %8s %9s\n' "$target" "$path" "$status" "$ttfb" "$total" "$bytes"
        done
    if [[ -f "$output_dir/health.txt" ]]; then printf '\nSERVICE HEALTH\n'; cat "$output_dir/health.txt"; fi
    if [[ -f "$output_dir/resources.txt" ]]; then printf '\nRESOURCES\n'; cat "$output_dir/resources.txt"; fi
    if [[ -f "$output_dir/browser.json" ]]; then
        printf '\nBROWSER RESULTS\n'
        jq -r '.pages[]? | "[\(.verdict)] \(.path): load=\(.load_ms // "timeout")ms, render=\(.render_ms // "unknown")ms, JS errors=\(.errors|length), failed requests=\(.failed|length), pending=\(.pending|length)", (.problems[]? | "  - " + .)' "$output_dir/browser.json"
    fi
    if [[ -f "$output_dir/har-summary.json" ]]; then
        printf '\nYOUR BROWSER: SLOWEST REQUESTS (milliseconds)\n'
        jq -r 'sort_by(.total_ms) | reverse | .[:10][] | "HTTP \(.status) total=\(.total_ms) wait=\(.wait_ms) blocked=\(.blocked_ms) \(.url)"' "$output_dir/har-summary.json"
    fi
    printf '\nHOW TO INTERPRET\nSlow TTFB suggests server/DB delay. Fast HTTP but browser errors or pending requests suggests a frontend/resource issue.\nFast direct checks do not prove the tunnel or your browser session is healthy. Reports contain metadata only, not response bodies/cookies/passwords.\n'
} > "$report"
cat "$report"
printf '\nReport saved to %s\n' "$report"