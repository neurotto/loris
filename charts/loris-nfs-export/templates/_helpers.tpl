{{/*
Validate the exporter configuration before emitting any resources. With neither
dataPath nor client CIDRs set, the chart remains disabled by default.
*/}}
{{- define "loris-nfs-export.validate" -}}
{{- $v := .Values -}}
{{- if not (kindIs "string" $v.siteName) -}}
  {{- fail "siteName must be a DNS label string" -}}
{{- end -}}
{{- if or (gt (len $v.siteName) 63) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $v.siteName)) -}}
  {{- fail "siteName must be a lowercase DNS label of 1-63 characters" -}}
{{- end -}}
{{- $portKind := kindOf $v.nfsPort -}}
{{- if and (ne $portKind "int") (ne $portKind "int64") (ne $portKind "float64") (ne $portKind "string") -}}
  {{- fail "nfsPort must be an integer from 1024 to 65535" -}}
{{- end -}}
{{- if not (regexMatch "^[0-9]+$" (printf "%v" $v.nfsPort)) -}}
  {{- fail "nfsPort must be an integer from 1024 to 65535" -}}
{{- end -}}
{{- $port := int64 $v.nfsPort -}}
{{- if or (lt $port 1024) (gt $port 65535) -}}
  {{- fail "nfsPort must be an integer from 1024 to 65535" -}}
{{- end -}}
{{- $uidKind := kindOf $v.exportUid -}}
{{- if and (ne $uidKind "int") (ne $uidKind "int64") (ne $uidKind "float64") (ne $uidKind "string") -}}
  {{- fail "exportUid must be a nonzero integer from 1 to 2147483647" -}}
{{- end -}}
{{- if not (regexMatch "^[0-9]+$" (printf "%v" $v.exportUid)) -}}
  {{- fail "exportUid must be a nonzero integer from 1 to 2147483647" -}}
{{- end -}}
{{- $uid := int64 $v.exportUid -}}
{{- if or (lt $uid 1) (gt $uid 2147483647) -}}
  {{- fail "exportUid must be a nonzero integer from 1 to 2147483647" -}}
{{- end -}}
{{- if not (kindIs "string" $v.dataPath) -}}
  {{- fail "dataPath must be an absolute path string" -}}
{{- end -}}
{{- if and $v.dataPath (or (not (hasPrefix "/" $v.dataPath)) (eq $v.dataPath "/") (regexMatch "(^|/)\\.\\.(/|$)" $v.dataPath)) -}}
  {{- fail "dataPath must be an absolute, non-root path without '..' components" -}}
{{- end -}}
{{- if not (kindIs "slice" $v.allowedClientCIDRs) -}}
  {{- fail "allowedClientCIDRs must be a list of CIDR strings" -}}
{{- end -}}
{{- range $index, $cidr := $v.allowedClientCIDRs -}}
  {{- if not (kindIs "string" $cidr) -}}
    {{- fail (printf "allowedClientCIDRs[%d] must be a CIDR string" $index) -}}
  {{- end -}}
  {{- $parts := splitList "/" $cidr -}}
  {{- if ne (len $parts) 2 -}}
    {{- fail (printf "allowedClientCIDRs[%d] must be a valid IPv4 or IPv6 CIDR" $index) -}}
  {{- end -}}
  {{- $address := index $parts 0 -}}
  {{- $prefix := index $parts 1 -}}
  {{- if not (regexMatch "^(0|[1-9][0-9]{0,2})$" $prefix) -}}
    {{- fail (printf "allowedClientCIDRs[%d] must have a valid CIDR prefix length" $index) -}}
  {{- end -}}
  {{- $maxPrefix := 32 -}}
  {{- if contains ":" $address -}}
    {{- $maxPrefix = 128 -}}
    {{- $ipv6Address := $address -}}
    {{- if contains "." $address -}}
      {{- if not (regexMatch "^([0-9A-Fa-f]*:)+((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$" $address) -}}
        {{- fail (printf "allowedClientCIDRs[%d] must be a valid IPv4 or IPv6 CIDR" $index) -}}
      {{- end -}}
      {{- $ipv6Address = regexReplaceAll "((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$" $address "0:0" -}}
    {{- end -}}
    {{- if or (not (regexMatch "^[0-9A-Fa-f:]+$" $ipv6Address)) (contains ":::" $ipv6Address) -}}
      {{- fail (printf "allowedClientCIDRs[%d] must be a valid IPv4 or IPv6 CIDR" $index) -}}
    {{- end -}}
    {{- $doubleColonParts := splitList "::" $ipv6Address -}}
    {{- if gt (len $doubleColonParts) 2 -}}
      {{- fail (printf "allowedClientCIDRs[%d] must be a valid IPv4 or IPv6 CIDR" $index) -}}
    {{- end -}}
    {{- $groups := 0 -}}
    {{- range $side := $doubleColonParts -}}
      {{- if $side -}}
        {{- range $group := splitList ":" $side -}}
          {{- if not (regexMatch "^[0-9A-Fa-f]{1,4}$" $group) -}}
            {{- fail (printf "allowedClientCIDRs[%d] must be a valid IPv4 or IPv6 CIDR" $index) -}}
          {{- end -}}
          {{- $groups = add $groups 1 -}}
        {{- end -}}
      {{- end -}}
    {{- end -}}
    {{- if and (eq (len $doubleColonParts) 1) (ne $groups 8) -}}
      {{- fail (printf "allowedClientCIDRs[%d] must be a valid IPv4 or IPv6 CIDR" $index) -}}
    {{- end -}}
    {{- if and (eq (len $doubleColonParts) 2) (ge $groups 8) -}}
      {{- fail (printf "allowedClientCIDRs[%d] must be a valid IPv4 or IPv6 CIDR" $index) -}}
    {{- end -}}
  {{- else if not (regexMatch "^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$" $address) -}}
    {{- fail (printf "allowedClientCIDRs[%d] must be a valid IPv4 or IPv6 CIDR" $index) -}}
  {{- end -}}
  {{- if gt (int $prefix) $maxPrefix -}}
    {{- fail (printf "allowedClientCIDRs[%d] prefix length exceeds the address family range" $index) -}}
  {{- end -}}
{{- end -}}
{{- $pathSet := ne $v.dataPath "" -}}
{{- $cidrsSet := gt (len $v.allowedClientCIDRs) 0 -}}
{{- if or $pathSet $cidrsSet -}}
  {{- if not (and $pathSet $cidrsSet) -}}
    {{- fail "exporter is enabled only when both dataPath and allowedClientCIDRs are provided" -}}
  {{- end -}}
  {{- if not (kindIs "string" $v.nodeHostname) -}}
    {{- fail "nodeHostname must be a nonempty hostname string when the exporter is enabled" -}}
  {{- end -}}
  {{- if or (eq $v.nodeHostname "") (gt (len $v.nodeHostname) 63) (not (regexMatch "^[A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9])?$" $v.nodeHostname)) -}}
    {{- fail "nodeHostname must be a nonempty Kubernetes hostname label of at most 63 characters when the exporter is enabled" -}}
  {{- end -}}
  {{- $gidKind := kindOf $v.exportGid -}}
  {{- if and (ne $gidKind "int") (ne $gidKind "int64") (ne $gidKind "float64") (ne $gidKind "string") -}}
    {{- fail "exportGid must be a nonzero integer from 1 to 2147483647 when the exporter is enabled" -}}
  {{- end -}}
  {{- if not (regexMatch "^[0-9]+$" (printf "%v" $v.exportGid)) -}}
    {{- fail "exportGid must be a nonzero integer from 1 to 2147483647 when the exporter is enabled" -}}
  {{- end -}}
  {{- $gid := int64 $v.exportGid -}}
  {{- if or (lt $gid 1) (gt $gid 2147483647) -}}
    {{- fail "exportGid must be a nonzero integer from 1 to 2147483647 when the exporter is enabled" -}}
  {{- end -}}
{{- else if ne (printf "%v" $v.exportGid) "" -}}
  {{- $gidKind := kindOf $v.exportGid -}}
  {{- if and (ne $gidKind "int") (ne $gidKind "int64") (ne $gidKind "float64") (ne $gidKind "string") -}}
    {{- fail "exportGid must be empty or a nonzero integer from 1 to 2147483647" -}}
  {{- end -}}
  {{- if not (regexMatch "^[0-9]+$" (printf "%v" $v.exportGid)) -}}
    {{- fail "exportGid must be empty or a nonzero integer from 1 to 2147483647" -}}
  {{- end -}}
  {{- $gid := int64 $v.exportGid -}}
  {{- if or (lt $gid 1) (gt $gid 2147483647) -}}
    {{- fail "exportGid must be empty or a nonzero integer from 1 to 2147483647" -}}
  {{- end -}}
{{- end -}}
{{- end -}}
