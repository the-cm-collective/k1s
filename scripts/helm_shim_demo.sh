#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[shim-demo] missing dependency: $1" >&2
    exit 2
  fi
}

need python
need helm
need kubectl
PYTHONPATH=${PYTHONPATH:-$ROOT_DIR/src}
PORT=${PORT:-8445}
TOKEN=${TOKEN:-helm-demo}
RUNTIME=${RUNTIME:-stub}
CHART_NAME=${CHART_NAME:-demochart}
NAMESPACE=${NAMESPACE:-demo}
TMPDIR=${TMPDIR:-/tmp}
WORKDIR="$(mktemp -d "$TMPDIR/helm-shim-XXXX")"
KUBECONFIG_PATH="$WORKDIR/kubeconfig"
LOG_PATH="$WORKDIR/shim.log"
CHART_DIR="$WORKDIR/$CHART_NAME"

cleanup() {
  local ec=$?
  if [[ -n "${SHIM_PID:-}" ]] && kill -0 "$SHIM_PID" 2>/dev/null; then
    kill "$SHIM_PID" 2>/dev/null || true
    wait "$SHIM_PID" 2>/dev/null || true
  fi
  rm -rf "$WORKDIR"
  exit $ec
}
trap cleanup EXIT

export PYTHONPATH
AE_APISHIM_ENABLE=1 AE_APISHIM_RUNTIME="$RUNTIME" python -m ae.apishim serve \
  --host 127.0.0.1 --port "$PORT" --token "$TOKEN" --allow-anonymous >"$LOG_PATH" 2>&1 &
SHIM_PID=$!

python -m ae.apishim kubeconfig \
  --server "http://127.0.0.1:$PORT" \
  --token "$TOKEN" \
  --context k1s-shim \
  --insecure-skip-tls-verify > "$KUBECONFIG_PATH"
export KUBECONFIG="$KUBECONFIG_PATH"

mkdir "$CHART_DIR"
helm create "$CHART_DIR" >/dev/null
mkdir -p "$CHART_DIR/templates"
cat <<YAML > "$CHART_DIR/values.yaml"
replicaCount: 1
image:
  repository: nginx
  tag: "1.27"
service:
  type: NodePort
  # Leave nodePort unset to let the shim allocate within AE_APISHIM_NODEPORT range
resources: {}
ingress:
  enabled: true
  className: ""
  hosts:
    - host: demo.local
      paths:
        - path: /
          pathType: Prefix
  tls: []
# Enable built-in HPA template
autoscaling:
  enabled: true
  minReplicas: 1
  maxReplicas: 4
  targetCPUUtilizationPercentage: 50

workloads:
  enableStatefulSet: true
  enableDaemonSet: true
  enableJob: true
  enableCronJob: true
  cronSchedule: "* * * * *"
YAML

helm dependency update "$CHART_DIR" >/dev/null

cat <<'YAML' > "$CHART_DIR/templates/extra-workloads.yaml"
{{- if .Values.workloads.enableStatefulSet }}
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ include "demochart.fullname" . }}-sts
  labels: {{- include "demochart.labels" . | nindent 4 }}
spec:
  serviceName: {{ include "demochart.fullname" . }}-sts
  replicas: 2
  selector:
    matchLabels: {{- include "demochart.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels: {{- include "demochart.selectorLabels" . | nindent 8 }}
    spec:
      containers:
        - name: main
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          ports:
            - containerPort: 80
{{- end }}
---
{{- if .Values.workloads.enableDaemonSet }}
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: {{ include "demochart.fullname" . }}-ds
  labels: {{- include "demochart.labels" . | nindent 4 }}
spec:
  selector:
    matchLabels: {{- include "demochart.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels: {{- include "demochart.selectorLabels" . | nindent 8 }}
    spec:
      containers:
        - name: agent
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          ports:
            - containerPort: 80
{{- end }}
---
{{- if .Values.workloads.enableJob }}
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "demochart.fullname" . }}-job
  labels: {{- include "demochart.labels" . | nindent 4 }}
spec:
  parallelism: 1
  completions: 1
  template:
    metadata:
      labels: {{- include "demochart.selectorLabels" . | nindent 8 }}
    spec:
      restartPolicy: Never
      containers:
        - name: job
          image: busybox:1.36
          command: ["sh", "-c", "echo job run && sleep 1"]
{{- end }}
---
{{- if .Values.workloads.enableCronJob }}
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ include "demochart.fullname" . }}-cron
  labels: {{- include "demochart.labels" . | nindent 4 }}
  annotations:
    cronjob.k1s.dev/intervalSeconds: "0"
spec:
  schedule: "{{ .Values.workloads.cronSchedule }}"
  jobTemplate:
    spec:
      template:
        metadata:
          labels: {{- include "demochart.selectorLabels" . | nindent 12 }}
        spec:
          restartPolicy: Never
          containers:
            - name: cron
              image: busybox:1.36
              command: ["sh", "-c", "echo cron run && sleep 1"]
{{- end }}
YAML

helm install "$CHART_NAME" "$CHART_DIR" -n "$NAMESPACE" --create-namespace --wait
kubectl -n "$NAMESPACE" get deploy,svc,ing
kubectl -n "$NAMESPACE" get statefulset,daemonset,job,cronjob,hpa

ASSIGNED_PORT=$(kubectl -n "$NAMESPACE" get svc "$CHART_NAME" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || echo "n/a")
echo "[shim-demo] service nodePort: $ASSIGNED_PORT"

helm uninstall "$CHART_NAME" -n "$NAMESPACE"
kubectl delete namespace "$NAMESPACE" >/dev/null

echo "\nRun completed. Logs: $LOG_PATH"
