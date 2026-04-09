import atexit
import json
import os
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, request


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TS_MONITOR_DB_PATH", os.path.join(BASE_DIR, "tailscale_monitor.db"))
TAILSCALE_BIN = os.environ.get("TAILSCALE_BIN", "tailscale")
TAILSCALE_SOCKET = os.environ.get("TAILSCALE_SOCKET")
POLL_INTERVAL_SECONDS = int(os.environ.get("TS_POLL_INTERVAL_SECONDS", "60"))
TAILSCALE_TIMEOUT_SECONDS = int(os.environ.get("TS_TAILSCALE_TIMEOUT_SECONDS", "15"))
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "5189"))

DEFAULT_HISTORY_LIMIT = 500
MAX_HISTORY_LIMIT = 5000
ROLLED_UP_RESOLUTIONS = (60, 300, 3600, 86400)
ALLOWED_RESOLUTIONS = {
    "raw": 0,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}

app = Flask(__name__)
collection_lock = threading.Lock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso8601(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def utc_now_iso() -> str:
    return to_iso8601(utc_now())


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "online"}:
            return True
        if lowered in {"false", "0", "no", "offline"}:
            return False
    return None


def parse_bool(value: str | None, default: bool = False) -> bool:
    parsed = coerce_bool(value)
    if parsed is None:
        return default
    return parsed


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def bucket_start_for(collected_at: str, resolution_seconds: int) -> str:
    moment = parse_iso8601(collected_at)
    if moment is None:
        return collected_at
    bucket_epoch = int(moment.timestamp()) // resolution_seconds * resolution_seconds
    return to_iso8601(datetime.fromtimestamp(bucket_epoch, tz=timezone.utc))


