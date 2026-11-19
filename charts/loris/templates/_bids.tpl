{{- define "loris.bids.settings" -}}
{{- $settings := . -}}
{{- if hasKey $settings "subjectPrefix" -}}
{{- $prefix := $settings.subjectPrefix -}}
{{- if or (not (kindIs "string" $prefix)) (not (regexMatch "^([A-Za-z][A-Za-z0-9]*)?$" (toString $prefix))) (gt (len (toString $prefix)) 254) -}}
{{- fail "subjectPrefix must be an alphanumeric string starting with a letter, at most 254 characters, or empty" -}}
{{- end -}}
{{- end -}}
{{- if hasKey $settings "recruitmentTarget" -}}
{{- $target := $settings.recruitmentTarget -}}
{{- if and (ne (toJson $target) "null") (or (not (regexMatch "^[1-9][0-9]*$" (toString $target))) (kindIs "string" $target)) -}}
{{- fail "recruitmentTarget must be a positive integer or null" -}}
{{- end -}}
{{- end -}}
{{- if and (hasKey $settings "mode") (not (has $settings.mode (list "preflight" "setup" "import"))) }}{{ fail "mode must be preflight, setup or import" }}{{ end -}}
{{- if and (hasKey $settings "datasetType") (not (has $settings.datasetType (list "raw" "derivative"))) }}{{ fail "datasetType must be raw or derivative" }}{{ end -}}
{{- range $flag := list "enabled" "suspend" "createCandidates" "createSessions" -}}
{{- if and (hasKey $settings $flag) (not (kindIs "bool" (get $settings $flag))) }}{{ fail (printf "%s must be boolean" $flag) }}{{ end -}}
{{- end -}}
{{- if hasKey $settings "schedule" -}}
{{- if not (regexMatch "^(@(annually|yearly|monthly|weekly|daily|midnight|hourly)|[^[:space:]]+([[:space:]]+[^[:space:]]+){4})$" $settings.schedule) -}}
{{- fail "schedule must be a five-field cron expression or supported cron shorthand" -}}
{{- end -}}
{{- end -}}
{{- if and (hasKey $settings "timeZone") (empty $settings.timeZone) }}{{ fail "timeZone must not be empty" }}{{ end -}}
{{- range $alias := list "projectAlias" -}}
{{- if and (hasKey $settings $alias) (not (empty (get $settings $alias))) (gt (len (get $settings $alias)) 4) }}{{ fail (printf "%s must contain 1-4 characters" $alias) }}{{ end -}}
{{- end -}}
{{- end -}}

