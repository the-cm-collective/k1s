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
NAMESPACE=${NAMESPACE:-demo-helm}
HELM_TEMPLATE_ONLY=${HELM_TEMPLATE_ONLY:-0}
HELM_TIMEOUT=${HELM_TIMEOUT:-120s}
TMPDIR=${TMPDIR:-/tmp}
WORKDIR="$(mktemp -d "$TMPDIR/helm-shim-XXXX")"
KUBECONFIG_PATH="$WORKDIR/kubeconfig"
LOG_PATH="$WORKDIR/shim.log"
CHART_DIR="$WORKDIR/$CHART_NAME"
MANIFEST_PATH="$WORKDIR/rendered.yaml"

populate_chart() {
  local chart_dir=$1
  mkdir -p "$chart_dir/templates"
  cat <<YAML > "$chart_dir/values.yaml"
replicaCount: 1
image:
  repository: docker.io/nginx
  tag: "1.27"
serviceAccount:
  create: true
  annotations: {}
  name: ""
service:
  type: NodePort
  port: 80
  targetPort: 80
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
  enableCronJob: false
  # If enabled for debugging, keep the interval conservative to avoid rapid job spam.
  cronSchedule: "0 * * * *"
YAML

  cat <<'YAML' > "$chart_dir/templates/extra-workloads.yaml"
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
          image: docker.io/busybox:1.36
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
    cronjob.k1s.dev/intervalSeconds: "3600"
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
              image: docker.io/busybox:1.36
              command: ["sh", "-c", "echo cron run && sleep 1"]
{{- end }}
YAML
}

render_chart() {
  local chart_dir=$1
  helm template "$CHART_NAME" "$chart_dir" -n "$NAMESPACE" --disable-openapi-validation --no-hooks > "$MANIFEST_PATH"
}

is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

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
chmod 600 "$KUBECONFIG_PATH"
export KUBECONFIG="$KUBECONFIG_PATH"

mkdir "$CHART_DIR"
helm create "$CHART_DIR" >/dev/null
populate_chart "$CHART_DIR"
helm dependency update "$CHART_DIR" >/dev/null

if [[ "$NAMESPACE" != "default" ]]; then
  if ! kubectl get ns "$NAMESPACE" >/dev/null 2>&1; then
    kubectl create namespace "$NAMESPACE" -o yaml --dry-run=client | kubectl apply --validate=false -f -
  fi
fi

if is_true "$HELM_TEMPLATE_ONLY"; then
  render_chart "$CHART_DIR"

  if [[ ! -s "$MANIFEST_PATH" ]] || ! grep -q '^kind:' "$MANIFEST_PATH"; then
    echo "[shim-demo] rendered manifest empty, regenerating chart" >&2
    rm -rf "$CHART_DIR"
    helm create "$CHART_DIR" >/dev/null
    populate_chart "$CHART_DIR"
    helm dependency update "$CHART_DIR" >/dev/null
    render_chart "$CHART_DIR"
  fi
  if [[ ! -s "$MANIFEST_PATH" ]] || ! grep -q '^kind:' "$MANIFEST_PATH"; then
    echo "[shim-demo] manifest still empty, falling back to static workload" >&2
    cat > "$MANIFEST_PATH" <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: shim-fallback
spec:
  replicas: 1
  selector:
    matchLabels:
      app: shim-fallback
  template:
    metadata:
      labels:
        app: shim-fallback
    spec:
      containers:
        - name: web
          image: docker.io/nginx:1.27
          ports:
            - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: shim-fallback
spec:
  selector:
    app: shim-fallback
  ports:
    - port: 80
      targetPort: 80
YAML
  fi

  # Clean up any prior demo jobs/cronjobs to avoid clutter (especially from older cron demos).
  kubectl -n "$NAMESPACE" delete cronjob --all --ignore-not-found --wait=false --request-timeout=10s >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" delete jobs --all --ignore-not-found --wait=false --request-timeout=10s >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" --validate=false apply -f "$MANIFEST_PATH"
  kubectl -n "$NAMESPACE" get deploy,svc,ing
  kubectl -n "$NAMESPACE" get statefulset,daemonset,job,cronjob,hpa

  ASSIGNED_PORT=$(kubectl -n "$NAMESPACE" get svc "$CHART_NAME" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || echo "n/a")
  echo "[shim-demo] service nodePort: $ASSIGNED_PORT"

  kubectl -n "$NAMESPACE" delete -f "$MANIFEST_PATH" --ignore-not-found --wait=false --request-timeout=10s >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" delete cronjob --all --ignore-not-found --wait=false --request-timeout=10s >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" delete jobs --all --ignore-not-found --wait=false --request-timeout=10s >/dev/null 2>&1 || true
else
  helm uninstall "$CHART_NAME" -n "$NAMESPACE" --wait --timeout "$HELM_TIMEOUT" >/dev/null 2>&1 || true
  echo "[shim-demo] helm install $CHART_NAME"
  helm install "$CHART_NAME" "$CHART_DIR" -n "$NAMESPACE" --wait --timeout "$HELM_TIMEOUT" --disable-openapi-validation
  helm ls -n "$NAMESPACE"
  helm history "$CHART_NAME" -n "$NAMESPACE"
  kubectl -n "$NAMESPACE" get deploy,svc,ing
  kubectl -n "$NAMESPACE" get statefulset,daemonset,job,cronjob,hpa

  echo "[shim-demo] helm upgrade $CHART_NAME (replicaCount=2)"
  helm upgrade "$CHART_NAME" "$CHART_DIR" -n "$NAMESPACE" --set replicaCount=2 --wait --timeout "$HELM_TIMEOUT" --disable-openapi-validation
  helm history "$CHART_NAME" -n "$NAMESPACE"
  REV_COUNT=$(helm history "$CHART_NAME" -n "$NAMESPACE" | awk 'NR>1 {count++} END{print count+0}')
  if [[ "$REV_COUNT" -lt 2 ]]; then
    echo "[shim-demo] expected at least 2 Helm revisions, got $REV_COUNT" >&2
    exit 1
  fi

  ASSIGNED_PORT=$(kubectl -n "$NAMESPACE" get svc "$CHART_NAME" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || echo "n/a")
  echo "[shim-demo] service nodePort: $ASSIGNED_PORT"

  echo "[shim-demo] helm uninstall $CHART_NAME"
  helm uninstall "$CHART_NAME" -n "$NAMESPACE" --wait --timeout "$HELM_TIMEOUT"
  if helm ls -n "$NAMESPACE" -q | grep -qx "$CHART_NAME"; then
    echo "[shim-demo] release still present after uninstall" >&2
    exit 1
  fi
  if kubectl -n "$NAMESPACE" get secrets,configmaps -o name 2>/dev/null | grep -q "sh.helm.release.v1.${CHART_NAME}.v"; then
    echo "[shim-demo] release records still present after uninstall" >&2
    exit 1
  fi
fi

if [[ "$NAMESPACE" != "default" ]]; then
  kubectl delete namespace "$NAMESPACE" >/dev/null 2>&1 || true
fi

echo "\nRun completed. Logs: $LOG_PATH"
