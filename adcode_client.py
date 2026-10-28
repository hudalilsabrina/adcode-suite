#!/usr/bin/env python3
"""ADCode "Included AI" client.

Reversed from the ADCode desktop build (`@adcode/desktop` 2.4.0, Electron).

What the app does (out/main/index.js):

    createBackendTokens -> createFirebaseAuth -> signUp()
        POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=<FIREBASE_KEY>
        body: {"returnSecureToken": true}            <- anonymous, no credentials

    buildTagflowProvider:
        baseUrl = `${apiBaseUrl()}/ai/tagflow`       <- apiBaseUrl = https://adcode.bluethenics.com/v1
        apiKey  = await token()                      <- Firebase idToken
        GET  /ai/tagflow/config                      <- model catalogue, no auth
        POST /ai/tagflow/chat/completions            <- OpenAI-compatible, always streams
        GET  /ai/tagflow/usage                       <- per-uid daily limits

Anonymous signups are rejected by the chat route with
`403 {"code":"verify-account"}`; the route also sends the Firebase
verification mail. Confirming that mail (accounts:update with the oobCode)
flips the account to verified and chat then returns 200.

So the farmable unit is: disposable inbox -> Firebase email/password signup
-> click the verification oobCode -> token -> $1/day of included AI.
"""

from __future__ import annotations

import json
import os

