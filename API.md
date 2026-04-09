# API Reference

This document is written for humans and agents that need to integrate with the Tailscale Monitoring backend without reading the implementation.

## Overview

Base URL:

```text
http://<host>:5189
```

The backend collects `tailscale status --json` snapshots on a schedule and stores three layers of data:

- `raw_snapshots`: full raw Tailscale JSON payloads plus collection errors
- `metric_samples`: flattened per-snapshot metrics for fast latest-status and raw history queries
- `metric_rollups`: precomputed aggregates for common chart resolutions

The collector runs automatically in the background. Clients can also force a collection with `POST /collect`.

## Data Model

### Raw snapshot

A raw snapshot represents the stored output of one `tailscale status --json` call.

Fields:

- `id`: integer raw snapshot id
- `timestamp`: ISO-8601 UTC timestamp when the snapshot was collected
- `instance_id`: stable identifier inferred from the local Tailscale node when available
- `device_name`: hostname or DNS name of the local Tailscale node
- `collection_error`: string error message if collection failed, otherwise `null`
- `status`: full raw Tailscale status JSON object

### Metric sample

A metric sample is the lightweight representation of one raw snapshot.

Fields:

- `id`: integer metric sample id
- `raw_snapshot_id`: integer id of the associated raw snapshot
- `timestamp`: ISO-8601 UTC timestamp
- `instance_id`: local node identifier when available
- `device_name`: local node display name
- `backend_state`: Tailscale backend state from the raw payload
- `self_online`: boolean or `null`
- `peer_count`: integer count of peers in the raw payload
- `online_peer_count`: integer count of peers marked online
- `total_rx_bytes`: integer sum of `RxBytes` across self + peers
- `total_tx_bytes`: integer sum of `TxBytes` across self + peers
- `collection_error`: string or `null`
- `status`: optional full raw Tailscale JSON object, only included when requested

### Rollup point

A rollup point is an aggregate produced from multiple metric samples in one time bucket.

Fields:

- `id`: representative metric sample id for the bucket
- `timestamp`: bucket start time in ISO-8601 UTC
- `instance_id`: local node identifier when available
- `device_name`: local node display name
- `backend_state`: representative backend state
- `self_online`: representative boolean or `null`
- `peer_count`: averaged peer count for the bucket
- `online_peer_count`: averaged online peer count for the bucket
- `total_rx_bytes`: maximum `total_rx_bytes` seen in the bucket
- `total_tx_bytes`: maximum `total_tx_bytes` seen in the bucket
- `sample_count`: number of metric samples included in the bucket
- `error_count`: number of samples in the bucket with a collection error

## Endpoints

### `GET /health`

Returns service health and backend metadata.

Example response:

```json
{
  "ok": true,
  "poll_interval_seconds": 60,
  "database_path": "/data/tailscale_monitor.db",
  "tailscale_bin": "tailscale",
  "tailscale_socket": "/var/run/tailscale/tailscaled.sock",
  "raw_snapshot_count": 1440,
  "metric_sample_count": 1440,
  "rollup_resolutions_seconds": [60, 300, 3600, 86400]
}
```

Use this for:

- container health checks
- verifying database activity
- checking which Tailscale socket the backend is using

### `GET /status`

Returns the latest metric sample.

Query parameters:

- `instance_id`: optional string filter
- `include_status`: optional boolean, default `false`
  - when `true`, includes the full raw Tailscale JSON payload under `status`
- `fresh`: optional boolean, default `false`
  - when `true`, forces a live collection before responding

Behavior:

- with `fresh=false`, returns the latest stored metric sample
- if no sample exists yet, the backend collects one immediately
- with `fresh=true`, always performs a new collection first

Example request:

```text
GET /status?include_status=true&fresh=true
```

Example response:

```json
{
  "id": 101,
  "raw_snapshot_id": 101,
  "timestamp": "2026-04-09T08:30:00+00:00",
  "instance_id": "n12345",
  "device_name": "mac-mini",
  "backend_state": "Running",
  "self_online": true,
  "peer_count": 8,
  "online_peer_count": 6,
  "total_rx_bytes": 123456,
  "total_tx_bytes": 654321,
  "collection_error": null,
  "status": {
    "BackendState": "Running"
  }
}
```

Integration note:

- prefer `GET /status` for dashboard summaries
- only request `include_status=true` when a client genuinely needs the raw Tailscale payload

### `GET /history`

Returns historical points for charts.

Query parameters:

- `from`: optional ISO-8601 timestamp
- `to`: optional ISO-8601 timestamp
- `resolution`: optional bucket size
  - accepted values: `raw`, `1m`, `5m`, `15m`, `1h`, `1d`, or integer seconds
