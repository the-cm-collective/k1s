<div class="playground-shell">
  <div class="playground-main">
    <div class="playground-hero">
      <h2>Interactive Lab Playground</h2>
    </div>

<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" onerror="this.onerror=null; this.href='/static/vendor/xterm.css';" />
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js" onerror="window.__xterm_cdn_failed=true;"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js" onerror="window.__xterm_fit_cdn_failed=true;"></script>
<script>
(function(){
  function loadScript(src){
    var s = document.createElement('script');
    s.src = src;
    s.defer = true;
    document.head.appendChild(s);
  }
  function loadCss(href){
    var l = document.createElement('link');
    l.rel = 'stylesheet';
    l.href = href;
    document.head.appendChild(l);
  }
  function ensureLocal(){
    if (window.__xterm_cdn_failed || !window.Terminal) {
      loadCss('/static/vendor/xterm.css');
      loadScript('/static/vendor/xterm.js');
    }
    if (window.__xterm_fit_cdn_failed || !(window.FitAddon && window.FitAddon.FitAddon)) {
      loadScript('/static/vendor/xterm-addon-fit.js');
    }
  }
  if (document.readyState === 'complete') {
    ensureLocal();
  } else {
    window.addEventListener('load', ensureLocal);
  }
})();
</script>

Use this page to try k1s in minutes — no Kubernetes experience required. The playground can run fully read‑only or, when enabled, perform safe actions like "apply example" and "scale".

Quick start:

- Step 1: Scroll to "A. Environment & Backend", enable <strong>Controlled Actions</strong>, click <strong>Use Token</strong> to load `AE_LABS_TOKEN`, then click `Start Session`.
- Step 2: In "B. Apply Example", choose `echo` and click `Apply Selected Example`.
- Step 3: In "F. Ingress Test", click `Open App` to view the service, and in "C. Logs & Events" watch activity live.

<div class="callout" role="note">Tip: If the page says “Labs: unavailable”, you’re in read‑only mode — verifiers still run and CLI commands are shown to try locally.</div>

This page prefers HTMX + SSE for live updates and uses a small `labs.js` helper for sessions. If the dev‑only lab orchestrator is not running, the page falls back to read‑only checks with copyable CLI commands.

## A. Environment & Backend

To start a session: enable <strong>Controlled Actions</strong>, click <strong>Use Token</strong> to load `AE_LABS_TOKEN`, then click `Start Session`.

- API Base: <span id="env-api-base">(detecting)</span>
- Orchestrator: <span id="env-orch">(checking)</span>
- Backend:
  - <select id="backend-select">
      <option value="auto" selected>Auto</option>
      <option value="k1s-host">k1s-Host (local)</option>
      <option value="k1s-docker">k1s-in-Docker (compose)</option>
      <option value="k3s">k3s via k3d</option>
    </select>
  - <label><input type="checkbox" id="toggle-actions"/> Enable Controlled Actions</label>
  - Token: <input id="labs-token" type="password" placeholder="paste AE_LABS_TOKEN"/> <button id="btn-use-token" onclick="window.k1sUseToken && window.k1sUseToken()">Use Token</button> <span id="labs-token-nudge" class="nudge hidden" role="status" aria-live="polite">Paste AE_LABS_TOKEN, then click <em>Use Token</em>.</span>
  - <button id="btn-start-session" class="btn-primary">Start Session</button>
  - Session: <span id="session-id">(none)</span>

<div id="env-notes"></div>
<div id="backend-status" class="ribbon muted"></div>
<div class="status-bar">
  <span class="status-label">Direct API curl:</span>
  <div class="api-curl">
    <code id="api-curl"></code>
    <button id="api-curl-copy" disabled>Copy</button>
  </div>
  <span class="status-label">Status:</span>
  <div class="status-stack">
    <span id="status-summary" class="pending" hx-ext="sse" sse-connect="" sse-swap="message">n/a</span>
    <div class="status-toggle" role="group" aria-label="Status scope">
      <button id="status-mode-app" type="button" aria-pressed="false" title="Focus on the app from this session">App</button>
      <button id="status-mode-cluster" type="button" class="is-active" aria-pressed="true" title="Show totals across all apps">Cluster</button>
    </div>
  </div>
  <span id="status-mode-note" class="status-note nudge">Cluster shows totals across all apps. Switch to App after applying an example.</span>
