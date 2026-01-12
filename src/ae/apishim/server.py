from __future__ import annotations

import json
import os
import re
import ssl
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs
import time
import json as _jsonlib
from pathlib import Path
from dataclasses import dataclass
import base64
import hashlib
import socket

from .store import ObjectStore, K8sObject
from ae.controller.state import SQLiteStateStore
from ae.runtime import DockerRuntime, PodmanRuntime, StubRuntime, RemoteRuntime, RuntimeAdapter
from .adapter import build_adapter


K8S_VERSION = {
    "major": "0",
    "minor": "1",
    "gitVersion": "v0.1.0-k1s-shim",
}

RESERVED_GROUPS = {
    "",
    "apps",
    "networking.k8s.io",
    "rbac.authorization.k8s.io",
    "policy",
    "autoscaling",
    "apiextensions.k8s.io",
}


def _json(d: Dict[str, Any]) -> bytes:
    return json.dumps(d, separators=(",", ":")).encode("utf-8")


def _read_json(body: bytes) -> Dict[str, Any]:
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except json.JSONDecodeError:
        return {}


def _ns_name(path: str) -> Tuple[str, Optional[str], Optional[str]]:
    # Returns (resource plural, namespace, name)
    # Patterns we support:
    # /api/v1/namespaces
    # /api/v1/namespaces/<ns>
    # /api/v1/namespaces/<ns>/<plural>
    # /api/v1/namespaces/<ns>/<plural>/<name>
    # /api/v1/<plural>
    m = re.match(r"^/api/v1/namespaces/([^/]+)/([^/]+)/([^/]+)$", path)
    if m:
        return (m.group(2), m.group(1), m.group(3))
    m = re.match(r"^/api/v1/namespaces/([^/]+)/([^/]+)$", path)
    if m:
        return (m.group(2), m.group(1), None)
    m = re.match(r"^/api/v1/namespaces/([^/]+)$", path)
    if m:
        return ("namespaces", None, m.group(1))
    m = re.match(r"^/api/v1/([^/]+)/([^/]+)$", path)
    if m:
        return (m.group(1), None, m.group(2))
    m = re.match(r"^/api/v1/([^/]+)$", path)
    if m:
        return (m.group(1), None, None)
    return ("", None, None)


