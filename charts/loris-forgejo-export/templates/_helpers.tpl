{{- define "loris-forgejo-export.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "loris-forgejo-export.labels" -}}
app.kubernetes.io/name: loris-forgejo-export
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* Usage: include "loris-forgejo-export.selectorLabels" (list $ "sync") */}}
{{- define "loris-forgejo-export.selectorLabels" -}}
{{- $root := index . 0 -}}
app.kubernetes.io/name: loris-forgejo-export
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{- define "loris-forgejo-export.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
{{- end -}}

{{- define "loris-forgejo-export.botUser" -}}
{{- .Values.forgejo.botUser | default (printf "%sloris-export" .Values.forgejo.organizationPrefix) -}}
{{- end -}}

{{- define "loris-forgejo-export.webhookSecret" -}}
{{- .Values.triggers.webhook.existingSecret | default (printf "%s-webhook" (include "loris-forgejo-export.fullname" .)) -}}
{{- end -}}

{{/* Sites with forgejo.enabled, as a JSON list. */}}
{{- define "loris-forgejo-export.managedSites" -}}
{{- $sites := list -}}
{{- range .Values.sites -}}
{{- if and (kindIs "map" .) (kindIs "map" .forgejo) .forgejo.enabled -}}
{{- $sites = append $sites . -}}
{{- end -}}
{{- end -}}
{{- toJson $sites -}}
{{- end -}}

{{- define "loris-forgejo-export.registry" -}}
{{- $f := .Values.forgejo -}}
{{- $settings := dict
  "organizationPrefix" $f.organizationPrefix
  "botUser" (include "loris-forgejo-export.botUser" .)
  "publicationBotSuffix" $f.publicationBotSuffix
  "publicationTeam" $f.publicationTeam
  "exportTeam" $f.exportTeam
  "workerRepository" $f.worker.repository
  "runnerLabel" $f.worker.runnerLabel
  "requestCommand" $f.worker.requestCommand
  "variablePrefix" $f.worker.variablePrefix
  "dataClaim" .Values.loris.dataClaim
  "clonesSubPath" .Values.loris.clonesSubPath
  "importCronJobPrefix" .Values.loris.importCronJobPrefix -}}
{{- toPrettyJson (dict "settings" $settings "bidsImport" (dict "enabled" (.Values.bidsImport.enabled | default false)) "sites" .Values.sites) -}}
{{- end -}}

{{/*
Chart-level validation. The full site registry is validated again by
files/registry.py when every pod starts.
*/}}
{{- define "loris-forgejo-export.validate" -}}
{{- $v := .Values -}}
{{- $f := $v.forgejo -}}
{{- if not (kindIs "slice" $v.sites) -}}
  {{- fail "sites must be a list" -}}
{{- end -}}
{{- if not (regexMatch "^https?://[^/\\s]+(/.*)?$" ($f.url | default "")) -}}
  {{- fail "forgejo.url must be the in-cluster http(s) URL of Forgejo" -}}
{{- end -}}
{{- $scheme := ternary "https?" "https" ($f.allowInsecurePublicUrl | default false) -}}
{{- if not (regexMatch (printf "^%s://[^/\\s]+(/.*)?$" $scheme) ($f.publicUrl | default "")) -}}
  {{- fail "forgejo.publicUrl must be the public https URL that sites reach Forgejo at (http needs forgejo.allowInsecurePublicUrl)" -}}
{{- end -}}
{{- if and $f.publicIP (not (regexMatch "^[0-9a-fA-F.:]+$" $f.publicIP)) -}}
  {{- fail "forgejo.publicIP must be an IP address" -}}
{{- end -}}
{{- if and $f.annexUrl (not (regexMatch "^annex\\+https?://.+/git-annex-p2phttp$" $f.annexUrl)) -}}
  {{- fail "forgejo.annexUrl must look like annex+http(s)://host/git-annex-p2phttp" -}}
{{- end -}}
{{- if not $f.adminSecret.name -}}
  {{- fail "forgejo.adminSecret.name is required" -}}
{{- end -}}
{{- if not (regexMatch "^([a-z0-9][a-z0-9-]*-)?$" ($f.organizationPrefix | default "")) -}}
  {{- fail "forgejo.organizationPrefix must be empty or lowercase letters, digits and hyphens ending with '-'" -}}
{{- end -}}
{{- if not (regexMatch "^[A-Z][A-Z0-9_]*_$" $f.worker.variablePrefix) -}}
  {{- fail "forgejo.worker.variablePrefix must be uppercase and end with '_'" -}}
{{- end -}}
{{- if not (regexMatch "^[a-z0-9][a-z0-9._-]*$" $f.worker.requestCommand) -}}
  {{- fail "forgejo.worker.requestCommand must be a lowercase command name" -}}
{{- end -}}
{{- if and $v.triggers.webhook.enabled $v.triggers.argoEvents.enabled -}}
  {{- fail "triggers.webhook and triggers.argoEvents are mutually exclusive" -}}
{{- end -}}
{{- if and $v.triggers.argoEvents.enabled (or (not $v.triggers.argoEvents.eventSourceName) (not $v.triggers.argoEvents.eventName) (not (regexMatch "^https?://" ($v.triggers.argoEvents.webhookUrl | default "")))) -}}
  {{- fail "triggers.argoEvents requires eventSourceName, eventName and an http(s) webhookUrl" -}}
{{- end -}}
{{- if eq (lower $f.publicationTeam) (lower $f.exportTeam) -}}
  {{- fail "forgejo.publicationTeam and forgejo.exportTeam must differ" -}}
{{- end -}}
{{- $limit := sub (sub 40 (len ($f.organizationPrefix | default ""))) (len $f.publicationBotSuffix) -}}
{{- $names := dict -}}
{{- $accounts := dict (lower (include "loris-forgejo-export.botUser" .)) "forgejo.botUser" -}}
{{- range $index, $site := $v.sites -}}
  {{- if not (and (kindIs "map" $site) (kindIs "string" $site.name) $site.name) -}}
    {{- fail (printf "sites[%d].name is required" $index) -}}
  {{- end -}}
  {{- if hasKey $names $site.name -}}
    {{- fail (printf "sites[%d].name %q is duplicated" $index $site.name) -}}
  {{- end -}}
  {{- $_ := set $names $site.name true -}}
  {{- if and (kindIs "map" $site.forgejo) $site.forgejo.enabled -}}
    {{- $where := printf "site %q" $site.name -}}
    {{- if or (gt (len $site.name) (int $limit)) (not (regexMatch "^[a-z0-9]([a-z0-9-]*[a-z0-9])?$" $site.name)) -}}
      {{- fail (printf "%s: name must be a lowercase DNS label of at most %d characters" $where (int $limit)) -}}
    {{- end -}}
    {{- $org := printf "%s%s" ($f.organizationPrefix | default "") $site.name -}}
    {{- range $account := list $org (printf "%s%s" $org $f.publicationBotSuffix) -}}
      {{- if hasKey $accounts (lower $account) -}}
        {{- fail (printf "%s: Forgejo account %q collides with %s" $where $account (get $accounts (lower $account))) -}}
      {{- end -}}
      {{- $_ := set $accounts (lower $account) (printf "site %q" $site.name) -}}
    {{- end -}}
    {{- if ne ($site.source | default "") "pvc" -}}
      {{- fail (printf "%s: Forgejo sites require source pvc" $where) -}}
    {{- end -}}
    {{- if or (hasKey $site "server") (hasKey $site "nfsPort") -}}
      {{- fail (printf "%s: Forgejo sites must not set server or nfsPort" $where) -}}
    {{- end -}}
    {{- if ne ($site.claimName | default "") $v.loris.dataClaim -}}
      {{- fail (printf "%s: claimName must be %q" $where $v.loris.dataClaim) -}}
    {{- end -}}
    {{- $subPath := printf "%s/%s" $v.loris.clonesSubPath $site.name -}}
    {{- if ne ($site.subPath | default "") $subPath -}}
      {{- fail (printf "%s: subPath must be %q" $where $subPath) -}}
    {{- end -}}
    {{- $datasets := $site.forgejo.datasets | default list -}}
    {{- if not (kindIs "slice" $datasets) -}}
      {{- fail (printf "%s: forgejo.datasets must be a list" $where) -}}
    {{- end -}}
    {{- $seen := dict -}}
    {{- range $datasets -}}
      {{- if or (not (kindIs "string" .)) (not (regexMatch "^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$" .)) (regexMatch "(?i)\\.(git|wiki|rss|atom)$" .) (eq (lower .) (lower $f.worker.repository)) -}}
        {{- fail (printf "%s: forgejo.datasets entry %v is not a valid repository/folder name" $where .) -}}
      {{- end -}}
      {{- if hasKey $seen (lower .) -}}
        {{- fail (printf "%s: forgejo.datasets entry %q is duplicated" $where .) -}}
      {{- end -}}
      {{- $_ := set $seen (lower .) true -}}
    {{- end -}}
    {{- $imports := $site.bidsImport | default dict -}}
    {{- if and (not $datasets) (or $site.enabled $imports.enabled) -}}
      {{- fail (printf "%s: sites without datasets must remain disabled and nonimporting" $where) -}}
    {{- end -}}
    {{- if and $v.bidsImport.enabled $site.enabled $imports.enabled -}}
      {{- if or (ne ($imports.symlinkedContent | default "") "identity") (ne ($imports.updates | default "") "additive") -}}
        {{- fail (printf "%s: importing Forgejo sites require bidsImport.symlinkedContent identity and bidsImport.updates additive" $where) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{- define "loris-forgejo-export.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .Values.loris.runAsUser }}
runAsGroup: {{ .Values.loris.runAsGroup }}
fsGroup: {{ .Values.loris.runAsGroup }}
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "loris-forgejo-export.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end -}}

{{- define "loris-forgejo-export.scheduling" -}}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* Environment shared by every component. */}}
{{- define "loris-forgejo-export.commonEnv" -}}
- name: LFX_REGISTRY
  value: /etc/loris-forgejo-export/registry.json
- name: LFX_FORGEJO_URL
  value: {{ .Values.forgejo.url | quote }}
- name: LFX_FORGEJO_PUBLIC_URL
  value: {{ .Values.forgejo.publicUrl | quote }}
- name: HOME
  value: /tmp
{{- end -}}

{{/*
Bot credentials for sync and the provider. The reconciler creates the Secret
after install, so it is mounted (optional) and read from file on every use.
*/}}
{{- define "loris-forgejo-export.botVolumeMount" -}}
- name: bot
  mountPath: /var/run/secrets/loris-forgejo-export/bot
  readOnly: true
{{- end -}}

{{- define "loris-forgejo-export.botVolume" -}}
- name: bot
  secret:
    secretName: {{ include "loris-forgejo-export.fullname" . }}-bot
    optional: true
    defaultMode: 0440
{{- end -}}

{{- define "loris-forgejo-export.registryVolumeMount" -}}
- name: registry
  mountPath: /etc/loris-forgejo-export
  readOnly: true
{{- end -}}

{{- define "loris-forgejo-export.registryVolume" -}}
- name: registry
  configMap:
    name: {{ include "loris-forgejo-export.fullname" . }}-registry
{{- end -}}

{{/*
Prints "true" when at least one site has forgejo.enabled, after validating the
values. Every template is gated on it, so default values render nothing.
*/}}
{{- define "loris-forgejo-export.enabled" -}}
{{- if include "loris-forgejo-export.managedSites" . | fromJsonArray -}}
{{- include "loris-forgejo-export.validate" . -}}
true
{{- end -}}
{{- end -}}

{{/* Managed sites whose LORIS discovery CronJob sync may start, as a JSON list of names. */}}
{{- define "loris-forgejo-export.importingSites" -}}
{{- $names := list -}}
{{- range include "loris-forgejo-export.managedSites" . | fromJsonArray -}}
{{- if and $.Values.bidsImport.enabled .enabled (kindIs "map" .bidsImport) .bidsImport.enabled -}}
{{- $names = append $names .name -}}
{{- end -}}
{{- end -}}
{{- toJson $names -}}
{{- end -}}

{{/* Anchored regex of the managed <org>/<dataset> repository names. */}}
{{- define "loris-forgejo-export.repositoryRegex" -}}
{{- $names := list -}}
{{- range include "loris-forgejo-export.managedSites" . | fromJsonArray -}}
{{- $org := printf "%s%s" ($.Values.forgejo.organizationPrefix | default "") .name -}}
{{- range .forgejo.datasets | default list -}}
{{- $names = append $names (regexQuoteMeta (printf "%s/%s" $org .)) -}}
{{- end -}}
{{- end -}}
{{- printf "^(%s)$" (join "|" $names) -}}
{{- end -}}

{{/* Org webhook target registered by the reconciler; empty in scheduled-only mode. */}}
{{- define "loris-forgejo-export.webhookUrl" -}}
{{- if .Values.triggers.webhook.enabled -}}
{{- printf "http://%s-webhook.%s.svc.cluster.local:8080/forgejo" (include "loris-forgejo-export.fullname" .) .Release.Namespace -}}
{{- else if .Values.triggers.argoEvents.enabled -}}
{{- .Values.triggers.argoEvents.webhookUrl -}}
{{- end -}}
{{- end -}}

{{/* ConfigMap data of the scripts ConfigMap: one key per files/*.py. */}}
{{- define "loris-forgejo-export.scriptsData" -}}
{{- $data := dict -}}
{{- range $path, $_ := .Files.Glob "files/*.py" -}}
{{- $_ := set $data (base $path) ($.Files.Get $path) -}}
{{- end -}}
{{- toJson $data -}}
{{- end -}}

{{/*
ConfigMap data of the worker-template ConfigMap: repository path -> content,
with '/' encoded as '__'. forgejo.worker.templateFiles fully replaces the
bundled files/worker-template/**.
*/}}
{{- define "loris-forgejo-export.workerTemplateData" -}}
{{- $files := dict -}}
{{- if .Values.forgejo.worker.templateFiles -}}
{{- range $path, $content := .Values.forgejo.worker.templateFiles -}}
{{- $_ := set $files $path (toString $content) -}}
{{- end -}}
{{- else -}}
{{- range $path, $_ := .Files.Glob "files/worker-template/**" -}}
{{- $_ := set $files (trimPrefix "files/worker-template/" $path) ($.Files.Get $path) -}}
{{- end -}}
{{- end -}}
{{- $data := dict -}}
{{- range $path, $content := $files -}}
{{- $key := replace "/" "__" (trimPrefix "/" $path) -}}
{{- if or (not (regexMatch "^[-._a-zA-Z0-9]+$" $key)) (eq $key ".") (eq $key "..") (hasPrefix ".." $key) (gt (len $key) 253) -}}
{{- fail (printf "worker template path %q is not a valid repository file path" $path) -}}
{{- end -}}
{{- $_ := set $data $key $content -}}
{{- end -}}
{{- toJson $data -}}
{{- end -}}

{{- define "loris-forgejo-export.checksumAnnotations" -}}
checksum/scripts: {{ include "loris-forgejo-export.scriptsData" . | sha256sum }}
checksum/registry: {{ include "loris-forgejo-export.registry" . | sha256sum }}
{{- end -}}

{{/* Usage: include "loris-forgejo-export.podLabels" (list $ "sync") */}}
{{- define "loris-forgejo-export.podLabels" -}}
{{ include "loris-forgejo-export.labels" (index . 0) }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{- define "loris-forgejo-export.scriptsVolumeMount" -}}
- name: scripts
  mountPath: /opt/loris-forgejo-export
  readOnly: true
{{- end -}}

{{- define "loris-forgejo-export.scriptsVolume" -}}
- name: scripts
  configMap:
    name: {{ include "loris-forgejo-export.fullname" . }}-scripts
    defaultMode: 0444
{{- end -}}

{{- define "loris-forgejo-export.tmpVolumeMount" -}}
- name: tmp
  mountPath: /tmp
{{- end -}}

{{- define "loris-forgejo-export.tmpVolume" -}}
- name: tmp
  emptyDir: {}
{{- end -}}

{{- define "loris-forgejo-export.clonesVolumeMount" -}}
- name: data
  mountPath: /clones
  subPath: {{ .Values.loris.clonesSubPath }}
{{- end -}}

{{- define "loris-forgejo-export.chunksVolumeMount" -}}
- name: data
  mountPath: /provider-chunks
  subPath: {{ .Values.loris.chunksSubPath }}
{{- end -}}

{{- define "loris-forgejo-export.dataVolume" -}}
- name: data
  persistentVolumeClaim:
    claimName: {{ .Values.loris.dataClaim }}
{{- end -}}

{{/* Common container settings. Usage: include "loris-forgejo-export.container" (list $ "sync") */}}
{{- define "loris-forgejo-export.container" -}}
{{- $root := index . 0 -}}
name: {{ index . 1 }}
image: {{ include "loris-forgejo-export.image" $root }}
imagePullPolicy: {{ $root.Values.image.pullPolicy }}
command: [python3, /opt/loris-forgejo-export/{{ index . 1 }}.py]
securityContext:
  {{- include "loris-forgejo-export.containerSecurityContext" $root | nindent 2 }}
{{- end -}}

{{/*
Pod spec of sync Jobs, shared by the sync CronJob and the Argo Sensor. The
first env entry is LFX_SYNC_REPOSITORY, which push triggers override.
*/}}
{{- define "loris-forgejo-export.syncPodSpec" -}}
serviceAccountName: {{ include "loris-forgejo-export.fullname" . }}-sync
restartPolicy: Never
securityContext:
  {{- include "loris-forgejo-export.podSecurityContext" . | nindent 2 }}
{{- include "loris-forgejo-export.scheduling" . }}
containers:
  - {{ include "loris-forgejo-export.container" (list . "sync") | nindent 4 | trim }}
    env:
      - name: LFX_SYNC_REPOSITORY
        value: ""
      {{- include "loris-forgejo-export.commonEnv" . | nindent 6 }}
      - name: LFX_NAMESPACE
        valueFrom:
          fieldRef:
            fieldPath: metadata.namespace
      - name: LFX_FORGEJO_PUBLIC_IP
        value: {{ .Values.forgejo.publicIP | default "" | quote }}
      - name: LFX_WAIT_SECONDS
        value: "300"
      - name: LFX_ANNEX_URL
        value: {{ .Values.forgejo.annexUrl | default "" | quote }}
      - name: LFX_STAGE_TIMEOUT
        value: {{ .Values.sync.stageTimeout | quote }}
      - name: LFX_IMPORT_TIMEOUT
        value: {{ .Values.sync.importTimeout | quote }}
      - name: LFX_POLL_SECONDS
        value: "5"
    resources:
      {{- toYaml .Values.sync.resources | nindent 6 }}
    volumeMounts:
      {{- include "loris-forgejo-export.scriptsVolumeMount" . | nindent 6 }}
      {{- include "loris-forgejo-export.registryVolumeMount" . | nindent 6 }}
      {{- include "loris-forgejo-export.botVolumeMount" . | nindent 6 }}
      {{- include "loris-forgejo-export.clonesVolumeMount" . | nindent 6 }}
      {{- include "loris-forgejo-export.tmpVolumeMount" . | nindent 6 }}
volumes:
  {{- include "loris-forgejo-export.scriptsVolume" . | nindent 2 }}
  {{- include "loris-forgejo-export.registryVolume" . | nindent 2 }}
  {{- include "loris-forgejo-export.botVolume" . | nindent 2 }}
  {{- include "loris-forgejo-export.dataVolume" . | nindent 2 }}
  {{- include "loris-forgejo-export.tmpVolume" . | nindent 2 }}
{{- end -}}

{{/* Pod template of sync Jobs. */}}
{{- define "loris-forgejo-export.syncPodTemplate" -}}
metadata:
  labels:
    {{- include "loris-forgejo-export.podLabels" (list . "sync") | nindent 4 }}
  annotations:
    {{- include "loris-forgejo-export.checksumAnnotations" . | nindent 4 }}
spec:
  {{- include "loris-forgejo-export.syncPodSpec" . | nindent 2 }}
{{- end -}}

{{/* Job spec of sync Jobs, shared by the sync CronJob and the Argo Sensor. */}}
{{- define "loris-forgejo-export.syncJobSpec" -}}
backoffLimit: 0
activeDeadlineSeconds: {{ .Values.sync.activeDeadlineSeconds }}
ttlSecondsAfterFinished: {{ .Values.sync.ttlSecondsAfterFinished }}
template:
  {{- include "loris-forgejo-export.syncPodTemplate" . | nindent 2 }}
{{- end -}}

{{/* Job spec of reconciler runs, shared by the CronJob and the revision Job. */}}
{{- define "loris-forgejo-export.reconcileJobSpec" -}}
{{- $fullname := include "loris-forgejo-export.fullname" . -}}
{{- $workerTemplate := include "loris-forgejo-export.workerTemplateData" . | fromJson -}}
backoffLimit: 0
ttlSecondsAfterFinished: 86400
template:
  metadata:
    labels:
      {{- include "loris-forgejo-export.podLabels" (list . "reconciler") | nindent 6 }}
    annotations:
      {{- include "loris-forgejo-export.checksumAnnotations" . | nindent 6 }}
      checksum/worker-template: {{ toJson $workerTemplate | sha256sum }}
  spec:
    serviceAccountName: {{ $fullname }}-reconciler
    restartPolicy: Never
    securityContext:
      {{- include "loris-forgejo-export.podSecurityContext" . | nindent 6 }}
    {{- with include "loris-forgejo-export.scheduling" . }}{{ . | indent 4 }}{{- end }}
    containers:
      - {{ include "loris-forgejo-export.container" (list . "reconcile") | nindent 8 | trim }}
        env:
          {{- include "loris-forgejo-export.commonEnv" . | nindent 10 }}
          - name: LFX_NAMESPACE
            valueFrom:
              fieldRef:
                fieldPath: metadata.namespace
          - name: LFX_FULLNAME
            value: {{ $fullname | quote }}
          - name: LFX_RELEASE
            value: {{ .Release.Name | quote }}
          - name: LFX_ADMIN_USER
            valueFrom:
              secretKeyRef:
                name: {{ .Values.forgejo.adminSecret.name | quote }}
                key: {{ .Values.forgejo.adminSecret.usernameKey | quote }}
          - name: LFX_ADMIN_PASS
            valueFrom:
              secretKeyRef:
                name: {{ .Values.forgejo.adminSecret.name | quote }}
                key: {{ .Values.forgejo.adminSecret.passwordKey | quote }}
          - name: LFX_FORGEJO_PUBLIC_IP
            value: {{ .Values.forgejo.publicIP | default "" | quote }}
          - name: LFX_WEBHOOK_URL
            value: {{ include "loris-forgejo-export.webhookUrl" . | quote }}
          {{- if .Values.triggers.webhook.enabled }}
          - name: LFX_WEBHOOK_SECRET
            valueFrom:
              secretKeyRef:
                name: {{ include "loris-forgejo-export.webhookSecret" . | quote }}
                key: secret
          {{- end }}
          - name: LFX_WORKER_TEMPLATE
            value: /etc/loris-forgejo-export/worker-template
          - name: LFX_ENROLLMENT
            value: {{ ternary "1" "0" (.Values.reconciler.enrollment.enabled | default false) | quote }}
          - name: LFX_DRY_RUN
            value: "0"
          - name: LFX_WAIT_SECONDS
            value: "300"
        resources:
          {{- toYaml .Values.reconciler.resources | nindent 10 }}
        volumeMounts:
          {{- include "loris-forgejo-export.scriptsVolumeMount" . | nindent 10 }}
          {{- include "loris-forgejo-export.registryVolumeMount" . | nindent 10 }}
          {{- include "loris-forgejo-export.tmpVolumeMount" . | nindent 10 }}
    volumes:
      {{- include "loris-forgejo-export.scriptsVolume" . | nindent 6 }}
      {{- /* registry.json and worker-template/ share /etc/loris-forgejo-export. */}}
      - name: registry
        projected:
          sources:
            - configMap:
                name: {{ $fullname }}-registry
            {{- if $workerTemplate }}
            - configMap:
                name: {{ $fullname }}-worker-template
                items:
                  {{- range $key, $_ := $workerTemplate }}
                  - key: {{ $key | quote }}
                    path: {{ printf "worker-template/%s" $key | quote }}
                  {{- end }}
            {{- end }}
      {{- include "loris-forgejo-export.tmpVolume" . | nindent 6 }}
{{- end -}}
