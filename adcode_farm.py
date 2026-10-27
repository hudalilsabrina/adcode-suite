#!/usr/bin/env python3
"""Farm ADCode accounts.

One unit = one disposable inbox + one verified Firebase account =
$1.00/day of included AI (2M tokens, 1M requests, 1000 RPM).

    python3 adcode_farm.py -n 60 --mail tempik -w 6

Results append to accounts.jsonl; history to farm.log.
"""

from __future__ import annotations

def load_dotenv_file(path: str = ".env") -> None:
    """Baca .env sederhana (KEY=VALUE) tanpa dependensi eksternal."""
    import os as _os
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                _os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


load_dotenv_file()


def preflight() -> None:
    """Gagal cepat kalau konfigurasi wajib belum diisi.

    Tanpa ini, TEMPIK_BASE yang kosong jatuh ke placeholder mail.example.com
    dan errornya jadi "No address associated with hostname" — kelihatan seperti
    bug jaringan padahal cuma .env belum dibuat.
    """
    import os as _os
    base = _os.environ.get("TEMPIK_BASE", "")
    if not base or "example.com" in base:
        raise SystemExit(
            "TEMPIK_BASE belum diset.\n"
            "  cp .env.example .env && chmod 600 .env\n"
            "  lalu isi TEMPIK_BASE dengan endpoint temp mail kamu."
        )
    try:
        import urllib.error as _ue
        import urllib.request as _u
        req = _u.Request(base.rstrip("/") + "/session",
                         headers={"user-agent": "Mozilla/5.0"})
        with _u.urlopen(req, timeout=15) as r:
            r.read(1)
    except _ue.HTTPError:
        pass  # server menjawab (403/404 dsb) — host hidup, itu yang dicek
    except Exception as e:
        raise SystemExit(f"TEMPIK_BASE tidak bisa dihubungi ({base}): {type(e).__name__}: {e}")


preflight()



import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adcode_client import AdcodeError, register_one, rotate_proxy  # noqa: E402

ROOT = Path(__file__).resolve().parent
ACCOUNTS = ROOT / "accounts.jsonl"
LOG = ROOT / "farm.log"

KEEP = ("uid", "email", "password", "refresh_token", "status", "limits")

_lock = threading.Lock()
_ok = 0


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with _lock:
        print(line, flush=True)
        with LOG.open("a") as f:
            f.write(line + "\n")


def save(rec: dict) -> None:
    with _lock:
        with ACCOUNTS.open("a") as f:
            f.write(json.dumps({k: rec[k] for k in KEEP if k in rec}) + "\n")


def one(idx: int, mail: str, retry: int) -> bool:
    global _ok
    for attempt in range(1, retry + 2):
        rotate_proxy()
        try:
            rec = register_one(mail=mail)
        except AdcodeError as e:
            msg = str(e)
            log(f"[{idx}] attempt {attempt} api {e.status} {e.code}: {msg[:80]}")
            # Firebase caps verification mails per project/IP; retrying fast
            # just burns the budget, so back off hard on quota errors.
            if "QUOTA_EXCEEDED" in msg or "TOO_MANY_ATTEMPTS" in msg:
                time.sleep(20 * attempt)
            else:
                time.sleep(2)
            continue
        except Exception as e:  # noqa: BLE001
            log(f"[{idx}] attempt {attempt} crash {type(e).__name__}: {str(e)[:80]}")
            time.sleep(2)
            continue

        if rec.get("status") == "active":
            save(rec)
            with _lock:
                _ok += 1
            log(f"[{idx}] OK {rec['email']}")
            return True
        log(f"[{idx}] attempt {attempt} {rec.get('status')} {rec.get('email','')}")
        time.sleep(1)
    log(f"[{idx}] GAVE UP")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--count", type=int, default=1)
    ap.add_argument("-w", "--workers", type=int, default=4)
    ap.add_argument("--mail", choices=["tempik", "mailtm"], default="tempik")
    ap.add_argument("--gap", type=float, default=0.5, help="delay between submissions")
    ap.add_argument("--retry", type=int, default=2)
    a = ap.parse_args()

    started = time.time()
    log(f"START n={a.count} workers={a.workers} mail={a.mail}")

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futures = []
        for i in range(1, a.count + 1):
            futures.append(ex.submit(one, i, a.mail, a.retry))
            time.sleep(a.gap)
        for f in futures:
            try:
                f.result()
            except Exception:  # noqa: BLE001
                pass

    dur = (time.time() - started) / 60
    log(f"DONE ok={_ok}/{a.count} in {dur:.1f} min")
    return 0 if _ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
