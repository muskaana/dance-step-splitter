"""Segment a video by audio silence — for singing/music practice.

Uses ffmpeg's `silencedetect` audio filter to find quiet gaps in the track,
then treats each silence as a candidate boundary between verses / phrases.
Reuses the merge + split helpers from `segmenter.py` so min/max segment
durations behave identically to the pose-driven segmenter.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

from . import segmenter

# Loud-floor threshold for silencedetect — anything below this is "silence".
# -30 dB matches typical mixing for vocal/musical silences without picking up
# breath noise.
SILENCE_DB = -30
# Minimum gap that counts as a real boundary candidate (seconds).
SILENCE_DURATION = 0.4

_SILENCE_LINE_RE = re.compile(
    r"silence_(?:start|end):\s*(?P<t>-?\d+(?:\.\d+)?)"
)


def _probe_duration(path: Path) -> float:
    """Quick wrapper — same as the one in main, kept here so the module is
    self-contained for callers that import only audio_segmenter."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, check=False,
        )
        return float((result.stdout or "").strip())
    except Exception:
        return 0.0


def _detect_silences(
    video_path: Path,
    crop_start: Optional[float],
    crop_end: Optional[float],
) -> list[tuple[float, float]]:
    """Return list of (silence_start, silence_end) tuples in seconds,
    expressed in the *cropped* timeline (i.e. silence_start=0 means the
    silence began at the crop_start boundary)."""
    cmd: list[str] = ["ffmpeg", "-hide_banner", "-nostats"]
    if crop_start is not None and crop_start > 0:
        cmd.extend(["-ss", f"{crop_start:.3f}"])
    if crop_end is not None and crop_end > 0:
        cmd.extend(["-to", f"{crop_end:.3f}"])
    cmd.extend(
        [
            "-i", str(video_path),
            "-af", f"silencedetect=noise={SILENCE_DB}dB:duration={SILENCE_DURATION}",
            "-f", "null", "-",
        ]
    )
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    starts: list[float] = []
    ends: list[float] = []
    for line in (result.stderr or "").splitlines():
        if "silence_start" in line:
            m = _SILENCE_LINE_RE.search(line)
            if m:
                starts.append(max(0.0, float(m.group("t"))))
        elif "silence_end" in line:
            m = _SILENCE_LINE_RE.search(line)
            if m:
                ends.append(max(0.0, float(m.group("t"))))
    # Pair up starts and ends in order. Lengths may mismatch by 1 if the
    # clip ends mid-silence; clamp to the shorter list.
    n = min(len(starts), len(ends))
    return list(zip(starts[:n], ends[:n]))


def segment_by_audio(
    video_path: Path,
    crop_start: Optional[float] = None,
    crop_end: Optional[float] = None,
    min_segment_duration: float = 8.0,
    max_segment_duration: float = 30.0,
) -> list[segmenter.StepSegment]:
    """Split `video_path`'s audio track into verse/phrase segments.

    Returns `StepSegment` instances with timestamps in the ORIGINAL video's
    timeline (i.e. absolute seconds), matching how `segmenter.segment_steps`
    already returns its boundaries — so downstream code (sequence_builder,
    library persistence, frontend) treats both segmenters interchangeably.
    """
    cs = float(crop_start) if crop_start is not None else 0.0
    if crop_end is not None and crop_end > 0:
        ce = float(crop_end)
    else:
        ce = _probe_duration(video_path)
    if ce <= cs:
        return []

    silences = _detect_silences(video_path, crop_start, crop_end)

    # Build boundaries inside [cs, ce]. The silence midpoint is a good
    # break point — it sits in the actual gap rather than chopping mid-note.
    boundaries: list[float] = [cs]
    for s, e in silences:
        # silences are in the cropped timeline → add cs to get absolute time.
        mid = cs + (s + e) / 2.0
        if cs < mid < ce:
            boundaries.append(mid)
    boundaries.append(ce)
    boundaries = sorted(set(boundaries))

    # Convert consecutive boundaries to StepSegments with provisional indices.
    raw: list[segmenter.StepSegment] = []
    for i in range(len(boundaries) - 1):
        a, b = boundaries[i], boundaries[i + 1]
        if b <= a:
            continue
        raw.append(
            segmenter.StepSegment(
                index=len(raw),
                start_time=float(a),
                end_time=float(b),
                start_frame=int(a * 30),   # nominal — only used for display
                end_frame=int(b * 30),
                duration=float(b - a),
                peak_motion=0.0,
            )
        )

    # Reuse the dance segmenter's min/max enforcers so behaviour is identical
    # to the existing pipeline.
    merged = segmenter._merge_short_segments(raw, min_segment_duration)
    # The split helper needs a motion DataFrame; for audio segments we just
    # split at the midpoint of any over-long stretch.
    out: list[segmenter.StepSegment] = []
    for s in merged:
        if s.duration <= max_segment_duration + segmenter.DURATION_TOLERANCE:
            out.append(s)
            continue
        # Recursively halve until each piece fits.
        stack = [s]
        while stack:
            cur = stack.pop()
            if cur.duration <= max_segment_duration + segmenter.DURATION_TOLERANCE:
                out.append(cur)
                continue
            mid = (cur.start_time + cur.end_time) / 2.0
            left = segmenter.StepSegment(
                index=0,
                start_time=cur.start_time,
                end_time=mid,
                start_frame=int(cur.start_time * 30),
                end_frame=int(mid * 30),
                duration=mid - cur.start_time,
                peak_motion=0.0,
            )
            right = segmenter.StepSegment(
                index=0,
                start_time=mid,
                end_time=cur.end_time,
                start_frame=int(mid * 30),
                end_frame=int(cur.end_time * 30),
                duration=cur.end_time - mid,
                peak_motion=0.0,
            )
            # Push right first so left is processed next (preserves order).
            stack.extend([right, left])

    for i, s in enumerate(out):
        s.index = i
    return out