{{- define "loris.bids.validate" -}}
{{- $common := .Values.bidsImport -}}
{{- $fields := list "project" "projectAlias" "cohort" "recruitmentTarget" "defaultVisit" "datasetType" "createCandidates" "createSessions" "mode" "subjectPrefix" -}}
{{- range $key, $_ := $common -}}
{{- if not (has $key (list "enabled" "schedule" "timeZone" "suspend" "activeDeadlineSeconds" "ttlSecondsAfterFinished" "defaults")) -}}
{{- fail (printf "Deprecated/unknown bidsImport.%s: move registration settings to bidsImport.defaults or sites[].bidsImport; use site name/siteAlias instead of centre and automatic dataset run IDs" $key) -}}
{{- end -}}
{{- end -}}
{{- range $key, $_ := $common.defaults -}}
{{- if not (has $key $fields) }}{{ fail (printf "Unknown bidsImport.defaults.%s" $key) }}{{ end -}}
{{- end -}}
{{- include "loris.bids.settings" $common -}}
{{- include "loris.bids.settings" $common.defaults -}}
{{- $names := dict -}}
{{- $aliases := dict -}}
{{- range .Values.sites -}}
{{- include "loris.bids.settings" . -}}
{{- if or (not (regexMatch "^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$" .name)) (hasKey $names .name) -}}
{{- fail "Site names must be unique DNS labels of at most 40 characters" -}}
{{- end -}}
{{- $_ := set $names .name true -}}
{{- $source := include "loris.siteSource" . -}}
{{- if not (has $source (list "nfs" "pvc")) }}{{ fail "sites[].source must be nfs or pvc" }}{{ end -}}
{{- if eq $source "nfs" -}}
{{- if and .enabled (empty .server) }}{{ fail (printf "NFS site %s requires server" .name) }}{{ end -}}
{{- if or (hasKey . "claimName") (hasKey . "subPath") }}{{ fail (printf "NFS site %s must not set claimName or subPath" .name) }}{{ end -}}
{{- else -}}
{{- if and .enabled (not (regexMatch "^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$" (toString (.claimName | default "")))) -}}
{{- fail (printf "PVC site %s requires claimName of an existing claim" .name) -}}
{{- end -}}
{{- with .subPath -}}
{{- $sub := toString . -}}
{{- if or (hasPrefix "/" $sub) (has ".." (splitList "/" $sub)) (contains "$(" $sub) -}}
{{- fail "PVC site subPath must be relative without .. or variable expansion" -}}
{{- end -}}
{{- end -}}
{{- if or (hasKey . "server") (hasKey . "nfsPort") (hasKey . "capacity") }}{{ fail (printf "PVC site %s must not set NFS fields (server, nfsPort, capacity)" .name) }}{{ end -}}
{{- end -}}
{{- $import := .bidsImport | default dict -}}
{{- range $key, $_ := $import -}}
{{- if not (has $key (concat $fields (list "enabled" "schedule" "timeZone" "suspend" "datasets" "symlinkedContent" "updates"))) -}}
{{- fail (printf "Unknown site bidsImport.%s; use name/siteAlias, not centre/centreAlias" $key) -}}
{{- end -}}
{{- end -}}
{{- if and (hasKey $import "symlinkedContent") (not (has $import.symlinkedContent (list "resolve" "identity"))) }}{{ fail "sites[].bidsImport.symlinkedContent must be resolve or identity" }}{{ end -}}
{{- if and (hasKey $import "updates") (not (has $import.updates (list "reject" "additive"))) }}{{ fail "sites[].bidsImport.updates must be reject or additive" }}{{ end -}}
{{- if $import.enabled -}}
{{- if not .enabled }}{{ fail "An import-enabled site must also have its site mount enabled" }}{{ end -}}
{{- if or (not (kindIs "string" .siteAlias)) (not (regexMatch "^.{1,4}$" .siteAlias)) -}}
{{- fail "Import-enabled sites require siteAlias containing 1-4 characters" -}}
{{- end -}}
{{- if hasKey $aliases .siteAlias }}{{ fail "Import-enabled site aliases must be unique" }}{{ end -}}
{{- $_ := set $aliases .siteAlias true -}}
{{- if and $common.enabled (not $.Values.loris.data.persistence.enabled) -}}
{{- fail "bidsImport requires persistent LORIS data shared with the portal" -}}
{{- end -}}
{{- end -}}
{{- $all := list $common.defaults $import -}}
{{- range $folder, $override := $import.datasets | default dict -}}
{{- if or (eq $folder ".") (eq $folder "..") (contains "/" $folder) -}}
{{- fail "Dataset override keys must be immediate folder names" -}}
{{- end -}}
{{- range $key, $_ := $override -}}
{{- if not (has $key (concat $fields (list "enabled" "acceptFingerprint"))) }}{{ fail (printf "Unknown dataset override %s" $key) }}{{ end -}}
{{- end -}}
{{- if hasKey $override "acceptFingerprint" -}}
{{- if not (regexMatch "^[0-9a-f]{64}$" (toString $override.acceptFingerprint)) }}{{ fail "sites[].bidsImport.datasets.*.acceptFingerprint must be a 64-character lowercase hex fingerprint" }}{{ end -}}
{{- if ne ($import.updates | default "reject") "additive" }}{{ fail "sites[].bidsImport.datasets.*.acceptFingerprint requires bidsImport.updates: additive" }}{{ end -}}
{{- end -}}
{{- $all = append $all (omit $override "acceptFingerprint") -}}
{{- end -}}
{{- range $settings := $all -}}
{{- include "loris.bids.settings" $settings -}}
{{- end -}}
{{- end -}}
{{- if hasKey .Values "siteClones" }}{{ fail "siteClones was removed; use sites[].source nfs or pvc (claimName/subPath), with contentProvider.url only for optional provider-neutral on-demand content" }}{{ end -}}
{{- with .Values.contentProvider.url -}}
{{- if not (regexMatch "^https?://[A-Za-z0-9.-]+(:[0-9]+)?/?$" (toString .)) }}{{ fail "contentProvider.url must be an http(s) base URL without a path" }}{{ end -}}
{{- end -}}
{{- range $field := list "activeDeadlineSeconds" "ttlSecondsAfterFinished" -}}
{{- if not (regexMatch "^[1-9][0-9]*$" (toString (get $common $field))) }}{{ fail (printf "bidsImport.%s must be positive" $field) }}{{ end -}}
{{- end -}}
{{- end -}}
