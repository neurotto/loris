{{- define "loris.bootstrap" -}}
- name: prepare-loris-state
  image: {{ include "loris.image" . | quote }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command:
    - sh
    - -ec
    - |
      if [ ! -f /project-state/.loris-seeded ]; then
        cp -a /var/www/loris/project/. /project-state/
        touch /project-state/.loris-seeded
      fi
      mkdir -p /data-state/data/assembly_bids
      {{- range .Values.sites }}
      {{- if .enabled }}
      mkdir -p /data-state/data/assembly_bids/{{ .name }}
      {{- if eq (include "loris.siteSource" .) "clone" }}
      # Clone root for the external sync job; not recursive, annexed content keeps its own modes.
      mkdir -p {{ printf "/data-state/%s/%s" $.Values.siteClones.subPath .name | quote }}
      chown www-data:www-data {{ printf "/data-state/%s" $.Values.siteClones.subPath | quote }} {{ printf "/data-state/%s/%s" $.Values.siteClones.subPath .name | quote }}
      {{- end }}
      {{- end }}
      {{- end }}
      chown -R www-data:www-data /project-state /data-state/data
  volumeMounts:
    - name: project-state
      mountPath: /project-state
    - name: loris-data
      mountPath: /data-state
- name: install-loris
  image: {{ include "loris.image" . | quote }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command: [php, /usr/local/bin/bootstrap-loris.php]
  env:
    - name: LORIS_DB_HOST
      value: loris-mariadb
    - name: LORIS_DB_NAME
      value: {{ .Values.database.name | quote }}
    - name: LORIS_DB_USER
      value: {{ .Values.loris.databaseUser | quote }}
    - name: LORIS_ADMIN_USER
      value: {{ .Values.loris.adminUser | quote }}
    - name: LORIS_PUBLIC_URL
      value: {{ .Values.loris.publicUrl | quote }}
    - name: MARIADB_ROOT_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ .Values.database.secretName }}
          key: {{ .Values.database.rootPasswordKey }}
    - name: LORIS_DB_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ .Values.database.secretName }}
          key: {{ .Values.database.appPasswordKey }}
    - name: LORIS_FRONTEND_ADMIN_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ .Values.database.secretName }}
          key: {{ .Values.database.adminPasswordKey }}
  securityContext:
    runAsNonRoot: true
    runAsUser: 33
    runAsGroup: 33
  volumeMounts:
    - name: project-state
      mountPath: /var/www/loris/project
    - name: loris-data
      mountPath: /data/loris
{{- $root := . }}
{{- $modules := include "loris.requiredModules" . }}
- name: activate-loris-modules
  image: {{ include "loris.image" $root | quote }}
  imagePullPolicy: {{ $root.Values.image.pullPolicy }}
  # Re-enable deployment-required modules on every portal start. Other modules are
  # never touched, so Module Manager choices by LORIS admins survive syncs and restarts.
  command:
    - php
    - -r
    - |
      $names = json_decode(getenv('LORIS_REQUIRED_MODULES'), true, 2, JSON_THROW_ON_ERROR);
      $db = new PDO('mysql:host=' . getenv('LORIS_DB_HOST') . ';dbname=' . getenv('LORIS_DB_NAME') . ';charset=utf8mb4',
          getenv('LORIS_DB_USER'), getenv('LORIS_DB_PASSWORD'), [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
      $marks = implode(',', array_fill(0, count($names), '?'));
      $known = $db->prepare("SELECT Name FROM modules WHERE Name IN ($marks)");
      $known->execute($names);
      $missing = array_diff($names, $known->fetchAll(PDO::FETCH_COLUMN));
      if ($missing) {
          fwrite(STDERR, 'Unknown LORIS modules in required modules: ' . implode(', ', $missing) . "\n");
          exit(1);
      }
      $update = $db->prepare("UPDATE modules SET Active = 'Y' WHERE Active <> 'Y' AND Name IN ($marks)");
      $update->execute($names);
      $inactive = $db->prepare("SELECT COUNT(*) FROM modules WHERE Active <> 'Y' AND Name IN ($marks)");
      $inactive->execute($names);
      if ((int)$inactive->fetchColumn() !== 0) {
          fwrite(STDERR, "LORIS module activation verification failed\n");
          exit(1);
      }
      printf("LORIS modules active: %s (re-enabled %d)\n", implode(', ', $names), $update->rowCount());
  env:
    - name: LORIS_REQUIRED_MODULES
      value: {{ $modules | quote }}
    - name: LORIS_DB_HOST
      value: loris-mariadb
    - name: LORIS_DB_NAME
      value: {{ $root.Values.database.name | quote }}
    - name: LORIS_DB_USER
      value: {{ $root.Values.loris.databaseUser | quote }}
    - name: LORIS_DB_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ $root.Values.database.secretName }}
          key: {{ $root.Values.database.appPasswordKey }}
  securityContext:
    runAsNonRoot: true
    runAsUser: 33
    runAsGroup: 33
    allowPrivilegeEscalation: false
    capabilities:
      drop: [ALL]
{{- end }}

{{- define "loris.requiredModules" -}}
{{- $extra := .Values.loris.additionalRequiredModules | default list -}}
{{- if not (kindIs "slice" $extra) }}{{ fail "loris.additionalRequiredModules must be a list of LORIS module names" }}{{ end -}}
{{- range $extra -}}
{{- if not (and (kindIs "string" .) (regexMatch "^[a-z][a-z0-9_]*$" .)) }}{{ fail (printf "loris.additionalRequiredModules entry %v must be a LORIS module name" .) }}{{ end -}}
{{- end -}}
{{- concat (list "api" "imaging_browser" "brainbrowser") $extra | uniq | toJson -}}
{{- end }}
