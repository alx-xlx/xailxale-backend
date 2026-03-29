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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_db() -> None:
    with get_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collected_at TEXT NOT NULL,
                instance_id TEXT,
                device_name TEXT,
                backend_state TEXT,
                self_online INTEGER,
                peer_count INTEGER NOT NULL DEFAULT 0,
                online_peer_count INTEGER NOT NULL DEFAULT 0,
                total_rx_bytes INTEGER NOT NULL DEFAULT 0,
                total_tx_bytes INTEGER NOT NULL DEFAULT 0,
                status_json TEXT NOT NULL,
                collection_error TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_snapshots_collected_at
            ON snapshots (collected_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_snapshots_instance_collected
            ON snapshots (instance_id, collected_at DESC)
            """
        )


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


def insert_snapshot(
    *,
    collected_at: str,
    status_payload: dict[str, Any] | None,
    collection_error: str | None,
) -> int:
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
        cursor = connection.execute(
            """
            INSERT INTO snapshots (
                collected_at,
                instance_id,
                device_name,
                backend_state,
                self_online,
                peer_count,
                online_peer_count,
                total_rx_bytes,
                total_tx_bytes,
                status_json,
                collection_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                collected_at,
                metrics["instance_id"],
                metrics["device_name"],
                metrics["backend_state"],
                None if metrics["self_online"] is None else int(metrics["self_online"]),
                metrics["peer_count"],
                metrics["online_peer_count"],
                metrics["total_rx_bytes"],
                metrics["total_tx_bytes"],
                status_json,
                collection_error,
            ),
        )
        return int(cursor.lastrowid)


def get_snapshot_by_id(snapshot_id: int) -> dict[str, Any] | None:
    with get_db_connection() as connection:
        row = connection.execute(
            """
            SELECT id, collected_at, instance_id, device_name, backend_state,
                   self_online, peer_count, online_peer_count, total_rx_bytes,
                   total_tx_bytes, status_json, collection_error
            FROM snapshots
            WHERE id = ?
            """,
            (snapshot_id,),
        ).fetchone()
    return serialize_snapshot(row) if row else None


def serialize_snapshot(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None

    payload = json.loads(row["status_json"]) if row["status_json"] else {}
    return {
        "id": row["id"],
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
        "status": payload,
    }


def latest_snapshot(instance_id: str | None = None) -> dict[str, Any] | None:
    query = """
        SELECT id, collected_at, instance_id, device_name, backend_state,
               self_online, peer_count, online_peer_count, total_rx_bytes,
               total_tx_bytes, status_json, collection_error
        FROM snapshots
    """
    params: list[Any] = []

    if instance_id:
        query += " WHERE instance_id = ?"
        params.append(instance_id)

    query += " ORDER BY collected_at DESC LIMIT 1"

    with get_db_connection() as connection:
        row = connection.execute(query, params).fetchone()
    return serialize_snapshot(row) if row else None


def collect_and_store_snapshot() -> dict[str, Any]:
    with collection_lock:
        collected_at = utc_now_iso()
        status_payload, collection_error = collect_live_status()
        snapshot_id = insert_snapshot(
            collected_at=collected_at,
            status_payload=status_payload,
            collection_error=collection_error,
        )
        snapshot = get_snapshot_by_id(snapshot_id)
        return snapshot or {
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
        # Take an initial snapshot quickly, then continue on the configured interval.
        while not self._stop_event.is_set():
            try:
                collect_and_store_snapshot()
            except Exception:
                # Keep the collector alive even if a single collection fails unexpectedly.
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
) -> list[dict[str, Any]]:
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
                SELECT id, collected_at, instance_id, device_name, backend_state,
                       self_online, peer_count, online_peer_count, total_rx_bytes,
                       total_tx_bytes, status_json, collection_error
                FROM snapshots
                {where_sql}
                ORDER BY collected_at DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
            return [serialize_snapshot(row) for row in reversed(rows) if row is not None]

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
            FROM snapshots
            {where_sql}
            GROUP BY ((CAST(strftime('%s', collected_at) AS INTEGER) / ?) * ?), instance_id
            ORDER BY bucket_start DESC
            LIMIT ?
            """,
            [*params, resolution_seconds, resolution_seconds, limit],
        ).fetchall()

    response = []
    for row in reversed(rows):
        response.append(
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
    return response


@app.route("/health")
def health() -> Any:
    return jsonify(
        {
            "ok": True,
            "poll_interval_seconds": collector.interval_seconds,
            "database_path": DB_PATH,
            "tailscale_bin": TAILSCALE_BIN,
            "tailscale_socket": TAILSCALE_SOCKET,
        }
    )


@app.route("/status")
def status() -> Any:
    instance_id = request.args.get("instance_id")
    snapshot = latest_snapshot(instance_id=instance_id)
    if snapshot is None:
        snapshot = collect_and_store_snapshot()
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

    points = history_query(
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
            },
        }
    )


@app.route("/instances")
def instances() -> Any:
    with get_db_connection() as connection:
        rows = connection.execute(
            """
            SELECT instance_id, device_name, MAX(collected_at) AS last_seen
            FROM snapshots
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


@app.route("/collect", methods=["POST"])
def collect_now() -> Any:
    snapshot = collect_and_store_snapshot()
    return jsonify(snapshot), 201


def bootstrap() -> None:
    init_db()
    collector.start()


bootstrap()
atexit.register(collector.stop)


if __name__ == "__main__":
    app.run(host=APP_HOST, port=APP_PORT)
