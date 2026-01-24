#!/usr/bin/env node
'use strict';

const { WebSocket } = require('undici');

const base = process.env.APISHIM_BASE || 'http://127.0.0.1:8445';
const pod = process.env.POD_NAME;
const token = process.env.TOKEN_PF || '';
const port = process.env.PF_PORT || '8080';
const request = process.env.PF_HTTP_REQ || 'GET / HTTP/1.1\r\nHost: echo\r\nConnection: close\r\n\r\n';
const timeoutMs = Number(process.env.PF_TIMEOUT_MS || 5000);
const rawPath = process.env.PF_JS_RAW_PATH || '';

if (!pod) {
  console.error('POD_NAME is required');
  process.exit(2);
}

function toWsUrl(httpBase) {
  const u = new URL(httpBase);
  const wsProto = u.protocol === 'https:' ? 'wss:' : 'ws:';
  const basePath = u.pathname.replace(/\/$/, '');
  const tokenParam = token ? `&token=${encodeURIComponent(token)}` : '';
  return `${wsProto}//${u.host}${basePath}/api/v1/namespaces/default/pods/${encodeURIComponent(pod)}/portforward?ports=${encodeURIComponent(port)}${tokenParam}`;
}

const wsUrl = toWsUrl(base);
let gotData = false;
let rawChunks = [];
let finished = false;
let sawError = false;

const ws = new WebSocket(wsUrl, 'portforward.k8s.io');
ws.binaryType = 'arraybuffer';

function finish(code) {
  if (finished) return;
  finished = true;
  if (rawPath) {
    try {
      const fs = require('fs');
      const buf = Buffer.concat(rawChunks);
      fs.writeFileSync(rawPath, buf);
      console.log(`raw-bytes=${buf.length} path=${rawPath}`);
    } catch (err) {
      console.error(`raw dump failed: ${err}`);
    }
  }
  process.exit(code);
}

ws.onopen = () => {
  const payload = Buffer.concat([Buffer.from([0]), Buffer.from(request)]);
  ws.send(payload);
};

ws.onmessage = (ev) => {
  let buf;
  if (Buffer.isBuffer(ev.data)) {
    buf = ev.data;
  } else if (ev.data instanceof ArrayBuffer) {
    buf = Buffer.from(ev.data);
  } else if (ArrayBuffer.isView(ev.data)) {
    buf = Buffer.from(ev.data.buffer);
  } else {
    buf = Buffer.from(String(ev.data));
  }
  rawChunks.push(buf);
  if (buf.length > 0) {
    const ch = buf[0];
    const data = buf.slice(1);
    if (ch === 0 || ch === 1) {
      gotData = gotData || data.length > 0;
      if (data.length > 0) {
        const text = data.toString('utf8');
        process.stdout.write(text.slice(0, 200));
      }
    }
  }
  if (gotData) {
    ws.close();
    setTimeout(() => finish(0), 100);
  }
};

ws.onerror = (err) => {
  sawError = true;
  if (!gotData) {
    console.error(`ws error: ${err && err.message ? err.message : err}`);
  }
};

ws.onclose = () => {
  if (!gotData) {
    if (sawError) {
      finish(1);
      return;
    }
    finish(1);
  }
};

setTimeout(() => {
  if (!gotData) {
    console.error(`no data within ${timeoutMs}ms`);
    try { ws.close(); } catch {}
    finish(1);
  }
}, timeoutMs);
