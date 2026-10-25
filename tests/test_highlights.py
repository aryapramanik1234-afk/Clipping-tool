from __future__ import annotations

import unittest

from app.highlights import Segment, clean_segments, find_highlights, suggested_metadata


class HighlightTests(unittest.TestCase):
    def test_clean_segments_sorts_and_discards_invalid_rows(self):
        result = clean_segments([
            {"start": 5, "end": 6, "text": "  later   phrase "},
            {"start": 2, "end": 1, "text": "bad timing"},
            {"start": 0, "end": 1, "text": "   "},
            {"start": 1, "end": 2, "text": "first phrase"},
        ])
        self.assertEqual([item.text for item in result], ["first phrase", "later phrase"])
        self.assertEqual(result[0].start, 1.0)

    def test_candidates_stay_inside_duration_and_use_segment_boundaries(self):
        segments = []
        for index in range(16):
            start = index * 4
            if index < 8:
                text = "Here is the surprising lesson: three small steps changed how we work today."
            else:
                text = "The useful method is simple, because a clear example makes the idea easier to remember."
            segments.append(Segment(start, start + 4, text))

        candidates = find_highlights(segments, min_duration=20, max_duration=32, max_clips=4)
        self.assertGreaterEqual(len(candidates), 2)
        for candidate in candidates:
            self.assertGreaterEqual(candidate["duration"], 20)
            self.assertLessEqual(candidate["duration"], 32)
            self.assertIn(candidate["start"], [item.start for item in segments])
            self.assertIn(candidate["end"], [item.end for item in segments])
            self.assertGreaterEqual(candidate["score"], 30)

        for index, left in enumerate(candidates):
            for right in candidates[index + 1 :]:
                overlap = max(0, min(left["end"], right["end"]) - max(left["start"], right["start"]))
                self.assertLess(overlap / min(left["duration"], right["duration"]), 0.55)

    def test_invalid_clip_settings_return_no_candidates(self):
        self.assertEqual(find_highlights([Segment(0, 5, "hello")], min_duration=30, max_duration=20), [])

    def test_metadata_uses_original_transcript_words(self):
        text = "Why three simple habits make focused work easier. This is one example."
        result = suggested_metadata(text)
        self.assertTrue(result["title"].startswith("Why three simple habits"))
        self.assertIn("#Shorts", result["hashtags"])
        self.assertLessEqual(len(result["title"]), 96)
        self.assertIn("focused", result["caption"])


if __name__ == "__main__":
    unittest.main()
