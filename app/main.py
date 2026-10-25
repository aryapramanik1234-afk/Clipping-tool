"""FastAPI shell for the local, approval-first clipping prototype."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid
import zipfile

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from . import db
from .pipeline import MAX_SOURCE_DURATION_SECONDS, process_video
from .rendering import command_exists


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
MAX_UPLOAD_BYTES = int(os.environ.get("CLIPPER_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
PROCESSING_STATES = {"queued", "validating", "transcribing", "finding_highlights", "rendering"}
_WORKER_LOCK = threading.Lock()


def run_serial_job(video_id: str) -> None:
    """Limit CPU/memory-heavy local processing to one video at a time."""
    with _WORKER_LOCK:
        process_video(video_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Create private per-user media and SQLite files on POSIX systems.
    if os.name == "posix":
        os.umask(0o077)
    db.init_db()
    # Resume any job interrupted by a local process restart. Rendering is
    # deterministic/idempotent, so existing clip IDs and files are reused.
    paused = db.processing_paused()
    for video in db.list_videos():
        if video["status"] in PROCESSING_STATES:
            db.update_video(
                video["id"],
                status="paused" if paused else "queued",
                error="Restarted after an interrupted local run." if not paused else None,
            )
            if not paused:
                threading.Thread(target=run_serial_job, args=(video["id"],), daemon=True).start()
    yield


app = FastAPI(title="Clipline Local Studio", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ClipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=100)
    caption: str | None = Field(default=None, max_length=5000)
    hashtags: list[str] | None = Field(default=None, max_length=12)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_clip_seconds: float | None = Field(default=None, ge=10, le=180)
    max_clip_seconds: float | None = Field(default=None, ge=10, le=180)


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str = "youtube"
    scheduled_at: str


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    processing_paused: bool


def _video_public(video: dict) -> dict:
    return {
        key: video.get(key)
        for key in (
            "id", "filename", "size_bytes", "duration_seconds", "width", "height",
            "min_clip_seconds", "max_clip_seconds", "status", "error", "created_at",
            "clip_count", "usable_clip_count", "rights_attested",
        )
        if key in video
    }


def _clip_public(clip: dict) -> dict:
    return {
        "id": clip["id"],
        "video_id": clip["video_id"],
        "video_filename": clip.get("video_filename"),
        "source_duration": clip.get("source_duration"),
        "start_seconds": clip["start_seconds"],
        "end_seconds": clip["end_seconds"],
        "duration_seconds": clip["duration_seconds"],
        "score": clip["score"],
        "title": clip["title"],
        "caption": clip["caption"],
        "hashtags": clip.get("hashtags", []),
        "transcript": clip["transcript"],
        "status": clip["status"],
        "created_at": clip["created_at"],
        "video_url": f"/api/clips/{clip['id']}/video" if clip.get("clip_path") else None,
        "subtitle_url": f"/api/clips/{clip['id']}/subtitles" if clip.get("subtitle_path") else None,
        "thumbnail_url": f"/api/clips/{clip['id']}/thumbnail" if clip.get("thumbnail_path") else None,
        "export_url": f"/api/clips/{clip['id']}/export",
    }


def _safe_file_path(value: str | None, label: str) -> Path:
    if not value:
        raise HTTPException(status_code=404, detail=f"{label} is not available yet")
    path = Path(value)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"{label} file is missing")
    return path


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": app.version,
        "ffmpeg": command_exists("ffmpeg"),
        "ffprobe": command_exists("ffprobe"),
        "transcription": importlib.util.find_spec("faster_whisper") is not None,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_source_duration_seconds": MAX_SOURCE_DURATION_SECONDS,
        "publisher": "not_connected",
        "publish_mode": "manual_export_only",
    }


@app.get("/api/videos")
def videos() -> list[dict]:
    return [_video_public(video) for video in db.list_videos()]


@app.post("/api/videos", status_code=202)
async def upload_video(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    min_clip_seconds: float = Form(20),
    max_clip_seconds: float = Form(45),
    rights_attested: bool = Form(False),
) -> dict:
    if not rights_attested:
        raise HTTPException(status_code=400, detail="Confirm that you own or are authorized to use this source video.")
    if min_clip_seconds < 10 or max_clip_seconds > 180 or max_clip_seconds < min_clip_seconds:
        raise HTTPException(status_code=400, detail="Choose clip durations from 10 to 180 seconds, with maximum not less than minimum.")

    original_name = Path(file.filename or "source-video").name
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Choose a supported video file: MP4, MOV, M4V, MKV, WEBM or AVI.")

    video_id = uuid.uuid4().hex
    upload_dir = db.data_dir() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    partial_path = upload_dir / f".{video_id}.part"
    final_path = upload_dir / f"{video_id}{extension}"
    digest = hashlib.sha256()
    size = 0
    try:
        with partial_path.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File is larger than the configured {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
                    )
                digest.update(chunk)
                output.write(chunk)
        if size < 1024:
            raise HTTPException(status_code=400, detail="The selected file is empty or too small to be a video.")
        sha256 = digest.hexdigest()
        existing = db.get_video_by_hash(sha256)
        if existing:
            raise HTTPException(
                status_code=409,
                detail={"message": "This exact source file is already in the library.", "video_id": existing["id"]},
            )
        os.replace(partial_path, final_path)
        safe_name = re.sub(r"[\x00-\x1f]", "", original_name)[:240] or f"source{extension}"
        paused = db.processing_paused()
        now = db.utc_now()
        db.create_video(
            {
                "id": video_id,
                "filename": safe_name,
                "path": str(final_path),
                "sha256": sha256,
                "size_bytes": size,
                "min_clip_seconds": min_clip_seconds,
                "max_clip_seconds": max_clip_seconds,
                "status": "paused" if paused else "queued",
                "rights_attested": True,
                "created_at": now,
                "updated_at": now,
            }
        )
        if not paused:
            background_tasks.add_task(run_serial_job, video_id)
        return _video_public(db.get_video(video_id))
    except HTTPException:
        partial_path.unlink(missing_ok=True)
        if final_path.exists() and not db.get_video(video_id):
            final_path.unlink(missing_ok=True)
        raise
    except sqlite3.IntegrityError:
        partial_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        duplicate = db.get_video_by_hash(digest.hexdigest())
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail={"message": "This exact source file is already in the library.", "video_id": duplicate["id"]},
            ) from None
        raise HTTPException(status_code=409, detail="A duplicate file prevented this upload from being saved.") from None
    except OSError:
        partial_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Could not store the upload on local disk. Check free space and permissions.") from None
    finally:
        await file.close()


@app.get("/api/videos/{video_id}")
def video_detail(video_id: str) -> dict:
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    try:
        segments = json.loads(video.get("transcript_json") or "[]")
    except json.JSONDecodeError:
        segments = []
    clips = [_clip_public(clip) for clip in db.list_clips() if clip["video_id"] == video_id]
    return {
        **_video_public(video),
        "transcript_text": video.get("transcript_text", ""),
        "transcript_segments": segments,
        "clips": clips,
        "events": db.events_for_video(video_id),
    }


@app.get("/api/videos/{video_id}/source")
def source_video(video_id: str) -> FileResponse:
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    path = _safe_file_path(video.get("path"), "Source video")
    media_type = mimetypes.guess_type(video.get("filename", ""))[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=video.get("filename", "source-video"))


@app.post("/api/videos/{video_id}/retry", status_code=202)
def retry_video(
    video_id: str,
    background_tasks: BackgroundTasks,
    body: RetryRequest | None = None,
) -> dict:
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video["status"] not in {"needs_setup", "failed", "no_highlights", "paused"}:
        raise HTTPException(status_code=409, detail="This video is not in a retryable state.")
    if video["status"] == "no_highlights" and body is None:
        raise HTTPException(status_code=400, detail="Provide a shorter clip-length range to retry a batch with no candidates.")
    if body and (body.min_clip_seconds is not None or body.max_clip_seconds is not None):
        minimum = body.min_clip_seconds if body.min_clip_seconds is not None else float(video["min_clip_seconds"])
        maximum = body.max_clip_seconds if body.max_clip_seconds is not None else float(video["max_clip_seconds"])
        if maximum < minimum:
            raise HTTPException(status_code=400, detail="Maximum clip length cannot be shorter than minimum.")
        db.update_video(video_id, min_clip_seconds=minimum, max_clip_seconds=maximum)
    if db.processing_paused():
        db.update_video(video_id, status="paused", error=None)
        return _video_public(db.get_video(video_id))
    db.update_video(video_id, status="queued", error=None)
    background_tasks.add_task(run_serial_job, video_id)
    return _video_public(db.get_video(video_id))


@app.get("/api/clips")
def clips(status: str | None = None) -> list[dict]:
    valid_statuses = {"needs_review", "approved", "rejected", "scheduled_local", "published"}
    if status and status not in valid_statuses:
        raise HTTPException(status_code=400, detail="Unknown clip status")
    return [_clip_public(clip) for clip in db.list_clips(status)]


@app.get("/api/clips/{clip_id}")
def clip_detail(clip_id: str) -> dict:
    clip = db.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    return _clip_public(clip)


@app.patch("/api/clips/{clip_id}")
def update_clip(clip_id: str, body: ClipUpdate) -> dict:
    clip = db.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if clip["status"] in {"rejected", "published"}:
        raise HTTPException(status_code=409, detail="Rejected or published clips cannot be edited here.")
    updates: dict = {}
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title cannot be empty.")
        updates["title"] = title
    if body.caption is not None:
        caption = body.caption.strip()
        if len(caption.encode("utf-8")) > 5000:
            raise HTTPException(status_code=400, detail="Caption exceeds 5,000 UTF-8 bytes.")
        updates["caption"] = caption
    if body.hashtags is not None:
        cleaned = []
        for tag in body.hashtags:
            normalized = tag.strip().lstrip("#")
            if normalized:
                cleaned.append("#" + re.sub(r"[^\w]", "", normalized)[:40])
        updates["hashtags_json"] = json.dumps(list(dict.fromkeys(cleaned))[:12], ensure_ascii=False)
    db.update_clip(clip_id, **updates)
    return _clip_public(db.get_clip(clip_id))


@app.post("/api/clips/{clip_id}/review")
def review_clip(clip_id: str, body: ReviewRequest) -> dict:
    clip = db.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if body.action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="Action must be approve or reject.")
    if clip["status"] in {"scheduled_local", "published"}:
        raise HTTPException(status_code=409, detail="A planned or published clip cannot be reviewed again.")
    db.update_clip(clip_id, status="approved" if body.action == "approve" else "rejected")
    return _clip_public(db.get_clip(clip_id))


@app.post("/api/clips/{clip_id}/schedule")
def schedule_clip(clip_id: str, body: ScheduleRequest) -> dict:
    if body.platform != "youtube":
        raise HTTPException(status_code=400, detail="This MVP only supports a YouTube Shorts local calendar plan.")
    try:
        when = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Provide a valid ISO 8601 date and time.") from None
    if when.tzinfo is None:
        raise HTTPException(status_code=400, detail="Include a timezone offset in the scheduled time.")
    if when.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Choose a time in the future.")
    try:
        schedule, created = db.create_or_update_schedule(
            clip_id,
            body.platform,
            when.astimezone(timezone.utc).isoformat(timespec="minutes"),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Clip not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {
        "id": schedule["id"],
        "clip_id": schedule["clip_id"],
        "platform": schedule["platform"],
        "scheduled_at": schedule["scheduled_at"],
        "status": "local_only",
        "created": created,
        "publisher_connected": False,
        "message": "Saved to this app's calendar only. YouTube publishing is not connected.",
    }


@app.get("/api/events")
def events(limit: int = 100) -> list[dict]:
    return db.recent_events(limit)


@app.get("/api/calendar")
def calendar() -> list[dict]:
    output = []
    for item in db.list_schedules():
        output.append(
            {
                "id": item["id"],
                "clip_id": item["clip_id"],
                "platform": item["platform"],
                "scheduled_at": item["scheduled_at"],
                "status": item["status"],
                "title": item["title"],
                "video_filename": item["video_filename"],
                "thumbnail_url": f"/api/clips/{item['clip_id']}/thumbnail" if item.get("thumbnail_path") else None,
            }
        )
    return output


@app.delete("/api/calendar/{schedule_id}", status_code=204)
def unschedule(schedule_id: str) -> None:
    if not db.cancel_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Calendar item not found")
    return None


@app.get("/api/analytics")
def analytics() -> dict:
    return {
        "counts": db.analytics_summary(),
        "metrics": db.metrics_for_dashboard(),
        "available": False,
        "note": "Performance analytics are not connected. The dashboard will show real YouTube metrics only after an authorized Analytics API integration is added.",
    }


@app.get("/api/usage")
def usage() -> dict:
    summary = db.analytics_summary()
    output_bytes = 0
    clip_root = db.data_dir() / "clips"
    if clip_root.exists():
        output_bytes = sum(path.stat().st_size for path in clip_root.rglob("*") if path.is_file())
    return {
        "source_bytes": summary["source_bytes"],
        "output_bytes": output_bytes,
        "video_count": summary["videos"],
        "candidate_count": summary["candidates"],
        "estimated_cloud_api_cost": 0,
        "cost_note": "This local-only prototype calls no paid AI, storage, or publishing API. Electricity, device wear, and network costs are not included.",
    }


@app.get("/api/settings")
def settings() -> dict:
    return {
        "processing_paused": db.processing_paused(),
        "publishing_enabled": False,
        "publisher": "not_connected",
        "platform": "youtube_shorts",
        "default_min_clip_seconds": 20,
        "default_max_clip_seconds": 45,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_source_duration_seconds": MAX_SOURCE_DURATION_SECONDS,
        "storage_directory": (Path(os.environ.get("CLIPPER_DATA_DIR", "data")).name or "private folder") + "/",
    }


@app.patch("/api/settings")
def update_settings(body: SettingsUpdate, background_tasks: BackgroundTasks) -> dict:
    db.set_processing_paused(body.processing_paused)
    if not body.processing_paused:
        for video_id in db.paused_video_ids():
            db.update_video(video_id, status="queued", error=None)
            background_tasks.add_task(run_serial_job, video_id)
    return settings()


@app.get("/api/clips/{clip_id}/video")
def clip_video(clip_id: str) -> FileResponse:
    clip = db.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    path = _safe_file_path(clip.get("clip_path"), "Rendered clip")
    return FileResponse(path, media_type="video/mp4", filename=f"{clip_id}.mp4")


@app.get("/api/clips/{clip_id}/subtitles")
def clip_subtitles(clip_id: str) -> FileResponse:
    clip = db.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    path = _safe_file_path(clip.get("subtitle_path"), "Subtitle")
    return FileResponse(path, media_type="application/x-subrip", filename=f"{clip_id}.srt")


@app.get("/api/clips/{clip_id}/thumbnail")
def clip_thumbnail(clip_id: str) -> FileResponse:
    clip = db.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    path = _safe_file_path(clip.get("thumbnail_path"), "Cover image")
    return FileResponse(path, media_type="image/jpeg", filename=f"{clip_id}-cover.jpg")


@app.get("/api/clips/{clip_id}/export")
def export_clip(clip_id: str) -> FileResponse:
    clip = db.get_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    video_path = _safe_file_path(clip.get("clip_path"), "Rendered clip")
    subtitle_path = _safe_file_path(clip.get("subtitle_path"), "Subtitle")
    export_dir = db.data_dir() / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    zip_path = export_dir / f"{clip_id}-{uuid.uuid4().hex[:8]}.zip"
    metadata = {
        "title": clip["title"],
        "description": clip["caption"],
        "hashtags": clip["hashtags"],
        "source_video": clip["video_filename"],
        "source_start_seconds": clip["start_seconds"],
        "source_end_seconds": clip["end_seconds"],
        "duration_seconds": clip["duration_seconds"],
        "selection_score": clip["score"],
        "review_status": clip["status"],
        "publishing_note": "This export has not been uploaded to YouTube.",
    }
    # MP4 is already compressed; avoid spending CPU re-compressing large media.
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.write(video_path, "clip.mp4")
        archive.write(subtitle_path, "captions.srt")
        if clip.get("thumbnail_path") and Path(clip["thumbnail_path"]).is_file():
            archive.write(clip["thumbnail_path"], "cover.jpg")
        archive.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"clipline-{clip_id}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(_request, exc: Exception) -> JSONResponse:
    # Do not send stack traces, local paths, or environment values to the browser.
    import logging

    logging.getLogger("clipper.api").exception("Unhandled API error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Unexpected local server error. Check the terminal log."})
