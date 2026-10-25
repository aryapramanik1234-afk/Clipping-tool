"""Small SQLite persistence layer for the single-user local prototype."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    duration_seconds REAL,
    width INTEGER,
    height INTEGER,
    min_clip_seconds REAL NOT NULL DEFAULT 20,
    max_clip_seconds REAL NOT NULL DEFAULT 45,
    status TEXT NOT NULL,
    error TEXT,
    transcript_json TEXT NOT NULL DEFAULT '[]',
    transcript_text TEXT NOT NULL DEFAULT '',
    rights_attested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    duration_seconds REAL NOT NULL,
    score REAL NOT NULL,
    title TEXT NOT NULL,
    caption TEXT NOT NULL,
    hashtags_json TEXT NOT NULL DEFAULT '[]',
    transcript TEXT NOT NULL,
    clip_path TEXT,
    subtitle_path TEXT,
    thumbnail_path TEXT,
    status TEXT NOT NULL DEFAULT 'needs_review',
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(video_id, content_hash)
);
CREATE INDEX IF NOT EXISTS clips_video_id_idx ON clips(video_id);
CREATE INDEX IF NOT EXISTS clips_status_idx ON clips(status);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'local_only',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS schedules_scheduled_at_idx ON schedules(scheduled_at);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    metric_date TEXT NOT NULL,
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    shares INTEGER,
    average_view_duration REAL,
    retention_json TEXT,
    fetched_at TEXT NOT NULL,
    UNIQUE(clip_id, metric_date)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
    clip_id TEXT REFERENCES clips(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_video_id_idx ON events(video_id, created_at);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES ('processing_paused', 'false', CURRENT_TIMESTAMP);
"""