def get_db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def init_db() -> None:
    with get_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collected_at TEXT NOT NULL,
                instance_id TEXT,
                device_name TEXT,
                status_json TEXT NOT NULL,
                collection_error TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_raw_snapshots_collected_at
            ON raw_snapshots (collected_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_raw_snapshots_instance_collected
            ON raw_snapshots (instance_id, collected_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metric_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_snapshot_id INTEGER NOT NULL UNIQUE,
                collected_at TEXT NOT NULL,
                instance_id TEXT,
                device_name TEXT,
                backend_state TEXT,
                self_online INTEGER,
                peer_count INTEGER NOT NULL DEFAULT 0,
                online_peer_count INTEGER NOT NULL DEFAULT 0,
                total_rx_bytes INTEGER NOT NULL DEFAULT 0,
                total_tx_bytes INTEGER NOT NULL DEFAULT 0,
                collection_error TEXT,
                FOREIGN KEY(raw_snapshot_id) REFERENCES raw_snapshots(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_metric_samples_collected_at
            ON metric_samples (collected_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_metric_samples_instance_collected
            ON metric_samples (instance_id, collected_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metric_rollups (
                resolution_seconds INTEGER NOT NULL,
                bucket_start TEXT NOT NULL,
                instance_key TEXT NOT NULL,
                instance_id TEXT,
                first_sample_id INTEGER NOT NULL,
                last_sample_id INTEGER NOT NULL,
                device_name TEXT,
                backend_state TEXT,
                self_online INTEGER,
                avg_peer_count REAL NOT NULL DEFAULT 0,
                avg_online_peer_count REAL NOT NULL DEFAULT 0,
                max_total_rx_bytes INTEGER NOT NULL DEFAULT 0,
                max_total_tx_bytes INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (resolution_seconds, bucket_start, instance_key)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_metric_rollups_resolution_instance_bucket
            ON metric_rollups (resolution_seconds, instance_id, bucket_start DESC)
            """
        )
        maybe_migrate_legacy_snapshots(connection)


def maybe_migrate_legacy_snapshots(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "snapshots"):
        return

    raw_count = connection.execute("SELECT COUNT(*) AS count FROM raw_snapshots").fetchone()["count"]
    if raw_count:
        return

    legacy_count = connection.execute("SELECT COUNT(*) AS count FROM snapshots").fetchone()["count"]
    if not legacy_count:
        return

    connection.execute(
        """
        INSERT OR IGNORE INTO raw_snapshots (
            id,
            collected_at,
            instance_id,
            device_name,
            status_json,
            collection_error
        )
        SELECT
            id,
            collected_at,
            instance_id,
            device_name,
            status_json,
            collection_error
        FROM snapshots
        ORDER BY collected_at ASC, id ASC
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO metric_samples (
            raw_snapshot_id,
            collected_at,
            instance_id,
            device_name,
            backend_state,
            self_online,
            peer_count,
            online_peer_count,
            total_rx_bytes,
            total_tx_bytes,
            collection_error
        )
        SELECT
            id,
            collected_at,
            instance_id,
            device_name,
            backend_state,
            self_online,
            peer_count,
            online_peer_count,
            total_rx_bytes,
            total_tx_bytes,
            collection_error
        FROM snapshots
        ORDER BY collected_at ASC, id ASC
        """
    )
    rebuild_rollups(connection)


def extract_metrics(status_payload: dict[str, Any]) -> dict[str, Any]:
    self_node = status_payload.get("Self") or {}
    peers = status_payload.get("Peer") or {}
    peer_entries = list(peers.values()) if isinstance(peers, dict) else []

    online_peer_count = sum(1 for peer in peer_entries if coerce_bool(peer.get("Online")) is True)
    total_rx_bytes = sum(safe_int(peer.get("RxBytes")) for peer in peer_entries) + safe_int(self_node.get("RxBytes"))
    total_tx_bytes = sum(safe_int(peer.get("TxBytes")) for peer in peer_entries) + safe_int(self_node.get("TxBytes"))

    instance_id = (
        self_node.get("ID")
        or self_node.get("StableID")
        or self_node.get("NodeID")
        or self_node.get("DNSName")
        or self_node.get("HostName")
    )
    device_name = self_node.get("HostName") or self_node.get("DNSName") or instance_id

    return {
        "instance_id": instance_id,
        "device_name": device_name,
        "backend_state": status_payload.get("BackendState"),
        "self_online": coerce_bool(self_node.get("Online")),
        "peer_count": len(peer_entries),
        "online_peer_count": online_peer_count,
        "total_rx_bytes": total_rx_bytes,
        "total_tx_bytes": total_tx_bytes,
    }


def collect_live_status() -> tuple[dict[str, Any] | None, str | None]:
    command = [TAILSCALE_BIN]
    if TAILSCALE_SOCKET:
        command.extend(["--socket", TAILSCALE_SOCKET])
    command.extend(["status", "--json"])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TAILSCALE_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return None, f"{TAILSCALE_BIN} binary was not found"
    except subprocess.TimeoutExpired:
        return None, f"{TAILSCALE_BIN} status timed out after {TAILSCALE_TIMEOUT_SECONDS} seconds"
    except Exception as exc:
        return None, f"failed to execute {TAILSCALE_BIN}: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        return None, detail

    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"tailscale returned invalid JSON: {exc}"


def upsert_rollup(connection: sqlite3.Connection, metric_sample_id: int) -> None:
    sample = connection.execute(
        """
        SELECT id, collected_at, instance_id, device_name, backend_state, self_online,
               peer_count, online_peer_count, total_rx_bytes, total_tx_bytes, collection_error
        FROM metric_samples
        WHERE id = ?
        """,
        (metric_sample_id,),
    ).fetchone()
    if sample is None:
        return

    instance_key = sample["instance_id"] or ""
    error_increment = 1 if sample["collection_error"] else 0

    for resolution_seconds in ROLLED_UP_RESOLUTIONS:
        bucket_start = bucket_start_for(sample["collected_at"], resolution_seconds)
        connection.execute(
            """
            INSERT INTO metric_rollups (
                resolution_seconds,
                bucket_start,
                instance_key,
                instance_id,
                first_sample_id,
                last_sample_id,
                device_name,
                backend_state,
                self_online,
                avg_peer_count,
                avg_online_peer_count,
                max_total_rx_bytes,
                max_total_tx_bytes,
                sample_count,
                error_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT (resolution_seconds, bucket_start, instance_key)
            DO UPDATE SET
                last_sample_id = excluded.last_sample_id,
                device_name = COALESCE(excluded.device_name, metric_rollups.device_name),
                backend_state = COALESCE(excluded.backend_state, metric_rollups.backend_state),
                self_online = COALESCE(excluded.self_online, metric_rollups.self_online),
                avg_peer_count = (
                    (metric_rollups.avg_peer_count * metric_rollups.sample_count) + excluded.avg_peer_count
                ) / (metric_rollups.sample_count + 1),
                avg_online_peer_count = (
                    (metric_rollups.avg_online_peer_count * metric_rollups.sample_count) + excluded.avg_online_peer_count
                ) / (metric_rollups.sample_count + 1),
                max_total_rx_bytes = MAX(metric_rollups.max_total_rx_bytes, excluded.max_total_rx_bytes),
                max_total_tx_bytes = MAX(metric_rollups.max_total_tx_bytes, excluded.max_total_tx_bytes),
                sample_count = metric_rollups.sample_count + 1,
                error_count = metric_rollups.error_count + excluded.error_count
            """,
            (
                resolution_seconds,
                bucket_start,
                instance_key,
                sample["instance_id"],
                sample["id"],
                sample["id"],
                sample["device_name"],
                sample["backend_state"],
                sample["self_online"],
                float(sample["peer_count"] or 0),
                float(sample["online_peer_count"] or 0),
                int(sample["total_rx_bytes"] or 0),
                int(sample["total_tx_bytes"] or 0),
                error_increment,
            ),
        )


def rebuild_rollups(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM metric_rollups")
    sample_rows = connection.execute(
        "SELECT id FROM metric_samples ORDER BY collected_at ASC, id ASC"
    ).fetchall()
    for row in sample_rows:
        upsert_rollup(connection, int(row["id"]))


def insert_snapshot(
    *,
    collected_at: str,
    status_payload: dict[str, Any] | None,
    collection_error: str | None,
) -> tuple[int, int]:
    metrics = extract_metrics(status_payload) if status_payload else {
        "instance_id": None,
        "device_name": None,
        "backend_state": None,
        "self_online": None,
        "peer_count": 0,
        "online_peer_count": 0,
        "total_rx_bytes": 0,
        "total_tx_bytes": 0,
    }
    status_json = json.dumps(status_payload or {}, separators=(",", ":"))

    with get_db_connection() as connection:
        raw_cursor = connection.execute(
            """
            INSERT INTO raw_snapshots (
                collected_at,
                instance_id,
                device_name,
                status_json,
                collection_error
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                collected_at,
                metrics["instance_id"],
                metrics["device_name"],
                status_json,
                collection_error,
            ),
        )
        raw_snapshot_id = int(raw_cursor.lastrowid)
        metric_cursor = connection.execute(
            """
            INSERT INTO metric_samples (
                raw_snapshot_id,
                collected_at,
                instance_id,
                device_name,
                backend_state,
                self_online,
                peer_count,
                online_peer_count,
                total_rx_bytes,
                total_tx_bytes,
                collection_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_snapshot_id,
                collected_at,
                metrics["instance_id"],
                metrics["device_name"],
                metrics["backend_state"],
                None if metrics["self_online"] is None else int(metrics["self_online"]),
                metrics["peer_count"],
                metrics["online_peer_count"],
                metrics["total_rx_bytes"],
                metrics["total_tx_bytes"],
                collection_error,
            ),
        )
        metric_sample_id = int(metric_cursor.lastrowid)
        upsert_rollup(connection, metric_sample_id)

    return raw_snapshot_id, metric_sample_id


def fetch_raw_snapshot(raw_snapshot_id: int) -> dict[str, Any] | None:
    with get_db_connection() as connection:
        row = connection.execute(
            """
            SELECT id, collected_at, instance_id, device_name, status_json, collection_error
            FROM raw_snapshots
            WHERE id = ?
            """,
            (raw_snapshot_id,),
        ).fetchone()
    if row is None:
        return None

    payload = json.loads(row["status_json"]) if row["status_json"] else {}
    return {
        "id": row["id"],
        "timestamp": row["collected_at"],
        "instance_id": row["instance_id"],
        "device_name": row["device_name"],
        "collection_error": row["collection_error"],
        "status": payload,
    }


def serialize_metric_row(row: sqlite3.Row | None, *, include_status: bool = False) -> dict[str, Any] | None:
    if row is None:
        return None

    serialized = {
        "id": row["id"],
        "raw_snapshot_id": row["raw_snapshot_id"],
        "timestamp": row["collected_at"],
        "instance_id": row["instance_id"],
        "device_name": row["device_name"],
        "backend_state": row["backend_state"],
        "self_online": None if row["self_online"] is None else bool(row["self_online"]),
        "peer_count": row["peer_count"],
        "online_peer_count": row["online_peer_count"],
        "total_rx_bytes": row["total_rx_bytes"],
        "total_tx_bytes": row["total_tx_bytes"],
        "collection_error": row["collection_error"],
    }

    if include_status:
        raw_snapshot = fetch_raw_snapshot(int(row["raw_snapshot_id"]))
        serialized["status"] = {} if raw_snapshot is None else raw_snapshot["status"]

    return serialized


def latest_metric_sample(instance_id: str | None = None, *, include_status: bool = False) -> dict[str, Any] | None:
    query = """
        SELECT id, raw_snapshot_id, collected_at, instance_id, device_name, backend_state,
               self_online, peer_count, online_peer_count, total_rx_bytes,
               total_tx_bytes, collection_error
        FROM metric_samples
    """
    params: list[Any] = []

    if instance_id:
        query += " WHERE instance_id = ?"
        params.append(instance_id)

    query += " ORDER BY collected_at DESC, id DESC LIMIT 1"

    with get_db_connection() as connection:
        row = connection.execute(query, params).fetchone()
    return serialize_metric_row(row, include_status=include_status) if row else None


def collect_and_store_snapshot(*, include_status: bool = True) -> dict[str, Any]:
    with collection_lock:
        collected_at = utc_now_iso()
        status_payload, collection_error = collect_live_status()
        _, metric_sample_id = insert_snapshot(
            collected_at=collected_at,
            status_payload=status_payload,
            collection_error=collection_error,
        )
        with get_db_connection() as connection:
            row = connection.execute(
                """
                SELECT id, raw_snapshot_id, collected_at, instance_id, device_name, backend_state,
                       self_online, peer_count, online_peer_count, total_rx_bytes,
                       total_tx_bytes, collection_error
                FROM metric_samples
                WHERE id = ?
                """,
                (metric_sample_id,),
            ).fetchone()
        return serialize_metric_row(row, include_status=include_status) or {
            "timestamp": collected_at,
            "collection_error": collection_error,
            "status": status_payload or {},
        }


class SnapshotCollector:
    def __init__(self, interval_seconds: int) -> None:
        self.interval_seconds = max(15, interval_seconds)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="tailscale-collector")

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                collect_and_store_snapshot(include_status=False)
            except Exception:
                insert_snapshot(
                    collected_at=utc_now_iso(),
                    status_payload=None,
                    collection_error="collector crashed while processing a snapshot",
                )

            if self._stop_event.wait(self.interval_seconds):
                break


collector = SnapshotCollector(POLL_INTERVAL_SECONDS)


def parse_limit(raw_limit: str | None) -> int:
    if raw_limit is None:
        return DEFAULT_HISTORY_LIMIT
    return max(1, min(MAX_HISTORY_LIMIT, safe_int(raw_limit, DEFAULT_HISTORY_LIMIT)))


def parse_resolution(raw_resolution: str | None) -> int:
    if not raw_resolution:
        return 0

    if raw_resolution in ALLOWED_RESOLUTIONS:
        return ALLOWED_RESOLUTIONS[raw_resolution]

    if raw_resolution.isdigit():
        return max(0, int(raw_resolution))

    raise ValueError("resolution must be one of raw, 1m, 5m, 15m, 1h, 1d, or an integer number of seconds")


def history_query(
    *,
    from_dt: datetime | None,
    to_dt: datetime | None,
    resolution_seconds: int,
    instance_id: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], str]:
    where_clauses: list[str] = []
    params: list[Any] = []

    if from_dt:
        where_clauses.append("collected_at >= ?")
        params.append(from_dt.isoformat())
    if to_dt:
        where_clauses.append("collected_at <= ?")
        params.append(to_dt.isoformat())
    if instance_id:
        where_clauses.append("instance_id = ?")
        params.append(instance_id)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    with get_db_connection() as connection:
        if resolution_seconds <= 0:
            rows = connection.execute(
                f"""
                SELECT id, raw_snapshot_id, collected_at, instance_id, device_name, backend_state,
                       self_online, peer_count, online_peer_count, total_rx_bytes,
                       total_tx_bytes, collection_error
                FROM metric_samples
                {where_sql}
                ORDER BY collected_at DESC, id DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
            points = [serialize_metric_row(row, include_status=False) for row in reversed(rows) if row is not None]
            return points, "raw_samples"

        if resolution_seconds in ROLLED_UP_RESOLUTIONS:
            rollup_clauses: list[str] = ["resolution_seconds = ?"]
            rollup_params: list[Any] = [resolution_seconds]
            if from_dt:
                rollup_clauses.append("bucket_start >= ?")
                rollup_params.append(from_dt.isoformat())
            if to_dt:
                rollup_clauses.append("bucket_start <= ?")
                rollup_params.append(to_dt.isoformat())
            if instance_id:
                rollup_clauses.append("instance_id = ?")
                rollup_params.append(instance_id)

            rollup_where = f"WHERE {' AND '.join(rollup_clauses)}"
            rows = connection.execute(
                f"""
                SELECT resolution_seconds, bucket_start, instance_id, device_name, backend_state,
                       self_online, avg_peer_count, avg_online_peer_count, max_total_rx_bytes,
                       max_total_tx_bytes, sample_count, error_count, last_sample_id
                FROM metric_rollups
                {rollup_where}
                ORDER BY bucket_start DESC, last_sample_id DESC
                LIMIT ?
                """,
                [*rollup_params, limit],
            ).fetchall()
            points = [
                {
                    "id": row["last_sample_id"],
                    "timestamp": row["bucket_start"],
                    "instance_id": row["instance_id"],
                    "device_name": row["device_name"],
                    "backend_state": row["backend_state"],
                    "self_online": None if row["self_online"] is None else bool(row["self_online"]),
                    "peer_count": round(row["avg_peer_count"] or 0, 2),
                    "online_peer_count": round(row["avg_online_peer_count"] or 0, 2),
                    "total_rx_bytes": row["max_total_rx_bytes"] or 0,
                    "total_tx_bytes": row["max_total_tx_bytes"] or 0,
                    "sample_count": row["sample_count"],
                    "error_count": row["error_count"],
                }
                for row in reversed(rows)
            ]
            return points, "precomputed_rollup"

        rows = connection.execute(
            f"""
            SELECT
                MIN(id) AS id,
                MIN(collected_at) AS bucket_start,
                instance_id,
                MIN(device_name) AS device_name,
                MIN(backend_state) AS backend_state,
                MIN(self_online) AS self_online,
                AVG(peer_count) AS peer_count,
                AVG(online_peer_count) AS online_peer_count,
                MAX(total_rx_bytes) AS total_rx_bytes,
                MAX(total_tx_bytes) AS total_tx_bytes,
                COUNT(*) AS sample_count,
                SUM(CASE WHEN collection_error IS NOT NULL THEN 1 ELSE 0 END) AS error_count
            FROM metric_samples
            {where_sql}
            GROUP BY ((CAST(strftime('%s', collected_at) AS INTEGER) / ?) * ?), instance_id
            ORDER BY bucket_start DESC, id DESC
            LIMIT ?
            """,
            [*params, resolution_seconds, resolution_seconds, limit],
        ).fetchall()

    points = []
    for row in reversed(rows):
        points.append(
            {
                "id": row["id"],
                "timestamp": row["bucket_start"],
                "instance_id": row["instance_id"],
                "device_name": row["device_name"],
                "backend_state": row["backend_state"],
                "self_online": None if row["self_online"] is None else bool(row["self_online"]),
                "peer_count": round(row["peer_count"] or 0, 2),
                "online_peer_count": round(row["online_peer_count"] or 0, 2),
                "total_rx_bytes": row["total_rx_bytes"] or 0,
                "total_tx_bytes": row["total_tx_bytes"] or 0,
                "sample_count": row["sample_count"],
                "error_count": row["error_count"],
            }
        )
    return points, "ad_hoc_rollup"


@app.route("/health")
def health() -> Any:
    with get_db_connection() as connection:
        raw_count = connection.execute("SELECT COUNT(*) AS count FROM raw_snapshots").fetchone()["count"]
        sample_count = connection.execute("SELECT COUNT(*) AS count FROM metric_samples").fetchone()["count"]

    return jsonify(
        {
            "ok": True,
            "poll_interval_seconds": collector.interval_seconds,
            "database_path": DB_PATH,
            "tailscale_bin": TAILSCALE_BIN,
            "tailscale_socket": TAILSCALE_SOCKET,
            "raw_snapshot_count": raw_count,
            "metric_sample_count": sample_count,
            "rollup_resolutions_seconds": list(ROLLED_UP_RESOLUTIONS),
        }
    )


@app.route("/status")
def status() -> Any:
    instance_id = request.args.get("instance_id")
    include_status = parse_bool(request.args.get("include_status"), default=False)
    fresh = parse_bool(request.args.get("fresh"), default=False)

    if fresh:
        snapshot = collect_and_store_snapshot(include_status=include_status)
    else:
        snapshot = latest_metric_sample(instance_id=instance_id, include_status=include_status)
        if snapshot is None:
            snapshot = collect_and_store_snapshot(include_status=include_status)

    return jsonify(snapshot)


@app.route("/history")
def history() -> Any:
    from_dt = parse_iso8601(request.args.get("from"))
    to_dt = parse_iso8601(request.args.get("to"))
    instance_id = request.args.get("instance_id")
    limit = parse_limit(request.args.get("limit"))

    if request.args.get("from") and from_dt is None:
        return jsonify({"error": "invalid from timestamp; use ISO-8601"}), 400
    if request.args.get("to") and to_dt is None:
        return jsonify({"error": "invalid to timestamp; use ISO-8601"}), 400
    if from_dt and to_dt and from_dt > to_dt:
        return jsonify({"error": "from must be earlier than to"}), 400

    try:
        resolution_seconds = parse_resolution(request.args.get("resolution"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    points, source = history_query(
        from_dt=from_dt,
        to_dt=to_dt,
        resolution_seconds=resolution_seconds,
        instance_id=instance_id,
        limit=limit,
    )

    return jsonify(
        {
            "points": points,
            "query": {
                "from": from_dt.isoformat() if from_dt else None,
                "to": to_dt.isoformat() if to_dt else None,
                "resolution_seconds": resolution_seconds,
                "instance_id": instance_id,
                "limit": limit,
                "source": source,
            },
        }
    )


@app.route("/instances")
def instances() -> Any:
    with get_db_connection() as connection:
        rows = connection.execute(
            """
            SELECT instance_id, device_name, MAX(collected_at) AS last_seen
            FROM metric_samples
            WHERE instance_id IS NOT NULL
            GROUP BY instance_id, device_name
            ORDER BY device_name ASC, instance_id ASC
            """
        ).fetchall()

    return jsonify(
        [
            {
                "instance_id": row["instance_id"],
                "device_name": row["device_name"],
                "last_seen": row["last_seen"],
            }
            for row in rows
        ]
    )


@app.route("/snapshots/<int:raw_snapshot_id>")
def snapshot_detail(raw_snapshot_id: int) -> Any:
    snapshot = fetch_raw_snapshot(raw_snapshot_id)
    if snapshot is None:
        return jsonify({"error": "snapshot not found"}), 404
    return jsonify(snapshot)


@app.route("/collect", methods=["POST"])
def collect_now() -> Any:
    snapshot = collect_and_store_snapshot(include_status=True)
    return jsonify(snapshot), 201


def bootstrap() -> None:
    init_db()
    collector.start()


bootstrap()
atexit.register(collector.stop)


if __name__ == "__main__":
    app.run(host=APP_HOST, port=APP_PORT)
