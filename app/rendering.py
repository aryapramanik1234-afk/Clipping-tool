"""FFmpeg/FFprobe helpers for the local clipping worker."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from .highlights import Segment, clean_segments


class MediaError(RuntimeError):
    pass


class ToolUnavailable(MediaError):
    pass


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def probe_video(path: Path) -> dict:
    if not command_exists("ffprobe"):
        raise ToolUnavailable("FFprobe is missing. Install FFmpeg/FFprobe, then retry this video.")
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise MediaError("The uploaded file could not be read as a video. Check the format and try again.")
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        duration = float(payload.get("format", {}).get("duration", 0))
    except (ValueError, TypeError, StopIteration, json.JSONDecodeError):
        raise MediaError("The video is missing valid duration or video-stream metadata.") from None
    if duration <= 0:
        raise MediaError("The video duration could not be determined.")
    return {
        "duration_seconds": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def _srt_timestamp(seconds: float) -> str:
    millis_total = max(0, round(seconds * 1000))
    hours, remainder = divmod(millis_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(segments: Iterable[Segment | dict], clip_start: float, clip_end: float, destination: Path) -> None:
    normalized = clean_segments(segments)
    cues: list[str] = []
    cue_index = 1
    for segment in normalized:
        start = max(clip_start, segment.start)
        end = min(clip_end, segment.end)
        text = segment.text.replace("-->", "→").strip()
        if end <= start or not text:
            continue
        cues.append(
            f"{cue_index}\n{_srt_timestamp(start - clip_start)} --> {_srt_timestamp(end - clip_start)}\n{text}\n"
        )
        cue_index += 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(cues), encoding="utf-8")


def _escape_filter_path(path: Path) -> str:
    # FFmpeg filter syntax uses ':' and quotes as delimiters even when the full
    # command is passed without a shell. Escape the few characters a path may contain.
    value = str(path.resolve())
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def _run(command: list[str], *, timeout: int = 7200) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise MediaError(result.stderr.strip()[-1200:] or "FFmpeg could not render this clip.")
    return result


def render_clip(
    source: Path,
    output: Path,
    subtitle_file: Path,
    thumbnail: Path,
    start: float,
    end: float,
) -> None:
    """Render a centered 9:16 H.264/AAC MP4, SRT sidecar, and cover image.

    The crop intentionally stays centered in this first release; it does not
    claim to detect or follow a face/speaker. Subtitle burn-in is attempted when
    FFmpeg has libass; the SRT sidecar remains available if not.
    """
    if not command_exists("ffmpeg"):
        raise ToolUnavailable("FFmpeg is missing. Install FFmpeg/FFprobe, then retry this video.")
    duration = end - start
    if duration <= 0:
        raise MediaError("Clip end time must be later than its start time.")
    output.parent.mkdir(parents=True, exist_ok=True)
    thumbnail.parent.mkdir(parents=True, exist_ok=True)

    crop_filter = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,format=yuv420p"
    subtitles_filter = (
        f"{crop_filter},subtitles='{_escape_filter_path(subtitle_file)}':"
        "force_style='FontName=DejaVu Sans,FontSize=22,Outline=2,Shadow=0,MarginV=100'"
    )
    prefix = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
        "-i", str(source), "-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
    ]
    encoding = [
        "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-map_metadata", "-1",
        "-movflags", "+faststart", str(output),
    ]

    try:
        _run(prefix + ["-vf", subtitles_filter] + encoding)
    except MediaError as exc:
        message = str(exc).lower()
        if "subtitles" not in message and "libass" not in message and "no such filter" not in message:
            raise
        # A minimal FFmpeg package can encode video but not burn captions. Keep
        # the SRT sidecar and still render the clip instead of failing the job.
        _run(prefix + ["-vf", crop_filter] + encoding)

    thumbnail_filter = "scale=540:960:force_original_aspect_ratio=increase,crop=540:960,setsar=1"
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss",
            f"{start + duration / 2:.3f}", "-i", str(source), "-frames:v", "1",
            "-vf", thumbnail_filter, "-q:v", "2", str(thumbnail),
        ],
        timeout=300,
    )
