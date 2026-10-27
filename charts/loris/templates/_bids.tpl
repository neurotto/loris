{{- define "loris.bids.settings" -}}
{{- $settings := . -}}
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
{{- $fields := list "project" "projectAlias" "cohort" "recruitmentTarget" "defaultVisit" "datasetType" "createCandidates" "createSessions" "mode" -}}
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
{{- if not (has $source (list "nfs" "clone")) }}{{ fail "sites[].source must be nfs or clone" }}{{ end -}}
{{- if and .enabled (eq $source "nfs") (empty .server) }}{{ fail (printf "NFS site %s requires server" .name) }}{{ end -}}
{{- if and .enabled (eq $source "clone") (not $.Values.loris.data.persistence.enabled) -}}
{{- fail "Clone-sourced sites require persistent LORIS data" -}}
{{- end -}}
{{- $import := .bidsImport | default dict -}}
{{- range $key, $_ := $import -}}
{{- if not (has $key (concat $fields (list "enabled" "schedule" "timeZone" "suspend" "datasets"))) -}}
{{- fail (printf "Unknown site bidsImport.%s; use name/siteAlias, not centre/centreAlias" $key) -}}
{{- end -}}
{{- end -}}
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
{{- if not (has $key (concat $fields (list "enabled"))) }}{{ fail (printf "Unknown dataset override %s" $key) }}{{ end -}}
{{- end -}}
{{- $all = append $all $override -}}
{{- end -}}
{{- range $settings := $all -}}
{{- include "loris.bids.settings" $settings -}}
{{- end -}}
{{- end -}}
{{- $clones := .Values.siteClones.subPath | default "" -}}
{{- if not (regexMatch "^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$" $clones) }}{{ fail "siteClones.subPath must be a relative path" }}{{ end -}}
{{- if or (has ".." (splitList "/" $clones)) (has "." (splitList "/" $clones)) (eq (index (splitList "/" $clones) 0) "data") -}}
{{- fail "siteClones.subPath must not contain . or .. and must stay outside the LORIS data directory" -}}
{{- end -}}
{{- with .Values.siteClones.cacheURL -}}
{{- if not (regexMatch "^https?://[A-Za-z0-9.-]+(:[0-9]+)?/?$" (toString .)) }}{{ fail "siteClones.cacheURL must be an http(s) base URL without a path" }}{{ end -}}
{{- end -}}
{{- range $field := list "activeDeadlineSeconds" "ttlSecondsAfterFinished" -}}
{{- if not (regexMatch "^[1-9][0-9]*$" (toString (get $common $field))) }}{{ fail (printf "bidsImport.%s must be positive" $field) }}{{ end -}}
{{- end -}}
{{- end -}}
