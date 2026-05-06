{{- define "k1s-node-local.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "k1s-node-local.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "k1s-node-local.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "k1s-node-local.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/name: {{ include "k1s-node-local.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: k1s-microk8s-dev-stack
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "k1s-node-local.selectorLabels" -}}
app.kubernetes.io/name: {{ include "k1s-node-local.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "k1s-node-local.image" -}}
{{- $root := .root -}}
{{- $image := .image -}}
{{- $registry := default $root.Values.global.registryHost $image.registry -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry $image.repository $image.tag -}}
{{- else -}}
{{- printf "%s:%s" $image.repository $image.tag -}}
{{- end -}}
{{- end -}}

{{- define "k1s-node-local.targetNamespace" -}}
{{- if .Values.target.namespace -}}
{{- .Values.target.namespace -}}
{{- else -}}
{{- .Release.Namespace -}}
{{- end -}}
{{- end -}}

{{- define "k1s-node-local.controllerServiceName" -}}
{{- if .Values.target.controllerServiceName -}}
{{- .Values.target.controllerServiceName -}}
{{- else if .Values.target.controllerReleaseName -}}
{{- printf "%s-k1s-core-ha-controller" .Values.target.controllerReleaseName -}}
{{- else -}}
{{- "" -}}
{{- end -}}
{{- end -}}

{{- define "k1s-node-local.controllerUrl" -}}
{{- if .Values.target.controllerUrl -}}
{{- .Values.target.controllerUrl -}}
{{- else -}}
{{- $serviceName := include "k1s-node-local.controllerServiceName" . -}}
{{- if not $serviceName -}}
{{- fail "set target.controllerUrl or target.controllerReleaseName/target.controllerServiceName" -}}
{{- end -}}
{{- printf "http://%s.%s.svc.cluster.local:9110" $serviceName (include "k1s-node-local.targetNamespace" .) -}}
{{- end -}}
{{- end -}}

{{- define "k1s-node-local.agentTokenSecretName" -}}
{{- if .Values.target.agentTokenSecretName -}}
{{- .Values.target.agentTokenSecretName -}}
{{- else if .Values.target.controllerReleaseName -}}
{{- printf "%s-k1s-core-ha-auth" .Values.target.controllerReleaseName -}}
{{- else -}}
{{- printf "%s-agent-token" (include "k1s-node-local.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "k1s-node-local.nodeLabelsCsv" -}}
{{- $pairs := list -}}
{{- range $key, $value := .Values.node.labels -}}
{{- $pairs = append $pairs (printf "%s=%v" $key $value) -}}
{{- end -}}
{{- join "," (sortAlpha $pairs) -}}
{{- end -}}