class ShimHandler(BaseHTTPRequestHandler):
    server_version = "k1s-apishim"
    admin_token: Optional[str] = os.getenv("AE_APISHIM_TOKEN")
    read_token: Optional[str] = os.getenv("AE_APISHIM_READ_TOKEN")
    rbac_enabled: bool = os.getenv("AE_APISHIM_RBAC", "0") == "1"
    # Simple in-memory RBAC rules: (verb, resource) -> allowed roles
    rbac_policies: dict[tuple[str, str], set[str]] = {
        ("get", "*"): {"admin", "read"},
        ("list", "*"): {"admin", "read"},
        ("watch", "*"): {"admin", "read"},
        ("create", "*"): {"admin"},
        ("update", "*"): {"admin"},
        ("patch", "*"): {"admin"},
        ("delete", "*"): {"admin"},
    }
    store: ObjectStore
    state: "SQLiteStateStore"
    client_cert_required: bool = False
    crd_registry: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    crd_index: Dict[str, List[Tuple[str, str, str]]] = {}
    crd_lock = threading.RLock()

    def _authz(self, role: str = "read") -> bool:
        admin = self.admin_token
        reader = self.read_token
        if not admin and not reader:
            return True
        hdr = self.headers.get("Authorization", "")
        tok = hdr[7:] if hdr.startswith("Bearer ") else ""
        ok = False
        if role == "write":
            ok = tok and tok == admin
        elif role == "read":
            ok = tok and (tok == admin or tok == reader)
        elif role in {"rbac-read", "rbac-write"}:
            ok = tok and (tok == admin or tok == reader)
        if ok:
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", "Bearer")
        self._json_status(
            HTTPStatus.UNAUTHORIZED,
            reason="Unauthorized",
            message="missing/invalid bearer token",
        )
        return False

    def _rbac_allows(self, verb: str, resource: str) -> bool:
        if not self.rbac_enabled:
            return True
        hdr = self.headers.get("Authorization", "")
        tok = hdr[7:] if hdr.startswith("Bearer ") else ""
        role = None
        if tok and tok == self.admin_token:
            role = "admin"
        elif tok and tok == self.read_token:
            role = "read"
        if role is None:
            return False
        allowed = self.rbac_policies.get((verb, resource)) or self.rbac_policies.get((verb, "*"))
        return bool(allowed and role in allowed)

    def _ok(self, payload: Dict[str, Any]) -> None:
        data = _json(payload)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self, msg: str = "not found") -> None:
        self._json_status(HTTPStatus.NOT_FOUND, reason="NotFound", message=msg)

    def _json_status(self, code: int, *, reason: str, message: str) -> None:
        body = {
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure" if 400 <= code else "Success",
            "message": message,
            "reason": reason,
            "code": code,
        }
        data = _json(body)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _max_rv_for(self, group: str, version: str, resource: str, namespace: Optional[str]) -> int:
        try:
            if namespace is None:
                items = self.server.store.list_all(group, version, resource)  # type: ignore[attr-defined]
            else:
                items = self.server.store.list(group, version, resource, namespace)  # type: ignore[attr-defined]
            return max((i.resource_version for i in items), default=0)
        except Exception:
            return 0

    # ---------------- WebSocket port-forward (best-effort) ----------------
    def _handle_port_forward_ws(self, target_host: str, target_port: int) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            return
        accept_seed = (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("utf-8")
        accept = base64.b64encode(hashlib.sha1(accept_seed).digest()).decode("utf-8")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            upstream = socket.create_connection((target_host, target_port), timeout=5.0)
        except Exception:
            try:
                self.connection.close()
            except Exception:
                pass
            return

    # ---------------- SPDY/3.1 port-forward (kubectl) ----------------
    def _handle_port_forward_spdy(self, target_host: str, target_port: int) -> None:
        # Accept upgrade
        self.send_response(101, "Switching Protocols")
        self.send_header("Connection", "Upgrade")
        self.send_header("Upgrade", self.headers.get("Upgrade", "SPDY/3.1"))
        self.end_headers()
        conn = self.connection
        conn.settimeout(0.05)
        try:
            upstream = socket.create_connection((target_host, target_port), timeout=5.0)
            upstream.settimeout(0.05)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return

        SPDY_DICT = (
            b"optionsgetheadpostputdeletetraceacceptaccept-charsetaccept-encodingaccept-language"
            b"authorizationexpectfromhostif-modified-sinceif-matchif-none-matchif-rangeif-unmodified-"
            b"sincemax-forwardsproxy-authorizationrange refererteuser-agent100101200201202203204205206"
            b"300301302303304305306307400401402403404405406407408409410411412413414415416417500501502"
            b"503504505accept-rangesageetaglocationproxy-authenticatepublicretry-afterservervarywarning"
            b"www-authenticateallowcontent-basecontent-encodingcache-controlconnectiondatetrailertransfer"
            b"-encodingupgradeviawarningcontent-languagecontent-lengthcontent-locationcontent-md5content-"
            b"rangecontent-typeetagexpireslast-modifiedset-cookieMondayTuesdayWednesdayThursdayFridaySaturday"
            b"SundayJanFebMarAprMayJunJulAugSepOctNovDecchunkedtext/htmlimage/pngimage/jpgimage/gifapplication"
            b"/xmlapplication/xhtmltext/plainpublicprivatemax-agegztcomparallel bytesruning"
        )
        dctx = zlib.decompressobj(zdict=SPDY_DICT)
        data_streams: dict[int, int] = {}  # sid -> target_port
        error_streams: dict[int, int] = {}  # sid -> data_stream sid
        window_size = 1 << 20  # 1MiB default
        stream_windows: dict[int, int] = {}
        ports: list[int] = []
        try:
            ports = [int(p) for p in (self.headers.get("X-Stream-Port", "") or "").split(",") if p.strip()]
        except Exception:
            ports = []
        if not ports:
            ports = [target_port]
        upstream_cache: dict[int, socket.socket] = {}

        def read_exact(sock, n: int) -> bytes | None:
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            return buf

        def send_data_frame(stream_id: int, payload: bytes, flags: int = 0) -> None:
            header = bytearray()
            header += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
            header.append(flags & 0xFF)
            header += len(payload).to_bytes(3, "big")
            conn.sendall(bytes(header) + payload)

        def send_window_update(stream_id: int, delta: int) -> None:
            # Control frame: C bit + version 3 + type 0x09 (WINDOW_UPDATE)
            header = bytearray()
            header += b"\x80\x03"          # control + version
            header += (0x09).to_bytes(2, "big")
            header += b"\x00"              # flags
            header += (8).to_bytes(3, "big")  # length
            header += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
            header += (delta & 0x7FFFFFFF).to_bytes(4, "big")
            conn.sendall(bytes(header))
            stream_windows[stream_id] = stream_windows.get(stream_id, window_size) + delta

        def send_ping(opaque: bytes = b"\x00\x00\x00\x01") -> None:
            header = bytearray()
            header += b"\x80\x03"
            header += (0x06).to_bytes(2, "big")  # PING
            header += b"\x00"
            header += (4).to_bytes(3, "big")
            header += opaque[:4]
            conn.sendall(bytes(header))

        def echo_ping(opaque: bytes) -> None:
            send_ping(opaque)

        def send_settings(settings: dict[int, int]) -> None:
            # SETTINGS frame: type=0x04
            payload = bytearray()
            payload += len(settings).to_bytes(4, "big")
            for sid, val in settings.items():
                payload.append(0)  # flags per setting (0)
                payload += sid.to_bytes(2, "big")
                payload += val.to_bytes(4, "big")
            header = bytearray()
            header += b"\x80\x03"
            header += (0x04).to_bytes(2, "big")
            header += b"\x00"
            header += len(payload).to_bytes(3, "big")
            conn.sendall(bytes(header) + payload)

        def send_rst(stream_id: int, code: int = 2) -> None:
            # RST_STREAM: type=0x03, status code (e.g., 2=PROTOCOL_ERROR)
            header = bytearray()
            header += b"\x80\x03"
            header += (0x03).to_bytes(2, "big")
            header += b"\x00"
            header += (8).to_bytes(3, "big")
            header += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
            header += (code & 0x7FFFFFFF).to_bytes(4, "big")
            conn.sendall(bytes(header))

        def send_goaway(last_stream: int = 0, status: int = 0) -> None:
            header = bytearray()
            header += b"\x80\x03"
            header += (0x07).to_bytes(2, "big")  # GOAWAY
            header += b"\x00"
            header += (8).to_bytes(3, "big")
            header += (last_stream & 0x7FFFFFFF).to_bytes(4, "big")
            header += (status & 0x7FFFFFFF).to_bytes(4, "big")
            conn.sendall(bytes(header))

        def parse_syn_stream(payload: bytes) -> dict[str, str]:
            headers: dict[str, str] = {}
            if len(payload) < 10:
                return headers
            header_block = payload[10:]
            try:
                decompressed = dctx.decompress(header_block)
                import io

                f = io.BytesIO(decompressed)
                num = int.from_bytes(f.read(4), "big")
                for _ in range(num):
                    nlen = int.from_bytes(f.read(4), "big")
                    name = f.read(nlen).decode("utf-8", "ignore")
                    vlen = int.from_bytes(f.read(4), "big")
                    value = f.read(vlen).decode("utf-8", "ignore")
                    headers[name] = value
            except Exception:
                return headers
            return headers

        try:
            # Send initial SETTINGS to advertise window
            send_settings({0x04: window_size})  # SETTINGS_INITIAL_WINDOW_SIZE
            last_ping = time.time()
            while True:
                # keepalive ping every 10s
                now = time.time()
                if now - last_ping > 10:
                    try:
                        send_ping()
                    except Exception:
                        break
                    last_ping = now
                # Client -> server SPDY frames
                try:
                    hdr = conn.recv(8)
                except socket.timeout:
                    hdr = None
                if hdr:
                    if len(hdr) < 8:
                        break
                    is_control = (hdr[0] & 0x80) != 0
                    if is_control:
                        frame_type = int.from_bytes(hdr[2:4], "big")
                        flags = hdr[4]
                        length = int.from_bytes(hdr[5:8], "big")
                        if length > (1 << 20):
                            send_goaway(status=2)
                            break
                        payload = read_exact(conn, length) or b""
                        if frame_type == 1:  # SYN_STREAM
                            sid = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
                            headers = parse_syn_stream(payload)
                            stype = headers.get("streamtype", "").lower()
                            port = target_port
                            try:
                                if headers.get("port"):
                                    port = int(headers["port"])
                                elif headers.get("streamname"):
                                    port = int(headers["streamname"])
                            except Exception:
                                port = target_port
                            if stype == "data":
                                data_streams[sid] = port
                                stream_windows[sid] = window_size
                            elif stype == "error":
                                error_streams[sid] = data_streams.get(sid - 1, target_port)
                        # ignore others
                        elif frame_type == 4:  # SETTINGS
                            # Update initial window if provided (id 0x04)
                            try:
                                num = int.from_bytes(payload[0:4], "big")
                                idx = 4
                                for _ in range(num):
                                    if idx + 8 > len(payload):
                                        break
                                    _flags = payload[idx]
                                    sid_setting = int.from_bytes(payload[idx + 1:idx + 3], "big")
                                    val = int.from_bytes(payload[idx + 3:idx + 7], "big")
                                    idx += 8
                                    if sid_setting == 0x04:
                                        window_size = val
                            except Exception:
                                pass
                        elif frame_type == 9:  # WINDOW_UPDATE
                            if len(payload) >= 8:
                                sid = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
                                delta = int.from_bytes(payload[4:8], "big")
                                stream_windows[sid] = stream_windows.get(sid, window_size) + delta
                                if stream_windows[sid] > (1 << 24):
                                    send_rst(sid, code=2)  # PROTOCOL_ERROR
                        elif frame_type == 3:  # RST_STREAM
                            if len(payload) >= 8:
                                sid = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
                                upstream_sock = upstream_cache.pop(sid, None)
                                if upstream_sock:
                                    try:
                                        upstream_sock.close()
                                    except Exception:
                                        pass
                                data_streams.pop(sid, None)
                                error_streams.pop(sid, None)
                        elif frame_type == 6:  # PING
                            echo_ping(payload[:4])
                        elif frame_type == 7:  # GOAWAY
                            break
                    else:
                        stream_id = int.from_bytes(hdr[0:4], "big") & 0x7FFFFFFF
                        flags = hdr[4]
                        length = int.from_bytes(hdr[5:8], "big")
                        if length > (1 << 20):
                            send_rst(stream_id, code=2)
                            break
                        payload = read_exact(conn, length) or b""
                        if stream_id in data_streams and payload:
                            port = data_streams[stream_id]
                            # Enforce flow control window
                            wnd = stream_windows.get(stream_id, window_size)
                            if wnd <= 0:
                                # Buffer drop and send RST to client to signal flow control violation
                                send_rst(stream_id, code=2)
                                continue
                            if stream_id not in upstream_cache:
                                try:
                                    upstream_cache[stream_id] = socket.create_connection((target_host, port), timeout=5.0)
                                    upstream_cache[stream_id].settimeout(0.05)
                                except Exception:
                                    upstream_cache.pop(stream_id, None)
                                    continue
                            try:
                                upstream_cache[stream_id].sendall(payload)
                                stream_windows[stream_id] = max(0, wnd - len(payload))
                            except Exception:
                                break
                            try:
                                send_window_update(stream_id, len(payload))
                            except Exception:
                                pass
                        elif stream_id in error_streams and payload:
                            # stderr payload: forward to error stream (stream_id)
                            try:
                                send_data_frame(stream_id, payload, flags=0)
                            except Exception:
                                pass
                        if flags & 0x02:  # FIN flag
                            upstream_sock = upstream_cache.pop(stream_id, None)
                            if upstream_sock:
                                try:
                                    upstream_sock.close()
                                except Exception:
                                    pass
                            send_rst(stream_id, code=0)

                # Server -> client data
                for sid, sock_up in list(upstream_cache.items()):
                    try:
                        resp = sock_up.recv(4096)
                        if resp:
                            send_data_frame(sid, resp, flags=0)
                            try:
                                send_window_update(sid, len(resp))
                            except Exception:
                                pass
                        else:
                            upstream_cache.pop(sid, None)
                            try:
                                sock_up.close()
                            except Exception:
                                pass
                            for esid, dport in list(error_streams.items()):
                                if data_streams.get(sid) == dport:
                                    try:
                                        send_data_frame(esid, b"", flags=0x01)  # FIN on error stream
                                    except Exception:
                                        pass
                    except socket.timeout:
                        continue
                    except Exception:
                        upstream_cache.pop(sid, None)
                        try:
                            sock_up.close()
                        except Exception:
                            pass
        finally:
            try:
                send_goaway(last_stream=max(data_streams.keys()) if data_streams else 0, status=0)
            except Exception:
                pass
            for s in upstream_cache.values():
                try:
                    s.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

        def recv_frame(sock) -> bytes | None:
            hdr = sock.recv(2)
            if len(hdr) < 2:
                return None
            opcode = hdr[0] & 0x0F
            masked = hdr[1] & 0x80
            length = hdr[1] & 0x7F
            if length == 126:
                ext = sock.recv(2)
                if len(ext) < 2:
                    return None
                length = int.from_bytes(ext, "big")
            elif length == 127:
                ext = sock.recv(8)
                if len(ext) < 8:
                    return None
                length = int.from_bytes(ext, "big")
            mask = b""
            if masked:
                mask = sock.recv(4)
                if len(mask) < 4:
                    return None
            payload = b""
            while len(payload) < length:
                chunk = sock.recv(length - len(payload))
                if not chunk:
                    break
                payload += chunk
            if len(payload) < length:
                return None
            if masked and mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 8:  # close
                return None
            return payload

        def send_frame(sock, data: bytes) -> None:
            hdr = bytearray()
            hdr.append(0x82)  # FIN + binary
            l = len(data)
            if l < 126:
                hdr.append(l)
            elif l < (1 << 16):
                hdr.append(126)
                hdr += l.to_bytes(2, "big")
            else:
                hdr.append(127)
                hdr += l.to_bytes(8, "big")
            sock.sendall(bytes(hdr) + data)

        self.connection.settimeout(0.2)
        upstream.settimeout(0.2)
        try:
            while True:
                try:
                    data = recv_frame(self.connection)
                    if data is None:
                        break
                    if data:
                        upstream.sendall(data)
                except socket.timeout:
                    pass
                except Exception:
                    break
                try:
                    resp = upstream.recv(4096)
                    if resp:
                        send_frame(self.connection, resp)
                    else:
                        break
                except socket.timeout:
                    pass
                except Exception:
                    break
        finally:
            try:
                upstream.close()
            except Exception:
                pass
            try:
                self.connection.close()
            except Exception:
                pass

    def _stream_watch(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: Optional[str],
        query: Dict[str, List[str]],
        transform,
    ) -> None:
        # Watches use latest observed rv; no pagination
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            start = time.time()
            timeout = int(query.get("timeoutSeconds", ["0"])[0] or 0) or None
            heartbeat = int(query.get("heartbeatSeconds", ["0"])[0] or 0) or None
            allow_bm = query.get("allowWatchBookmarks", ["0"])[0] in ("1", "true", "True")
            rv_param = query.get("resourceVersion", [""])[0] or None
            # Emit initial bookmark if requested/allowed
            if allow_bm:
                try:
                    current = self._max_rv_for(group, version, resource, namespace)
                except Exception:
                    current = 0
                initial_rv = rv_param if rv_param else str(current)
                bm = {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": str(initial_rv)}}}
                self.wfile.write(json.dumps(bm, separators=(",", ":")).encode("utf-8") + b"\n")
                self.wfile.flush()
            for ev_type, obj in self.server.store.watch(group, version, resource, namespace, heartbeat_seconds=heartbeat, allow_bookmarks=allow_bm, since_rv=int(rv_param) if rv_param and rv_param.isdigit() else None):  # type: ignore[attr-defined]
                body = {"type": ev_type, "object": transform(obj)}
                line = json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"
                self.wfile.write(line)
                self.wfile.flush()
                if timeout is not None and timeout > 0 and (time.time() - start) >= timeout:
                    break
        except BrokenPipeError:
            pass

    def _serve_dynamic_group_discovery(self, path: str) -> bool:
        m_group = re.match(r"^/apis/([^/]+)$", path)
        if m_group:
            group = m_group.group(1)
            versions = self._crd_versions_for_group(group)
            if not versions:
                return False
            payload = {
                "kind": "APIGroup",
                "apiVersion": "v1",
                "name": group,
                "versions": [{"groupVersion": f"{group}/{ver}", "version": ver} for ver in versions],
                "preferredVersion": {"groupVersion": f"{group}/{versions[0]}", "version": versions[0]},
                "serverAddressByClientCIDRs": [],
            }
            self._ok(payload)
            return True
        m_version = re.match(r"^/apis/([^/]+)/([^/]+)$", path)
        if m_version:
            group, version = m_version.group(1), m_version.group(2)
            resources = self._crd_resources_for(group, version)
            if not resources:
                return False
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": f"{group}/{version}",
                    "resources": resources,
                }
            )
            return True
        return False

    @classmethod
    def _crd_versions_for_group(cls, group: str) -> List[str]:
        with cls.crd_lock:
            versions = sorted({ver for g, ver, _ in cls.crd_registry.keys() if g == group})
        return versions

    @classmethod
    def _dynamic_group_names(cls) -> List[str]:
        with cls.crd_lock:
            names = sorted({g for (g, _, _) in cls.crd_registry.keys()})
        return names

    @classmethod
    def _crd_resources_for(cls, group: str, version: str) -> List[Dict[str, Any]]:
        with cls.crd_lock:
            entries = [
                (plural, meta)
                for (g, v, plural), meta in cls.crd_registry.items()
                if g == group and v == version
            ]
        resources: List[Dict[str, Any]] = []
        for plural, meta in entries:
            resources.append(
                {
                    "name": plural,
                    "singularName": meta.get("singularName", ""),
                    "namespaced": meta.get("namespaced", True),
                    "kind": meta.get("kind", ""),
                    "verbs": [
                        "get",
                        "list",
                        "create",
                        "delete",
                        "patch",
                        "update",
                        "watch",
                    ],
                    "shortNames": meta.get("shortNames", []),
                }
            )
        return resources

    @classmethod
    def _register_crd(cls, obj: K8sObject) -> None:
        spec = obj.spec or {}
        group = spec.get("group")
        versions = spec.get("versions", [])
        names = spec.get("names", {})
        plural = names.get("plural")
        kind = names.get("kind")
        scope = spec.get("scope", "Namespaced")
        if not group or not versions or not plural or not kind:
            return
        namespaced = scope.lower() == "namespaced"
        crd_name = obj.name
        with cls.crd_lock:
            cls._unregister_crd(crd_name)
            keys: List[Tuple[str, str, str]] = []
            for ver in versions:
                if not ver.get("served", True):
                    continue
                vname = ver.get("name")
                if not vname:
                    continue
                key = (group, vname, plural)
                cls.crd_registry[key] = {
                    "kind": kind,
                    "namespaced": namespaced,
                    "shortNames": names.get("shortNames", []),
                    "singularName": names.get("singular", ""),
                }
                keys.append(key)
            if keys:
                cls.crd_index[crd_name] = keys

    @classmethod
    def _unregister_crd(cls, crd_name: str) -> None:
        with cls.crd_lock:
            keys = cls.crd_index.pop(crd_name, [])
            for key in keys:
                cls.crd_registry.pop(key, None)

    @classmethod
    def _lookup_crd(cls, group: str, version: str, plural: str) -> Optional[Dict[str, Any]]:
        with cls.crd_lock:
            return cls.crd_registry.get((group, version, plural))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        # Allow unauthenticated discovery/OpenAPI for kubectl validation
        if path not in {"/openapi/v2", "/swagger.json", "/api", "/apis", "/version"}:
            if not self._authz(role="read"):
                return

        if path == "/healthz" or path == "/readyz":
            self._ok({"status": "ok"})
            return
        if path == "/version":
            self._ok(K8S_VERSION)
            return
        if path == "/api":
            self._ok({"versions": ["v1"]})
            return
        if path == "/apis":
            groups = [
                        {
                            "name": "apps",
                            "versions": [{"groupVersion": "apps/v1", "version": "v1"}],
                            "preferredVersion": {"groupVersion": "apps/v1", "version": "v1"},
                        },
                        {
                            "name": "networking.k8s.io",
                            "versions": [
                                {"groupVersion": "networking.k8s.io/v1", "version": "v1"}
                            ],
                            "preferredVersion": {
                                "groupVersion": "networking.k8s.io/v1",
                                "version": "v1",
                            },
                        },
                        {
                            "name": "rbac.authorization.k8s.io",
                            "versions": [
                                {"groupVersion": "rbac.authorization.k8s.io/v1", "version": "v1"}
                            ],
                            "preferredVersion": {
                                "groupVersion": "rbac.authorization.k8s.io/v1",
                                "version": "v1",
                            },
                        },
                        {
                            "name": "policy",
                            "versions": [{"groupVersion": "policy/v1", "version": "v1"}],
                            "preferredVersion": {"groupVersion": "policy/v1", "version": "v1"},
                        },
                        {
                            "name": "autoscaling",
                            "versions": [
                                {"groupVersion": "autoscaling/v2", "version": "v2"}
                            ],
                            "preferredVersion": {
                                "groupVersion": "autoscaling/v2",
                                "version": "v2",
                            },
                        },
                        {
                            "name": "apiextensions.k8s.io",
                            "versions": [
                                {"groupVersion": "apiextensions.k8s.io/v1", "version": "v1"}
                            ],
                            "preferredVersion": {
                                "groupVersion": "apiextensions.k8s.io/v1",
                                "version": "v1",
                            },
                        },
            ]
            existing = {g["name"] for g in groups}
            for dyn in self._dynamic_group_names():
                if dyn in existing:
                    continue
                versions = self._crd_versions_for_group(dyn)
                if not versions:
                    continue
                groups.append(
                    {
                        "name": dyn,
                        "versions": [
                            {"groupVersion": f"{dyn}/{ver}", "version": ver}
                            for ver in versions
                        ],
                        "preferredVersion": {
                            "groupVersion": f"{dyn}/{versions[0]}",
                            "version": versions[0],
                        },
                    }
                )
            self._ok({"groups": groups})
            return
        if path in ("/openapi/v2", "/swagger.json"):
            # Minimal swagger stub for client-go consumers
            doc = {
                "swagger": "2.0",
                "info": {"title": "k1s apishim", "version": "0.1.0"},
                "paths": {},
                "definitions": {},
            }
            self._ok(doc)
            return
        if path == "/api/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "v1",
                    "resources": [
                        {
                            "name": "namespaces",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "Namespace",
                            "verbs": ["get", "list", "create", "delete", "patch", "update"],
                            "shortNames": ["ns"],
                        },
                        {
                            "name": "configmaps",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "ConfigMap",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["cm"],
                        },
                        {
                            "name": "secrets",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Secret",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                        {
                            "name": "serviceaccounts",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "ServiceAccount",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["sa"],
                        },
                        {
                            "name": "services",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Service",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["svc"],
                        },
                        {
                            "name": "endpoints",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Endpoints",
                            "verbs": ["get", "list"],
                            "shortNames": ["ep"],
                        },
                        {
                            "name": "nodes",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "Node",
                            "verbs": ["get", "list"],
                        },
                        {
                            "name": "pods",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Pod",
                            "verbs": ["get", "list"],
                            "shortNames": ["po"],
                        },
                    ],
                }
            )
            return
        if path == "/apis/apps/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "apps/v1",
                    "resources": [
                        {
                            "name": "deployments",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Deployment",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["deploy", "deploys"],
                        },
                        {
                            "name": "deployments/status",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Deployment",
                            "verbs": ["get", "patch", "update"],
                        },
                        {
                            "name": "deployments/scale",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Scale",
                            "verbs": ["get", "patch", "update"],
                        },
                    ],
                }
            )
            return
        if path == "/apis/networking.k8s.io/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "networking.k8s.io/v1",
                    "resources": [
                        {
                            "name": "ingresses",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Ingress",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["ing"],
                        }
                    ],
                }
            )
            return
        if path == "/apis/rbac.authorization.k8s.io/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "rbac.authorization.k8s.io/v1",
                    "resources": [
                        {
                            "name": "roles",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Role",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                        {
                            "name": "rolebindings",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "RoleBinding",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                        {
                            "name": "clusterroles",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "ClusterRole",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                        {
                            "name": "clusterrolebindings",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "ClusterRoleBinding",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                    ],
                }
            )
            return
        if path == "/apis/policy/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "policy/v1",
                    "resources": [
                        {
                            "name": "poddisruptionbudgets",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "PodDisruptionBudget",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["pdb"],
                        }
                    ],
                }
            )
            return
        if path == "/apis/autoscaling/v2":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "autoscaling/v2",
                    "resources": [
                        {
                            "name": "horizontalpodautoscalers",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "HorizontalPodAutoscaler",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["hpa"],
                        }
                    ],
                }
            )
            return
        if path == "/apis/apiextensions.k8s.io/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "apiextensions.k8s.io/v1",
                    "resources": [
                        {
                            "name": "customresourcedefinitions",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "CustomResourceDefinition",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["crd"],
                        }
                    ],
                }
            )
            return

        if self._serve_dynamic_group_discovery(path):
            return

        # Lists and gets for core resources
        plural, ns, name = _ns_name(path)
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"}:
            if name is None:
                # watch support on LIST endpoints
                if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                    if not self._rbac_allows("watch", plural):
                        self._deny(403)
                        return
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    try:
                        start = time.time()
                        timeout = int(q.get("timeoutSeconds", ["0"]) [0] or 0) or None
                        heartbeat = int(q.get("heartbeatSeconds", ["0"]) [0] or 0) or None
                        allow_bm = q.get("allowWatchBookmarks", ["0"]) [0] in ("1", "true", "True")
                        for ev_type, obj in self.server.store.watch("", "v1", plural, ns, heartbeat_seconds=heartbeat, allow_bookmarks=allow_bm):  # type: ignore[attr-defined]
                            line = json.dumps({"type": ev_type, "object": _to_obj(obj)}, separators=(",", ":")).encode("utf-8") + b"\n"
                            self.wfile.write(line)
                            self.wfile.flush()
                            if timeout is not None and timeout > 0 and (time.time() - start) >= timeout:
                                break
                    except BrokenPipeError:
                        pass
                    return
                # LIST
                if not self._rbac_allows("list", plural):
                    self._deny(403)
                    return
                try:
                    limit = int(q.get("limit", ["0"])[0] or 0)
                except Exception:
                    limit = 0
                cont = q.get("continue", [""])[0] or None
                if plural == "namespaces":
                    items = self.server.store.list("", "v1", "namespaces", None)  # type: ignore[attr-defined]
                else:
                    if ns is None:
                        items = self.server.store.list_all("", "v1", plural)  # type: ignore[attr-defined]
                    else:
                        items = self.server.store.list("", "v1", plural, ns)  # type: ignore[attr-defined]
                self._ok(_list_with_rv(items, _to_obj, kind=_kind(plural), api_version="v1", limit=limit if limit > 0 else None, continue_token=cont))
                return
            else:
                # GET
                if not self._rbac_allows("get", plural):
                    self._deny(403)
                    return
                obj = self.server.store.get("", "v1", plural, None if plural == "namespaces" else ns, name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_obj(obj))
                return
        # Endpoints (projected from controller state)
        if plural == "endpoints":
            if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                self.send_response(HTTPStatus.NOT_IMPLEMENTED)
                self.end_headers()
                return
            if name is None:
                # list endpoints within namespace (or all)
                svcs = (
                    self.server.store.list_all("", "v1", "services")  # type: ignore[attr-defined]
                    if ns is None
                    else self.server.store.list("", "v1", "services", ns)  # type: ignore[attr-defined]
                )
                items: List[Dict[str, Any]] = []
                for svc in svcs:
                    ep = _endpoints_for_service(self.server.state, svc)  # type: ignore[attr-defined]
                    if ep:
                        items.append(ep)
                rv = max((int(i["metadata"].get("resourceVersion", "0")) for i in items), default=0)
                try:
                    limit = int(q.get("limit", ["0"])[0] or 0)
                except Exception:
                    limit = 0
                cont = q.get("continue", [""])[0] or None
                selected = items
                cont_token = None
                if cont:
                    for idx, obj in enumerate(items):
                        if obj["metadata"].get("name") == cont:
                            selected = items[idx + 1 :]
                            break
                if limit > 0 and len(selected) > limit:
                    cont_token = selected[limit]["metadata"].get("name")
                    selected = selected[:limit]
                meta = {"resourceVersion": str(rv)}
                if cont_token:
                    meta["continue"] = cont_token
                self._ok({"kind": "EndpointsList", "apiVersion": "v1", "metadata": meta, "items": selected})
                return
            svc = self.server.store.get("", "v1", "services", ns, name)  # type: ignore[attr-defined]
            if not svc:
                self._not_found()
                return
            ep = _endpoints_for_service(self.server.state, svc)  # type: ignore[attr-defined]
            if not ep:
                self._not_found()
                return
            self._ok(ep)
            return

        # Pods (projected from runtime containers)
        if plural == "pods":
            containers = []
            try:
                containers = self.server.runtime.list_containers_info()  # type: ignore[attr-defined]
            except Exception:
                containers = []
            now_rv = int(time.time() * 1000)
            pod_objs = []
            for c in containers:
                labels = c.get("labels", {}) or {}
                c_ns = labels.get("ae.namespace") or "default"
                if ns and c_ns != ns:
                    continue
                pod_objs.append(_pod_obj(c, now_rv, labels.get("ae.node")))
            if name is None:
                try:
                    limit = int(q.get("limit", ["0"])[0] or 0)
                except Exception:
                    limit = 0
                cont = q.get("continue", [""])[0] or None
                selected = pod_objs
                cont_token = None
                if cont:
                    for idx, obj in enumerate(pod_objs):
                        if obj["metadata"].get("name") == cont:
                            selected = pod_objs[idx + 1 :]
                            break
                if limit > 0 and len(selected) > limit:
                    cont_token = selected[limit]["metadata"].get("name")
                    selected = selected[:limit]
                meta = {"resourceVersion": str(now_rv)}
                if cont_token:
                    meta["continue"] = cont_token
                self._ok({"kind": "PodList", "apiVersion": "v1", "metadata": meta, "items": selected})
                return
            for p in pod_objs:
                if p["metadata"]["name"] == name:
                    self._ok(p)
                    return
            self._not_found()
            return
        # Pod logs (simple text, no streaming)
        m_logs = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/log$", path)
        if m_logs:
            ns, pod_name = m_logs.group(1), m_logs.group(2)
            tail = q.get("tailLines", ["100"])[0]
            try:
                tail_i = int(tail)
            except Exception:
                tail_i = 100
            try:
                lines = list(self.server.runtime.read_logs(pod_name, follow=False, tail=tail_i))  # type: ignore[attr-defined]
                body = "".join(lines)
                data = body.encode("utf-8", errors="ignore")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                self._json_status(HTTPStatus.INTERNAL_SERVER_ERROR, reason="InternalError", message=str(exc))
            return
        # Pod exec (POST JSON: {command:[...], timeoutSeconds?})
        m_exec = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/exec$", path)
        if m_exec:
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.end_headers()
            return
        # Port-forward (not supported yet)
        m_pf = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/portforward$", path)
        if m_pf:
            # Attempt WebSocket upgrade for simple TCP tunneling. Clients like kubectl use SPDY;
            # this is a best-effort fallback for custom tools.
            qs = parse_qs(parsed.query)
            ports = qs.get("ports") or []
            try:
                target_port = int(ports[0])
            except Exception:
                self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="ports query param required")
                return
            target_host = "127.0.0.1"
            upgrade = (self.headers.get("Upgrade") or "").lower()
            if upgrade == "websocket":
                self._handle_port_forward_ws(target_host, target_port)
            elif upgrade.startswith("spdy"):
                # SPDY/3.1 not implemented yet; hint and fail fast
                self._json_status(
                    HTTPStatus.UPGRADE_REQUIRED,
                    reason="UpgradeRequired",
                    message="SPDY port-forward not yet supported; use WebSocket upgrade or NodePort/VIP",
                )
            else:
                self._json_status(
                    HTTPStatus.NOT_IMPLEMENTED,
                    reason="NotImplemented",
                    message="port-forward requires WebSocket Upgrade; kubectl SPDY not supported yet",
                )
            return

        # Nodes (projected from controller state)
        if path == "/api/v1/nodes":
            nodes = self.server.state.list_nodes()  # type: ignore[attr-defined]
            now_rv = int(time.time() * 1000)
            items = []
            for idx, (rec, st) in enumerate(nodes, start=1):
                items.append(_node_obj(rec, st, now_rv + idx))
            self._ok({"kind": "NodeList", "apiVersion": "v1", "metadata": {"resourceVersion": str(now_rv)}, "items": items})
            return
        if path.startswith("/api/v1/nodes/"):
            node_name = path.split("/")[-1]
            nodes = self.server.state.list_nodes()  # type: ignore[attr-defined]
            for rec, st in nodes:
                if node_name in {rec.node_id, rec.name or ""}:
                    self._ok(_node_obj(rec, st, int(time.time() * 1000)))
                    return
            self._not_found()
            return

        # apps/v1 deployments
        if path.startswith("/apis/apps/v1"):
            d_plural, d_ns, d_name = _apps_ns_name(path)
            if d_plural == "deployments":
                if d_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        if not self._rbac_allows("watch", "deployments"):
                            self._deny(403)
                            return
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        try:
                            start = time.time()
                            timeout = int(q.get("timeoutSeconds", ["0"]) [0] or 0) or None
                            heartbeat = int(q.get("heartbeatSeconds", ["0"]) [0] or 0) or None
                            allow_bm = q.get("allowWatchBookmarks", ["0"]) [0] in ("1", "true", "True")
                            for ev_type, obj in self.server.store.watch("apps", "v1", "deployments", d_ns, heartbeat_seconds=heartbeat, allow_bookmarks=allow_bm):  # type: ignore[attr-defined]
                                line = json.dumps({"type": ev_type, "object": _to_deployment(obj)}, separators=(",", ":")).encode("utf-8") + b"\n"
                                self.wfile.write(line)
                                self.wfile.flush()
                                if timeout is not None and timeout > 0 and (time.time() - start) >= timeout:
                                    break
                        except BrokenPipeError:
                            pass
                        return
                    if not self._rbac_allows("list", "deployments"):
                        self._deny(403)
                        return
                    try:
                        limit = int(q.get("limit", ["0"])[0] or 0)
                    except Exception:
                        limit = 0
                    cont = q.get("continue", [""])[0] or None
                    items = (
                        self.server.store.list_all("apps", "v1", "deployments")  # type: ignore[attr-defined]
                        if d_ns is None
                        else self.server.store.list("apps", "v1", "deployments", d_ns)  # type: ignore[attr-defined]
                    )
                    self._ok(
                        _list_with_rv(items, _to_deployment, kind="Deployment", api_version="apps/v1", limit=limit if limit > 0 else None, continue_token=cont)
                    )
                    return
                else:
                    if not self._rbac_allows("get", "deployments"):
                        self._deny(403)
                        return
                    obj = self.server.store.get("apps", "v1", "deployments", d_ns, d_name)  # type: ignore[attr-defined]
                    if not obj:
                        self._not_found()
                        return
                    self._ok(_to_deployment(obj))
                    return
            if d_plural == "deployments/status" and d_name:
                obj = self.server.store.get("apps", "v1", "deployments", d_ns, d_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_deployment(obj))
                return
            if d_plural == "deployments/scale" and d_name:
                obj = self.server.store.get("apps", "v1", "deployments", d_ns, d_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_scale(obj))
                return

        # networking ingresses
        if path.startswith("/apis/networking.k8s.io/v1"):
            n_plural, n_ns, n_name = _net_ns_name(path)
            if n_plural == "ingresses":
                if n_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        try:
                            start = time.time()
                            timeout = int(q.get("timeoutSeconds", ["0"]) [0] or 0) or None
                            heartbeat = int(q.get("heartbeatSeconds", ["0"]) [0] or 0) or None
                            allow_bm = q.get("allowWatchBookmarks", ["0"]) [0] in ("1", "true", "True")
                            for ev_type, obj in self.server.store.watch("networking.k8s.io", "v1", "ingresses", n_ns, heartbeat_seconds=heartbeat, allow_bookmarks=allow_bm):  # type: ignore[attr-defined]
                                line = json.dumps({"type": ev_type, "object": _to_ingress(obj)}, separators=(",", ":")).encode("utf-8") + b"\n"
                                self.wfile.write(line)
                                self.wfile.flush()
                                if timeout is not None and timeout > 0 and (time.time() - start) >= timeout:
                                    break
                        except BrokenPipeError:
                            pass
                        return
                    items = (
                        self.server.store.list_all("networking.k8s.io", "v1", "ingresses")  # type: ignore[attr-defined]
                        if n_ns is None
                        else self.server.store.list("networking.k8s.io", "v1", "ingresses", n_ns)  # type: ignore[attr-defined]
                    )
                    try:
                        limit = int(q.get("limit", ["0"])[0] or 0)
                    except Exception:
                        limit = 0
                    cont = q.get("continue", [""])[0] or None
                    self._ok(_list_with_rv(items, _to_ingress, kind="Ingress", api_version="networking.k8s.io/v1", limit=limit if limit > 0 else None, continue_token=cont))
                    return
                else:
                    obj = self.server.store.get("networking.k8s.io", "v1", "ingresses", n_ns, n_name)  # type: ignore[attr-defined]
                    if not obj:
                        self._not_found()
                        return
                    self._ok(_to_ingress(obj))
                    return

        # apiextensions CRDs
        if path.startswith("/apis/apiextensions.k8s.io/v1"):
            crd_plural, crd_name = _gv_cluster_name(
                path, "apiextensions.k8s.io", "v1", "customresourcedefinitions"
            )
            if crd_plural == "customresourcedefinitions":
                if crd_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch(
                            "apiextensions.k8s.io",
                            "v1",
                            "customresourcedefinitions",
                            None,
                            q,
                            transform=_to_crd,
                        )
                        return
                    items = self.server.store.list_all(  # type: ignore[attr-defined]
                        "apiextensions.k8s.io", "v1", "customresourcedefinitions"
                    )
                    self._ok(
                        {
                            "kind": "CustomResourceDefinitionList",
                            "apiVersion": "apiextensions.k8s.io/v1",
                            "items": [_to_crd(i) for i in items],
                        }
                    )
                    return
                obj = self.server.store.get(  # type: ignore[attr-defined]
                    "apiextensions.k8s.io", "v1", "customresourcedefinitions", None, crd_name
                )
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_crd(obj))
                return

        if self._handle_custom_resource_get(path, q):
            return

        # rbac: roles/rolebindings (namespaced) and clusterroles/clusterrolebindings (cluster-scoped)
        if path.startswith("/apis/rbac.authorization.k8s.io/v1"):
            # namespaced
            r_plural, r_ns, r_name = _gv_ns_name(path, "rbac.authorization.k8s.io", "v1", "roles")
            if r_plural == "roles":
                if r_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("rbac.authorization.k8s.io", "v1", "roles", r_ns, q, transform=_to_generic("rbac.authorization.k8s.io", "v1", "Role", "roles"))
                        return
                    items = (
                        self.server.store.list_all("rbac.authorization.k8s.io", "v1", "roles")  # type: ignore[attr-defined]
                        if r_ns is None
                        else self.server.store.list("rbac.authorization.k8s.io", "v1", "roles", r_ns)  # type: ignore[attr-defined]
                    )
                    self._ok({"kind": "RoleList", "apiVersion": "rbac.authorization.k8s.io/v1", "items": [_to_generic("rbac.authorization.k8s.io", "v1", "Role", "roles")(i) for i in items]})
                    return
                obj = self.server.store.get("rbac.authorization.k8s.io", "v1", "roles", r_ns, r_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("rbac.authorization.k8s.io", "v1", "Role", "roles")(obj))
                return
            rb_plural, rb_ns, rb_name = _gv_ns_name(path, "rbac.authorization.k8s.io", "v1", "rolebindings")
            if rb_plural == "rolebindings":
                if rb_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("rbac.authorization.k8s.io", "v1", "rolebindings", rb_ns, q, transform=_to_generic("rbac.authorization.k8s.io", "v1", "RoleBinding", "rolebindings"))
                        return
                    items = (
                        self.server.store.list_all("rbac.authorization.k8s.io", "v1", "rolebindings")  # type: ignore[attr-defined]
                        if rb_ns is None
                        else self.server.store.list("rbac.authorization.k8s.io", "v1", "rolebindings", rb_ns)  # type: ignore[attr-defined]
                    )
                    self._ok({"kind": "RoleBindingList", "apiVersion": "rbac.authorization.k8s.io/v1", "items": [_to_generic("rbac.authorization.k8s.io", "v1", "RoleBinding", "rolebindings")(i) for i in items]})
                    return
                obj = self.server.store.get("rbac.authorization.k8s.io", "v1", "rolebindings", rb_ns, rb_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("rbac.authorization.k8s.io", "v1", "RoleBinding", "rolebindings")(obj))
                return
            # cluster-scoped
            cr_plural, cr_name = _gv_cluster_name(path, "rbac.authorization.k8s.io", "v1", "clusterroles")
            if cr_plural == "clusterroles":
                if cr_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("rbac.authorization.k8s.io", "v1", "clusterroles", None, q, transform=_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRole", "clusterroles"))
                        return
                    items = self.server.store.list_all("rbac.authorization.k8s.io", "v1", "clusterroles")  # type: ignore[attr-defined]
                    self._ok({"kind": "ClusterRoleList", "apiVersion": "rbac.authorization.k8s.io/v1", "items": [_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRole", "clusterroles")(i) for i in items]})
                    return
                obj = self.server.store.get("rbac.authorization.k8s.io", "v1", "clusterroles", None, cr_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRole", "clusterroles")(obj))
                return
            crb_plural, crb_name = _gv_cluster_name(path, "rbac.authorization.k8s.io", "v1", "clusterrolebindings")
            if crb_plural == "clusterrolebindings":
                if crb_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("rbac.authorization.k8s.io", "v1", "clusterrolebindings", None, q, transform=_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRoleBinding", "clusterrolebindings"))
                        return
                    items = self.server.store.list_all("rbac.authorization.k8s.io", "v1", "clusterrolebindings")  # type: ignore[attr-defined]
                    self._ok({"kind": "ClusterRoleBindingList", "apiVersion": "rbac.authorization.k8s.io/v1", "items": [_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRoleBinding", "clusterrolebindings")(i) for i in items]})
                    return
                obj = self.server.store.get("rbac.authorization.k8s.io", "v1", "clusterrolebindings", None, crb_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRoleBinding", "clusterrolebindings")(obj))
                return

        # policy/v1 PodDisruptionBudget
        if path.startswith("/apis/policy/v1"):
            p_plural, p_ns, p_name = _gv_ns_name(path, "policy", "v1", "poddisruptionbudgets")
            if p_plural == "poddisruptionbudgets":
                if p_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("policy", "v1", "poddisruptionbudgets", p_ns, q, transform=_to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets"))
                        return
                    items = (
                        self.server.store.list_all("policy", "v1", "poddisruptionbudgets")  # type: ignore[attr-defined]
                        if p_ns is None
                        else self.server.store.list("policy", "v1", "poddisruptionbudgets", p_ns)  # type: ignore[attr-defined]
                    )
                    self._ok({"kind": "PodDisruptionBudgetList", "apiVersion": "policy/v1", "items": [_to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets")(i) for i in items]})
                    return
                obj = self.server.store.get("policy", "v1", "poddisruptionbudgets", p_ns, p_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets")(obj))
                return

        # autoscaling/v2 HPA
        if path.startswith("/apis/autoscaling/v2"):
            h_plural, h_ns, h_name = _gv_ns_name(path, "autoscaling", "v2", "horizontalpodautoscalers")
            if h_plural == "horizontalpodautoscalers":
                if h_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("autoscaling", "v2", "horizontalpodautoscalers", h_ns, q, transform=_to_generic("autoscaling", "v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers"))
                        return
                    items = (
                        self.server.store.list_all("autoscaling", "v2", "horizontalpodautoscalers")  # type: ignore[attr-defined]
                        if h_ns is None
                        else self.server.store.list("autoscaling", "v2", "horizontalpodautoscalers", h_ns)  # type: ignore[attr-defined]
                    )
                    self._ok({"kind": "HorizontalPodAutoscalerList", "apiVersion": "autoscaling/v2", "items": [_to_generic("autoscaling", "v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers")(i) for i in items]})
                    return
                obj = self.server.store.get("autoscaling", "v2", "horizontalpodautoscalers", h_ns, h_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("autoscaling", "v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers")(obj))
                return
        self._not_found()

    def do_POST(self) -> None:  # noqa: N802
        if not self._authz(role="write"):
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        doc = _read_json(body)

        # Pod exec (JSON {command:[], timeoutSeconds?})
        m_exec = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/exec$", self.path)
        if m_exec:
            cmd = doc.get("command") or doc.get("cmd")
            timeout = doc.get("timeoutSeconds") or doc.get("timeout")
            if not isinstance(cmd, list) or not cmd:
                self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="command must be a non-empty list")
                return
            try:
                rc = int(
                    self.server.runtime.exec(  # type: ignore[attr-defined]
                        m_exec.group(2), [str(c) for c in cmd], timeout=int(timeout) if timeout else None
                    )
                )
                self._ok({"kind": "Status", "status": "Success", "code": 200, "metadata": {}, "details": {"exitCode": rc}})
            except Exception as exc:
                self._json_status(HTTPStatus.INTERNAL_SERVER_ERROR, reason="InternalError", message=str(exc))
            return
        # Port-forward stub
        m_pf = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/portforward$", self.path)
        if m_pf:
            self._json_status(
                HTTPStatus.NOT_IMPLEMENTED,
                reason="NotImplemented",
                message="port-forward requires WebSocket Upgrade; kubectl SPDY not supported yet",
            )
            return

        plural, ns, name = _ns_name(self.path)
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"}:
            md = doc.get("metadata") or {}
            name_in = md.get("name") or name
            if not isinstance(name_in, str) or not name_in:
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="metadata.name required")
                return
            ns_in = md.get("namespace") or ns
            if plural == "namespaces":
                ns_in = None
            if not _valid_name(name_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                return
            if ns_in is not None and not _valid_name(ns_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                return
            created = self.server.store.upsert(  # type: ignore[attr-defined]
                "",
                "v1",
                plural,
                ns_in,
                name_in,
                metadata=_normalize_metadata(md, name_in, ns_in, plural),
                spec=doc.get("data") if plural in {"configmaps", "secrets"} else (doc.get("spec") or {}),
                status=doc.get("status") or {},
            )
            self.send_response(HTTPStatus.CREATED)
            out = _json(_to_obj(created))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        # apps/v1 deployments
        if self.path.startswith("/apis/apps/v1"):
            d_plural, d_ns, d_name = _apps_ns_name(self.path)
            if d_plural == "deployments":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or d_name
                ns_in = md.get("namespace") or d_ns
                if not name_in or not _valid_name(name_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                    return
                if not ns_in or not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                    return
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apps",
                    "v1",
                    "deployments",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "deployments"),
                    spec=doc.get("spec") or {},
                    status=_synthesize_deploy_status(doc.get("spec") or {}, doc.get("status") or {}),
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_deployment(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
        # networking.k8s.io/v1 ingresses
        if self.path.startswith("/apis/networking.k8s.io/v1"):
            n_plural, n_ns, n_name = _net_ns_name(self.path)
            if n_plural == "ingresses":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or n_name
                ns_in = md.get("namespace") or n_ns
                if not name_in or not _valid_name(name_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                    return
                if not ns_in or not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                    return
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "networking.k8s.io",
                    "v1",
                    "ingresses",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "ingresses"),
                    spec=doc.get("spec") or {},
                    status=doc.get("status") or {},
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_ingress(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        # CRDs
        if self.path.startswith("/apis/apiextensions.k8s.io/v1"):
            crd_plural, crd_name = _gv_cluster_name(
                self.path, "apiextensions.k8s.io", "v1", "customresourcedefinitions"
            )
            if crd_plural == "customresourcedefinitions":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or crd_name
                if not name_in or not _valid_name(name_in):
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata.name (DNS-1123 label)",
                    )
                    return
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apiextensions.k8s.io",
                    "v1",
                    "customresourcedefinitions",
                    None,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, None, "customresourcedefinitions"),
                    spec=doc.get("spec") or {},
                    status=doc.get("status") or {},
                )
                self._register_crd(created)
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_crd(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        if self._handle_custom_resource_post(doc):
            return

        # rbac (namespaced and cluster resources)
        if self.path.startswith("/apis/rbac.authorization.k8s.io/v1"):
            # namespaced roles/rolebindings
            for plural, kind in (("roles", "Role"), ("rolebindings", "RoleBinding")):
                r_plural, r_ns, r_name = _gv_ns_name(self.path, "rbac.authorization.k8s.io", "v1", plural)
                if r_plural == plural:
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or r_name
                    ns_in = md.get("namespace") or r_ns
                    if not name_in or not _valid_name(name_in):
                        self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                        return
                    if not ns_in or not _valid_name(ns_in):
                        self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                        return
                    created = self.server.store.upsert(  # type: ignore[attr-defined]
                        "rbac.authorization.k8s.io",
                        "v1",
                        plural,
                        ns_in,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, ns_in, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                    self.send_response(HTTPStatus.CREATED)
                    out = _json(_to_generic("rbac.authorization.k8s.io", "v1", kind, plural)(created))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
            # clusterroles/clusterrolebindings
            for plural, kind in ("clusterroles", "ClusterRole"), ("clusterrolebindings", "ClusterRoleBinding"):
                cr_plural, cr_name = _gv_cluster_name(self.path, "rbac.authorization.k8s.io", "v1", plural)
                if cr_plural == plural:
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or cr_name
                    if not name_in or not _valid_name(name_in):
                        self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                        return
                    created = self.server.store.upsert(  # type: ignore[attr-defined]
                        "rbac.authorization.k8s.io",
                        "v1",
                        plural,
                        None,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, None, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                    self.send_response(HTTPStatus.CREATED)
                    out = _json(_to_generic("rbac.authorization.k8s.io", "v1", kind, plural)(created))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return

        # policy/v1 PDB
        if self.path.startswith("/apis/policy/v1"):
            p_plural, p_ns, p_name = _gv_ns_name(self.path, "policy", "v1", "poddisruptionbudgets")
            if p_plural == "poddisruptionbudgets":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or p_name
                ns_in = md.get("namespace") or p_ns
                if not name_in or not _valid_name(name_in) or not ns_in or not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata")
                    return
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "policy",
                    "v1",
                    "poddisruptionbudgets",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "poddisruptionbudgets"),
                    spec=_spec_payload("poddisruptionbudgets", doc),
                    status=doc.get("status") or {},
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets")(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        # autoscaling/v2 HPA
        if self.path.startswith("/apis/autoscaling/v2"):
            h_plural, h_ns, h_name = _gv_ns_name(self.path, "autoscaling", "v2", "horizontalpodautoscalers")
            if h_plural == "horizontalpodautoscalers":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or h_name
                ns_in = md.get("namespace") or h_ns
                if not name_in or not _valid_name(name_in) or not ns_in or not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata")
                    return
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "autoscaling",
                    "v2",
                    "horizontalpodautoscalers",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "horizontalpodautoscalers"),
                    spec=_spec_payload("horizontalpodautoscalers", doc),
                    status=doc.get("status") or {},
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_generic("autoscaling", "v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers")(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        self._not_found()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authz():
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        doc = _read_json(body)
        plural, ns, name = _ns_name(self.path)
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"} and name:
            md = doc.get("metadata") or {}
            name_in = md.get("name") or name
            ns_in = md.get("namespace") or ns
            if plural == "namespaces":
                ns_in = None
            if not _valid_name(name_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                return
            if ns_in is not None and not _valid_name(ns_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                return
            updated = self.server.store.upsert(  # type: ignore[attr-defined]
                "",
                "v1",
                plural,
                ns_in,
                name_in,
                metadata=_normalize_metadata(md, name_in, ns_in, plural),
                spec=doc.get("data") if plural in {"configmaps", "secrets"} else (doc.get("spec") or {}),
                status=doc.get("status") or {},
            )
            self._ok(_to_obj(updated))
            return
        # apps/v1 deployments and networking ingresses
        if self.path.startswith("/apis/apps/v1"):
            d_plural, d_ns, d_name = _apps_ns_name(self.path)
            if d_plural == "deployments" and d_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or d_name
                ns_in = md.get("namespace") or d_ns
                if not name_in or not _valid_name(name_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                    return
                if ns_in is not None and not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                    return
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apps",
                    "v1",
                    "deployments",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "deployments"),
                    spec=doc.get("spec") or {},
                    status=_synthesize_deploy_status(doc.get("spec") or {}, doc.get("status") or {}),
                )
                self._ok(_to_deployment(updated))
                return
            if d_plural == "deployments/scale" and d_name:
                obj = self.server.store.get("apps", "v1", "deployments", d_ns, d_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                desired = doc.get("spec", {}).get("replicas")
                if isinstance(desired, int) and desired >= 0:
                    spec = dict(obj.spec)
                    spec["replicas"] = desired
                    updated = self.server.store.upsert(  # type: ignore[attr-defined]
                        "apps",
                        "v1",
                        "deployments",
                        d_ns,
                        d_name,
                        metadata=obj.metadata,
                        spec=spec,
                        status=_synthesize_deploy_status(spec, obj.status),
                    )
                    self._ok(_to_scale(updated))
                    return
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="spec.replicas must be >= 0")
                return
        if self.path.startswith("/apis/networking.k8s.io/v1"):
            n_plural, n_ns, n_name = _net_ns_name(self.path)
            if n_plural == "ingresses" and n_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or n_name
                ns_in = md.get("namespace") or n_ns
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "networking.k8s.io",
                    "v1",
                    "ingresses",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "ingresses"),
                    spec=doc.get("spec") or {},
                    status=doc.get("status") or {},
                )
                self._ok(_to_ingress(updated))
                return
        if self.path.startswith("/apis/apiextensions.k8s.io/v1"):
            crd_plural, crd_name = _gv_cluster_name(
                self.path, "apiextensions.k8s.io", "v1", "customresourcedefinitions"
            )
            if crd_plural == "customresourcedefinitions" and crd_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or crd_name
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apiextensions.k8s.io",
                    "v1",
                    "customresourcedefinitions",
                    None,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, None, "customresourcedefinitions"),
                    spec=doc.get("spec") or {},
                    status=doc.get("status") or {},
                )
                self._register_crd(updated)
                self._ok(_to_crd(updated))
                return
        if self._handle_custom_resource_put(doc):
            return
        if self.path.startswith("/apis/rbac.authorization.k8s.io/v1"):
            for plural, kind in (("roles", "Role"), ("rolebindings", "RoleBinding")):
                r_plural, r_ns, r_name = _gv_ns_name(self.path, "rbac.authorization.k8s.io", "v1", plural)
                if r_plural == plural and r_name:
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or r_name
                    ns_in = md.get("namespace") or r_ns
                    updated = self.server.store.upsert(  # type: ignore[attr-defined]
                        "rbac.authorization.k8s.io",
                        "v1",
                        plural,
                        ns_in,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, ns_in, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                    self._ok(_to_generic("rbac.authorization.k8s.io", "v1", kind, plural)(updated))
                    return
            for plural, kind in (("clusterroles", "ClusterRole"), ("clusterrolebindings", "ClusterRoleBinding")):
                cr_plural, cr_name = _gv_cluster_name(self.path, "rbac.authorization.k8s.io", "v1", plural)
                if cr_plural == plural and cr_name:
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or cr_name
                    updated = self.server.store.upsert(  # type: ignore[attr-defined]
                        "rbac.authorization.k8s.io",
                        "v1",
                        plural,
                        None,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, None, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                    self._ok(_to_generic("rbac.authorization.k8s.io", "v1", kind, plural)(updated))
                    return
        # policy/v1 PDB
        if self.path.startswith("/apis/policy/v1"):
            p_plural, p_ns, p_name = _gv_ns_name(self.path, "policy", "v1", "poddisruptionbudgets")
            if p_plural == "poddisruptionbudgets" and p_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or p_name
                ns_in = md.get("namespace") or p_ns
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "policy",
                    "v1",
                    "poddisruptionbudgets",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "poddisruptionbudgets"),
                    spec=_spec_payload("poddisruptionbudgets", doc),
                    status=doc.get("status") or {},
                )
                self._ok(_to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets")(updated))
                return
        # autoscaling/v2 HPA
        if self.path.startswith("/apis/autoscaling/v2"):
            h_plural, h_ns, h_name = _gv_ns_name(self.path, "autoscaling", "v2", "horizontalpodautoscalers")
            if h_plural == "horizontalpodautoscalers" and h_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or h_name
                ns_in = md.get("namespace") or h_ns
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "autoscaling",
                    "v2",
                    "horizontalpodautoscalers",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "horizontalpodautoscalers"),
                    spec=_spec_payload("horizontalpodautoscalers", doc),
                    status=doc.get("status") or {},
                )
                self._ok(_to_generic("autoscaling", "v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers")(updated))
                return
        self._not_found()

    def do_PATCH(self) -> None:  # noqa: N802
        if not self._authz():
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        patch = _read_json(body)
        plural, ns, name = _ns_name(self.path)
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"} and name:
            obj = self.server.store.get("", "v1", plural, None if plural == "namespaces" else ns, name)  # type: ignore[attr-defined]
            if not obj:
                self._not_found()
                return
            base = _to_obj(obj)
            if ctype in ("application/merge-patch+json", "application/strategic-merge-patch+json"):
                merged = _merge_dict(base, patch)
            elif ctype in ("application/json", ""):
                merged = patch  # full doc replace
            else:
                self._json_status(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    reason="UnsupportedMediaType",
                    message="only merge/strategic-merge or json supported",
                )
                return
            md = merged.get("metadata") or {}
            spec_or_data = merged.get("data") if plural in {"configmaps", "secrets"} else merged.get("spec")
            name_eff = md.get("name") or name
            ns_eff = None if plural == "namespaces" else (md.get("namespace") or ns)
            if not _valid_name(name_eff):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                return
            if ns_eff is not None and not _valid_name(ns_eff):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                return
            updated = self.server.store.upsert(  # type: ignore[attr-defined]
                "",
                "v1",
                plural,
                ns_eff,
                name_eff,
                metadata=_normalize_metadata(md, name_eff, ns_eff, plural),
                spec=spec_or_data or {},
                status=merged.get("status") or {},
            )
            self._ok(_to_obj(updated))
            return
        if self._handle_custom_resource_patch(ctype, patch):
            return
        if self._patch_extended_resources(ctype, patch):
            return
        self._not_found()

    def _patch_extended_resources(self, ctype: str, patch: Dict[str, Any]) -> bool:
        specs = [
            ("rbac.authorization.k8s.io", "v1", "roles", "Role"),
            ("rbac.authorization.k8s.io", "v1", "rolebindings", "RoleBinding"),
            ("rbac.authorization.k8s.io", "v1", "clusterroles", "ClusterRole"),
            ("rbac.authorization.k8s.io", "v1", "clusterrolebindings", "ClusterRoleBinding"),
            ("policy", "v1", "poddisruptionbudgets", "PodDisruptionBudget"),
            ("autoscaling", "v2", "horizontalpodautoscalers", "HorizontalPodAutoscaler"),
        ]
        for group, version, res, kind in specs:
            if not self.path.startswith(f"/apis/{group}/{version}"):
                continue
            if res.startswith("cluster"):
                plural, name = _gv_cluster_name(self.path, group, version, res)
                if plural != res or not name:
                    continue
                obj = self.server.store.get(group, version, res, None, name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return True
                base = _to_generic(group, version, kind, res)(obj)
                merged = self._apply_patch_merge(base, patch, ctype)
                if merged is None:
                    return True
                md = merged.get("metadata") or {}
                name_eff = md.get("name") or name
                updated = self.server.store.upsert(
                    group,
                    version,
                    res,
                    None,
                    name_eff,
                    metadata=_normalize_metadata(md, name_eff, None, res),
                    spec=_spec_payload(res, merged),
                    status=merged.get("status") or {},
                )  # type: ignore[attr-defined]
                self._ok(_to_generic(group, version, kind, res)(updated))
                return True
            plural, ns, name = _gv_ns_name(self.path, group, version, res)
            if plural != res or not name:
                continue
            obj = self.server.store.get(group, version, res, ns, name)  # type: ignore[attr-defined]
            if not obj:
                self._not_found()
                return True
            base = _to_generic(group, version, kind, res)(obj)
            merged = self._apply_patch_merge(base, patch, ctype)
            if merged is None:
                return True
            md = merged.get("metadata") or {}
            name_eff = md.get("name") or name
            ns_eff = md.get("namespace") or ns
            updated = self.server.store.upsert(
                group,
                version,
                res,
                ns_eff,
                name_eff,
                metadata=_normalize_metadata(md, name_eff, ns_eff, res),
                spec=_spec_payload(res, merged),
                status=merged.get("status") or {},
            )  # type: ignore[attr-defined]
            self._ok(_to_generic(group, version, kind, res)(updated))
            return True
        return False

    def _apply_patch_merge(
        self, base: Dict[str, Any], patch: Dict[str, Any], ctype: str
    ) -> Dict[str, Any] | None:
        if ctype in ("application/merge-patch+json", "application/strategic-merge-patch+json"):
            return _merge_dict(base, patch)
        if ctype in ("application/json", ""):
            return patch
        self._json_status(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            reason="UnsupportedMediaType",
            message="only merge/strategic-merge or json supported",
        )
        return None

    def _handle_custom_resource_get(self, path: str, query: Dict[str, List[str]]) -> bool:
        parsed = _parse_custom_resource_path(path)
        if not parsed:
            return False
        group, version, namespace, plural, name = parsed
        meta = self._lookup_crd(group, version, plural)
        if not meta:
            return False
        namespaced = meta.get("namespaced", True)
        if namespaced:
            store_ns = namespace
        else:
            store_ns = None
            if namespace is not None:
                self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="resource is cluster-scoped; omit namespace")
                return True
        transform = lambda obj: _render_custom_resource(obj, group, version, meta.get("kind", plural))
        if name is None:
            if query.get("watch", ["0"]) [0] in ("1", "true", "True"):
                self._stream_watch(group, version, plural, store_ns, query, transform)
                return True
            if namespaced and namespace is None:
                items = self.server.store.list_all(group, version, plural)  # type: ignore[attr-defined]
            else:
                items = self.server.store.list(group, version, plural, store_ns)  # type: ignore[attr-defined]
            self._ok(
                {
                    "kind": f"{meta.get('kind', plural)}List",
                    "apiVersion": f"{group}/{version}",
                    "items": [transform(i) for i in items],
                }
            )
            return True
        if namespaced and namespace is None:
            self._json_status(
                HTTPStatus.BAD_REQUEST,
                reason="BadRequest",
                message="namespaced resources require /namespaces/<name> in path",
            )
            return True
        obj = self.server.store.get(group, version, plural, store_ns, name)  # type: ignore[attr-defined]
        if not obj:
            self._not_found()
            return True
        self._ok(transform(obj))
        return True

    def _handle_custom_resource_post(self, doc: Dict[str, Any]) -> bool:
        parsed = _parse_custom_resource_path(self.path)
        if not parsed:
            return False
        group, version, namespace, plural, path_name = parsed
        meta = self._lookup_crd(group, version, plural)
        if not meta:
            return False
        namespaced = meta.get("namespaced", True)
        md = doc.get("metadata") or {}
        name_in = md.get("name") or path_name
        ns_in = md.get("namespace") or namespace
        if not name_in or not _valid_name(name_in):
            self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
            return True
        if namespaced:
            if not ns_in or not _valid_name(ns_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid or missing namespace")
                return True
        else:
            ns_in = None
        created = self.server.store.upsert(  # type: ignore[attr-defined]
            group,
            version,
            plural,
            ns_in,
            name_in,
            metadata=_normalize_metadata(md, name_in, ns_in, plural),
            spec=doc.get("spec") or {},
            status=doc.get("status") or {},
        )
        self.send_response(HTTPStatus.CREATED)
        out = _json(_render_custom_resource(created, group, version, meta.get("kind", plural)))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
        return True

    def _handle_custom_resource_put(self, doc: Dict[str, Any]) -> bool:
        parsed = _parse_custom_resource_path(self.path)
        if not parsed:
            return False
        group, version, namespace, plural, path_name = parsed
        meta = self._lookup_crd(group, version, plural)
        if not meta:
            return False
        namespaced = meta.get("namespaced", True)
        md = doc.get("metadata") or {}
        name_in = md.get("name") or path_name
        ns_in = md.get("namespace") or namespace
        if not name_in or not _valid_name(name_in):
            self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
            return True
        if namespaced:
            if not ns_in or not _valid_name(ns_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid or missing namespace")
                return True
        else:
            ns_in = None
        updated = self.server.store.upsert(  # type: ignore[attr-defined]
            group,
            version,
            plural,
            ns_in,
            name_in,
            metadata=_normalize_metadata(md, name_in, ns_in, plural),
            spec=doc.get("spec") or {},
            status=doc.get("status") or {},
        )
        self._ok(_render_custom_resource(updated, group, version, meta.get("kind", plural)))
        return True

    def _handle_custom_resource_patch(self, ctype: str, patch: Dict[str, Any]) -> bool:
        parsed = _parse_custom_resource_path(self.path)
        if not parsed:
            return False
        group, version, namespace, plural, name = parsed
        meta = self._lookup_crd(group, version, plural)
        if not meta:
            return False
        namespaced = meta.get("namespaced", True)
        store_ns = namespace if namespaced else None
        obj = self.server.store.get(group, version, plural, store_ns, name)  # type: ignore[attr-defined]
        if not obj:
            self._not_found()
            return True
        base = _render_custom_resource(obj, group, version, meta.get("kind", plural))
        merged = self._apply_patch_merge(base, patch, ctype)
        if merged is None:
            return True
        md = merged.get("metadata") or {}
        name_eff = md.get("name") or name
        ns_eff = md.get("namespace") or namespace
        if namespaced and not ns_eff:
            self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="missing namespace")
            return True
        if not namespaced:
            ns_eff = None
        updated = self.server.store.upsert(  # type: ignore[attr-defined]
            group,
            version,
            plural,
            ns_eff,
            name_eff,
            metadata=_normalize_metadata(md, name_eff, ns_eff, plural),
            spec=merged.get("spec") or {},
            status=merged.get("status") or {},
        )
        self._ok(_render_custom_resource(updated, group, version, meta.get("kind", plural)))
        return True

    def _handle_custom_resource_delete(self) -> bool:
        parsed = _parse_custom_resource_path(self.path)
        if not parsed:
            return False
        group, version, namespace, plural, name = parsed
        meta = self._lookup_crd(group, version, plural)
        if not meta:
            return False
        namespaced = meta.get("namespaced", True)
        if namespaced and namespace is None:
            self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="namespaced resource delete requires namespace")
            return True
        store_ns = namespace if namespaced else None
        ok = self.server.store.delete(group, version, plural, store_ns, name)  # type: ignore[attr-defined]
        if not ok:
            self._not_found()
            return True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")
        return True

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authz():
            return
        plural, ns, name = _ns_name(self.path)
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"} and name:
            ok = self.server.store.delete("", "v1", plural, None if plural == "namespaces" else ns, name)  # type: ignore[attr-defined]
            if not ok:
                self._not_found()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if self.path.startswith("/apis/apiextensions.k8s.io/v1"):
            crd_plural, crd_name = _gv_cluster_name(
                self.path, "apiextensions.k8s.io", "v1", "customresourcedefinitions"
            )
            if crd_plural == "customresourcedefinitions" and crd_name:
                ok = self.server.store.delete("apiextensions.k8s.io", "v1", "customresourcedefinitions", None, crd_name)  # type: ignore[attr-defined]
                if not ok:
                    self._not_found()
                    return
                self._unregister_crd(crd_name)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
                return
        if self._handle_custom_resource_delete():
            return
        self._not_found()


def _kind(plural: str) -> str:
    return {
        "namespaces": "Namespace",
        "configmaps": "ConfigMap",
        "secrets": "Secret",
        "serviceaccounts": "ServiceAccount",
        "services": "Service",
    }[plural]


def _api_version(group: str, version: str) -> str:
    return f"{group}/{version}" if group else version


def _to_obj(o: K8sObject) -> Dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    return {
        "apiVersion": _api_version(o.group, o.version),
        "kind": _kind(o.resource),
        "metadata": meta,
        **(
            {"data": o.spec}
            if o.resource in {"configmaps", "secrets"}
            else ({} if not o.spec else {"spec": o.spec})
        ),
        **({} if not o.status else {"status": o.status}),
    }


def _to_deployment(o: K8sObject) -> Dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    # attach/ensure generation
    gen_val = meta.get("generation")
    try:
        gen = int(gen_val) if gen_val is not None else 1
    except Exception:
        gen = 1
    meta["generation"] = gen
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": meta,
        "spec": dict(o.spec),
        "status": _synthesize_deploy_status(o.spec, o.status),
    }


def _synthesize_deploy_status(spec: Dict[str, Any], base_status: Dict[str, Any]) -> Dict[str, Any]:
    replicas = int(spec.get("replicas", 1))
    available = replicas
    ready = replicas
    updated = replicas
    status = dict(base_status)
    status.update(
        {
            "replicas": replicas,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
            "conditions": [
                {
                    "type": "Available",
                    "status": "True" if available >= replicas else "False",
                    "reason": "MinimumReplicasAvailable",
                },
                {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"},
            ],
        }
    )
    return status


def _to_scale(o: K8sObject) -> Dict[str, Any]:
    meta = {"name": o.name}
    if o.namespace:
        meta["namespace"] = o.namespace
    replicas = int(o.spec.get("replicas", 1))
    return {
        "apiVersion": "autoscaling/v1",
        "kind": "Scale",
        "metadata": meta,
        "spec": {"replicas": replicas},
        "status": {"replicas": replicas, "selector": ""},
    }


def _to_ingress(o: K8sObject) -> Dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    out = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": meta,
        "spec": dict(o.spec),
    }
    if o.status:
        out["status"] = o.status
    return out


def _list_with_rv(
    items: List[K8sObject],
    transform,
    *,
    kind: str,
    api_version: str,
    limit: Optional[int] = None,
    continue_token: Optional[str] = None,
) -> Dict[str, Any]:
    selected = items
    cont_token: Optional[str] = None
    if continue_token:
        for idx, it in enumerate(items):
            if getattr(it, "name", None) == continue_token:
                selected = items[idx + 1 :]
                break
    if isinstance(limit, int) and limit > 0 and len(items) > limit:
        selected = items[:limit]
        cont_token = items[limit].name if len(items) > limit else None
    rv = max((i.resource_version for i in selected), default=0)
    meta: Dict[str, Any] = {"resourceVersion": str(rv)}
    if cont_token:
        meta["continue"] = cont_token
    return {
        "kind": f"{kind}List",
        "apiVersion": api_version,
        "metadata": meta,
        "items": [transform(i) for i in selected],
    }


def _to_crd(o: K8sObject) -> Dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    body = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": meta,
        "spec": dict(o.spec),
    }
    if o.status:
        body["status"] = o.status
    return body


def _apps_ns_name(path: str) -> Tuple[str, Optional[str], Optional[str]]:
    m = re.match(r"^/apis/apps/v1/namespaces/([^/]+)/deployments(?:/([^/]+))?$", path)
    if m:
        return ("deployments", m.group(1), m.group(2))
    m = re.match(r"^/apis/apps/v1/namespaces/([^/]+)/deployments/([^/]+)/(status|scale)$", path)
    if m:
        return (f"deployments/{m.group(3)}", m.group(1), m.group(2))
    m = re.match(r"^/apis/apps/v1/deployments(?:/([^/]+))?$", path)
    if m:
        return ("deployments", None, m.group(1))
    return ("", None, None)


def _net_ns_name(path: str) -> Tuple[str, Optional[str], Optional[str]]:
    m = re.match(r"^/apis/networking.k8s.io/v1/namespaces/([^/]+)/ingresses(?:/([^/]+))?$", path)
    if m:
        return ("ingresses", m.group(1), m.group(2))
    m = re.match(r"^/apis/networking.k8s.io/v1/ingresses(?:/([^/]+))?$", path)
    if m:
        return ("ingresses", None, m.group(1))
    return ("", None, None)


def _gv_ns_name(path: str, group: str, version: str, plural: str) -> Tuple[str, Optional[str], Optional[str]]:
    pattern = rf"^/apis/{re.escape(group)}/{re.escape(version)}/namespaces/([^/]+)/{re.escape(plural)}(?:/([^/]+))?$"
    m = re.match(pattern, path)
    if m:
        return (plural, m.group(1), m.group(2))
    pattern_all = rf"^/apis/{re.escape(group)}/{re.escape(version)}/{re.escape(plural)}(?:/([^/]+))?$"
    m = re.match(pattern_all, path)
    if m:
        return (plural, None, m.group(1))
    return ("", None, None)


def _gv_cluster_name(path: str, group: str, version: str, plural: str) -> Tuple[str, Optional[str]]:
    pattern = rf"^/apis/{re.escape(group)}/{re.escape(version)}/{re.escape(plural)}(?:/([^/]+))?$"
    m = re.match(pattern, path)
    if m:
        return (plural, m.group(1))
    return ("", None)


def _to_generic(group: str, version: str, kind: str, resource: str):
    def convert(o: K8sObject) -> Dict[str, Any]:
        meta = dict(o.metadata)
        meta.setdefault("name", o.name)
        if o.namespace:
            meta.setdefault("namespace", o.namespace)
        meta.setdefault("resourceVersion", str(o.resource_version))
        body: Dict[str, Any] = {
            "apiVersion": _api_version(group, version),
            "kind": kind,
            "metadata": meta,
        }
        data = dict(o.spec)
        if resource in {"roles", "clusterroles"}:
            body["rules"] = data.get("rules", [])
        elif resource in {"rolebindings", "clusterrolebindings"}:
            if data.get("roleRef"):
                body["roleRef"] = data["roleRef"]
            if data.get("subjects") is not None:
                body["subjects"] = data.get("subjects", [])
        elif resource == "poddisruptionbudgets":
            body["spec"] = data.get("spec", data)
            if o.status:
                body["status"] = o.status
        elif resource == "horizontalpodautoscalers":
            body["spec"] = data.get("spec", data)
            if o.status:
                body["status"] = o.status
        else:
            if data:
                body["spec"] = data
            if o.status:
                body["status"] = o.status
        return body

    return convert


def _spec_payload(resource: str, merged: Dict[str, Any]) -> Dict[str, Any]:
    if resource in {"roles", "clusterroles"}:
        return {"rules": merged.get("rules", [])}
    if resource in {"rolebindings", "clusterrolebindings"}:
        return {
            "subjects": merged.get("subjects", []),
            "roleRef": merged.get("roleRef"),
        }
    if resource == "poddisruptionbudgets":
        return {"spec": merged.get("spec", merged.get("body", {}))}
    if resource == "horizontalpodautoscalers":
        return {"spec": merged.get("spec", merged.get("body", {}))}
    return merged.get("spec") or {}


def _render_custom_resource(o: K8sObject, group: str, version: str, kind: str) -> Dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    body: Dict[str, Any] = {
        "apiVersion": f"{group}/{version}",
        "kind": kind,
        "metadata": meta,
    }
    if o.spec:
        body["spec"] = o.spec
    if o.status:
        body["status"] = o.status
    return body


def _parse_custom_resource_path(path: str) -> Optional[Tuple[str, str, Optional[str], str, Optional[str]]]:
    m = re.match(
        r"^/apis/([^/]+)/([^/]+)(?:/namespaces/([^/]+))?/([^/]+)(?:/([^/]+))?$",
        path,
    )
    if not m:
        return None
    group, version, namespace, plural, name = m.groups()
    if group in RESERVED_GROUPS and plural in {
        "deployments",
        "deployments/status",
        "deployments/scale",
        "ingresses",
        "customresourcedefinitions",
    }:
        return None
    return (group, version, namespace, plural, name)


def _normalize_metadata(md: Dict[str, Any], name: str, ns: Optional[str], plural: str) -> Dict[str, Any]:
    out = dict(md)
    out["name"] = name
    if ns and plural != "namespaces":
        out["namespace"] = ns
    return out


def _merge_dict(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)  # type: ignore[arg-type]
        else:
            out[k] = v
    return out


_DNS1123_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")


def _valid_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    if not name or len(name) > 253:
        return False
    return _DNS1123_RE.match(name) is not None


def _service_target(svc: K8sObject) -> Optional[str]:
    spec = svc.spec or {}
    selector = spec.get("selector") or {}
    if not selector:
        selector = (spec.get("selector") or {}).get("matchLabels") or {}
    return (
        selector.get("app")
        or selector.get("app.kubernetes.io/name")
        or svc.metadata.get("labels", {}).get("app")
        or svc.metadata.get("annotations", {}).get("apishim.k1s.dev/app")
        or svc.metadata.get("name")
    )


def _endpoints_for_service(state: SQLiteStateStore, svc: K8sObject) -> Optional[Dict[str, Any]]:
    target = _service_target(svc)
    if not target:
        return None
    app_name = f"{svc.namespace}--{target}" if svc.namespace else target
    endpoints = state.list_service_endpoints(app_name)
    ports_spec = []
    for p in svc.spec.get("ports", []):
        ports_spec.append(
            {
                "name": p.get("name"),
                "port": p.get("port"),
                "protocol": p.get("protocol", "TCP"),
            }
        )
    ready_addrs = []
    not_ready = []
    for ep in endpoints:
        entry = {"ip": ep.ip}
        if ep.ready:
            ready_addrs.append(entry)
        else:
            not_ready.append(entry)
    meta = {
        "name": svc.name,
        "namespace": svc.namespace,
        "resourceVersion": str(svc.resource_version),
    }
    body = {
        "apiVersion": "v1",
        "kind": "Endpoints",
        "metadata": meta,
        "subsets": [
            {
                "addresses": ready_addrs or [],
                "notReadyAddresses": not_ready or [],
                "ports": ports_spec,
            }
        ],
    }
    return body


def _node_obj(record, status, rv: int) -> Dict[str, Any]:
    meta = {
        "name": record.name or record.node_id,
        "resourceVersion": str(rv),
        "labels": record.labels or {},
    }
    conditions: List[Dict[str, Any]] = []
    if status:
        conditions.append(
            {
                "type": "Ready",
                "status": "True" if status.status == "ready" else "False",
                "lastHeartbeatTime": status.seen_at.isoformat(),
                "reason": "AgentHeartbeat",
            }
        )
    else:
        conditions.append({"type": "Ready", "status": "Unknown"})
    node_status = {"conditions": conditions}
    return {"apiVersion": "v1", "kind": "Node", "metadata": meta, "status": node_status}


def _runtime_from_env() -> RuntimeAdapter:
    backend = (os.getenv("AE_APISHIM_RUNTIME") or os.getenv("AE_RUNTIME_BACKEND") or "stub").lower()
    if backend in {"stub", "test"}:
        return StubRuntime()
    if backend in {"podman", "oci"}:
        try:
            return PodmanRuntime()
        except Exception:
            return DockerRuntime()
    if backend == "remote":
        return RemoteRuntime()
    return DockerRuntime()


def _pod_obj(container: dict, rv: int, node_name: Optional[str]) -> Dict[str, Any]:
    labels = container.get("labels", {}) or {}
    app = labels.get("ae.app") or "app"
    replica_id = labels.get("ae.replica_id") or container.get("name") or "replica"
    ns = "default"
    meta = {
        "name": replica_id,
        "namespace": ns,
        "labels": labels,
        "resourceVersion": str(rv),
    }
    status = {
        "phase": "Running",
        "podIP": container.get("pod_ip"),
        "hostIP": container.get("host_ip"),
        "containerStatuses": [
            {
                "name": labels.get("ae.container", "main"),
                "ready": bool(container.get("running", False)),
                "restartCount": int(container.get("restart_count", 0) or 0),
                "state": {
                    "running": {"startedAt": container.get("started_at")}
                    if container.get("running")
                    else {"terminated": {"exitCode": 1}}
                },
            }
        ],
    }
    if node_name:
        meta["nodeName"] = node_name
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": meta,
        "spec": {"nodeName": node_name},
        "status": status,
    }


class ShimServer(HTTPServer):
    def __init__(self, server_address: Tuple[str, int], token: Optional[str]) -> None:
        super().__init__(server_address, ShimHandler)
        self.store = ObjectStore()
        ShimHandler.admin_token = token or os.getenv("AE_APISHIM_TOKEN")
        ShimHandler.read_token = os.getenv("AE_APISHIM_READ_TOKEN")
        db_path = Path(os.getenv("AE_STATE_DB", "state/controller.db"))
        self.state = SQLiteStateStore(db_path)
        ShimHandler.state = self.state  # type: ignore[assignment]
        self.runtime = _runtime_from_env()
        self._bootstrap_crds()
        # Start adapter worker to reconcile apps/v1 Deployments into k1s
        try:
            self._adapter = build_adapter(self.store, runtime=self.runtime)
            self._adapter.start()
        except Exception:
            self._adapter = None

    def _bootstrap_crds(self) -> None:
        try:
            objs = self.store.list_all("apiextensions.k8s.io", "v1", "customresourcedefinitions")
        except Exception:
            objs = []
        for obj in objs:
            ShimHandler._register_crd(obj)


def run_server(host: str = "127.0.0.1", port: int = 8445, token: Optional[str] = None, tls: bool = False) -> None:
    if os.getenv("AE_APISHIM_ENABLE") != "1":
        raise RuntimeError("apishim disabled: set AE_APISHIM_ENABLE=1 to start the shim server")
    tok = token or os.getenv("AE_APISHIM_TOKEN")
    if not tok:
        raise RuntimeError("AE_APISHIM_TOKEN must be set (or --token) to start the shim server")
    httpd = ShimServer((host, port), tok)
    if tls:
        # Dev TLS: requires user-provided cert/key via env or skip.
        cert_file = os.getenv("AE_APISHIM_TLS_CERT")
        key_file = os.getenv("AE_APISHIM_TLS_KEY")
        if not (cert_file and key_file):
            raise RuntimeError("TLS requested but AE_APISHIM_TLS_CERT/KEY not set")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        client_ca = os.getenv("AE_APISHIM_TLS_CLIENT_CA")
        if client_ca:
            ctx.load_verify_locations(cafile=client_ca)
            ctx.verify_mode = ssl.CERT_REQUIRED
            ShimHandler.client_cert_required = True
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