def _load_dotenv(path=".env"):
    """Baca .env (KEY=VALUE) tanpa dependensi eksternal."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, path), os.path.join(os.path.dirname(here), path)):
        try:
            with open(cand) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return
        except FileNotFoundError:
            continue


_load_dotenv()
import random
import string
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# Public Firebase Web API key baked into the ADCode desktop bundle.
FIREBASE_KEY = "AIzaSyATZzX5Fw2HjR34VxB3UQWTmljprA5uXqE"

API_BASE = "https://adcode.bluethenics.com/v1"
TAGFLOW = f"{API_BASE}/ai/tagflow"

IDENTITY = "https://identitytoolkit.googleapis.com/v1"
SECURE_TOKEN = "https://securetoken.googleapis.com/v1/token"

UA = "ADCode/2.4.0"

DEFAULT_MODEL = "tagflow-code-27b"
MODELS = [
    "tagflow-code-27b",
    "heabsy-qwen38",
    "heabsy-glm53-flash-unc",
    "heabsy-mimo-v26-flash",
]


class AdcodeError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


PROXY = os.environ.get("ADCODE_PROXY", "")


def set_proxy(url: str) -> None:
    global PROXY
    PROXY = url or ""


def rotate_proxy() -> str:
    """Ask the local bridge for a fresh exit IP (no-op when no bridge is set)."""
    if not PROXY:
        return ""
    base = PROXY.rstrip("/")
    for path in ("/__new_session", "/rotate"):
        try:
            req = urllib.request.Request(base + path, data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=40) as r:
                if r.status == 200:
                    return PROXY
        except Exception:  # noqa: BLE001
            continue
    return PROXY


def _opener() -> urllib.request.OpenerDirector:
    if PROXY:
        handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def _request(
    url: str,
    body: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
    method: Optional[str] = None,
    timeout: int = 60,
) -> tuple[int, str]:
    headers = {"content-type": "application/json", "user-agent": UA}
    if token:
        headers["authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data else "GET"))
    try:
        with _opener().open(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        raise AdcodeError(f"{type(e).__name__}: {e}") from e


def rand_email_local() -> str:
    return "adc" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


def rand_password() -> str:
    return "Adc!" + "".join(random.choices(string.ascii_letters + string.digits, k=14))



def _err_msg(body: str) -> str:
    """Firebase errors are pretty-printed; keep one compact line."""
    try:
        return json.loads(body)["error"]["message"]
    except Exception:  # noqa: BLE001
        return " ".join(body.split())[:120]


# ---------------------------------------------------------------- Firebase

def signup(email: str, password: str) -> Dict[str, Any]:
    """Create a Firebase email/password account."""
    st, body = _request(
        f"{IDENTITY}/accounts:signUp?key={FIREBASE_KEY}",
        {"email": email, "password": password, "returnSecureToken": True},
    )
    if st != 200:
        raise AdcodeError(f"signup failed: {_err_msg(body)}", st, "signup")
    d = json.loads(body)
    return {
        "uid": d["localId"],
        "email": email,
        "password": password,
        "id_token": d["idToken"],
        "refresh_token": d["refreshToken"],
    }


def send_verification(id_token: str) -> None:
    st, body = _request(
        f"{IDENTITY}/accounts:sendOobCode?key={FIREBASE_KEY}",
        {"requestType": "VERIFY_EMAIL", "idToken": id_token},
    )
    if st != 200:
        raise AdcodeError(f"sendOobCode failed: {_err_msg(body)}", st, "oob")


def verify_email(oob_code: str) -> None:
    st, body = _request(
        f"{IDENTITY}/accounts:update?key={FIREBASE_KEY}",
        {"oobCode": oob_code},
    )
    if st != 200:
        raise AdcodeError(f"verify failed: {_err_msg(body)}", st, "verify")


def refresh(refresh_token: str) -> Dict[str, str]:
    st, body = _request(
        f"{SECURE_TOKEN}?key={FIREBASE_KEY}",
        {"grantType": "refresh_token", "refreshToken": refresh_token},
    )
    if st != 200:
        raise AdcodeError(f"refresh failed: {body[:160]}", st, "refresh")
    d = json.loads(body)
    return {"id_token": d["id_token"], "refresh_token": d["refresh_token"]}


# ---------------------------------------------------------------- Included AI

def usage(id_token: str) -> Dict[str, Any]:
    st, body = _request(f"{TAGFLOW}/usage", token=id_token)
    if st != 200:
        raise AdcodeError(f"usage failed: {body[:160]}", st, "usage")
    return json.loads(body)


def catalogue() -> Dict[str, Any]:
    st, body = _request(f"{TAGFLOW}/config")
    if st != 200:
        raise AdcodeError(f"config failed: {body[:160]}", st, "config")
    return json.loads(body)


def chat(
    id_token: str,
    messages: List[Dict[str, Any]],
    model: str = DEFAULT_MODEL,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> str:
    """One non-streaming completion. The upstream always answers with SSE, so
    the chunks are folded back into a single JSON body here."""
    payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature

    st, body = _request(f"{TAGFLOW}/chat/completions", payload, token=id_token, timeout=180)
    if st != 200:
        raise AdcodeError(body[:300], st, _err_code(body))
    return _fold_sse(body)


def _err_code(body: str) -> str:
    try:
        return json.loads(body).get("error", {}).get("code", "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _fold_sse(raw: str) -> str:
    """Collapse an SSE stream into a single OpenAI-shaped completion.

    Tool calls arrive as incremental deltas (index -> partial function
    arguments), so they have to be accumulated per index and reassembled —
    dropping them makes every agent request look like an empty answer.
    """
    text_parts: List[str] = []
    model = ""
    finish = "stop"
    usage_obj: Dict[str, Any] = {}
    calls: Dict[int, Dict[str, Any]] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            d = json.loads(chunk)
        except Exception:  # noqa: BLE001
            continue
        model = d.get("model", model) or model
        if d.get("usage"):
            usage_obj = d["usage"]
        for ch in d.get("choices", []):
            delta = ch.get("delta") or {}
            piece = delta.get("content")
            if piece:
                text_parts.append(piece)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = calls.setdefault(
                    idx, {"id": "", "type": "function",
                          "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if calls:
        ordered = [calls[i] for i in sorted(calls)]
        for n, c in enumerate(ordered):
            if not c["id"]:
                c["id"] = f"call_{n}"
        message["tool_calls"] = ordered
        if not message["content"]:
            message["content"] = None
    out = {
        "object": "chat.completion",
        "model": model or DEFAULT_MODEL,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }
        ],
    }
    if usage_obj:
        out["usage"] = usage_obj
    return json.dumps(out)


# ---------------------------------------------------------------- mail.tm

class MailTm:
    """Disposable inbox used to receive the Firebase verification mail."""

    BASE = "https://api.mail.tm"

    def __init__(self) -> None:
        self.address = ""
        self.password = ""
        self.token = ""

    def _req(self, path: str, body=None, token=None, method=None):
        headers = {"content-type": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.BASE}{path}", data=data, headers=headers,
            method=method or ("POST" if data else "GET"),
        )
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode())

    def create(self) -> str:
        doms = self._req("/domains")["hydra:member"]
        domain = doms[0]["domain"]
        local = rand_email_local()
        self.address = f"{local}@{domain}"
        self.password = rand_password()
        self._req("/accounts", {"address": self.address, "password": self.password})
        self.token = self._req("/token", {"address": self.address, "password": self.password})["token"]
        return self.address

    def messages(self) -> List[Dict[str, Any]]:
        return self._req("/messages", token=self.token)["hydra:member"]

    def body(self, msg_id: str) -> str:
        d = self._req(f"/messages/{msg_id}", token=self.token)
        return json.dumps(d)

    def wait_for_oob(self, timeout: int = 120, interval: int = 4) -> str:
        """Return the Firebase oobCode from the verification mail."""
        import re

        deadline = time.time() + timeout
        seen = set()
        while time.time() < deadline:
            try:
                for m in self.messages():
                    mid = m.get("id")
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    blob = self.body(mid)
                    hit = re.search(r"oobCode(?:=|\\u003d|%3D)([A-Za-z0-9_-]{20,})", blob)
                    if hit:
                        return hit.group(1)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval)
        return ""



# ---------------------------------------------------------------- tempik

class TempikMail:
    """Own mail server (Tempik-compatible). Instant inboxes, no rate limit."""

    BASE = os.environ.get("TEMPIK_BASE", "https://mail.example.com/api").rstrip("/")

    def __init__(self) -> None:
        self.address = ""
        self.password = rand_password()
        self.sid = ""

    def _req(self, path: str, body=None, method=None):
        headers = {"user-agent": UA, "accept": "application/json"}
        if self.sid:
            headers["x-session-id"] = self.sid
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["content-type"] = "application/json"
        req = urllib.request.Request(
            f"{self.BASE}{path}", data=data, headers=headers,
            method=method or ("POST" if data else "GET"),
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw.strip() else None

    def create(self) -> str:
        if not self.sid:
            self.sid = self._req("/session")["sessionId"]
        box = self._req("/inboxes", {})
        self.address = box["address"]
        return self.address

    def wait_for_oob(self, timeout: int = 120, interval: int = 3) -> str:
        import re

        deadline = time.time() + timeout
        seen = set()
        while time.time() < deadline:
            try:
                msgs = self._req(f"/inboxes/{self.address}/messages") or []
                for m in msgs:
                    mid = m.get("id") or m.get("message_id")
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    blob = json.dumps(self._req(f"/inboxes/{self.address}/messages/{mid}"))
                    hit = re.search(r"oobCode(?:=|\\u003d|%3D)([A-Za-z0-9_-]{20,})", blob)
                    if hit:
                        return hit.group(1)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval)
        return ""


# ---------------------------------------------------------------- one account

def register_one(proxy: Optional[str] = None, mail: str = "tempik") -> Dict[str, Any]:
    """Create + verify one ADCode account and prove it can talk to included AI."""
    box = TempikMail() if mail == "tempik" else MailTm()
    email = box.create()
    pw = box.password

    acct = signup(email, pw)
    send_verification(acct["id_token"])

    oob = box.wait_for_oob(timeout=150)
    if not oob:
        return {"email": email, "status": "no_mail"}

    verify_email(oob)
    tok = refresh(acct["refresh_token"])
    acct["id_token"] = tok["id_token"]
    acct["refresh_token"] = tok["refresh_token"]

    try:
        u = usage(acct["id_token"])
    except AdcodeError as e:
        return {"email": email, "status": f"usage_fail:{e.status}"}

    # A 500 here is usually a transient upstream hiccup, not a bad account:
    # usage() already proved the email is verified, so retry then accept.
    ok = False
    last_err = ""
    for attempt in range(3):
        try:
            out = chat(acct["id_token"], [{"role": "user", "content": "reply with the word OK"}], max_tokens=16)
            ok = "OK" in json.loads(out)["choices"][0]["message"]["content"].upper()
            last_err = ""
            break
        except AdcodeError as e:
            last_err = f"{e.status}:{e.code}"
            if e.status in (401, 403):
                return {"email": email, "status": f"chat_fail:{e.status}:{e.code}"}
            time.sleep(3)

    if last_err and not ok:
        acct["status"] = "active"
        acct["note"] = f"chat untested ({last_err})"
        acct["limits"] = u.get("limits")
        return acct

    acct["status"] = "active" if ok else "chat_weird"
    acct["limits"] = u.get("limits")
    return acct


if __name__ == "__main__":
    print("catalogue:", json.dumps(catalogue())[:200])