</div>

<!-- Global banner for errors and important notices -->
<div id="banner" class="ribbon hidden"></div>

<!-- Labs token controls are now part of the Backend row, above Start Session -->

### k3d Controls (k3s backend)

- <button id="btn-k3d-ensure" class="hidden">Create k3d Cluster</button>
- <a id="k3d-open-ingress" href="#" target="_blank" rel="noopener" class="hidden">Open k3s Ingress (LB)</a>

### Quick Links

- <a id="open-dashboard" href="#" target="_blank" rel="noopener" class="hidden">Open Dashboard</a>

### Helm Shim Demo (Watch Mode)

<div class="helm-demo-box">
  <div class="helm-demo-controls">
    <button id="btn-helm-demo" class="btn-secondary" disabled>Run Helm Shim Demo</button>
    <button id="btn-helm-demo-stop" class="btn-secondary" disabled>Stop</button>
  </div>
  <div id="helm-demo-status" class="helm-demo-status">Labs backend unavailable.</div>
  <pre id="helm-demo-log" class="helm-demo-log hidden scrollbar-hide" aria-live="polite"></pre>
</div>

Want to watch a Helm deployment materialize inside the dashboard? Click “Run Helm Shim Demo” above (requires Labs token). Behind the scenes, the server runs the commands below; you can still run them manually if you prefer. The demo creates a few core Kubernetes objects so you can see how they show up in the UI:

- `Namespace` (`demo-helm`) - isolates the demo resources so they are easy to find and clean up.
- `Deployment` (`demochart`) - declares the desired number of Pods and handles rolling updates.
- `Service` (`demochart`) - provides a stable virtual IP/DNS name and load-balances traffic to the Pods.
- `ServiceAccount` (`demochart`) - gives Pods an identity for Kubernetes API access and permissions.

The chart also includes optional Ingress and HPA templates, but they are disabled by default in this demo.

```bash
# 1) Create a self-signed cert (dev-only)
openssl req -x509 -newkey rsa:2048 -sha256 -days 3 -nodes \
  -keyout /tmp/helm-shim.key -out /tmp/helm-shim.crt -subj "/CN=127.0.0.1"

# 2) Start the API shim (stub runtime) with TLS
AE_APISHIM_ENABLE=1 AE_APISHIM_RUNTIME=stub AE_APISHIM_TOKEN=helm-demo \
  AE_APISHIM_TLS_CERT=/tmp/helm-shim.crt AE_APISHIM_TLS_KEY=/tmp/helm-shim.key \
  PYTHONPATH=src python -m ae.apishim serve --host 127.0.0.1 --port 8445 --tls

# 3) Generate a kubeconfig pointing at the shim
PYTHONPATH=src python -m ae.apishim kubeconfig \
  --server https://127.0.0.1:8445 --token helm-demo \
  --context k1s-shim --insecure-skip-tls-verify > ~/.kube/helm-shim
export KUBECONFIG=~/.kube/helm-shim

# 4) Create namespace + sample chart and install it
kubectl create namespace demo-helm
helm create demochart
helm install demochart ./demochart -n demo-helm --wait

# (Optional) Inspect via k1s CLI while Helm is running
PYTHONPATH=src python -m ae.cli status demo-helm--demochart --wide --events

# 4) When finished, uninstall and clean up
helm uninstall demochart -n demo-helm && kubectl delete namespace demo-helm
```

Back in the playground:

1. Set Backend to `k1s-Host`, click `Start Session`, and use **Quick Links → Open Dashboard**.
2. The release appears as `demo-helm--demochart` (namespace `demo-helm`); sections **B–E** stream logs, events, ingress health, and nodePort hints while Helm reconciles.
3. When you run `helm uninstall`, the dashboard reflects the teardown in real time.

## B. Apply Example

