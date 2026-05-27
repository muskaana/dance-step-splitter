"""Music beat detection via librosa.

Pipes a video's audio track through ffmpeg into a mono PCM stream, then runs
librosa's beat tracker to estimate tempo (BPM) and per-beat timestamps in
seconds. The output is consumed by `segmenter.snap_to_beats` to nudge
motion-derived segment boundaries onto the nearest beat — and is also stored
on the library entry for future use (count-voice tempo, timeline overlay).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class BeatResult:
    bpm: float                 # estimated tempo, beats per minute
    beat_times: list[float]    # absolute timestamps in seconds, one per beat
    duration: float            # total audio duration in seconds


# Mono, 22.05 kHz is the librosa default and is plenty for beat tracking.
# Lower sample rate => smaller in-memory buffer + faster downstream FFTs.
_SAMPLE_RATE = 22050


def _read_audio_mono(
    video_path: str | Path,
    *,
    crop_start: float | None = None,
    crop_end: float | None = None,
) -> np.ndarray:
    """Read the video's audio track as a mono float32 PCM stream via ffmpeg.

    Avoids pulling in librosa's own loader (which would require a working
    soundfile/audioread backend that understands MP4) and gives us cheap
    crop-aware decoding for free.
    """
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if crop_start is not None and crop_start > 0:
        cmd += ["-ss", f"{crop_start:.3f}"]
    cmd += ["-i", str(video_path)]
    if crop_end is not None and crop_start is not None and crop_end > crop_start:
        cmd += ["-t", f"{crop_end - crop_start:.3f}"]
    elif crop_end is not None and crop_start is None:
        cmd += ["-t", f"{crop_end:.3f}"]
    cmd += [
        "-vn",                 # drop video
        "-ac", "1",            # mono
        "-ar", str(_SAMPLE_RATE),
        "-f", "f32le",         # 32-bit float little-endian PCM
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed extracting audio from {video_path}: "
            f"{proc.stderr.decode(errors='replace')[:500]}"
        )
    return np.frombuffer(proc.stdout, dtype=np.float32)


def detect_beats(
    video_path: str | Path,
    *,
    crop_start: float | None = None,
    crop_end: float | None = None,
) -> BeatResult | None:
    """Detect beats in `video_path`'s audio track.

    Returns None if the audio is silent, too short to detect beats, or
    librosa can't find a stable tempo. Callers should treat None as
    "skip beat-snapping for this clip" rather than an error.

    `crop_start` / `crop_end` mirror the segmenter's crop semantics — beat
    times are reported in *absolute* video time (i.e. crop_start is added
    back) so they line up with segment boundaries.
    """
    # Import lazily so the rest of the app still starts if librosa is missing
    # for any reason — beat detection is opt-in, not load-bearing.
    import librosa

    y = _read_audio_mono(video_path, crop_start=crop_start, crop_end=crop_end)
    if y.size < _SAMPLE_RATE * 2:
        # Less than ~2 seconds of audio — beat tracking is meaningless.
        return None
    duration = y.size / _SAMPLE_RATE

    # Belt-and-suspenders: silence guard. RMS < ~-50 dBFS = effectively muted.
    if float(np.sqrt(np.mean(np.square(y)))) < 0.001:
        return None

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=_SAMPLE_RATE)
    if beat_frames is None or len(beat_frames) < 4:
        # Need at least a few beats to be useful; otherwise the tracker
        # probably latched onto noise.
        return None

    # librosa returns numpy scalars; coerce to plain Python types for JSON.
    bpm = float(np.atleast_1d(tempo)[0])
    crop_offset = float(crop_start or 0)
    times = librosa.frames_to_time(beat_frames, sr=_SAMPLE_RATE)
    beat_times = [float(t) + crop_offset for t in times]

    return BeatResult(bpm=bpm, beat_times=beat_times, duration=duration)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Detect beats in a video file.")
    parser.add_argument("video", help="Path to video file")
    parser.add_argument("--start", type=float, default=None)
    parser.add_argument("--end", type=float, default=None)
    args = parser.parse_args()

    result = detect_beats(args.video, crop_start=args.start, crop_end=args.end)
    if result is None:
        print("No beats detected.")
    else:
        print(f"BPM: {result.bpm:.1f}  beats: {len(result.beat_times)}  "
              f"duration: {result.duration:.1f}s")
        print(
            "first 10 beats:",
            ", ".join(f"{t:.2f}s" for t in result.beat_times[:10]),
        )
