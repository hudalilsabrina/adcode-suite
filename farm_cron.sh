#!/usr/bin/env bash
# Farm ADCode: jalur langsung dulu, proxy hanya kalau IP kena rate-limit.
#
# Kenapa begini: bridge residential (dennak) sekarang sering balas 502/timeout,
# jadi memaksakan proxy bikin seluruh batch gagal padahal Firebase sehat.
# Sebaliknya, jalan langsung aman sampai ~120 signup per IP — Firebase baru
# balas TOO_MANY_ATTEMPTS_TRY_LATER setelah itu. Jadi: direct, dan hanya pindah
# ke proxy kalau memang kena batas itu.

set -uo pipefail
cd "$(dirname "$0")"

N="${1:-25}"
BRIDGE="http://127.0.0.1:10801"
LOG="farm_cron.log"

run() {  # $1 = proxy ("" = direct), $2 = jumlah akun
  ADCODE_PROXY="$1" python3 adcode_farm.py -n "$2" --mail tempik -w 2 --gap 1 --retry 3 2>&1
}

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') n=$N direct ==="
  out=$(run "" "$N")
  echo "$out" | tail -4

  if echo "$out" | grep -qE "TOO_MANY_ATTEMPTS|QUOTA_EXCEEDED"; then
    echo "--- kena rate limit, coba lewat proxy ---"
    ok=0
    for _ in 1 2 3 4; do
      code=$(curl -s -m 12 -x "$BRIDGE" -o /dev/null -w '%{http_code}' \
             https://identitytoolkit.googleapis.com/ 2>/dev/null)
      if [ "$code" != "000" ] && [ -n "$code" ]; then ok=1; break; fi
      curl -s -m 6 -X POST "$BRIDGE/__new_session" >/dev/null 2>&1 || true
    done
    if [ "$ok" = "1" ]; then
      echo "=== $(date '+%Y-%m-%d %H:%M:%S') retry via proxy ==="
      run "$BRIDGE" "$N" | tail -4
    else
      echo "[!] proxy juga tidak sehat, berhenti setelah percobaan direct"
    fi
  fi
} >> "$LOG" 2>&1