Pick a sample and apply it. In read-only mode the UI shows the exact CLI you can run locally. Each example is a `Deployment` spec YAML (k1s native): `apiVersion` and `kind` identify the schema, `metadata.name` becomes the app's ID, and `spec` is where you describe what to run and how it should behave. Typical `spec` fields include `image` (container), `replicas` (how many), `ports`/`service` (how traffic reaches it), `health` checks, plus optional sections like `ingress`, `resources`, `security`, `storage`, and `configRefs`/`secretRefs` for configuration.

- Example:
  - <select id="example-select">
      <option value="echo" selected>echo</option>
      <option value="shell-demo">shell-demo</option>
      <option value="multi-replica-echo">multi-replica-echo</option>
      <option value="echo-multiport">echo-multiport</option>
      <option value="echo-hpa">echo-hpa</option>
      <option value="echo-resources">echo-resources</option>
      <option value="echo-stateful">echo-stateful</option>
      <option value="echo-sec">echo-sec</option>
      <option value="echo-sec-adv">echo-sec-adv</option>
      <option value="echo-tcp">echo-tcp</option>
      <option value="echo-exec">echo-exec</option>
      <option value="echo-storage">echo-storage</option>
      <option value="echo-storage-delete">echo-storage-delete</option>
    </select>
- Action: <button id="btn-apply-echo" class="btn-primary" disabled>Apply Selected Example</button>
- YAML (read-only):

<pre><code id="example-yaml" class="lang-yaml">(select an example to preview its YAML)</code></pre>

<!-- CLI fallback removed to avoid hardcoding echo.yaml -->

### Verifiers

- Events include “ApplyCompleted” for a revision: <span id="v-apply-events" data-v="pending">pending</span>
- Status readyReplicas == spec.replicas: <span id="v-apply-ready" data-v="pending">pending</span>

## C. Logs & Events

Live feed of events and logs for your sample app. When sessions are enabled, this switches to streaming mode automatically.

<div id="observe">
  <button id="btn-observe-toggle" disabled>Start Tail</button>
  <label style="margin-left:10px"><input type="checkbox" id="follow-tail" checked/> Follow tail</label>
  <div id="observe-events" class="panel scrollbar-hide" aria-live="polite" aria-busy="true" data-source="events" style="height:220px;max-height:220px;overflow:auto;"></div>
  <div id="observe-logs" class="panel scrollbar-hide" aria-live="polite" aria-busy="true" data-source="logs" style="height:220px;max-height:220px;overflow:auto;"></div>
  <!-- HTMX SSE variant for logs; labs.js will arm sse-connect once a session exists -->
  <div id="logs-sse" class="panel hidden scrollbar-hide" hx-ext="sse" sse-connect="" sse-swap="message" hx-swap="beforeend" style="height:220px;max-height:220px;overflow:auto;"></div>
  <!-- HTMX SSE variant for events; labs.js will arm sse-connect once a session exists -->
  <div id="events-sse" class="panel hidden scrollbar-hide" hx-ext="sse" sse-connect="" sse-swap="message" style="height:220px;max-height:220px;overflow:auto;"></div>
</div>

## D. Debug Tools (Shell + Port-Forward)

Open an interactive shell or run a lightweight port-forward check. These are Labs‑only features: they require an active session and Controlled Actions enabled.

<div class="panel" id="labs-debug">
  <div class="row" style="flex-wrap:wrap; gap:10px;">
    <button id="btn-labs-shell" disabled>Open Remote Shell</button>
    <button id="btn-labs-pf" disabled>Open Port-Forward</button>
    <span class="muted">Uses apishim WebSocket exec + port-forward.</span>
  </div>
  <div class="muted" style="margin-top:6px;">If disabled, run locally: <code>ae shell &lt;app&gt;</code> (defaults to sh; use <code>-- bash</code> when available), add <code>-n &lt;ns&gt;</code> if needed, or use <code>kubectl exec</code> / <code>kubectl port-forward</code>.</div>
</div>

