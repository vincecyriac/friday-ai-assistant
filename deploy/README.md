# Deploying the FRIDAY sentinel

The sentinel is `python -m friday.sentinel`. It reads `.env` from the repo
root, keeps all state under `data/` (or `FRIDAY_DATA_DIR`), and listens on
`FRIDAY_SENTINEL_BIND` (default `127.0.0.1:8770`).

## Sentinel: first boot

```bash
git clone <repo> && cd friday-ai-assistant
python3 -m venv .venv
.venv/bin/pip install -e ".[sentinel]"
cp .env.template .env
.venv/bin/python -m friday.sentinel keygen >> .env        # once only: sets FRIDAY_MASTER_KEY — back it up
.venv/bin/python -m friday.sentinel user set-password vince
.venv/bin/python -m friday.sentinel token create desktop   # paste into the Mac's .env as FRIDAY_SENTINEL_TOKEN
.venv/bin/python -m friday.sentinel                        # foreground smoke run
```

Open `http://127.0.0.1:8770/` (or the Tailscale URL), sign in, and enter the
Gemini key under **Settings → llm**. The desktop pulls it on its next boot.

Behind Caddy/Nginx/Tailscale Serve set `FRIDAY_TRUSTED_PROXY=true` so the
session cookie is marked `Secure` from the forwarded scheme.

## Dashboard

`http://127.0.0.1:8770/` (or the Tailscale URL). Overview shows every node's
heartbeat and telemetry tiles; Activity streams the event bus (heartbeats,
telemetry, audit entries, config changes — later triage); Controls holds the call
mode, do-not-disturb, monitor switches and the sentinel's intervals (live, no
restart); Assistant is a text chat with FRIDAY that can read the same data and flip
the controls. Settings and Nodes & tokens are unchanged.

The dashboard is static files under `friday/sentinel/dashboard/`; `tailwind.css`
is committed. After editing any class in the HTML/JS run `deploy/build_css.sh`
(downloads the Tailwind 3.4.17 standalone binary into `.cache/` once; no Node
needed) — `tests/sentinel/test_dashboard_files.py` fails on a stale build.

## Linux (Fedora, Raspberry Pi OS): systemd

```bash
sed -e "s|__USER__|$USER|g" -e "s|__REPO__|$PWD|g" \
    deploy/systemd/friday-sentinel.service | sudo tee /etc/systemd/system/friday-sentinel.service
sudo systemctl daemon-reload
sudo systemctl enable --now friday-sentinel
journalctl -u friday-sentinel -f
```

`Type=notify` + `WatchdogSec=90`: the daemon reports READY and pings the
watchdog on every heartbeat, so a hung process is restarted automatically.

## macOS (development): launchd

```bash
mkdir -p data/logs ~/Library/LaunchAgents
sed "s|__REPO__|$PWD|g" deploy/launchd/com.friday.sentinel.plist > ~/Library/LaunchAgents/com.friday.sentinel.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.friday.sentinel.plist
tail -f data/logs/sentinel.log
```

## Remote access (Tailscale Serve)

The sentinel binds to localhost. To reach it from other nodes on your
tailnet, on the sentinel host:

```bash
tailscale serve --bg --https=443 --set-path=/sentinel http://127.0.0.1:8770
```

Then point the desktop at it in its `.env`:

```ini
FRIDAY_SENTINEL_URL=https://<host>.<tailnet>.ts.net/sentinel
FRIDAY_SENTINEL_TOKEN=<token from "friday-sentinel token create desktop">
```

The dashboard's WebSocket (`/ws`) also goes through the proxy; Tailscale Serve
passes it as-is.

`setup_remote.sh` in this directory is the existing script for exposing the
**desktop hub** the same way; it is unchanged.

## API

| Route | Auth | Purpose |
|---|---|---|
| `GET /health` | none | status, queue depths, platform, supervisor restarts |
| `GET /telemetry` | node token or session | latest telemetry snapshot (204 if none yet) |
| `GET /nodes` | node token or session | last heartbeat per node |
| `POST /events` | node token or session | one event or a list (≤100, ≤256 KB): `{"type":"a.b","source":"node","payload":{}}` → `202 {"ids":[...]}` |
| `GET /ws` | node token or session (header, cookie or `?token=`) | stream events; send `{"subscribe":["node.*"]}` to filter |
| `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` | session | dashboard sign-in (cookie: HttpOnly, SameSite=Lax, Secure over HTTPS) |
| `GET/PUT /api/settings`, `GET /api/settings/schema` | session | vault-backed settings; secrets masked |
| `GET/POST /api/tokens`, `DELETE /api/tokens/{id}` | session | node tokens (plaintext shown once) |
| `GET /api/audit` | session | who changed what |
| `GET /config?scope=desktop` | node token or session | decrypted config for a node scope; audited |
| `GET /api/events?type=&source=&since=&before=&limit=` | session | events, newest first (type is a glob) |
| `GET /api/telemetry` | node token or session | latest snapshot per node |
| `GET/POST /api/chat`, `GET/DELETE /api/chat/{id}` | session | assistant conversations |
| `POST /api/chat/{id}/messages` | session | one turn; `application/x-ndjson` stream of `delta` / `tool` / `result` / `error` / `done` |

Node tokens go in `Authorization: Bearer fn_…`. State-changing dashboard
calls (`POST`/`PUT`/`DELETE`) must also send `X-FRIDAY-Client: dashboard`.