- `instance_id`: optional string filter
- `limit`: optional integer, default `500`, max `5000`

Behavior:

- `resolution=raw` or omitted: returns raw metric samples from `metric_samples`
- `resolution=1m`, `5m`, `1h`, `1d`: returns precomputed rollups from `metric_rollups`
- other integer resolutions such as `900`: returns ad hoc rollups computed from `metric_samples`

Response shape:

```json
{
  "points": [
    {
      "id": 99,
      "timestamp": "2026-04-09T08:00:00+00:00",
      "instance_id": "n12345",
      "device_name": "mac-mini",
      "backend_state": "Running",
      "self_online": true,
      "peer_count": 8,
      "online_peer_count": 6,
      "total_rx_bytes": 120000,
      "total_tx_bytes": 650000,
      "sample_count": 5,
      "error_count": 0
    }
  ],
  "query": {
    "from": "2026-04-09T08:00:00+00:00",
    "to": "2026-04-09T09:00:00+00:00",
    "resolution_seconds": 300,
    "instance_id": "n12345",
    "limit": 500,
    "source": "precomputed_rollup"
  }
}
```

`query.source` meanings:

- `raw_samples`: direct rows from `metric_samples`
- `precomputed_rollup`: rows from `metric_rollups`
- `ad_hoc_rollup`: on-the-fly aggregation from `metric_samples`

Error responses:

- `400` if `from` is invalid
- `400` if `to` is invalid
- `400` if `from > to`
- `400` if `resolution` is not supported

Practical guidance:

- use `raw` for detailed inspection and short windows
- use `5m` or `1h` for dashboards and charts
- read `query.source` if your client cares about how the point set was produced

### `GET /instances`

Returns distinct monitored instances seen in stored metric samples.

Example response:

```json
[
  {
    "instance_id": "n12345",
    "device_name": "mac-mini",
    "last_seen": "2026-04-09T08:30:00+00:00"
  }
]
```

Use this for:

- populating instance filters in a UI
- discovering known nodes before querying `/status` or `/history`

### `GET /snapshots/<raw_snapshot_id>`

Returns one stored raw snapshot by id.

Path parameters:

- `raw_snapshot_id`: integer raw snapshot id

Success response:

```json
{
  "id": 101,
  "timestamp": "2026-04-09T08:30:00+00:00",
  "instance_id": "n12345",
  "device_name": "mac-mini",
  "collection_error": null,
  "status": {
    "BackendState": "Running"
  }
}
```

Error response:

```json
{
  "error": "snapshot not found"
}
```

Status code:

- `404` if the raw snapshot does not exist

Use this when a client has a `raw_snapshot_id` from `/status` or `/history` and wants the original Tailscale payload.

### `POST /collect`

Forces an immediate collection and stores both:

- a new `raw_snapshots` row
- a new `metric_samples` row
- updated rollups for precomputed resolutions

Response:

- status code `201`
- returns the newly created metric sample with `status` included

Example response:

```json
{
  "id": 102,
  "raw_snapshot_id": 102,
  "timestamp": "2026-04-09T08:31:00+00:00",
  "instance_id": "n12345",
  "device_name": "mac-mini",
  "backend_state": "Running",
  "self_online": true,
  "peer_count": 8,
  "online_peer_count": 6,
  "total_rx_bytes": 123999,
  "total_tx_bytes": 655000,
  "collection_error": null,
  "status": {
    "BackendState": "Running"
  }
}
```

Use this sparingly. The background collector already captures samples on a schedule.

## Boolean Parsing

The backend accepts these boolean-like query values:

- true values: `true`, `1`, `yes`, `online`
- false values: `false`, `0`, `no`, `offline`

Anything else falls back to the endpoint default.

## Time Format

All timestamps are returned in UTC ISO-8601 format.

Examples:

- `2026-04-09T08:30:00+00:00`
- `2026-04-09T08:30:00Z` is accepted as input

Naive timestamps without a timezone are interpreted as UTC.

## Integration Guidance For Agents

If you are an AI agent integrating with this backend:

- call `GET /health` first to confirm the backend is live
- call `GET /instances` if you need to discover available monitored nodes
- call `GET /status` for the current summary state
- call `GET /history` for charts and trend analysis
- call `GET /snapshots/<id>` only when you need the full raw Tailscale JSON payload
- avoid setting `include_status=true` on frequent polling unless you need the raw payload
- prefer rollup resolutions such as `5m` or `1h` for longer history windows

## Notes About Storage Semantics

This backend only knows what it collected.

That means:

- it can chart periods that were successfully sampled and stored
- it cannot reconstruct data for periods where no collection occurred
- `fresh=true` and `POST /collect` affect the current moment only and do not backfill missed history