<div id="labs-shell-modal" class="labs-modal hidden" role="dialog" aria-modal="true" aria-labelledby="labs-shell-title">
  <div class="modal">
    <div class="modal-header">
      <strong id="labs-shell-title">Remote Shell</strong>
      <button id="labs-shell-close" type="button">Close</button>
    </div>
    <div class="modal-body">
      <div class="row" style="flex-wrap:wrap; gap:10px; margin-bottom:10px;">
        <label>Pod <select id="labs-shell-pod"></select></label>
        <label>Container <input id="labs-shell-container" type="text" placeholder="optional" /></label>
        <label>Command <input id="labs-shell-cmd" type="text" value="sh" /></label>
        <label>Shim API <input id="labs-shell-base" type="text" placeholder="http://127.0.0.1:8443" /></label>
        <label>Token <input id="labs-shell-token" type="password" placeholder="apishim token" /></label>
      </div>
      <div id="labs-shell-terminal" class="terminal-wrap"></div>
      <div class="row" style="margin-top:10px; gap:8px; align-items:center;">
        <button id="labs-shell-connect" type="button">Connect</button>
        <button id="labs-shell-disconnect" type="button">Disconnect</button>
        <span id="labs-shell-status" class="pill"></span>
        <span class="hint">WebSocket exec (v5.channel.k8s.io).</span>
      </div>
    </div>
  </div>
</div>

<div id="labs-pf-modal" class="labs-modal hidden" role="dialog" aria-modal="true" aria-labelledby="labs-pf-title">
  <div class="modal">
    <div class="modal-header">
      <strong id="labs-pf-title">Port-Forward (WS)</strong>
      <button id="labs-pf-close" type="button">Close</button>
    </div>
    <div class="modal-body">
      <div class="pf-fields">
        <label>Pod <select id="labs-pf-pod"></select></label>
        <label>Port <input id="labs-pf-port" type="text" placeholder="8080" /></label>
        <label class="pf-span-2">Shim API <input id="labs-pf-base" type="text" placeholder="http://127.0.0.1:8443" /></label>
        <label class="pf-span-4">Token <input id="labs-pf-token" type="password" placeholder="apishim token" /></label>
      </div>
      <div class="pf-io">
        <label>Request
          <textarea id="labs-pf-request" rows="4" class="scrollbar-hide" style="width:100%;">GET / HTTP/1.1&#10;Host: localhost&#10;Connection: close&#10;&#10;</textarea>
        </label>
        <div class="pf-response">
          <div class="pf-response-head">
            <label for="labs-pf-response">Response</label>
            <div class="pf-response-toggle" role="group" aria-label="Response view">
              <button id="labs-pf-view-source" type="button" class="pf-view-btn active" data-pf-view="source" aria-pressed="true">Source</button>
              <button id="labs-pf-view-preview" type="button" class="pf-view-btn" data-pf-view="preview" aria-pressed="false">Preview</button>
            </div>
          </div>
          <div id="labs-pf-response-source" class="pf-response-view pf-source">
            <textarea id="labs-pf-response" rows="4" class="scrollbar-hide" style="width:100%;" readonly></textarea>
          </div>
          <div id="labs-pf-preview" class="pf-response-view pf-preview hidden">
            <iframe id="labs-pf-preview-frame" title="Port-forward response preview" sandbox></iframe>
          </div>
        </div>
      </div>
      <div class="pf-controls">
        <button id="labs-pf-connect" type="button">Connect</button>
        <button id="labs-pf-send" type="button">Send Request</button>
        <button id="labs-pf-disconnect" type="button">Disconnect</button>
        <span id="labs-pf-status" class="pill"></span>
        <span class="hint">WebSocket port-forward (portforward.k8s.io).</span>
      </div>
    </div>
  </div>
</div>

## E. Scale & Rollout

Try increasing replicas or enabling a tiny canary rollout. These buttons become active after a session starts and actions are enabled.

- Scale to 2/3: updates the app's desired replica count. The controller reconciles containers until readyReplicas matches the new spec. Watch the status badge and events to see the change propagate.
- Canary 10%: enables a canary rollout policy and shifts a small portion of traffic to a new revision. This is a safe way to test changes with limited impact. You can later fine‑tune weight in "G. Rollout Controls" or revert to 0%.
- Requirements: an active session, "Enable Controlled Actions" toggled on, and an example applied (e.g., echo).

