{{/*
Site data sources. Each enabled site appears read-only at /data/loris/data/assembly_bids/<site>:
  source: nfs   (default) an NFS PersistentVolume exported by the site (templates/site-volumes.yaml);
  source: clone a central git-annex/DataLad clone directory on the LORIS data PVC, at
                <siteClones.subPath>/<site>, written only by the external sync job that maintains it.
*/}}
{{- define "loris.siteSource" -}}
{{- .source | default "nfs" -}}
{{- end -}}

{{- define "loris.siteMount" -}}
{{- $site := index . 0 -}}
{{- $root := index . 1 -}}
{{- $name := index . 2 -}}
{{- if eq (include "loris.siteSource" $site) "clone" }}
- name: loris-data
  mountPath: /data/loris/data/assembly_bids/{{ $site.name }}
  subPath: {{ printf "%s/%s" $root.Values.siteClones.subPath $site.name | quote }}
  readOnly: true
{{- else }}
- name: {{ $name }}
  mountPath: /data/loris/data/assembly_bids/{{ $site.name }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "loris.siteVolume" -}}
{{- $site := index . 0 -}}
{{- $name := index . 1 -}}
{{- if ne (include "loris.siteSource" $site) "clone" }}
- name: {{ $name }}
  persistentVolumeClaim:
    claimName: loris-bids-{{ $site.name }}
{{- end }}
{{- end -}}
