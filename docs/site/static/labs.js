(() => {
  const $ = (sel) => document.querySelector(sel);
  let API = '';
  const state = {
    backend: 'auto',
    sessionId: null,
    orch: { available: false, token: null },
    appName: 'echo',
    appApplied: false,
    canaryRevision: null,
    canaryBaseRevision: null,
    canaryWeight: null,
    statusMode: 'cluster', // 'cluster' | 'app'
  };
  const helmDemo = { timer: null, running: false };

  function setText(id, txt, cls) {
    const el = typeof id === 'string' ? $(id) : id;
    if (!el) return;
    el.textContent = txt;
    if (cls) { el.className = cls; }
  }

  function setCanaryInfo(rev, base, weight) {
    state.canaryRevision = rev ?? null;
    state.canaryBaseRevision = base ?? null;
    state.canaryWeight = weight ?? null;
    try {
      const revEl = document.getElementById('canary-revision');
      if (revEl) revEl.textContent = (rev != null ? `rev ${rev}` : 'n/a');
    } catch(_){}
    try {
      const baseEl = document.getElementById('canary-base');
      if (baseEl) baseEl.textContent = (base != null ? `rev ${base}` : 'n/a');
    } catch(_){}
  }

  function clearCanaryInfo() {
    setCanaryInfo(null, null, null);
  }

  async function jsonGet(url) {
    try {
      const r = await fetch(url, { headers: { 'Accept': 'application/json' } });
      if (!r.ok) {
        if (r.status >= 500 && (!API || API==='') && window.DOCS_API_BASE && typeof url === 'string' && url.startsWith('/')) {
          try { await switchToDirectApi('proxy 5xx'); } catch(_){}
          const r2 = await fetch(`${window.DOCS_API_BASE}${url}`, { headers: { 'Accept': 'application/json' } });
          if (!r2.ok) throw new Error(`${r2.status}`);
          return await r2.json();
        }
        throw new Error(`${r.status}`);
      }
      return await r.json();
    } catch (e) {
      if ((!API || API==='') && window.DOCS_API_BASE && typeof url === 'string' && url.startsWith('/')) {
        try { await switchToDirectApi('proxy network error'); } catch(_){}
        const r3 = await fetch(`${window.DOCS_API_BASE}${url}`, { headers: { 'Accept': 'application/json' } });
        if (!r3.ok) throw new Error(`${r3.status}`);
        return await r3.json();
      }
      throw e;
    }
  }

  async function apiFetch(path, opts) {
    const rel = String(path||'');
    const url = `${API||''}${rel.startsWith('/')? rel : ('/' + rel)}`;
    try {
      const r = await fetch(url, opts||{});
      if (r.status >= 500 && (!API || API==='') && window.DOCS_API_BASE) {
        try { await switchToDirectApi('proxy 5xx'); } catch(_){}
        return await fetch(`${window.DOCS_API_BASE}${rel.startsWith('/')? rel : ('/' + rel)}`, opts||{});
      }
      return r;
    } catch (e) {
      if ((!API || API==='') && window.DOCS_API_BASE) {
        try { await switchToDirectApi('proxy network error'); } catch(_){}
        return await fetch(`${window.DOCS_API_BASE}${rel.startsWith('/')? rel : ('/' + rel)}`, opts||{});
      }
      throw e;
    }
  }

  function labsHeaders(extra) {
    const headers = Object.assign({}, extra || {});
    const tok = state.orch.token || (sessionStorage.getItem('labsToken')||'').trim();
    if (tok) headers['Authorization'] = `Bearer ${tok}`;
    return headers;
  }

  async function switchToDirectApi(reason){
    if (!window.DOCS_API_BASE) return;
    API = window.DOCS_API_BASE;
    try { setText('#env-api-base', API); } catch(_){}
    try { banner(`Switching to direct API (${reason})`, 'warn'); } catch(_){}
    try { if (state.sessionId) armSSE(); } catch(_){}
    try { refreshStatusNow(); } catch(_){}
  }

  // API curl hint (for quick local checks when API looks down)
  function setApiCurlHint(isFail) {
    try {
      const el = document.getElementById('api-curl');
      const btn = document.getElementById('api-curl-copy');
      if (!el || !btn) return;
      const cmd = 'curl -sS http://127.0.0.1:9108/openapi.json';
      el.textContent = cmd;
      btn.disabled = false;
      if (isFail) { try { el.classList.add('attn'); } catch(_){} }
    } catch(_){}
  }

  async function copyApiCurlHint() {
    try {
      const el = document.getElementById('api-curl');
      const txt = (el && el.textContent) ? el.textContent.trim() : '';
      if (!txt) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(txt);
      } else {
        const ta = document.createElement('textarea');
        ta.value = txt; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
      }
      try { toast('Copied API curl', 'ok'); } catch(_){}
    } catch(_){}
  }

  // Lightweight toast and banner helpers
  function toast(msg, type) {
    // Avoid duplicate overlays when the banner is already visible.
    try {
      const b = document.getElementById('banner');
      if (b && !b.classList.contains('hidden')) return;
    } catch(_){}
    let t = document.getElementById('toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'toast';
      t.className = 'ribbon';
      document.body.prepend(t);
    }
    t.textContent = msg;
    t.className = 'ribbon ' + (type||'ok');
    clearTimeout(t._timer);
    t._timer = setTimeout(()=>{ t.className = 'ribbon hidden'; t.textContent=''; }, 3000);
  }

  function banner(msg, type, durationMs) {
    const b = document.getElementById('banner');
    if (!b) return;
    // Reset contents
    b.innerHTML = '';
    const span = document.createElement('span');
    span.textContent = msg;
    const btn = document.createElement('button');
    btn.textContent = 'Clear';
    btn.className = 'close-btn';
    btn.addEventListener('click', () => { b.className = 'ribbon hidden'; b.innerHTML=''; clearTimeout(b._hideTimer); });
    b.appendChild(btn);
    b.appendChild(span);
    b.className = `ribbon ${type||'fail'}`;
    b.classList.remove('hidden');
    try { clearTimeout(b._hideTimer); } catch(_){}
    const ms = Math.max(5000, Math.min(8000, Number(durationMs||6000)));
    b._hideTimer = setTimeout(() => { b.className = 'ribbon hidden'; b.innerHTML=''; }, ms);
  }

  async function handleLabsAuth(resp, actionLabel) {
    if (!resp || (resp.status !== 401 && resp.status !== 403)) {
      return false;
    }
    const label = actionLabel || 'Action';
    try {
      banner(`${label} requires a Labs token. Paste AE_LABS_TOKEN and click “Use Token”.`, 'fail');
      const inp = document.getElementById('labs-token');
      if (inp) { inp.classList.add('attn'); inp.focus(); }
    } catch(_){}
    return true;
  }

  function bannerFetchFailure(actionLabel, err) {
    const label = actionLabel || 'Action';
    const msg = String(err || '');
    if (msg.toLowerCase().includes('failed to fetch')) {
      banner(`${label} failed: API unreachable or blocked by the browser (TLS/mixed content). Confirm docs proxy is up at https://docs.home.arpa:8443 and API mode is Proxy.`, 'fail', 8000);
      return;
    }
    banner(`${label} error: ${err}`, 'fail');
  }


  // Expose banner helper for ad-hoc feedback from other scripts
  try { window.k1sBanner = banner; } catch(_) {}

  // Button busy/feedback helper: shows the banner immediately and toggles a busy style
  async function withButtonFeedback(btn, msg, fn){
    try {
      if (btn) { btn.classList.add('is-busy'); btn.setAttribute('aria-busy','true'); }
      try { banner(msg, 'pending', 7000); } catch(_){}
      return await fn();
    } finally {
      try { if (btn) { btn.classList.remove('is-busy'); btn.removeAttribute('aria-busy'); } } catch(_){}
    }
  }

  // DNS hint helpers
  function setHostsHint(txt) {
    try {
      setText('#ingress-hosts-hint', txt || '');
      const btn = document.getElementById('ingress-hosts-copy');
      if (btn) btn.disabled = !(txt && txt.trim());
    } catch(_){}
  }

  async function copyHostsHint() {
    try {
      const el = document.getElementById('ingress-hosts-hint');
      const txt = (el && el.textContent) ? el.textContent.trim() : '';
      if (!txt) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(txt);
      } else {
        // Fallback: temporary textarea
        const ta = document.createElement('textarea');
        ta.value = txt; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
      }
      try { toast('Copied hosts entry', 'ok'); } catch(_){}
    } catch(_){}
  }

  // Direct curl helpers for ingress testing without /etc/hosts edits
  function setCurlHint(urlStr) {
    try {
      const el = document.getElementById('ingress-curl');
      const btn = document.getElementById('ingress-curl-copy');
      if (!el || !btn) return;
      let cmd = '';
      try {
        const u = new URL(urlStr);
        const scheme = (u.protocol || 'https:').replace(':','');
        const host = u.hostname;
        // If no port in URL, assume dev defaults
        let port = u.port;
        if (!port) port = (scheme === 'https') ? '8443' : '8888';
        const path = u.pathname || '/';
        const qs = u.search || '';
        const full = `${scheme}://${host}:${port}${path}${qs}`;
        const k = (scheme === 'https') ? '-k ' : '';
        cmd = `curl ${k}-sS --resolve "${host}:${port}:127.0.0.1" "${full}"`;
      } catch { cmd = ''; }
      el.textContent = cmd;
      btn.disabled = !(cmd && cmd.trim());
    } catch(_){}
  }

  async function copyCurlHint() {
    try {
      const el = document.getElementById('ingress-curl');
      const txt = (el && el.textContent) ? el.textContent.trim() : '';
      if (!txt) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(txt);
      } else {
        const ta = document.createElement('textarea');
        ta.value = txt; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
      }
      try { toast('Copied curl command', 'ok'); } catch(_){}
    } catch(_){}
  }

  async function initEnv() {
    // Prefer same-origin proxy if available; fallback to configured DOCS_API_BASE
    const pref = (localStorage.getItem('docsApiMode')||'proxy');
    const bs = document.getElementById('backend-status');
    if (bs) { bs.textContent = 'Detecting environment…'; }
    if (pref === 'direct') {
      API = window.DOCS_API_BASE || 'http://127.0.0.1:9108';
    } else {
      try {
        const r = await fetch('/health');
        if (r.ok) { API = ''; }
        else { throw new Error('no proxy'); }
      } catch {
        API = window.DOCS_API_BASE || 'http://127.0.0.1:9108';
      }
    }
    setText('#env-api-base', API || '(same origin)');
    // Prefill labs token from docs build (if provided) or session storage
    try {
      const envTok = (window.DOCS_LABS_TOKEN || '').trim();
      const savedTok = (sessionStorage.getItem('labsToken')||'').trim();
      const tok = envTok || savedTok || '';
      if (tok) {
        try { sessionStorage.setItem('labsToken', tok); } catch(_){ }
        state.orch.token = tok;
        const inp = document.getElementById('labs-token');
        if (inp) inp.value = tok;
      }
    } catch(_){}
    try { const dash = document.getElementById('open-dashboard'); if (dash) { dash.href = `${API}/dashboard`; dash.classList.remove('hidden'); } } catch(_){}
    // Check API health regardless of labs availability
    let apiHealth = 'unknown', apiClass = 'pending';
    try {
      const h = await fetch(`${API}/health`);
      apiHealth = h.ok ? 'ok' : `error ${h.status}`;
      apiClass = h.ok ? 'ok' : 'fail';
    } catch { apiHealth = 'unreachable'; apiClass = 'fail'; }
    if (bs) { bs.innerHTML = `API: <span class="pill ${apiClass}">${apiHealth}</span>`; }
    try { setApiCurlHint(apiClass === 'fail'); } catch(_){}

    // Orchestrator discovery (proxied). Interpret 401/403 as "auth required".
    try {
      const tok = (sessionStorage.getItem('labsToken')||'').trim();
      const headers = tok ? { 'Authorization': `Bearer ${tok}` } : {};
      const resp = await fetch(`${API}/labs/info`, { method: 'POST', headers });
      if (resp.ok) {
        const info = await resp.json();
        state.orch.available = true;
        setText('#env-orch', 'available', 'ok');
        if (bs) {
          const backs = (info.backends||[]).map(b=>`<code>${b}</code>`).join(', ');
          const k3d = info.k3d||{};
          bs.innerHTML = `Backends: ${backs} ${k3d.present? `(k3d present: http ${k3d.ports?.http||8081}, https ${k3d.ports?.https||8444})` : '(k3d not detected)'} · <span id="ribbon-api" class="pill">API: checking...</span> · <span id="ribbon-labs" class="pill ok">Labs: available</span>`;
          // Update the API pill now that the element exists
          try {
            const h2 = await fetch(`${API}/health`);
            const ok2 = h2.ok;
            const elApi = document.getElementById('ribbon-api');
            if (elApi) { elApi.textContent = ok2 ? 'API: ok' : `API: error ${h2.status}`; elApi.className = 'pill ' + (ok2 ? 'ok' : 'fail'); }
          } catch { try { const elApi = document.getElementById('ribbon-api'); if (elApi) { elApi.textContent = 'API: unreachable'; elApi.className = 'pill fail'; } } catch(_){} }
          const btnEnsure = document.getElementById('btn-k3d-ensure');
          const linkIngress = document.getElementById('k3d-open-ingress');
          if (btnEnsure) { btnEnsure.classList.remove('hidden'); }
          if (linkIngress) {
            const https = (k3d.ports && k3d.ports.https) || 8444;
            linkIngress.href = `https://localhost:${https}/`;
            linkIngress.classList.remove('hidden');
          }
        }
      } else if (resp.status === 401 || resp.status === 403) {
        state.orch.available = false;
        setText('#env-orch', 'auth required', 'fail');
        if (bs) {
          bs.innerHTML = `${bs.innerHTML} · Labs: <span class=\"pill fail\">auth required</span> · Mode: <code>${pref}</code> · Read-only session available`;
        }
        banner('Labs require a token. Paste AE_LABS_TOKEN and click “Use Token”.', 'fail');
        try {
          const inp = document.getElementById('labs-token');
          const nud = document.getElementById('labs-token-nudge');
          if (nud) nud.classList.remove('hidden');
          if (inp) { inp.classList.add('attn'); inp.focus(); }
        } catch(_){}
      } else {
        state.orch.available = false;
        setText('#env-orch', 'unavailable', 'muted');
        if (bs) {
          bs.innerHTML = `${bs.innerHTML} · Labs: <span class=\"pill fail\">unavailable</span> · Mode: <code>${pref}</code> · Read-only session available`;
        }
      }
    } catch {
      state.orch.available = false;
      setText('#env-orch', 'unavailable', 'muted');
      if (bs) {
        bs.innerHTML = `${bs.innerHTML} · Labs: <span class=\"pill fail\">unavailable</span> · Mode: <code>${pref}</code> · Read-only session available`;
      }
    }
    // Enable Start Session when actions are toggled and orch available
    const btnStart = $('#btn-start-session');
    const toggle = $('#toggle-actions');
    const sel = $('#backend-select');
    const updateStart = () => {
      if (!btnStart) return;
      if (state.orch.available) {
        btnStart.disabled = !(toggle && toggle.checked);
        btnStart.textContent = 'Start Session';
      } else {
        // Allow read-only sessions even without orchestrator
        btnStart.disabled = false;
        btnStart.textContent = 'Start Read-only Session';
      }
    };
    if (toggle) toggle.addEventListener('change', updateStart);
    if (sel) sel.addEventListener('change', (e)=>{ state.backend = e.target.value; });
    updateStart();
    // Prefill labs token from prior session if present
    try {
      const savedTok = (sessionStorage.getItem('labsToken')||'').trim();
      const inp = document.getElementById('labs-token');
      if (savedTok && inp) inp.value = savedTok;
    } catch(_){ }
    // Always kick read-only verifiers once so the page isn't "stuck" when
    // the orchestrator is unavailable; uses default app name (echo or session-derived)
    try { verifyApply(); } catch(_){}
    // Ensure Start button state reflects final orch availability
    try { updateStart(); } catch(_){}
    // Make Apply button provide guidance even before starting a session
    try { wireControls(); } catch(_){}
  }

  // Re-run Labs availability detection (used after setting token)
  async function recheckLabs() {
    const bs = document.getElementById('backend-status');
    const modePref = (localStorage.getItem('docsApiMode')||'proxy');
    try {
      const tok = (sessionStorage.getItem('labsToken')||'').trim();
      const headers = tok ? { 'Authorization': `Bearer ${tok}` } : {};
      const resp = await fetch(`${API}/labs/info`, { method: 'POST', headers });
      if (resp.ok) {
        const info = await resp.json();
        state.orch.available = true;
        setText('#env-orch', 'available', 'ok');
        if (bs) {
          const backs = (info.backends||[]).map(b=>`<code>${b}</code>`).join(', ');
          const k3d = info.k3d||{};
          bs.innerHTML = `Backends: ${backs} ${k3d.present? `(k3d present: http ${k3d.ports?.http||8081}, https ${k3d.ports?.https||8444})` : '(k3d not detected)'} · <span id=\"ribbon-api\" class=\"pill\">API: checking...</span> · <span id=\"ribbon-labs\" class=\"pill ok\">Labs: available</span>`;
          // Update the API pill now that the element exists (mirror initEnv)
          try {
            const h2 = await fetch(`${API}/health`);
            const ok2 = h2.ok;
            const elApi = document.getElementById('ribbon-api');
            if (elApi) {
              elApi.textContent = ok2 ? 'API: ok' : `API: error ${h2.status}`;
              elApi.className = 'pill ' + (ok2 ? 'ok' : 'fail');
            }
          } catch {
            try {
              const elApi = document.getElementById('ribbon-api');
              if (elApi) { elApi.textContent = 'API: unreachable'; elApi.className = 'pill fail'; }
            } catch(_){}
          }
        }
        try { refreshHelmDemoStatus(true); } catch(_){}
        return 'ok';
      }
      if (resp.status === 401 || resp.status === 403) {
        state.orch.available = false;
        setText('#env-orch', 'auth required', 'fail');
        if (bs) {
          bs.innerHTML = `${bs.innerHTML} · Labs: <span class="pill fail">auth required</span> · Mode: <code>${modePref}</code> · Read-only session available`;
        }
        banner('Labs require a token. Paste AE_LABS_TOKEN and click “Use Token”.', 'fail');
        try { fallbackToReadOnlyUI(); } catch(_){}
        try { updateHelmDemoUI({ running: false, message: 'Labs token required.' }); } catch(_){}
        return 'auth';
      }
      state.orch.available = false;
      setText('#env-orch', 'unavailable', 'muted');
      if (bs) {
        bs.innerHTML = `${bs.innerHTML} · Labs: <span class="pill fail">unavailable</span> · Mode: <code>${modePref}</code> · Read-only session available`;
      }
      try { fallbackToReadOnlyUI(); } catch(_){}
      try { updateHelmDemoUI({ running: false, message: 'Labs backend unavailable.' }); } catch(_){}
      return 'unavail';
    } catch {
      state.orch.available = false;
      setText('#env-orch', 'unavailable', 'muted');
      try {
        const bs2 = document.getElementById('backend-status');
        if (bs2) bs2.innerHTML = `${bs2.innerHTML} · Labs: <span class="pill fail">unavailable</span> · Mode: <code>${modePref}</code> · Read-only session available`;
      } catch {}
      try { fallbackToReadOnlyUI(); } catch(_){}
      try { updateHelmDemoUI({ running: false, message: 'Labs backend unavailable.' }); } catch(_){}
      return 'error';
    }
  }

  async function refreshHelmDemoStatus(quiet) {
    const statusEl = document.getElementById('helm-demo-status');
    if (!statusEl) return;
    if (!state.orch.available) {
      updateHelmDemoUI({ running: false, message: 'Labs backend unavailable.' });
      return;
    }
    try {
      const resp = await apiFetch(`/labs/helm-demo`, {
        method: 'POST',
        headers: labsHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ action: 'status' })
      });
      if (!resp.ok) throw new Error(await resp.text());
      const data = await resp.json();
      updateHelmDemoUI(data);
    } catch (e) {
      if (!quiet) banner(`Helm demo status error: ${e}`, 'fail');
      updateHelmDemoUI({ running: false, message: 'Helm demo status unavailable.' });
    }
  }

  async function startHelmDemo() {
    if (!state.orch.available) {
      banner('Labs backend unavailable — paste a token and start a session first.', 'fail');
      return;
    }
    const btn = document.getElementById('btn-helm-demo');
    await withButtonFeedback(btn, 'Starting Helm shim demo…', async () => {
      const resp = await apiFetch(`/labs/helm-demo`, {
        method: 'POST',
        headers: labsHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ action: 'start' })
      });
      if (await handleLabsAuth(resp, 'Helm demo start')) { return; }
      if (!resp.ok) { banner(`Helm demo start failed: ${await resp.text()}`, 'fail'); return; }
      const data = await resp.json();
      updateHelmDemoUI(data);
      try { toast('Helm demo started', 'ok'); } catch(_){}
    });
  }

  async function stopHelmDemo() {
    const btn = document.getElementById('btn-helm-demo-stop');
    await withButtonFeedback(btn, 'Stopping Helm demo…', async () => {
      const resp = await apiFetch(`/labs/helm-demo`, {
        method: 'POST',
        headers: labsHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ action: 'stop' })
      });
      if (await handleLabsAuth(resp, 'Helm demo stop')) { return; }
      if (!resp.ok) { banner(`Helm demo stop failed: ${await resp.text()}`, 'fail'); return; }
      const data = await resp.json();
      updateHelmDemoUI(data);
    });
  }

  function updateHelmDemoUI(data) {
    const statusEl = document.getElementById('helm-demo-status');
    const logEl = document.getElementById('helm-demo-log');
    const runBtn = document.getElementById('btn-helm-demo');
    const stopBtn = document.getElementById('btn-helm-demo-stop');
    if (!statusEl) return;
    const running = !!(data && data.running);
    helmDemo.running = running;
    let text = running ? 'Helm demo running…' : 'Helm demo idle.';
    if (data) {
      if (running && data.port) {
        text = `Helm demo running (shim port ${data.port})`;
      } else if (!running && data.exit_code !== undefined && data.exit_code !== null) {
        text = data.exit_code === 0 ? 'Helm demo completed successfully.' : `Helm demo failed (exit ${data.exit_code}).`;
      }
      if (data.message) {
        text += ` ${data.message}`;
      }
    }
    statusEl.textContent = text;
    if (logEl) {
      const log = (data && data.log ? String(data.log) : '').trim();
      if (log) {
        logEl.textContent = log;
        logEl.classList.remove('hidden');
      } else {
        logEl.textContent = '';
        logEl.classList.add('hidden');
      }
    }
    if (runBtn) runBtn.disabled = !state.orch.available || running;
    if (stopBtn) stopBtn.disabled = !running;
    if (helmDemo.timer) {
      clearInterval(helmDemo.timer);
      helmDemo.timer = null;
    }
    if (running) {
      helmDemo.timer = setInterval(() => refreshHelmDemoStatus(true), 5000);
    }
  }
  window.k1sRecheckLabs = recheckLabs;

  function randId(n=6){
    const a='abcdefghijklmnopqrstuvwxyz0123456789';
    let s='';
    for(let i=0;i<n;i++){ s += a[Math.floor(Math.random()*a.length)]; }
    return s;
  }

  async function startSession() {
    if (!state.orch.available) {
      // local-only session id for prefixing UI; no server token
      state.sessionId = randId();
      setText('#session-id', state.sessionId);
      state.appName = `echo-${state.sessionId}`;
      wireControls();
      // Ensure any SSE placeholders are disabled in read-only mode
      try {
        const ids = ['logs-sse','events-sse','status-summary'];
        ids.forEach(id => {
          const el = document.getElementById(id);
          if (el) el.removeAttribute('sse-connect');
        });
        // Prefer the non-HTMX panels
        const poly = document.getElementById('observe-logs');
        const polyEv = document.getElementById('observe-events');
        if (poly) poly.classList.remove('hidden');
        if (polyEv) polyEv.classList.remove('hidden');
      } catch(_){}
      // Show neutral status
      setText('#status-summary', 'n/a', 'pending');
      return;
    }
    try {
      const headers = { 'Content-Type': 'application/json' };
      try {
        const tok = state.orch.token || (sessionStorage.getItem('labsToken')||'').trim();
        if (tok) headers['Authorization'] = `Bearer ${tok}`;
      } catch(_){}
      const r = await fetch(`${API}/labs/session`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ backend: state.backend })
      });
      if (r.status === 401 || r.status === 403) {
        banner('Session requires a Labs token. Paste the token and click “Use Token”.', 'fail');
        try { const inp = document.getElementById('labs-token'); if (inp) { inp.classList.add('attn'); inp.focus(); } } catch(_){}
        return;
      }
      const data = await r.json();
      if (!r.ok || !data.session_id) throw new Error(data.error?.message || 'session');
      state.sessionId = data.session_id;
      setText('#session-id', state.sessionId);
      state.appName = `echo-${state.sessionId}`;
      state.orch.token = data.token || null;
      state.appApplied = false;
      wireControls();
      armSSE();
      try { toast('Session started — next: click "Apply Selected Example"', 'ok'); } catch(_){}
      try { setText('#status-summary', 'no app yet', 'pending'); } catch(_){}
    } catch (e) {
      setText('#env-notes', `Failed to start session: ${e}`, 'fail');
    }
  }

  // Reset UI to a safe read-only state when checks become unreachable
  function fallbackToReadOnlyUI(){
    try { banner('Backend unreachable — switching to read-only UI.', 'fail', 4000); } catch(_){}
    // Clear client session state
    state.sessionId = null;
    state.appApplied = false;
    state.appName = 'echo';
    // Stop/disable SSE and prefer non-HTMX panels
    try {
      const ids = ['logs-sse','events-sse','status-summary'];
      ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.removeAttribute('sse-connect');
      });
      const polyLogs = document.getElementById('observe-logs');
      const polyEv = document.getElementById('observe-events');
      if (polyLogs) polyLogs.classList.remove('hidden');
      if (polyEv) polyEv.classList.remove('hidden');
      const sseLogs = document.getElementById('logs-sse');
      const sseEv = document.getElementById('events-sse');
      if (sseLogs) sseLogs.classList.add('hidden');
      if (sseEv) sseEv.classList.add('hidden');
    } catch(_){}
    // Clear panels and indicators
    try { const ev = document.getElementById('observe-events'); if (ev) ev.textContent = ''; } catch(_){}
    try { const lg = document.getElementById('observe-logs'); if (lg) lg.textContent = ''; } catch(_){}
    try { setText('#v-apply-events','n/a','pending'); } catch(_){}
    try { setText('#v-apply-ready','n/a','pending'); } catch(_){}
    try { setText('#ingress-check','n/a','pending'); } catch(_){}
    try { setText('#ingress-curl',''); const b=document.getElementById('ingress-curl-copy'); if (b) b.disabled=true; } catch(_){}
    try { setHostsHint(''); } catch(_){}
    // Reset status to cluster-wide summary and refresh
    try { setStatusMode('cluster'); refreshStatusNow(); } catch(_){}
    // Disable action buttons now that we have no session
    try { wireControls(); } catch(_){}
    // Show neutral session indicator
    setText('#session-id','(none)');
  }

  function wireControls() {
    // Enable buttons if we have controlled actions, otherwise keep disabled
    const enableActions = $('#toggle-actions')?.checked && state.sessionId && state.orch.available;
    // Keep Apply clickable to surface guidance even when actions are unavailable
    ['#btn-scale-2','#btn-scale-3','#btn-canary-10','#btn-canary-apply','#btn-observe-toggle','#btn-reset']
      .forEach(id=>{ const el=$(id); if (el) el.disabled = !enableActions; });
    const btnApply = $('#btn-apply-echo');
    if (btnApply) {
      // Provide visual hint when actions are disabled
      if (!enableActions) {
        btnApply.removeAttribute('disabled');
        btnApply.title = 'Start Session and enable Controlled Actions to apply from the browser';
      } else {
        btnApply.removeAttribute('title');
      }
    }
    ['#btn-rollout-pause','#btn-rollout-resume']
      .forEach(id=>{ const el=$(id); if (el) el.disabled = !enableActions; });
    const canarySlider = $('#canary-weight');
    if (canarySlider) canarySlider.disabled = !enableActions;
    // Ingress link always enabled as a raw link to host when status has host
    verifyApply(); // kick initial verifiers
    refreshStatusNow();
  }

  function armSSE(){
    const app = state.appName;
    if (!app) return;
    if (!state.orch.available) return; // do not arm SSE without orchestrator
    // For logs, prefer explicit EventSource to the polyfill panel for reliability
    const sseLogs = document.getElementById('logs-sse');
    if (sseLogs) { try { sseLogs.classList.add('hidden'); } catch(_){} }
    const poly = document.getElementById('observe-logs');
    if (poly) { try { poly.classList.remove('hidden'); } catch(_){} }
    // HTMX SSE for events
    const sseEv = document.getElementById('events-sse');
    if (window.htmx && sseEv) {
      const params = { app, limit: '20' };
      try { if (state.orch && state.orch.token) params['token'] = state.orch.token; } catch(_){}
      sseEv.setAttribute('sse-connect', `${API}/labs/sse/events_html?` + new URLSearchParams(params).toString());
      try { window.htmx.process(sseEv); } catch(_){}
      sseEv.classList.remove('hidden');
      const polyEv = document.getElementById('observe-events');
      if (polyEv) polyEv.classList.add('hidden');
    }
    // HTMX SSE for status badge
    const sseStatus = document.getElementById('status-summary');
    if (window.htmx && sseStatus) {
      const qs = new URLSearchParams({ app });
      try { if (state.orch && state.orch.token) qs.set('token', state.orch.token); } catch(_){}
      sseStatus.setAttribute('sse-connect', `${API}/labs/sse/status_badge?` + qs.toString());
      try { window.htmx.process(sseStatus); } catch(_){}
    }
  }

  function makeIngressUrl(host, path) {
    const proto = (location.protocol || 'https:');
    const isDefault = (proto === 'https:' ? '443' : '80');
    const port = (location.port && location.port !== isDefault) ? (':' + location.port) : '';
    // If host already specifies a port, respect it
    if (/:[0-9]+$/.test(host)) {
      return `${proto}//${host}${path||'/'}`;
    }
    return `${proto}//${host}${port}${path||'/'}`;
  }

  async function verifyApply() {

    // Skip per-app probes until an app from this session has been applied
    if (!state.appApplied) {
      try { setText('#v-apply-events','no app yet','pending'); } catch(_){}
      try { setText('#v-apply-ready','no app yet','pending'); } catch(_){}
      return;
    }
    // Events check
    try {
      const ev = await jsonGet(`${API}/events/${encodeURIComponent(state.appName)}?limit=10`);
      const ok = Array.isArray(ev) && ev.some(e => {
        const m = (e.message||'').toLowerCase();
        const t = (e.event_type||'').toLowerCase();
        return m.includes('revision') || t.includes('applycompleted');
      });
      setText('#v-apply-events', ok?'ok':'fail', ok?'ok':'fail');
    } catch { setText('#v-apply-events','pending','pending'); }
    // Status ready check
    try {
      const st = await jsonGet(`${API}/status/${encodeURIComponent(state.appName)}`);
      const ok = Number(st.readyReplicas||0) === Number((st.spec||{}).replicas||0);
      setText('#v-apply-ready', ok?'ok':'fail', ok?'ok':'fail');
      setText('#status-summary', `${st.readyReplicas||0}/${(st.spec||{}).replicas||0} ready`, ok?'ok':'fail');
      // ingress link
      const link = $('#ingress-link');
      if (link && st.ingress_host) {
        link.textContent = st.ingress_host;
        const href = st.ingress_host.startsWith('http')
          ? st.ingress_host + (st.ingress_path||'/')
          : makeIngressUrl(st.ingress_host, st.ingress_path);
        link.href = href;
        try { setCurlHint(href); } catch(_){}
        // Kick a best‑effort ingress check only after a successful session
        try { if (state.sessionId && state.orch.available) verifyIngress(); } catch(_){}
      }
    } catch (e) {
      try {
        const msg = String(e||'');
        if (msg.includes('404')) {
          setText('#v-apply-ready','no app yet','pending');
          setText('#status-summary','no app yet','pending');
        } else {
          setText('#v-apply-ready','pending','pending');
        }
      } catch(_) { setText('#v-apply-ready','pending','pending'); }
    }
  }

  // Cluster summary (totals across all apps)
  async function refreshClusterSummary(){
    try {
      const data = await jsonGet(`${API}/status?limit=200`);
      const items = Array.isArray(data.items) ? data.items : [];
      const desired = items.reduce((n, s) => n + Number(s.desired_replicas||0), 0);
      const ready = items.reduce((n, s) => n + Number(s.ready_replicas||0), 0);
      const apps = items.length;
      const ok = desired > 0 ? (ready === desired) : (apps === 0);
      setText('#status-summary', `${ready}/${desired} ready (apps ${apps})`, ok ? 'ok' : (desired===0 ? 'pending' : 'fail'));
    } catch {
      setText('#status-summary', 'unreachable', 'fail');
    }
  }
  // App summary (selected app only)
  async function refreshAppSummary(){
    try {
      if (!state.appApplied) { setText('#status-summary','no app yet','pending'); return; }
      const st = await jsonGet(`${API}/status/${encodeURIComponent(state.appName)}`);
      const desired = Number((st.spec||{}).replicas||0);
      const ready = Number(st.readyReplicas||0);
      const ok = desired > 0 ? (ready === desired) : false;
      setText('#status-summary', `${ready}/${desired} ready`, ok? 'ok':'fail');
    } catch { setText('#status-summary','pending','pending'); }
  }

  function refreshStatusNow(){
    if (state.statusMode === 'app') { refreshAppSummary(); }
    else { refreshClusterSummary(); }
  }

  function setStatusMode(mode){
    state.statusMode = mode;
    try {
      const appBtn = document.getElementById('status-mode-app');
      const clusterBtn = document.getElementById('status-mode-cluster');
      const note = document.getElementById('status-mode-note');
      const isApp = mode === 'app';
      if (appBtn && clusterBtn) {
        appBtn.classList.toggle('is-active', isApp);
        clusterBtn.classList.toggle('is-active', !isApp);
        appBtn.setAttribute('aria-pressed', isApp ? 'true' : 'false');
        clusterBtn.setAttribute('aria-pressed', (!isApp).toString());
      }
      if (note) {
        note.textContent = isApp
          ? 'App shows readiness for the example you applied.'
          : 'Cluster shows totals across all apps. Switch to App after applying an example.';
      }
    } catch(_){}
  }
  // Example YAML loader (served from /examples/*.yaml by docs build)
  async function loadExampleYaml(name){
    try {
      const path = `/examples/${name}.yaml`;
      const r = await fetch(path, { cache: 'no-store' });
      const txt = r.ok ? await r.text() : '(example file not found)';
      const el = document.getElementById('example-yaml');
      if (el) el.textContent = txt;
    } catch {
      try { const el = document.getElementById('example-yaml'); if (el) el.textContent = '(failed to load example)'; } catch(_){}
    }
  }


  async function verifyScale(expected){
    // Verify via status first (readiness equality)
    let desired = null, ready = null, readyMatches = null;
    try {
      const st = await jsonGet(`${API}/status/${encodeURIComponent(state.appName)}`);
      desired = Number((st.spec||{}).replicas||0);
      ready = Number(st.readyReplicas||0);
      const okDesired = (typeof expected === 'number') ? (desired === expected) : true;
      const okReady = ready === desired && desired > 0;
      readyMatches = okDesired && okReady;
    } catch {}
    // Metrics presence check (not readiness equality).
    try {
      const txt = await (await fetch(`${API}/metrics`, { cache: 'no-store' })).text();
      const app = state.appName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const reNew = new RegExp(`^ae_app_ready_replicas\\{[^}]*app=\\"${app}\\"[^}]*\\}\\s+\\d+`, 'm');
      const reAlias = new RegExp(`^ae_ready_replicas\\{[^}]*app=\\"${app}\\"[^}]*\\}\\s+\\d+`, 'm');
      const present = reNew.test(txt) || reAlias.test(txt);
      setText('#v-scale-metrics', present ? 'ok' : 'pending', present ? 'ok' : 'pending');
    } catch { setText('#v-scale-metrics','pending','pending'); }
    // Events show a reconcile happened (ApplyStarted/ApplyCompleted with a revision)
    try {
      const ev = await jsonGet(`${API}/events/${encodeURIComponent(state.appName)}?limit=10`);
      const ok = Array.isArray(ev) && ev.some(e => {
        const t = (e.event_type||'').toLowerCase();
        const m = (e.message||'').toLowerCase();
        return t.includes('applycompleted') || m.includes('revision');
      });
      setText('#v-scale-events', ok?'ok':'fail', ok?'ok':'fail');
    } catch { setText('#v-scale-events','pending','pending'); }
    if (readyMatches !== null && desired !== null && ready !== null) {
      setText('#status-summary', `${ready}/${desired} ready`, readyMatches ? 'ok' : 'fail');
    }
  }

  async function verifyIngress(){
    // Use server-side check to avoid browser TLS constraints in dev
    const a = document.getElementById('ingress-link');
    if (!a || !a.href || a.href === '#' ) { setText('#ingress-check','n/a','pending'); return; }
    const payload = { url: a.href };
    try {
      const r = await apiFetch(`/labs/ingress_check`, {
        method: 'POST', headers: { 'Content-Type':'application/json', ...(state.orch.token? { 'Authorization': `Bearer ${state.orch.token}` } : {}) },
        body: JSON.stringify(payload)
      });
      if (!r.ok) { setText('#ingress-check','unreachable','fail'); return; }
      const j = await r.json();
      const dt = Number(j.elapsed_ms||0);
      if (j.ok) { setText('#ingress-check', `reachable (~${dt}ms)`, 'ok'); setHostsHint(''); return; }
      // Allow local override for self-signed TLS
      const allow = (localStorage.getItem('labsAllowUntrusted')||'').trim() === '1';
      if (allow) { setText('#ingress-check', 'untrusted TLS (dev)', 'ok'); setHostsHint(''); return; }
      setText('#ingress-check', j.code ? `error ${j.code}` : 'unreachable', 'fail');
      try {
        const u = new URL(a.href); const host = u.hostname;
        if (host && /\.home\.arpa$/i.test(host)) setHostsHint(`Add to /etc/hosts: 127.0.0.1 ${host}`);
        try { setCurlHint(a.href); } catch(_){}
      } catch(_){}
    } catch {
      setText('#ingress-check','unreachable','fail');
    }
  }

  function bind() {
    $('#btn-start-session')?.addEventListener('click', startSession);
    // Load YAML preview on example change
    const sel = document.getElementById('example-select');
    if (sel) {
      sel.addEventListener('change', ()=>{ try { loadExampleYaml(sel.value||'echo'); } catch(_){ } });
      try { loadExampleYaml(sel.value||'echo'); } catch(_){ }
    }
    $('#btn-apply-echo')?.addEventListener('click', async(e) => {
      // Guardrails and user guidance instead of silent no-op
      const actionsEnabled = $('#toggle-actions')?.checked && state.sessionId && state.orch.available;
      const selNow = document.getElementById('example-select');
      const example = selNow && selNow.value ? selNow.value : 'echo';
      if (!state.orch.available) {
        banner(`Controlled actions are unavailable. Start a session and enable "Enable Controlled Actions", or run: ae apply -f specs/examples/${example}.yaml`, 'fail');
        return;
      }
      if (!state.sessionId) {
        banner('Start Session first to namespace your app (echo-<session>).', 'fail');
        return;
      }
      if (!$('#toggle-actions')?.checked) {
        banner('Toggle "Enable Controlled Actions" to allow applies from the browser.', 'fail');
        return;
      }
      const btn = e.currentTarget || document.getElementById('btn-apply-echo');
      await withButtonFeedback(btn, `Submitting apply for “${state.appName}”…`, async () => {
        // use computed `example`
        try {
          const resp = await apiFetch(`/labs/apply`, {
            method: 'POST',
            headers: {
              'Content-Type':'application/json',
              ...(state.orch.token? { 'Authorization': `Bearer ${state.orch.token}` } : {}),
            },
            body: JSON.stringify({ session_id: state.sessionId, backend: state.backend, example })
          });
          if (!resp.ok) { banner(`Apply failed: ${await resp.text()}`, 'fail'); return; }
          // Prefer server-declared app name (e.g., echo-<session>)
          try {
            const out = await resp.json();
            if (out && out.app) { state.appName = out.app; }
            state.appApplied = true;
            clearCanaryInfo();
          } catch(_) { state.appApplied = true; }
          // Immediate, visible feedback like dashboard header
          try { banner(`Apply accepted for “${state.appName}” — reconciling…`, 'ok', 6000); } catch(_){}
          setTimeout(verifyApply, 800);
          setTimeout(()=>verifyScale(), 1200);
          setTimeout(verifyIngress, 1500);
          try { refreshStatusNow(); } catch(_){}
          // Auto-start the log tail for the newly applied example
          try {
            const observeBtn = document.getElementById('btn-observe-toggle');
            if (observeBtn && /Start/i.test(observeBtn.textContent||'')) { observeBtn.click(); }
          } catch(_){}
        } catch(e){ banner(`Apply error: ${e}`, 'fail'); return; }
      });
    });
    $('#btn-scale-2')?.addEventListener('click', async (e)=>{
      const btn = e.currentTarget || document.getElementById('btn-scale-2');
      await withButtonFeedback(btn, `Scaling “${state.appName}” to 2…`, async ()=>{ await doScale(2); });
    });
    $('#btn-scale-3')?.addEventListener('click', async (e)=>{
      const btn = e.currentTarget || document.getElementById('btn-scale-3');
      await withButtonFeedback(btn, `Scaling “${state.appName}” to 3…`, async ()=>{ await doScale(3); });
    });
    // Reset session: server-side cleanup (when available) + local UI clear
    $('#btn-reset')?.addEventListener('click', async(e)=>{
      const btn = e.currentTarget || document.getElementById('btn-reset');
      await withButtonFeedback(btn, 'Resetting session…', async ()=>{
      const prev = state.sessionId;
      // Attempt server cleanup when orchestrator is available (session optional)
      if (state.orch.available) {
        try {
          const r = await apiFetch(`/labs/reset`, {
            method: 'POST',
            headers: {'Content-Type':'application/json', ...(state.orch.token? { 'Authorization': `Bearer ${state.orch.token}` } : {})},
            body: JSON.stringify({ session_id: prev })
          });
          if (await handleLabsAuth(r, 'Reset')) { return; }
          if (!r.ok && r.status !== 404) { banner(`Reset failed: ${await r.text()}`, 'fail'); return; }
        } catch(e){ bannerFetchFailure('Reset', e); return; }
      }
      // Local UI/session clear regardless of backend availability
      try { banner('Session reset — resources will disappear shortly.', 'ok'); } catch(_){ try { toast('Session reset', 'ok'); } catch(_){} }
      state.sessionId = null;
      state.appApplied = false;
      state.appName = 'echo';
      // Stop/disable SSE and prefer non-HTMX panels
      try {
        const ids = ['logs-sse','events-sse','status-summary'];
        ids.forEach(id => {
          const el = document.getElementById(id);
          if (el) el.removeAttribute('sse-connect');
        });
        const polyLogs = document.getElementById('observe-logs');
        const polyEv = document.getElementById('observe-events');
        if (polyLogs) polyLogs.classList.remove('hidden');
        if (polyEv) polyEv.classList.remove('hidden');
        const sseLogs = document.getElementById('logs-sse');
        const sseEv = document.getElementById('events-sse');
        if (sseLogs) sseLogs.classList.add('hidden');
        if (sseEv) sseEv.classList.add('hidden');
      } catch(_){}
      // Clear panels and indicators
      try { const ev = document.getElementById('observe-events'); if (ev) ev.textContent = ''; } catch(_){}
      try { const lg = document.getElementById('observe-logs'); if (lg) lg.textContent = ''; } catch(_){}
      try { setText('#v-apply-events','n/a','pending'); } catch(_){}
      try { setText('#v-apply-ready','n/a','pending'); } catch(_){}
      try { clearCanaryInfo(); } catch(_){}
      try { setText('#ingress-check','n/a','pending'); } catch(_){}
      try { setText('#ingress-curl',''); const b=document.getElementById('ingress-curl-copy'); if (b) b.disabled=true; } catch(_){}
      try { setHostsHint(''); } catch(_){}
      // Reset status to cluster-wide summary and refresh
      try { setStatusMode('cluster'); refreshStatusNow(); } catch(_){}
      // Disable action buttons now that we have no session
      try { wireControls(); } catch(_){}
      setText('#session-id','(none)');
      });
    });
    $('#btn-canary-10')?.addEventListener('click', async(e)=>{
      if (!state.orch.available) return;
      const btn = e.currentTarget || document.getElementById('btn-canary-10');
      await withButtonFeedback(btn, 'Applying canary…', async ()=>{
        const resp = await apiFetch(`/labs/rollout`, {
          method: 'POST',
          headers: {'Content-Type':'application/json', ...(state.orch.token? { 'Authorization': `Bearer ${state.orch.token}` } : {})},
          body: JSON.stringify({ session_id: state.sessionId, action: 'canary', app: state.appName })
        });
        if (!resp.ok) { banner(`Canary failed: ${await resp.text()}`, 'fail'); return; }
        try {
          const out = await resp.json();
          if (out) { setCanaryInfo(out.revision ?? null, out.base_revision ?? null, out.canary_weight ?? null); }
        } catch(_){}
        setTimeout(verifyApply, 800);
        try { toast('Canary applied (default weight)', 'ok'); } catch (_){ }
      });
    });
    // Canary slider controls
    const cw = document.getElementById('canary-weight');
    const cwv = document.getElementById('canary-weight-val');
    const canaryWeightToPercent = (val)=>{
      const n = parseInt(String(val ?? ''), 10);
      if (!Number.isFinite(n) || n <= 0) return 10;
      return Math.max(10, Math.min(100, n * 10));
    };
    if (cw && cwv) {
      const updateCanaryLabel = ()=>{ cwv.textContent = String(canaryWeightToPercent(cw.value)); };
      cw.addEventListener('input', updateCanaryLabel);
      updateCanaryLabel();
    }
    $('#btn-canary-apply')?.addEventListener('click', async(e)=>{
      if (!state.orch.available) return;
      const rawWeight = (document.getElementById('canary-weight')||{value:'3'}).value;
      const weight = canaryWeightToPercent(rawWeight);
      const btn = e.currentTarget || document.getElementById('btn-canary-apply');
      await withButtonFeedback(btn, `Applying canary ${weight}%…`, async ()=>{
        const resp = await apiFetch(`/labs/rollout`, {
          method: 'POST', headers: {'Content-Type':'application/json', ...(state.orch.token? { 'Authorization': `Bearer ${state.orch.token}` } : {})},
          body: JSON.stringify({ session_id: state.sessionId, action: 'canary', app: state.appName, weight })
        });
        if (!resp.ok) { banner(`Canary failed: ${await resp.text()}`, 'fail'); return; }
        try {
          const out = await resp.json();
          if (out) { setCanaryInfo(out.revision ?? null, out.base_revision ?? null, out.canary_weight ?? null); }
        } catch(_){}
        setTimeout(verifyApply, 800);
        try { toast(`Canary weight ${weight} applied`, 'ok'); } catch(_){ }
      });
    });
    // Token adoption (auto-paste from clipboard if field empty)
    async function handleUseToken(){
      const el = document.getElementById('labs-token');
      let val = el && el.value ? el.value.trim() : '';
      if (!val && navigator.clipboard && navigator.clipboard.readText) {
        try { val = (await navigator.clipboard.readText()).trim(); if (val && el) el.value = val; } catch(_){ }
      }
      if (!val) { try { toast('No token found in field or clipboard', 'fail'); } catch(_){ } if (el) el.focus(); return; }
      state.orch.token = val; try { sessionStorage.setItem('labsToken', val); } catch(_){ }
      try {
        toast('Labs token set — checking…', 'ok');
        const nud = document.getElementById('labs-token-nudge');
        if (nud) nud.classList.add('hidden');
        if (el) el.classList.remove('attn');
      } catch(_){ }
      try { await recheckLabs(); } catch(_){ }
    }
    // Expose globally as a safety net for inline onclick
    window.k1sUseToken = handleUseToken;
    // Also wire standard listener
    $('#btn-use-token')?.addEventListener('click', handleUseToken);
    // Status mode toggles
    $('#status-mode-cluster')?.addEventListener('click', ()=>{ setStatusMode('cluster'); refreshStatusNow(); });
    $('#status-mode-app')?.addEventListener('click', ()=>{ setStatusMode('app'); refreshStatusNow(); });
    try { setStatusMode(state.statusMode || 'cluster'); } catch(_){}
    // Enter key triggers Use Token
    try { const t = document.getElementById('labs-token'); if (t) t.addEventListener('keydown', (e)=>{ if (e.key === 'Enter') { e.preventDefault(); document.getElementById('btn-use-token')?.click(); }}); } catch(_){ }
    // k3d ensure
    $('#btn-k3d-ensure')?.addEventListener('click', async()=>{
      try {
        const r = await fetch(`${API}/labs/k3d/ensure`, { method:'POST' });
        if (r.ok) {
          const j = await r.json();
          try { toast(`k3d cluster '${j.name}' ready (https ${j.ports.https})`, 'ok'); } catch(_){ }
        } else { try { toast('k3d ensure failed', 'fail'); } catch(_){ } }
      } catch { try { toast('k3d ensure error', 'fail'); } catch(_){ } }
    });
    $('#btn-rollout-pause')?.addEventListener('click', async(e)=>{
      if (!state.orch.available) return;
      const btn = e.currentTarget || document.getElementById('btn-rollout-pause');
      await withButtonFeedback(btn, 'Pausing rollout…', async ()=>{
        try {
          const r = await fetch(`${API}/labs/rollout`, {
            method: 'POST', headers: {'Content-Type':'application/json', ...(state.orch.token? { 'Authorization': `Bearer ${state.orch.token}` } : {})},
            body: JSON.stringify({ session_id: state.sessionId, action: 'pause', app: state.appName })
          });
          if (!r.ok) { banner(`Pause failed: ${await r.text()}`, 'fail'); return; }
        } catch(e){ banner(`Pause error: ${e}`, 'fail'); return; }
      });
    });
    $('#btn-rollout-resume')?.addEventListener('click', async(e)=>{
      if (!state.orch.available) return;
      const btn = e.currentTarget || document.getElementById('btn-rollout-resume');
      await withButtonFeedback(btn, 'Resuming rollout…', async ()=>{
        try {
          const r = await fetch(`${API}/labs/rollout`, {
            method: 'POST', headers: {'Content-Type':'application/json', ...(state.orch.token? { 'Authorization': `Bearer ${state.orch.token}` } : {})},
            body: JSON.stringify({ session_id: state.sessionId, action: 'resume', app: state.appName })
          });
          if (!r.ok) { banner(`Resume failed: ${await r.text()}`, 'fail'); return; }
        } catch(e){ banner(`Resume error: ${e}`, 'fail'); return; }
      });
    });
    document.getElementById('btn-helm-demo')?.addEventListener('click', () => startHelmDemo());
    document.getElementById('btn-helm-demo-stop')?.addEventListener('click', () => stopHelmDemo());
  }

  async function doScale(n){
    if (!state.orch.available) return;
    try { banner(`Scaling “${state.appName}” to ${n}…`, 'pending', 5000); } catch(_){}
    await apiFetch(`/labs/scale`, {
      method: 'POST',
      headers: {'Content-Type':'application/json', ...(state.orch.token? { 'Authorization': `Bearer ${state.orch.token}` } : {})},
      body: JSON.stringify({ session_id: state.sessionId, app: state.appName, replicas: n })
    });
    setTimeout(verifyApply, 800);
    setTimeout(()=>verifyScale(n), 1200);
    setTimeout(verifyIngress, 1500);
  }

    document.addEventListener('DOMContentLoaded', async () => {
    const followCtl = document.getElementById('follow-tail');
    const shouldFollow = () => !followCtl || followCtl.checked;
    const follow = (el) => { if (el && shouldFollow()) { try { el.scrollTop = el.scrollHeight; } catch(_){ } } };
    const forceFollowAll = () => {
      const ids = ['observe-logs','observe-events','logs-sse','events-sse'];
      ids.forEach(id=>{
        const el = document.getElementById(id);
        if (el) { try { el.scrollTop = el.scrollHeight; } catch(_){ } }
      });
    };
    // Auto-follow whenever content mutates
    const attachAutoFollow = (id) => {
      const el = document.getElementById(id);
      if (!el || !window.MutationObserver) return;
      const mo = new MutationObserver(() => follow(el));
      mo.observe(el, { childList: true, subtree: true });
      follow(el);
    };
    ['observe-logs','observe-events','logs-sse','events-sse'].forEach(attachAutoFollow);
    if (followCtl) {
      followCtl.addEventListener('change', () => {
        if (followCtl.checked) {
          requestAnimationFrame(forceFollowAll);
        }
      });
    }
    // Ensure status has a visible default
    try { setText('#status-summary', 'n/a', 'pending'); } catch(_){}
    await initEnv();
    try { setInterval(refreshStatusNow, 3000); } catch(_){}
    bind();
    // Observe tail toggle
    const observeBtn = document.getElementById('btn-observe-toggle');
    let esLogs = null, esEvents = null, esStatus = null;
    function stopStreams(){ try { if (esLogs) { esLogs.close(); esLogs=null; } } catch(_){} try { if (esEvents) { esEvents.close(); esEvents=null; } } catch(_){} try { if (esStatus) { esStatus.close(); esStatus=null; } } catch(_){} }
    async function pollEventsOnce(){
      try {
        const ev = await jsonGet(`${API}/events/${encodeURIComponent(state.appName)}?limit=20`);
        const box = document.getElementById('observe-events');
        if (box) {
          // Oldest-first so newest ends at the bottom and we can follow
          const items = Array.isArray(ev) ? ev.slice().reverse() : [];
          box.innerHTML = (items||[]).map(e=>{
            const ts = e.created_at || '-';
            const msg = (e.message||'');
            return `<div class="log-entry"><code>${ts}</code> ${msg}</div>`;
          }).join('') || '<div class="log-entry">No recent events</div>';
          follow(box);
        }
      } catch {}
    }
    if (observeBtn) {
      observeBtn.addEventListener('click', () => {
        if (!state.sessionId) return;
        if (observeBtn.textContent.includes('Start')) {
          observeBtn.textContent = 'Stop Tail';
          // Always use EventSource to populate the polyfill logs panel
          try {
            const qs = new URLSearchParams({ tail: '200' });
            try { if (state.orch && state.orch.token) qs.set('token', state.orch.token); } catch(_){}
            const url = `${API}/logs/${encodeURIComponent(state.appName)}/stream?` + qs.toString();
            esLogs = new EventSource(url);
            const box = document.getElementById('observe-logs');
            if (box) { box.innerHTML=''; box.classList.remove('hidden'); }
            const sseHide = document.getElementById('logs-sse'); if (sseHide) sseHide.classList.add('hidden');
            requestAnimationFrame(forceFollowAll);
            esLogs.onmessage = (ev) => {
              if (!box) return;
              const div = document.createElement('div');
              div.className = 'log-entry';
              div.textContent = ev.data || '';
              box.appendChild(div);
              follow(box);
            };
            esLogs.onerror = () => { /* retry by EventSource */ };
          } catch(e){ console.error('EventSource logs error', e); }
          // Events SSE (labs) with fallback to polling
          try {
            const q1 = { app: state.appName, limit: '20' };
            try { if (state.orch && state.orch.token) q1['token'] = state.orch.token; } catch(_){}
            const evUrl = `${API}/labs/sse/events?` + new URLSearchParams(q1).toString();
            esEvents = new EventSource(evUrl);
            esEvents.onmessage = (ev) => {
              const arr = JSON.parse(ev.data || '[]');
              const box = document.getElementById('observe-events');
              if (box) {
                // Oldest-first so new events appear at the bottom (match logs)
                const items = (Array.isArray(arr) ? arr.slice().reverse() : []);
                box.innerHTML = items.map(e=>{
                  const ts = e.created_at || '-';
                  const msg = (e.message||'');
                  return `<div class="log-entry"><code>${ts}</code> ${msg}</div>`;
                }).join('') || '<div class="log-entry">No recent events</div>';
                follow(box);
              }
            };
            esEvents.onerror = () => { try { if ((!API || API==='') && window.DOCS_API_BASE) { switchToDirectApi('events SSE error'); } } catch(_){} };
          } catch {
            pollEventsOnce();
            state._eventsTimer = setInterval(pollEventsOnce, 2000);
          }
          // Status SSE to keep verifiers fresh
          try {
            const q2 = { app: state.appName };
            try { if (state.orch && state.orch.token) q2['token'] = state.orch.token; } catch(_){}
            const stUrl = `${API}/labs/sse/status?` + new URLSearchParams(q2).toString();
            esStatus = new EventSource(stUrl);
            esStatus.onmessage = (ev) => {
              try {
                const s = JSON.parse(ev.data || 'null');
                if (!s) return;
                const ok = Number(s.ready||0) === Number(s.desired||0);
                setText('#v-apply-ready', ok?'ok':'fail', ok?'ok':'fail');
                setText('#status-summary', `${s.ready||0}/${s.desired||0} ready`, ok?'ok':'fail');
                const link = document.getElementById('ingress-link');
                if (link && s.ingress_host) {
                  link.textContent = s.ingress_host;
                  const href = s.ingress_host.startsWith('http')
                    ? s.ingress_host + (s.ingress_path||'/')
                    : makeIngressUrl(s.ingress_host, s.ingress_path);
                  link.href = href;
                }
              } catch {}
            };
            esStatus.onerror = () => { try { if ((!API || API==='') && window.DOCS_API_BASE) { switchToDirectApi('status SSE error'); } } catch(_){} };
          } catch {}
        } else {
          observeBtn.textContent = 'Start Tail';
          stopStreams();
          const ssePanel = document.getElementById('logs-sse');
          if (ssePanel) { try { ssePanel.classList.add('hidden'); } catch(_){} }
          const poly = document.getElementById('observe-logs');
          if (poly) poly.classList.remove('hidden');
          if (state._eventsTimer) { clearInterval(state._eventsTimer); state._eventsTimer = null; }
        }
      });
    }
    // Copy DNS hint
    try { document.getElementById('ingress-hosts-copy')?.addEventListener('click', copyHostsHint); } catch(_){}
    // Copy curl hint
    try { document.getElementById('ingress-curl-copy')?.addEventListener('click', copyCurlHint); } catch(_){}
    // Copy API curl hint
    try { document.getElementById('api-curl-copy')?.addEventListener('click', copyApiCurlHint); } catch(_){}
  });
  // Ensure HTMX-driven events panel auto-scrolls to bottom after swaps (when follow is enabled)
  try {
    document.body.addEventListener('htmx:afterSwap', (evt) => {
      try {
        const tgt = evt.target;
        if (tgt && tgt.id === 'events-sse') {
          const followCtl = document.getElementById('follow-tail');
          if (!followCtl || followCtl.checked) {
            tgt.scrollTop = tgt.scrollHeight;
          }
        }
      } catch(_){}
    });
  } catch(_){}
})();
