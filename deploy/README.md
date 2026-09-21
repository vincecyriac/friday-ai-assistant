# Deploying the FRIDAY sentinel

The sentinel is `python -m friday.sentinel`. It reads `.env` from the repo
root, keeps all state under `data/` (or `FRIDAY_DATA_DIR`), and listens on
`FRIDAY_SENTINEL_BIND` (default `127.0.0.1:8770`).

## Any node: install

```bash
git clone <repo> && cd friday-ai-assistant
python3 -m venv .venv
.venv/bin/pip install -e ".[sentinel]"        # headless server / Pi
cp .env.template .env                          # set FRIDAY_SENTINEL_TOKEN at least
.venv/bin/python -m friday.sentinel            # foreground smoke run
curl -s 127.0.0.1:8770/health
```

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
FRIDAY_SENTINEL_TOKEN=<same token as the sentinel>
```

`setup_remote.sh` in this directory is the existing script for exposing the
**desktop hub** the same way; it is unchanged.

## API

| Route | Auth | Purpose |
|---|---|---|
| `GET /health` | none | status, queue depths, platform, supervisor restarts |
| `GET /telemetry` | none | latest telemetry snapshot (204 if none yet) |
| `GET /nodes` | none | last heartbeat per node |
| `POST /events` | bearer | one event or a list (≤100, ≤256 KB): `{"type":"a.b","source":"node","payload":{}}` → `202 {"ids":[...]}` |
| `GET /ws` | bearer (header or `?token=`) | stream events; send `{"subscribe":["node.*"]}` to filter |