_VIDEO_UPDATE_COLUMNS = {
    "duration_seconds", "width", "height", "min_clip_seconds", "max_clip_seconds",
    "status", "error", "transcript_json", "transcript_text", "updated_at",
}
_CLIP_UPDATE_COLUMNS = {
    "title", "caption", "hashtags_json", "status", "updated_at",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def data_dir() -> Path:
    return Path(os.environ.get("CLIPPER_DATA_DIR", "data")).expanduser().resolve()


def database_path() -> Path:
    return data_dir() / "clipper.sqlite3"


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(_SCHEMA)


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _insert_event(
    conn: sqlite3.Connection,
    event_type: str,
    detail: str = "",
    *,
    video_id: str | None = None,
    clip_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events(video_id, clip_id, event_type, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (video_id, clip_id, event_type, detail[:500], utc_now()),
    )


def log_event(event_type: str, detail: str = "", *, video_id: str | None = None, clip_id: str | None = None) -> None:
    with connection() as conn:
        _insert_event(conn, event_type, detail, video_id=video_id, clip_id=clip_id)


def events_for_video(video_id: str) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT event_type, detail, created_at FROM events WHERE video_id = ? ORDER BY id DESC LIMIT 100",
            (video_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def recent_events(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(500, int(limit)))
    with connection() as conn:
        rows = conn.execute(
            """SELECT event_type, detail, video_id, clip_id, created_at
               FROM events ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def create_video(video: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO videos (
                id, filename, path, sha256, size_bytes, min_clip_seconds,
                max_clip_seconds, status, rights_attested, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                video["id"], video["filename"], video["path"], video["sha256"],
                video["size_bytes"], video["min_clip_seconds"], video["max_clip_seconds"],
                video["status"], int(video.get("rights_attested", False)),
                video["created_at"], video["updated_at"],
            ),
        )
        _insert_event(conn, "source_uploaded", f"Rights attestation recorded; initial state {video['status']}.", video_id=video["id"])


def get_video(video_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        return _as_dict(conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone())


def get_video_by_hash(sha256: str) -> dict[str, Any] | None:
    with connection() as conn:
        return _as_dict(conn.execute("SELECT * FROM videos WHERE sha256 = ?", (sha256,)).fetchone())


def list_videos() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT v.*, COUNT(c.id) AS clip_count,
                      SUM(CASE WHEN c.status IN ('needs_review','approved','scheduled_local','published') THEN 1 ELSE 0 END) AS usable_clip_count
               FROM videos v LEFT JOIN clips c ON c.video_id = v.id
               GROUP BY v.id ORDER BY v.created_at DESC"""
        ).fetchall()
        return [dict(row) for row in rows]


def update_video(video_id: str, **fields: Any) -> None:
    updates = {key: value for key, value in fields.items() if key in _VIDEO_UPDATE_COLUMNS}
    if not updates:
        return
    updates.setdefault("updated_at", utc_now())
    assignments = ", ".join(f"{column} = ?" for column in updates)
    values = list(updates.values()) + [video_id]
    with connection() as conn:
        previous = conn.execute("SELECT status FROM videos WHERE id = ?", (video_id,)).fetchone()
        conn.execute(f"UPDATE videos SET {assignments} WHERE id = ?", values)
        if previous and "status" in updates and previous["status"] != updates["status"]:
            _insert_event(conn, "video_status", f"{previous['status']} → {updates['status']}", video_id=video_id)


def create_clips(video_id: str, candidates: list[dict[str, Any]]) -> int:
    created = 0
    now = utc_now()
    with connection() as conn:
        for item in candidates:
            text = item["text"].strip()
            content_hash = hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()
            try:
                conn.execute(
                    """INSERT INTO clips (
                        id, video_id, start_seconds, end_seconds, duration_seconds, score,
                        title, caption, hashtags_json, transcript, clip_path, subtitle_path,
                        thumbnail_path, status, content_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'needs_review', ?, ?, ?)""",
                    (
                        item["id"], video_id, item["start_seconds"], item["end_seconds"],
                        item["duration_seconds"], item["score"], item["title"], item["caption"],
                        json.dumps(item["hashtags"], ensure_ascii=False), text,
                        item.get("clip_path"), item.get("subtitle_path"), item.get("thumbnail_path"),
                        content_hash, now, now,
                    ),
                )
                _insert_event(conn, "clip_created", f"Candidate at {item['start_seconds']:.2f}s, score {item['score']:.1f}.", video_id=video_id, clip_id=item["id"])
                created += 1
            except sqlite3.IntegrityError as exc:
                # A transcript-equivalent candidate already exists for this source.
                if "UNIQUE constraint failed: clips.video_id, clips.content_hash" not in str(exc):
                    raise
    return created


def get_clip(clip_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT c.*, v.filename AS video_filename, v.duration_seconds AS source_duration
               FROM clips c JOIN videos v ON v.id = c.video_id WHERE c.id = ?""",
            (clip_id,),
        ).fetchone()
        result = _as_dict(row)
        if result:
            result["hashtags"] = json.loads(result.pop("hashtags_json") or "[]")
        return result


def list_clips(status: str | None = None) -> list[dict[str, Any]]:
    query = """SELECT c.*, v.filename AS video_filename, v.duration_seconds AS source_duration
               FROM clips c JOIN videos v ON v.id = c.video_id"""
    params: tuple[Any, ...] = ()
    if status:
        query += " WHERE c.status = ?"
        params = (status,)
    query += " ORDER BY c.created_at DESC, c.score DESC"
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["hashtags"] = json.loads(item.pop("hashtags_json") or "[]")
            results.append(item)
        return results


def update_clip(clip_id: str, **fields: Any) -> None:
    updates = {key: value for key, value in fields.items() if key in _CLIP_UPDATE_COLUMNS}
    if not updates:
        return
    updates.setdefault("updated_at", utc_now())
    assignments = ", ".join(f"{column} = ?" for column in updates)
    values = list(updates.values()) + [clip_id]
    with connection() as conn:
        previous = conn.execute("SELECT status, video_id FROM clips WHERE id = ?", (clip_id,)).fetchone()
        conn.execute(f"UPDATE clips SET {assignments} WHERE id = ?", values)
        if previous and "status" in updates and previous["status"] != updates["status"]:
            _insert_event(
                conn,
                "clip_status",
                f"{previous['status']} → {updates['status']}",
                video_id=previous["video_id"],
                clip_id=clip_id,
            )


def create_or_update_schedule(clip_id: str, platform: str, scheduled_at: str) -> tuple[dict[str, Any], bool]:
    """Idempotently plan one destination per clip. Returns (row, created)."""
    key = hashlib.sha256(f"{clip_id}:{platform}".encode("utf-8")).hexdigest()
    now = utc_now()
    with connection() as conn:
        clip = conn.execute("SELECT status, video_id FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if clip is None:
            raise LookupError("Clip not found")
        if clip["status"] not in ("approved", "scheduled_local"):
            raise ValueError("Approve the clip before adding it to the calendar")

        existing = conn.execute("SELECT * FROM schedules WHERE idempotency_key = ?", (key,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE schedules SET scheduled_at = ?, status = 'local_only', updated_at = ? WHERE idempotency_key = ?",
                (scheduled_at, now, key),
            )
            conn.execute("UPDATE clips SET status = 'scheduled_local', updated_at = ? WHERE id = ?", (now, clip_id))
            _insert_event(conn, "local_schedule_updated", f"YouTube reminder moved to {scheduled_at}; no post was uploaded.", video_id=clip["video_id"], clip_id=clip_id)
            row = conn.execute("SELECT * FROM schedules WHERE idempotency_key = ?", (key,)).fetchone()
            return dict(row), False

        schedule_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO schedules (id, clip_id, platform, scheduled_at, status,
                   idempotency_key, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'local_only', ?, ?, ?)""",
            (schedule_id, clip_id, platform, scheduled_at, key, now, now),
        )
        conn.execute("UPDATE clips SET status = 'scheduled_local', updated_at = ? WHERE id = ?", (now, clip_id))
        _insert_event(conn, "local_schedule_created", f"YouTube reminder set for {scheduled_at}; no post was uploaded.", video_id=clip["video_id"], clip_id=clip_id)
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        return dict(row), True


def list_schedules() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT s.*, c.title, c.start_seconds, c.end_seconds, c.thumbnail_path,
                      v.filename AS video_filename
               FROM schedules s JOIN clips c ON c.id = s.clip_id
               JOIN videos v ON v.id = c.video_id
               WHERE s.status != 'cancelled' ORDER BY s.scheduled_at ASC"""
        ).fetchall()
        return [dict(row) for row in rows]


def cancel_schedule(schedule_id: str) -> bool:
    now = utc_now()
    with connection() as conn:
        row = conn.execute(
            """SELECT s.clip_id, s.platform, c.video_id FROM schedules s
               JOIN clips c ON c.id = s.clip_id WHERE s.id = ? AND s.status != 'cancelled'""",
            (schedule_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute("UPDATE schedules SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, schedule_id))
        conn.execute("UPDATE clips SET status = 'approved', updated_at = ? WHERE id = ? AND status = 'scheduled_local'", (now, row["clip_id"]))
        _insert_event(conn, "local_schedule_cancelled", f"Cancelled {row['platform']} reminder; no post was uploaded.", video_id=row["video_id"], clip_id=row["clip_id"])
        return True


def set_setting(key: str, value: str) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, utc_now()),
        )


def get_setting(key: str, default: str = "") -> str:
    with connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_processing_paused(paused: bool) -> None:
    set_setting("processing_paused", "true" if paused else "false")
    log_event("processing_paused" if paused else "processing_resumed", "Local worker control changed.")


def processing_paused() -> bool:
    return get_setting("processing_paused", "false").lower() == "true"


def paused_video_ids() -> list[str]:
    with connection() as conn:
        rows = conn.execute("SELECT id FROM videos WHERE status = 'paused' ORDER BY created_at").fetchall()
        return [row["id"] for row in rows]


def analytics_summary() -> dict[str, Any]:
    with connection() as conn:
        counts = conn.execute(
            """SELECT
                   (SELECT COUNT(*) FROM videos) AS videos,
                   (SELECT COUNT(*) FROM clips) AS candidates,
                   (SELECT COUNT(*) FROM clips WHERE status = 'needs_review') AS needs_review,
                   (SELECT COUNT(*) FROM clips WHERE status = 'approved') AS approved,
                   (SELECT COUNT(*) FROM clips WHERE status = 'scheduled_local') AS planned,
                   (SELECT COUNT(*) FROM clips WHERE status = 'published') AS published,
                   (SELECT COUNT(*) FROM metrics) AS metric_rows,
                   COALESCE((SELECT SUM(size_bytes) FROM videos), 0) AS source_bytes
               """
        ).fetchone()
        return dict(counts)


def metrics_for_dashboard() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT c.id AS clip_id, c.title, m.metric_date, m.views, m.likes, m.comments,
                      m.shares, m.average_view_duration, m.retention_json
               FROM metrics m JOIN clips c ON c.id = m.clip_id
               ORDER BY m.metric_date DESC"""
        ).fetchall()
        return [dict(row) for row in rows]
