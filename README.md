# adcode-suite

Free "Included AI" from the ADCode desktop editor, harvested through its own
Firebase auth and served to 9router as an OpenAI-compatible provider.

## What was reversed

`ADCode-amd64.deb` → `resources/app.asar` → `out/main/index.js`
(`@adcode/desktop` 2.4.0, Electron).

```
createBackendTokens() -> createFirebaseAuth() -> signUp()
    POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=<FIREBASE_KEY>
    {"returnSecureToken": true}                       # anonymous, no captcha

buildTagflowProvider()
    baseUrl = https://adcode.bluethenics.com/v1/ai/tagflow
    apiKey  = <Firebase idToken>
    GET  /ai/tagflow/config            # catalogue, no auth
    POST /ai/tagflow/chat/completions  # OpenAI-compatible, always SSE
    GET  /ai/tagflow/usage             # per-uid daily limits
```

The chat route rejects anonymous users with `403 {"code":"verify-account"}`
and mails a Firebase verification link. Confirming that link with
`accounts:update` + `oobCode` flips the account to verified; chat then
returns 200. So the farmable unit is a disposable inbox, not a GitHub login.

## Per-account allowance

```
dailyRequests    : 1,000,000
dailyTokens      : 2,000,000
dailyCostMicros  : 1,000,000   ($1.00/day)
requestsPerMinute: 1,000
```

Models: `tagflow-code-27b` (262k ctx), `heabsy-qwen38`, `heabsy-glm53-flash-unc`,
`heabsy-mimo-v26-flash` (131k ctx each).

## Files

| file | role |
|---|---|
| `adcode_client.py` | Firebase signup / verify / refresh, tagflow chat, mail.tm inbox |
| `adcode_farm.py` | `-n N` accounts, retry, appends to `accounts.jsonl` |
| `adcode_adapter.py` | OpenAI-compatible server on `127.0.0.1:20130` |
| `accounts.jsonl` | pool (uid, email, password, refresh_token) |
| `farm.log` | farm history |

## Run

```bash
# own mail server, 3 workers, exit IP rotated per attempt
ADCODE_PROXY=http://127.0.0.1:10801 \
  python3 adcode_farm.py -n 100 --mail tempik -w 3 --gap 0.6 --retry 3

systemctl status adcode-adapter   # pool front-end
curl -s 127.0.0.1:20130/health    # pool state
```

Mail providers: `--mail tempik` (own server, instant inboxes, no rate limit)
or `--mail mailtm` (public, `429`s under burst).

`ADCODE_PROXY` sends every Firebase call through the local bridge and calls
`/__new_session` before each attempt. Without it Firebase answers
`400 TOO_MANY_ATTEMPTS_TRY_LATER` after ~120 signups from one IP.

Adapter endpoints: `GET /v1/models`, `POST /v1/chat/completions`
(stream + non-stream), `GET /health`.

## 9router wiring

```
node id  : openai-compatible-chat-adcode
prefix   : adcode
baseUrl  : http://127.0.0.1:20130/v1
models   : adcode/tagflow-code-27b, adcode/heabsy-qwen38,
           adcode/heabsy-glm53-flash-unc, adcode/heabsy-mimo-v26-flash
```

## Adapter behaviour

- Firebase idTokens last 1 h → refreshed lazily per account and cached.
- Accounts rotate least-recently-used; a failure is retried on the next one (3 tries).
- `403 verify-account` → account marked dead and skipped.
- `429` / quota → 2 min cooldown, then back in rotation.
- `upstream-rate-limit` (provider-side throttle) → not charged to the account.
- `accounts.jsonl` is hot-reloaded, so farming while the adapter runs needs no restart.

## Notes

- The Firebase Web API key is embedded in the shipped bundle; it identifies the
  project, it is not a user secret.
- mail.tm rate-limits bursts (`429`); the farm retries and paces itself.
- Each account's allowance resets daily, so a fixed pool keeps working without
  re-farming.
