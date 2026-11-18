{{/*
Each enabled site appears read-only at /data/loris/data/assembly_bids/<site>
from the site's NFS PersistentVolume.
*/}}
{{- define "loris.siteSource" -}}
{{- .source | default "nfs" -}}
{{- end -}}

{{- define "loris.siteMount" -}}
{{- $site := index . 0 -}}
{{- $name := index . 2 -}}
- name: {{ $name }}
  mountPath: /data/loris/data/assembly_bids/{{ $site.name }}
  readOnly: true
{{- end -}}

{{- define "loris.siteVolume" -}}
{{- $site := index . 0 -}}
{{- $name := index . 1 -}}
- name: {{ $name }}
  persistentVolumeClaim:
    claimName: loris-bids-{{ $site.name }}
{{- end -}}
