{{- define "loris.image" -}}
{{- $image := printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- with .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" .) -}}
{{- fail "image.digest must be a sha256 digest with 64 lowercase hexadecimal characters" -}}
{{- end -}}
{{- printf "%s@%s" $image . -}}
{{- else -}}
{{- $image -}}
{{- end -}}
{{- end -}}
