{{- define "k1s-core-ha.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "k1s-core-ha.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "k1s-core-ha.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "k1s-core-ha.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/name: {{ include "k1s-core-ha.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: k1s-microk8s-dev-stack
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
k1s.dev/stack-name: {{ .Values.stack.name | quote }}
{{- end -}}

{{- define "k1s-core-ha.selectorLabels" -}}
app.kubernetes.io/name: {{ include "k1s-core-ha.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "k1s-core-ha.authSecretName" -}}
{{- if .Values.auth.existingSecret -}}
{{- .Values.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-auth" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "k1s-core-ha.controllerName" -}}
{{- printf "%s-controller" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.controllerServiceName" -}}
{{- printf "%s-controller" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.controllerHeadlessServiceName" -}}
{{- printf "%s-controller-headless" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.controllerExternalServiceName" -}}
{{- printf "%s-controller-external" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.controllerMetricsServiceName" -}}
{{- printf "%s-controller-metrics" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.edgeProxyServiceName" -}}
{{- printf "%s-edge-proxy" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.ratholeExternalServiceName" -}}
{{- printf "%s-rathole" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.apishimName" -}}
{{- printf "%s-apishim" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.apishimServiceName" -}}
{{- printf "%s-apishim" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.etcdName" -}}
{{- printf "%s-etcd" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.etcdHeadlessServiceName" -}}
{{- printf "%s-etcd-headless" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.etcdClientServiceName" -}}
{{- printf "%s-etcd" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.etcdMetricsServiceName" -}}
{{- printf "%s-etcd-metrics" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.natsName" -}}
{{- printf "%s-nats" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.natsHeadlessServiceName" -}}
{{- printf "%s-nats-headless" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.natsClientServiceName" -}}
{{- printf "%s-nats" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.natsMetricsServiceName" -}}
{{- printf "%s-nats-metrics" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.natsLeafExternalServiceName" -}}
{{- printf "%s-nats-leaf" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.bootstrapConfigMapName" -}}
{{- printf "%s-bootstrap" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.controllerPvcName" -}}
{{- printf "%s-controller-state" (include "k1s-core-ha.fullname" .) -}}
{{- end -}}

{{- define "k1s-core-ha.appsDomain" -}}
{{- if .Values.edgeProxy.siteDomainSuffix -}}
{{- .Values.edgeProxy.siteDomainSuffix -}}
{{- else -}}
{{- printf "apps.%s" .Values.stack.domain -}}
{{- end -}}
{{- end -}}

{{- define "k1s-core-ha.dashHost" -}}
{{- printf "dash.%s" .Values.stack.domain -}}
{{- end -}}

{{- define "k1s-core-ha.docsHost" -}}
{{- printf "docs.%s" .Values.stack.domain -}}
{{- end -}}

{{- define "k1s-core-ha.controllerBootstrapHost" -}}
{{- if .Values.bootstrap.controller.hostOverride -}}
{{- .Values.bootstrap.controller.hostOverride -}}
{{- else if .Values.bootstrap.controller.loadBalancerIP -}}
{{- .Values.bootstrap.controller.loadBalancerIP -}}
{{- else -}}
{{- printf "%s.%s.svc.cluster.local" (include "k1s-core-ha.controllerExternalServiceName" .) .Release.Namespace -}}
{{- end -}}
{{- end -}}

{{- define "k1s-core-ha.natsLeafBootstrapHost" -}}
{{- if .Values.bootstrap.natsLeaf.hostOverride -}}
{{- .Values.bootstrap.natsLeaf.hostOverride -}}
{{- else if .Values.bootstrap.natsLeaf.loadBalancerIP -}}
{{- .Values.bootstrap.natsLeaf.loadBalancerIP -}}
{{- else -}}
{{- printf "%s.%s.svc.cluster.local" (include "k1s-core-ha.natsLeafExternalServiceName" .) .Release.Namespace -}}
{{- end -}}
{{- end -}}

{{- define "k1s-core-ha.ratholeBootstrapHost" -}}
{{- if .Values.bootstrap.rathole.hostOverride -}}
{{- .Values.bootstrap.rathole.hostOverride -}}
{{- else if .Values.bootstrap.rathole.loadBalancerIP -}}
{{- .Values.bootstrap.rathole.loadBalancerIP -}}
{{- else -}}
{{- printf "%s.%s.svc.cluster.local" (include "k1s-core-ha.ratholeExternalServiceName" .) .Release.Namespace -}}
{{- end -}}
{{- end -}}

{{- define "k1s-core-ha.image" -}}
{{- $root := .root -}}
{{- $image := .image -}}
{{- $registry := default $root.Values.global.registryHost $image.registry -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry $image.repository $image.tag -}}
{{- else -}}
{{- printf "%s:%s" $image.repository $image.tag -}}
{{- end -}}
{{- end -}}
