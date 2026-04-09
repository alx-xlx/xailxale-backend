# Tailscale Monitoring Backend

This backend stores periodic `tailscale status --json` snapshots in SQLite so the app can render both the latest status and historical charts.

The iOS client for this backend lives in a separate repository: [xailxale-ios](https://github.com/alx-xlx/xailxale-ios).

The storage model is split into:

- raw snapshots for debugging and detailed inspection
- metric samples for fast latest-status and raw history queries
- precomputed rollups for common chart resolutions

## Features

- Background collector that polls Tailscale on a schedule
- SQLite persistence for raw snapshots, query-friendly metrics, and rollups
- Tailscale daemon bundled into the container
- Multi-arch Docker image support for `amd64` and `arm64`
- Healthchecked API container with persistent Tailscale and database state
- `GET /status` for a lightweight latest sample, with optional full raw payload
- `GET /history` for time-ranged chart data backed by metric samples and rollups
- `GET /instances` for known nodes
- `GET /snapshots/<id>` for a full stored raw snapshot
- `POST /collect` to force an immediate snapshot

## Local Python Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

The server listens on `0.0.0.0:5189` by default.

## Docker Deployment

The container:

- installs Tailscale inside the image
- can run its own `tailscaled` or reuse the host daemon
- optionally runs `tailscale up` using `TS_AUTHKEY` in self mode
- stores Tailscale state in a mounted volume
- stores SQLite in a mounted volume
- exposes port `5189`
- reports health from `GET /health`

### Docker Compose

The easiest setup flow is:

```bash
cp .env.example .env
```

Then edit `.env` and choose either self mode or host mode. In normal use, you should not need to edit `docker-compose.yml`; Compose reads `.env` automatically and passes those values into the container.

#### Self Mode

Use this when you want the container to manage its own Tailscale connection.

Recommended `.env` values:

```bash
TS_CONNECTION_MODE=self
TS_AUTHKEY=tskey-auth-xxxxxxxxxxxxxxxx
```

Then start the stack:

```bash
docker compose up -d --build
```

Open the API:

```bash
curl http://localhost:5189/health
```

In self mode:

- the container starts its own `tailscaled`
- `TS_AUTHKEY` is recommended for automatic sign-in
- `tailscale-state` persists login/session state
- `/dev/net/tun`, `NET_ADMIN`, and `NET_RAW` are typically needed unless you use `TS_USERSPACE=true`

#### Host Mode

Use this when the host machine is already logged into Tailscale and you want the container to read from the host daemon instead of starting its own.

Recommended `.env` values:

```bash
TS_CONNECTION_MODE=host
TS_AUTHKEY=
```

Then start the stack:

```bash
docker compose up -d --build
```

In host mode, the compose file mounts `/var/run/tailscale` from the host as read-only and the container uses `/host-tailscale/tailscaled.sock`.

In host mode:

- the container does not start its own `tailscaled`
- `TS_AUTHKEY` is not needed
- the host machine must already be logged into Tailscale
- the host must expose `/var/run/tailscale/tailscaled.sock`
- the app reads status through the host daemon socket

Persistent volumes:

- `tailscale-state`: Tailscale login/session state
- `tailscale-monitor-data`: SQLite database storage

### Required Container Capabilities

The compose file adds:

- `NET_ADMIN`
- `NET_RAW`

It also mounts `/dev/net/tun`. Those are typically required for self mode. Host mode can reuse the same compose file, though those privileges are mostly unnecessary there.

### Multi-Arch Build

Build and push a multi-platform image with Docker Buildx:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t your-dockerhub-user/tailscale-monitor:latest \
  --push .
```

## Environment Variables

### App

- `APP_HOST`: bind host, default `0.0.0.0`
- `APP_PORT`: bind port, default `5189`
- `TS_MONITOR_DB_PATH`: SQLite path, default `/data/tailscale_monitor.db` in Docker
- `TS_POLL_INTERVAL_SECONDS`: collector interval in seconds, default `60`
- `TS_TAILSCALE_TIMEOUT_SECONDS`: command timeout, default `15`
- `TAILSCALE_BIN`: override the `tailscale` CLI path
- `TAILSCALE_SOCKET`: override the Tailscale socket path used by the app

### Tailscale

- `TS_CONNECTION_MODE`: `self` or `host`, default `self`
- `TS_AUTHKEY`: auth key used for first-time sign-in
- `TS_HOSTNAME`: optional Tailscale hostname override
- `TS_ACCEPT_DNS`: default `false`
- `TS_ACCEPT_ROUTES`: default `false`
- `TS_ADVERTISE_TAGS`: optional comma-separated tags
- `TS_EXTRA_ARGS`: extra arguments appended to `tailscale up`
- `TS_SOCKET`: daemon socket path, default `/var/run/tailscale/tailscaled.sock`
- `TS_HOST_SOCKET`: host daemon socket path inside the container, default `/host-tailscale/tailscaled.sock`
- `TS_STATE_DIR`: daemon state directory, default `/var/lib/tailscale`
- `TS_USERSPACE`: set to `true` to run `tailscaled --tun=userspace-networking`

## API

### `GET /status`

Returns the latest stored metric sample. If the database is empty, the server collects one immediately.

Optional query params:

- `instance_id`
- `include_status=true` to include the full raw `tailscale status --json` payload
- `fresh=true` to force a live collection before responding

### `GET /history`

Query params:

- `from`: ISO-8601 timestamp
- `to`: ISO-8601 timestamp
- `resolution`: `raw`, `1m`, `5m`, `15m`, `1h`, `1d`, or integer seconds
- `instance_id`
- `limit`: default `500`, max `5000`

Responses include `query.source` so the client can tell whether the data came from:

- `raw_samples`
- `precomputed_rollup`
- `ad_hoc_rollup`

### `GET /instances`

Returns distinct instances seen in stored snapshots.

### `GET /snapshots/<id>`

Returns the full raw snapshot payload for a stored snapshot id.

### `POST /collect`

Triggers an immediate collection, stores the raw snapshot plus derived metrics, and returns the new sample with the raw payload.

## Additional Docs

- [API Reference](./API.md)
- [OpenAPI Specification](./openapi.yaml)
