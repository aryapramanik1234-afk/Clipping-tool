"""Sequential local media pipeline: probe -> transcribe -> rank -> render."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path

from . import db
from .highlights import Segment, find_highlights, suggested_metadata
from .rendering import MediaError, ToolUnavailable, probe_video, render_clip, write_srt


logger = logging.getLogger("clipper.pipeline")
MAX_SOURCE_DURATION_SECONDS = 8 * 60 * 60


class SetupRequired(ToolUnavailable):
    pass


@lru_cache(maxsize=1)
def _whisper_model():
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SetupRequired(
            "Local transcription is not installed. Install the optional AI dependencies, then retry."
        ) from exc
    model_name = os.environ.get("CLIPPER_WHISPER_MODEL", "small")
    device = os.environ.get("CLIPPER_WHISPER_DEVICE", "cpu")
    compute_type = os.environ.get("CLIPPER_WHISPER_COMPUTE_TYPE", "int8" if device == "cpu" else "float16")
    try:
        return WhisperModel(model_name, device=device, compute_type=compute_type)
    except Exception as exc:
        raise SetupRequired(
            "The local speech model could not be loaded. Check available memory and model settings, then retry."
        ) from exc


def transcribe_video(path: Path) -> list[Segment]:
    model = _whisper_model()
    try:
        raw_segments, _info = model.transcribe(str(path), vad_filter=True, beam_size=5)
        segments = [
            Segment(float(item.start), float(item.end), str(item.text).strip())
            for item in raw_segments
            if str(item.text).strip()
        ]
    except Exception as exc:
        raise MediaError("Local speech recognition could not transcribe this file.") from exc
    return segments


def _paused(video_id: str) -> bool:
    if db.processing_paused():
        db.update_video(video_id, status="paused", error=None)
        return True
    return False


def _stable_clip_id(video_id: str, start: float, end: float) -> str:
    value = f"{video_id}:{start:.3f}:{end:.3f}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def process_video(video_id: str) -> None:
    """Run one upload safely in a FastAPI background task."""
    video = db.get_video(video_id)
    if not video:
        return
    source = Path(video["path"])

    try:
        if _paused(video_id):
            return
        db.update_video(video_id, status="validating", error=None)
        media = probe_video(source)
        if media["duration_seconds"] > MAX_SOURCE_DURATION_SECONDS:
            raise MediaError("This MVP accepts source videos up to 8 hours long.")
        if not media["has_audio"]:
            raise MediaError("No audio track was found. Add an audio track or choose a spoken video.")
        db.update_video(
            video_id,
            status="transcribing",
            duration_seconds=media["duration_seconds"],
            width=media["width"],
            height=media["height"],
            error=None,
        )

        if _paused(video_id):
            return
        segments = transcribe_video(source)
        if not segments:
            raise MediaError("No speech was detected. Check the audio or provide a clearer source video.")
        transcript_text = " ".join(segment.text for segment in segments)
        transcript_json = json.dumps(
            [{"start": item.start, "end": item.end, "text": item.text} for item in segments],
            ensure_ascii=False,
        )
        db.update_video(
            video_id,
            status="finding_highlights",
            transcript_json=transcript_json,
            transcript_text=transcript_text,
            error=None,
        )

        if _paused(video_id):
            return
        candidates = find_highlights(
            segments,
            min_duration=float(video["min_clip_seconds"]),
            max_duration=float(video["max_clip_seconds"]),
            max_clips=4,
        )
        if not candidates:
            db.update_video(
                video_id,
                status="no_highlights",
                error="No complete transcript windows fit the chosen clip length. Try a shorter minimum duration or review the transcript.",
            )
            return

        db.update_video(video_id, status="rendering", error=None)
        output_rows: list[dict] = []
        root = db.data_dir() / "clips" / video_id
        for candidate in candidates:
            if _paused(video_id):
                return
            clip_id = _stable_clip_id(video_id, candidate["start"], candidate["end"])
            clip_dir = root / clip_id
            mp4_path = clip_dir / "clip.mp4"
            srt_path = clip_dir / "captions.srt"
            thumbnail_path = clip_dir / "cover.jpg"
            clip_dir.mkdir(parents=True, exist_ok=True)
            write_srt(segments, candidate["start"], candidate["end"], srt_path)
            render_clip(
                source,
                mp4_path,
                srt_path,
                thumbnail_path,
                candidate["start"],
                candidate["end"],
            )
            metadata = suggested_metadata(candidate["text"])
            output_rows.append(
                {
                    "id": clip_id,
                    "start_seconds": candidate["start"],
                    "end_seconds": candidate["end"],
                    "duration_seconds": candidate["duration"],
                    "score": candidate["score"],
                    "text": candidate["text"],
                    "title": metadata["title"],
                    "caption": metadata["caption"],
                    "hashtags": metadata["hashtags"],
                    "clip_path": str(mp4_path),
                    "subtitle_path": str(srt_path),
                    "thumbnail_path": str(thumbnail_path),
                }
            )

        db.create_clips(video_id, output_rows)
        db.update_video(video_id, status="ready", error=None)
    except SetupRequired as exc:
        db.update_video(video_id, status="needs_setup", error=str(exc))
    except ToolUnavailable as exc:
        db.update_video(video_id, status="needs_setup", error=str(exc))
    except MediaError as exc:
        db.update_video(video_id, status="failed", error=str(exc))
    except Exception as exc:  # Keep job state visible even when an unexpected tool error occurs.
        logger.exception("Unexpected processing failure for video %s", video_id)
        db.update_video(video_id, status="failed", error="Unexpected processing error. Check the server log and retry.")