How to read canary results: a canary is not a separate app. It is a new revision of the same app created when the manifest changes. The base revision is the previous spec, the canary revision is the latest. If you only toggle canary, both revisions may run the same image and look identical — make a small change (like `spec.image` or an env value) to see a visible difference. The dashboard shows the base vs canary revision IDs in “Canary routes to revision” below.

- <button id="btn-scale-2" disabled>Scale to 2</button>
- <button id="btn-scale-3" disabled>Scale to 3</button>
- <button id="btn-canary-10" disabled>Canary 10%</button>

CLI (fallback):

```
# Scale using the CLI
python -m ae.cli scale <app> --replicas 2
python -m ae.cli scale <app> --replicas 3

# Pause/resume an in‑progress rollout
python -m ae.cli rollout pause <app>
python -m ae.cli rollout resume <app>
```

Canary via YAML (conceptual):

```yaml
spec:
  image: mendhak/http-https-echo:38 # any spec change creates a new revision
  rollout:
    strategy: canary
    weight: 10   # send ~10% of traffic to the canary
```

#### Notes
- Scaling edits only `spec.replicas`; the current image revision stays the same.
- Canary weights bias routing between the old and new revision; readiness gates still apply before traffic shifts.
- Canary actions bump replicas to at least 2 so the dashboard shows a distinct canary revision.
- To fully roll forward, increase weight gradually (see section "G. Rollout Controls"). To roll back, set weight to 0 or re‑apply the previous image.

### Verifiers

- Events show scaling: <span id="v-scale-events" data-v="pending">pending</span>
- Metrics present for app: <span id="v-scale-metrics" data-v="pending">pending</span>
- Canary routes to revision: <span id="canary-revision">n/a</span> (base: <span id="canary-base">n/a</span>)

## F. Ingress Test

- Open App: <a id="ingress-link" href="#" target="_blank" rel="noopener">(disabled)</a>
- Last check: <span id="ingress-check">n/a</span>
- DNS hint: <code id="ingress-hosts-hint"></code> <button id="ingress-hosts-copy" disabled>Copy</button>
- Direct curl (no hosts):<br/><code id="ingress-curl"></code> <button id="ingress-curl-copy" disabled>Copy</button>

## G. Rollout Controls

Advanced controls you can ignore on your first run. Use the slider to choose a small weight, then "Apply" to shift a portion of traffic to the canary image.

- <button id="btn-rollout-pause" disabled>Pause Rollout</button>
- <button id="btn-rollout-resume" disabled>Resume Rollout</button>
- Canary weight: <input type="range" id="canary-weight" min="0" max="10" step="1" value="3"/> <span id="canary-weight-val">3</span>
- <button id="btn-canary-apply" disabled>Apply Canary Weight</button>

## H. Reset

- <button id="btn-reset" type="button">Reset Session</button>

---

<details>
<summary><strong>Final Notes</strong></summary>

- If you see “Labs: unavailable”, you are in read‑only mode. To unlock Apply/Scale/Shell, start the controller with `AE_LABS=1`, set `AE_LABS_TOKEN`, then toggle “Enable Controlled Actions” and click “Use Token” + “Start Session”.
- “Open App” only appears when the example defines `spec.ingress`. If it is blank, pick an ingress example (echo) or use the shell/port‑forward tools instead.
- If the app host does not resolve, copy the DNS hint into your hosts file or use the Direct curl line in section F.
- If logs/events look empty, switch Status to “App” after applying an example; cluster mode only shows totals.
- Auto backend prefers k1s‑in‑Docker when compose is running, otherwise k1s‑host. Override the backend if the banner does not match what you started.

</details>

  </div>
  <div class="playground-rail">
    <button id="btn-reset-fab" class="lab-reset-fab" type="button" aria-label="Reset labs and playground" title="Reset lab environment and playground">
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M17.65 6.35C16.2 4.9 14.21 4 12 4V1L7 6l5 5V7c1.66 0 3.14.69 4.22 1.78S18 11.34 18 13c0 3.31-2.69 6-6 6s-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8c0-2.21-.9-4.21-2.35-5.65z"/>
      </svg>
    </button>
  </div>
</div>
