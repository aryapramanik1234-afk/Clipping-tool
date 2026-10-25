"""Transcript-only, explainable highlight selection for the local MVP.

These scores rank candidate windows for human review. They are not virality or
performance predictions. Boundaries are kept at transcription-segment edges so
we do not deliberately cut through a recognized word.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from collections import Counter
from typing import Iterable


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


_HOOK_PHRASES = (
    "here's the thing",
    "here is the thing",
    "the reason is",
    "what happened next",
    "the biggest mistake",
    "the truth is",
    "what if",
    "the problem is",
    "the surprising part",
)
_HOOK_WORDS = {"why", "how", "what", "imagine", "mistake", "secret", "lesson"}
_VALUE_WORDS = {
    "steps", "step", "tip", "tips", "learned", "lesson", "method", "example",
    "because", "practice", "strategy", "simple", "important", "remember",
}
_SURPRISE_WORDS = {
    "actually", "instead", "unexpected", "surprisingly", "suddenly", "wrong",
    "changed", "difference", "but", "however", "although", "unless",
}
_EMOTION_WORDS = {
    "love", "hate", "fear", "excited", "funny", "wild", "beautiful", "terrible",
    "amazing", "frustrated", "happy", "sad", "shocked", "honestly",
}
_START_CONTEXT_PENALTIES = (
    "and ", "but ", "so ", "because ", "which ", "that ", "then ", "for example, ",
)
_STOPWORDS = {
    "about", "after", "again", "also", "another", "any", "are", "because", "been",
    "being", "between", "both", "but", "can", "could", "did", "does", "doing",
    "down", "during", "each", "for", "from", "have", "having", "here", "hers",
    "him", "his", "how", "into", "its", "just", "like", "more", "most", "much",
    "must", "not", "our", "out", "over", "same", "she", "should", "some", "such",
    "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "those", "through", "too", "under", "until", "very", "was", "were",
    "what", "when", "where", "which", "while", "who", "will", "with", "would",
    "you", "your", "and", "but", "the", "a", "an", "to", "of", "in", "on", "at",
    "it", "is", "i", "we", "he", "be", "as", "if", "or", "by", "do", "my", "me",
    "they", "them", "so", "up", "all", "no", "yes", "well", "yeah", "okay", "ok",
}


def clean_segments(raw_segments: Iterable[Segment | dict]) -> list[Segment]:
    """Normalize provider output and discard empty/invalid segments."""
    cleaned: list[Segment] = []
    for item in raw_segments:
        if isinstance(item, Segment):
            start, end, text = item.start, item.end, item.text
        else:
            start = item.get("start")
            end = item.get("end")
            text = item.get("text", "")
        try:
            start_f, end_f = float(start), float(end)
        except (TypeError, ValueError):
            continue
        text_s = re.sub(r"\s+", " ", str(text)).strip()
        if end_f <= start_f or not text_s:
            continue
        cleaned.append(Segment(max(0.0, start_f), end_f, text_s))
    return sorted(cleaned, key=lambda segment: (segment.start, segment.end))


def _score_text(text: str, duration: float) -> float:
    normalized = text.lower().strip()
    words = re.findall(r"[\w'-]+", normalized)
    if not words or duration <= 0:
        return 0.0

    unique_words = set(words)
    score = 34.0

    # Lightweight signals are deliberately transparent and only used to rank
    # review candidates. Context checks below help surface self-contained clips.
    if "?" in normalized:
        score += 8
    if any(phrase in normalized for phrase in _HOOK_PHRASES):
        score += 12
    if unique_words & _HOOK_WORDS:
        score += 5
    if unique_words & _VALUE_WORDS:
        score += min(12, 4 + 2 * len(unique_words & _VALUE_WORDS))
    if unique_words & _SURPRISE_WORDS:
        score += min(10, 4 + 2 * len(unique_words & _SURPRISE_WORDS))
    if unique_words & _EMOTION_WORDS:
        score += min(9, 3 + 2 * len(unique_words & _EMOTION_WORDS))
    if re.search(r"\b\d+(?:\.\d+)?\b", normalized):
        score += 4
    if normalized.endswith((".", "!", "?", "…")):
        score += 5
    else:
        score -= 7

    # Reward a plausible speech pace without penalizing languages with shorter
    # word forms too heavily. This is a weak tie-breaker, not a quality verdict.
    words_per_second = len(words) / duration
    if 1.4 <= words_per_second <= 3.3:
        score += 5
    elif words_per_second > 4.2:
        score -= 7

    if normalized.startswith(_START_CONTEXT_PENALTIES):
        score -= 9
    if len(words) < 24:
        score -= 8
    if len(words) > 150:
        score -= 6
    return round(max(0.0, min(100.0, score)), 2)


def _similarity(left: str, right: str) -> float:
    left_words = {word for word in re.findall(r"[a-z0-9']+", left.lower()) if word not in _STOPWORDS}
    right_words = {word for word in re.findall(r"[a-z0-9']+", right.lower()) if word not in _STOPWORDS}
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


def find_highlights(
    raw_segments: Iterable[Segment | dict],
    *,
    min_duration: float = 20,
    max_duration: float = 45,
    max_clips: int = 4,
) -> list[dict]:
    """Return ranked, non-redundant timestamp windows from transcript segments."""
    segments = clean_segments(raw_segments)
    if not segments or min_duration <= 0 or max_duration < min_duration or max_clips <= 0:
        return []

    candidates: list[dict] = []
    for start_index, first in enumerate(segments):
        pieces: list[str] = []
        for end_index in range(start_index, len(segments)):
            segment = segments[end_index]
            # Never extend beyond the configured ceiling. Segment boundaries
            # keep recognized speech intact; the small tolerance handles ASR rounding.
            duration = segment.end - first.start
            if duration > max_duration + 0.05:
                break
            pieces.append(segment.text)
            if duration + 0.05 < min_duration:
                continue

            text = " ".join(pieces).strip()
            score = _score_text(text, duration)
            if score < 30:
                continue
            candidates.append(
                {
                    "start": round(first.start, 3),
                    "end": round(segment.end, 3),
                    "duration": round(duration, 3),
                    "score": score,
                    "text": text,
                }
            )

    candidates.sort(key=lambda item: (item["score"], -item["duration"]), reverse=True)
    selected: list[dict] = []
    for candidate in candidates:
        duplicate = False
        for chosen in selected:
            overlap = max(0.0, min(candidate["end"], chosen["end"]) - max(candidate["start"], chosen["start"]))
            shortest = min(candidate["duration"], chosen["duration"])
            overlap_ratio = overlap / shortest if shortest > 0 else 0.0
            if overlap_ratio >= 0.55 or _similarity(candidate["text"], chosen["text"]) >= 0.86:
                duplicate = True
                break
        if not duplicate:
            selected.append(candidate)
            if len(selected) >= max_clips:
                break
    return selected


def suggested_metadata(text: str) -> dict:
    """Create conservative editable metadata using only words in the clip."""
    clean_text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", clean_text)
    first_sentence = sentences[0] if sentences else clean_text
    title = first_sentence.strip(' \t\n\r\"“”')
    if len(title) > 96:
        trimmed = title[:93].rsplit(" ", 1)[0].rstrip(".,;:")
        title = (trimmed or title[:93]) + "…"
    if not title:
        title = "Clip from the original video"

    caption = clean_text[:1800]
    if len(clean_text) > 1800:
        caption = caption.rsplit(" ", 1)[0].rstrip() + "…"

    words = re.findall(r"[a-zA-Z][a-zA-Z0-9'-]{3,}", clean_text.lower())
    keywords = [word for word, _ in Counter(word for word in words if word not in _STOPWORDS).most_common(2)]
    hashtags = ["#Shorts"]
    for word in keywords:
        tag = re.sub(r"[^a-zA-Z0-9]", "", word)
        if tag and ("#" + tag.title()) not in hashtags:
            hashtags.append("#" + tag.title())
    return {"title": title, "caption": caption, "hashtags": hashtags[:3]}
