from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from app import db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("CLIPPER_DATA_DIR")
        os.environ["CLIPPER_DATA_DIR"] = self.temp.name
        db.init_db()
        now = db.utc_now()
        db.create_video({
            "id": "video-1", "filename": "authorized.mp4", "path": str(Path(self.temp.name) / "authorized.mp4"),
            "sha256": "abc123", "size_bytes": 4096, "min_clip_seconds": 20, "max_clip_seconds": 45,
            "status": "ready", "rights_attested": True, "created_at": now, "updated_at": now,
        })
        db.create_clips("video-1", [{
            "id": "clip-1", "start_seconds": 10, "end_seconds": 35, "duration_seconds": 25,
            "score": 71, "title": "A truthful title", "caption": "Original words", "hashtags": ["#Shorts"],
            "text": "Original words that make a complete and useful point.",
            "clip_path": str(Path(self.temp.name) / "clip.mp4"),
            "subtitle_path": str(Path(self.temp.name) / "clip.srt"),
            "thumbnail_path": None,
        }])

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("CLIPPER_DATA_DIR", None)
        else:
            os.environ["CLIPPER_DATA_DIR"] = self.previous
        self.temp.cleanup()

    def test_schedule_is_idempotent_per_clip_and_platform(self):
        db.update_clip("clip-1", status="approved")
        first, created_first = db.create_or_update_schedule("clip-1", "youtube", "2030-01-02T10:00+00:00")
        second, created_second = db.create_or_update_schedule("clip-1", "youtube", "2030-01-03T10:00+00:00")
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["scheduled_at"], "2030-01-03T10:00+00:00")
        self.assertEqual(len(db.list_schedules()), 1)
        self.assertEqual(db.get_clip("clip-1")["status"], "scheduled_local")

    def test_schedule_requires_approval(self):
        with self.assertRaises(ValueError):
            db.create_or_update_schedule("clip-1", "youtube", "2030-01-02T10:00+00:00")

    def test_pause_setting_round_trips(self):
        self.assertFalse(db.processing_paused())
        db.set_processing_paused(True)
        self.assertTrue(db.processing_paused())
        db.set_processing_paused(False)
        self.assertFalse(db.processing_paused())

    def test_processing_and_review_events_are_persisted(self):
        db.update_video("video-1", status="transcribing")
        db.update_clip("clip-1", status="approved")
        events = db.events_for_video("video-1")
        event_types = {event["event_type"] for event in events}
        self.assertIn("source_uploaded", event_types)
        self.assertIn("clip_created", event_types)
        self.assertIn("video_status", event_types)
        self.assertIn("clip_status", event_types)

    def test_duplicate_transcript_is_not_inserted_twice(self):
        created = db.create_clips("video-1", [{
            "id": "clip-duplicate", "start_seconds": 11, "end_seconds": 36, "duration_seconds": 25,
            "score": 60, "title": "Another title", "caption": "Another caption", "hashtags": [],
            "text": "Original words that make a complete and useful point.",
        }])
        self.assertEqual(created, 0)
        self.assertEqual(len(db.list_clips()), 1)


if __name__ == "__main__":
    unittest.main()
