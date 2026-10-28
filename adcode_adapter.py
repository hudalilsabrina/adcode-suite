#!/usr/bin/env python3
"""OpenAI-compatible adapter for ADCode included AI.

Fronts the account pool in accounts.jsonl on 127.0.0.1:20130 so 9router (or
anything else) can treat it as one provider.

    GET  /v1/models
    POST /v1/chat/completions      (stream + non-stream)
    GET  /health                   pool state

Behaviour:
  * Firebase idTokens live 1 h -> refreshed lazily and cached per uid.
  * Accounts rotate least-recently-used; a failed account is skipped and the
    request retried on the next one (up to 3 tries).
  * `403 verify-account` marks the account dead, `429`/quota marks cooldown,
    so a bad account never wedges the pool.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import adcode_client as C  # noqa: E402

ROOT = Path(__file__).resolve().parent
ACCOUNTS = ROOT / "accounts.jsonl"
PORT = int(os.environ.get("ADCODE_PORT", "20130"))
TOKEN_TTL = 3000  # refresh a bit before the 1 h Firebase expiry


class Account:
    __slots__ = ("uid", "email", "refresh_token", "id_token", "expires_at",
                 "state", "used_at", "errors", "cooldown_until")

    def __init__(self, rec: dict) -> None:
        self.uid = rec.get("uid", "")
        self.email = rec.get("email", "")
        self.refresh_token = rec.get("refresh_token", "")
        self.id_token = ""
        self.expires_at = 0.0
        self.state = "ready"          # ready | dead | cooldown
        self.used_at = 0.0
        self.errors = 0
        self.cooldown_until = 0.0

    def token(self) -> str:
        if self.id_token and time.time() < self.expires_at:
            return self.id_token
        fresh = C.refresh(self.refresh_token)
        self.id_token = fresh["id_token"]
        self.refresh_token = fresh["refresh_token"]
        self.expires_at = time.time() + TOKEN_TTL
        return self.id_token

    def alive(self) -> bool:
        if self.state == "dead":
            return False
        if self.state == "cooldown" and time.time() >= self.cooldown_until:
            self.state = "ready"
            self.errors = 0
        return self.state == "ready"


class Pool:
    def __init__(self, path: Path) -> None:
        self.lock = threading.Lock()
        self.path = path
        self.mtime = 0.0
        self.accounts: List[Account] = []
        self.reload(path)

    def maybe_reload(self) -> None:
        """Pick up accounts appended by the farm without a restart."""
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if m != self.mtime:
            self.mtime = m
            self.reload(self.path)

    def reload(self, path: Path) -> None:
        seen = {a.uid for a in self.accounts}
        try:
            self.mtime = path.stat().st_mtime
        except OSError:
            pass
        if not path.exists():
            return
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("uid") in seen:
                continue
            seen.add(rec.get("uid"))
            self.accounts.append(Account(rec))

    def pick(self, exclude: set) -> Optional[Account]:
        self.maybe_reload()
        with self.lock:
            cands = [a for a in self.accounts if a.uid not in exclude and a.alive()]
            if not cands:
                return None
            # akun bersih dulu, baru yang pernah gagal, lalu LRU
            cands.sort(key=lambda a: (a.errors, a.used_at))
            a = cands[0]
            a.used_at = time.time()
            return a

    def stats(self) -> Dict[str, Any]:
        self.maybe_reload()
        with self.lock:
            return {
                "total": len(self.accounts),
                "ready": sum(1 for a in self.accounts if a.alive()),
                "dead": sum(1 for a in self.accounts if a.state == "dead"),
                "cooldown": sum(1 for a in self.accounts if a.state == "cooldown"),
            }


POOL = Pool(ACCOUNTS)


TRANSIENT_HINTS = ("urlerror", "timed out", "timeout", "502", "503", "504",
                   "connection", "tunnel", "network", "temporarily")


def _transient(exc: BaseException) -> bool:
    """Error jaringan = akun masih sehat, cuma jalurnya yang bermasalah."""
    msg = f"{type(exc).__name__} {exc}".lower()
    return any(h in msg for h in TRANSIENT_HINTS)


def _classify(acc: Account, status: int, body: str) -> None:
    low = body.lower()
    # provider-side throttle: the account is fine, the model is busy
    if "upstream-rate-limit" in low or '"retryable":true' in low:
        return
    if status == 403 and "verify-account" in low:
        acc.state = "dead"
        acc.errors += 1
    elif status in (401, 403):
        # stale token: force a refresh next time, don't kill the account
        acc.id_token = ""
        acc.expires_at = 0.0
        acc.errors += 1
        if acc.errors >= 3:
            acc.state = "dead"
    elif status == 429 or "rate" in low or "quota" in low or "limit" in low:
        acc.state = "cooldown"
        acc.cooldown_until = time.time() + 120
        acc.errors += 1
    else:
        acc.errors += 1


def upstream_chat(token: str, payload: dict, stream: bool) -> tuple[int, bytes, str]:
    body = dict(payload)
    body["stream"] = True  # upstream always streams; we fold if the client didn't ask
    req = urllib.request.Request(
        f"{C.TAGFLOW}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "user-agent": C.UA,
            "authorization": f"Bearer {token}",
            "accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
            return r.status, raw, r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("content-type", "")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}".encode(), ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quieter access log
        pass

    def _json(self, code: int, obj: Any) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/health", "/"):
            return self._json(200, {"ok": True, "pool": POOL.stats()})
        if self.path.rstrip("/") == "/v1/models":
            models = [
                {"id": m, "object": "model", "owned_by": "adcode"}
                for m in C.MODELS
            ]
            return self._json(200, {"object": "list", "data": models})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self._json(404, {"error": "not found"})

        length = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            return self._json(400, {"error": "bad json"})

        want_stream = bool(payload.get("stream"))
        model = payload.get("model") or C.DEFAULT_MODEL
        if model not in C.MODELS:
            model = C.DEFAULT_MODEL
        payload["model"] = model

        tried: set = set()
        last = (500, b'{"error":"no account"}', "")
        last_reason = ""
        for _ in range(5):
            acc = POOL.pick(tried)
            if acc is None:
                break
            tried.add(acc.uid)
            try:
                token = acc.token()
            except Exception as exc:  # noqa: BLE001
                # Refresh token gagal karena jaringan ≠ akun mati. Tandai
                # cooldown sebentar supaya tidak menghabiskan pool saat
                # koneksi ke identitytoolkit sedang goyang.
                if _transient(exc):
                    acc.state = "cooldown"
                    acc.cooldown_until = time.time() + 90
                    last_reason = f"refresh jaringan: {type(exc).__name__}"
                else:
                    acc.state = "dead"
                    acc.errors += 1
                    last_reason = f"refresh ditolak: {str(exc)[:60]}"
                continue

            status, raw, ctype = upstream_chat(token, payload, want_stream)
            if status == 200:
                if want_stream:
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("cache-control", "no-cache")
                    self.send_header("connection", "close")
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                folded = C._fold_sse(raw.decode(errors="replace"))  # noqa: SLF001
                data = folded.encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            _classify(acc, status, raw.decode(errors="replace"))
            last = (status, raw, ctype)

        status, raw, _ = last
        if raw == b'{"error":"no account"}':
            # tidak ada satu pun akun yang bisa dipakai — bedakan sebabnya
            st = POOL.stats()
            raw = json.dumps({
                "error": "no usable account",
                "reason": last_reason or "semua akun cooldown/dead",
                "pool": st,
            }).encode()
            status = 503
        self.send_response(status or 502)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"adcode-adapter on 127.0.0.1:{PORT} pool={POOL.stats()}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
