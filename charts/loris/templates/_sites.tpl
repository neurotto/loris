{{/*
Each enabled site appears read-only at /data/loris/data/assembly_bids/<site>:
  source: nfs (default) the site's NFS PersistentVolume (templates/site-volumes.yaml);
  source: pvc an existing claim in the release namespace, optionally at subPath.
*/}}
{{- define "loris.siteSource" -}}
{{- .source | default "nfs" -}}
{{- end -}}

{{- define "loris.siteMount" -}}
{{- $site := index . 0 -}}
{{- $name := index . 2 -}}
- name: {{ $name }}
  mountPath: /data/loris/data/assembly_bids/{{ $site.name }}
  {{- if and (eq (include "loris.siteSource" $site) "pvc") $site.subPath }}
  subPath: {{ $site.subPath | quote }}
  {{- end }}
  readOnly: true
{{- end -}}

{{- define "loris.siteVolume" -}}
{{- $site := index . 0 -}}
{{- $name := index . 1 -}}
- name: {{ $name }}
  persistentVolumeClaim:
    {{- if eq (include "loris.siteSource" $site) "pvc" }}
    claimName: {{ $site.claimName | quote }}
    {{- else }}
    claimName: loris-bids-{{ $site.name }}
    {{- end }}
{{- end -}}
