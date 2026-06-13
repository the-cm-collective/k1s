{{- define "k1s-edge-gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "k1s-edge-gateway.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "k1s-edge-gateway.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "k1s-edge-gateway.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/name: {{ include "k1s-edge-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: k1s-microk8s-dev-stack
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
k1s.dev/site-id: {{ .Values.gateway.siteId | quote }}
{{- end -}}

{{- define "k1s-edge-gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "k1s-edge-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "k1s-edge-gateway.image" -}}
{{- $root := .root -}}
{{- $image := .image -}}
{{- $registry := default $root.Values.global.registryHost $image.registry -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry $image.repository $image.tag -}}
{{- else -}}
{{- printf "%s:%s" $image.repository $image.tag -}}
{{- end -}}
{{- end -}}

{{- define "k1s-edge-gateway.targetNamespace" -}}
{{- if .Values.target.namespace -}}
{{- .Values.target.namespace -}}
{{- else -}}
{{- .Release.Namespace -}}
{{- end -}}
{{- end -}}

{{- define "k1s-edge-gateway.coreAuthSecretName" -}}
{{- if .Values.target.authSecretName -}}
{{- .Values.target.authSecretName -}}
{{- else if .Values.target.controllerReleaseName -}}
{{- printf "%s-k1s-core-ha-auth" .Values.target.controllerReleaseName -}}
{{- else -}}
{{- fail "set target.authSecretName or target.controllerReleaseName" -}}
{{- end -}}
{{- end -}}

{{- define "k1s-edge-gateway.natsServiceName" -}}
{{- if .Values.target.natsServiceName -}}
{{- .Values.target.natsServiceName -}}
{{- else if .Values.target.controllerReleaseName -}}
{{- printf "%s-k1s-core-ha-nats" .Values.target.controllerReleaseName -}}
{{- else -}}
{{- fail "set target.natsServiceName or target.controllerReleaseName" -}}
{{- end -}}
{{- end -}}

{{- define "k1s-edge-gateway.natsLeafServiceName" -}}
{{- if .Values.target.natsLeafServiceName -}}
{{- .Values.target.natsLeafServiceName -}}
{{- else if .Values.target.controllerReleaseName -}}
{{- printf "%s-k1s-core-ha-nats-leaf" .Values.target.controllerReleaseName -}}
{{- else -}}
{{- fail "set target.natsLeafServiceName or target.controllerReleaseName" -}}
{{- end -}}
{{- end -}}

{{- define "k1s-edge-gateway.ratholeServiceName" -}}
{{- if .Values.target.ratholeServiceName -}}
{{- .Values.target.ratholeServiceName -}}
{{- else if .Values.target.controllerReleaseName -}}
{{- printf "%s-k1s-core-ha-rathole" .Values.target.controllerReleaseName -}}
{{- else -}}
{{- fail "set target.ratholeServiceName or target.controllerReleaseName" -}}
{{- end -}}
{{- end -}}

{{- define "k1s-edge-gateway.controllerHeadlessServiceName" -}}
{{- if .Values.target.controllerReleaseName -}}
{{- printf "%s-k1s-core-ha-controller-headless" .Values.target.controllerReleaseName -}}
{{- else -}}
{{- fail "set target.controllerReleaseName" -}}
{{- end -}}
{{- end -}}

{{- define "k1s-edge-gateway.natsHost" -}}
{{- printf "%s.%s.svc.cluster.local" (include "k1s-edge-gateway.natsServiceName" .) (include "k1s-edge-gateway.targetNamespace" .) -}}
{{- end -}}

{{- define "k1s-edge-gateway.natsLeafHost" -}}
{{- printf "%s.%s.svc.cluster.local" (include "k1s-edge-gateway.natsLeafServiceName" .) (include "k1s-edge-gateway.targetNamespace" .) -}}
{{- end -}}

{{- define "k1s-edge-gateway.ratholeServerAddr" -}}
{{- if .Values.rathole.serverAddr -}}
{{- .Values.rathole.serverAddr -}}
{{- else -}}
{{- printf "%s.%s.svc.cluster.local:2333" (include "k1s-edge-gateway.ratholeServiceName" .) (include "k1s-edge-gateway.targetNamespace" .) -}}
{{- end -}}
{{- end -}}

{{- define "k1s-edge-gateway.ratholeDiscoveryHost" -}}
{{- if .Values.rathole.discoveryHost -}}
{{- .Values.rathole.discoveryHost -}}
{{- else -}}
{{- printf "%s.%s.svc.cluster.local" (include "k1s-edge-gateway.controllerHeadlessServiceName" .) (include "k1s-edge-gateway.targetNamespace" .) -}}
{{- end -}}
{{- end -}}

{{- define "k1s-edge-gateway.pvcName" -}}
{{- printf "%s-state" (include "k1s-edge-gateway.fullname" .) -}}
{{- end -}}
