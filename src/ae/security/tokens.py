"""
Join-token utilities for agent bootstrap.

Tokens are HMAC-SHA256 over (node_id, expiry, random nonce) using a shared secret.
Format: base64url(node_id:exp_ts:nonce:signature)
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
from datetime import datetime, timezone
from hashlib import sha256

DEFAULT_SECRET = os.getenv("AE_AGENT_JOIN_SECRET", "")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _db64(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def issue_token(node_id: str, expires_at: datetime, *, secret: str | None = None) -> str:
    secret = secret or DEFAULT_SECRET
    nonce = secrets.token_bytes(8)
    exp = int(expires_at.replace(tzinfo=timezone.utc).timestamp())
    body = f"{node_id}:{exp}:{_b64(nonce)}".encode()
    sig = hmac.new(secret.encode(), body, sha256).digest()
    return _b64(body + b":" + sig)


def verify_token(token: str, *, secret: str | None = None) -> tuple[str, datetime]:
    secret = secret or DEFAULT_SECRET
    raw = _db64(token)
    parts = raw.split(b":")
    if len(parts) != 4:
        raise ValueError("invalid token format")
    node_id = parts[0].decode()
    exp = int(parts[1].decode())
    nonce = parts[2]
    sig = parts[3]
    body = b":".join([parts[0], parts[1], parts[2]])
    want = hmac.new(secret.encode(), body, sha256).digest()
    if not hmac.compare_digest(sig, want):
        raise ValueError("bad signature")
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise ValueError("token expired")
    return node_id, expires_at
