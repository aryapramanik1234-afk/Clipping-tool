# Clipline — local-first clipping studio

A local prototype for an approval-first YouTube Shorts workflow. It accepts **authorized video uploads**, checks them with FFprobe, transcribes locally with optional `faster-whisper`, ranks timestamp windows with transparent transcript heuristics, and renders vertical H.264 clips with SRT subtitles. The dashboard lets you edit metadata, approve/reject candidates, export a bundle, and keep **local calendar reminders**.

> **Honest MVP boundary:** there is no OAuth connection, YouTube upload, automatic publishing, or platform analytics in this version. Calendar items are reminders only. Upload approved exports through YouTube Studio until an official publisher is implemented and tested. URL downloading and cloud-folder ingestion are deliberately disabled while source permissions are undecided.

## Quick start

1. Install Python 3.11+, `ffmpeg` and `ffprobe` (usually installed together). Get FFmpeg from [ffmpeg.org/download.html](https://ffmpeg.org/download.html). Verify with `ffmpeg -version` and `ffprobe -version`.
2. In the repository folder:

   ```bash
   python -m venv .venv
   # macOS / Linux
   source .venv/bin/activate
   # Windows PowerShell: .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   pip install -r requirements-ai.txt
   ```

   The second install adds local speech-to-text. The selected model is downloaded the first time a video is processed, so allow time and disk space for that first run. The default is the `small` model on CPU; for a lighter/slower-accuracy tradeoff, set `CLIPPER_WHISPER_MODEL=base` before starting.
3. Start the app:

   ```bash
   uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

4. Open `http://127.0.0.1:8000` in your browser. Upload one video you own or are authorized to edit, confirm the rights checkbox, choose a clip-length range, then monitor the Library / Review queue.
5. Review the full clip and transcript, correct the title/caption/hashtags, approve or reject, then download the **Export bundle** (MP4, SRT, cover image, and metadata JSON). Publish manually with YouTube Studio.

If FFmpeg or `faster-whisper` is missing, the upload is retained and marked **Setup needed** instead of pretending to process. Install the missing tool and press **Retry**.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests cover heuristic ranking bounds, clip de-duplication, approval-gated calendar plans, and the pause switch. No live YouTube upload is tested because no account credentials or publishing adapter are configured.

## Where files go

By default, the SQLite database, uploaded sources, transcripts, generated clips, subtitles, covers, and temporary exports are in `./data/`. The folder is git-ignored. Set `CLIPPER_DATA_DIR` to a private path with enough disk space to move the entire local workspace. Back it up securely; it may contain unpublished or sensitive footage.

## Read next

- [Architecture, tool comparison, setup details, operating cost, testing/reliability plan, and expansion stages](docs/ARCHITECTURE.md)
- [Official YouTube API and Shorts documentation](docs/ARCHITECTURE.md#official-platform-references)

## Important limitations

- The center crop is fixed; speaker/face tracking is not implemented. Inspect every crop.
- Subtitle alignment uses speech-recognition segments. SRT is included; burn-in depends on FFmpeg having the `libass` subtitle filter.
- Highlight scores are simple, explainable text signals—not a claim or prediction of virality. Metadata suggestions reuse transcript words and must be edited.
- B-roll, zooms, transitions, URL downloaders, cloud storage, API-based posting, retrying external uploads, and analytics ingestion are not implemented.
- The single-process FastAPI background worker is suitable for a personal prototype, not a durable production queue. See the architecture guide before exposing it to other users or the public internet.
