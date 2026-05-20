"""Segment a dance video into individual step time-ranges.

Approach — dynamic kinematic thresholding:
  1. Discard pose data outside [crop_start, crop_end].
  2. For each frame, compute the Euclidean speed of the wrist and ankle
     landmarks and sum them into a single motion-energy signal.
  3. Smooth the signal with a rolling average so jitter and single-frame
     dropouts don't masquerade as pauses.
  4. Choose a *dynamic* threshold from the signal itself — by default the
     15th percentile — so the same code works for slow lyrical choreography
     and high-energy hip hop alike.
  5. Find contiguous below-threshold regions ("pause regions"). Use the
     lowest point inside each as a candidate boundary.
  6. Merge segments shorter than `min_segment_duration` (default 2 s) so we
     don't over-slice into micro-fragments.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable, Optional

import numpy as np
import pandas as pd

LIMB_LANDMARKS: tuple[str, ...] = ("left_wrist", "right_wrist", "left_ankle", "right_ankle")


@dataclass
class StepSegment:
    """A single dance step time range."""

    index: int
    start_time: float
    end_time: float
    start_frame: int
    end_frame: int
    duration: float
    peak_motion: float

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wide(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame-indexed wide DataFrame, pivoting from long if needed."""
    if "landmark_name" in df.columns:
        wide = df.pivot_table(
            index=["frame", "timestamp"],
            columns="landmark_name",
            values=["x", "y", "z"],
        )
        wide.columns = [f"{name}_{coord}" for coord, name in wide.columns]
        wide = wide.reset_index().sort_values("frame").reset_index(drop=True)
        wide.attrs.update(df.attrs)
        return wide
    return df


def _crop_dataframe(
    df: pd.DataFrame,
    crop_start: Optional[float],
    crop_end: Optional[float],
) -> pd.DataFrame:
    """Discard rows whose timestamp falls outside [crop_start, crop_end]."""
    if crop_start is None and crop_end is None:
        return df
    mask = pd.Series(True, index=df.index)
    if crop_start is not None:
        mask &= df["timestamp"] >= crop_start
    if crop_end is not None:
        mask &= df["timestamp"] <= crop_end
    out = df[mask].copy()
    out.attrs.update(df.attrs)
    return out


# ---------------------------------------------------------------------------
# Kinematic motion signal
# ---------------------------------------------------------------------------


def compute_motion_signal(
    df: pd.DataFrame,
    landmarks: Iterable[str] = LIMB_LANDMARKS,
    smooth_window: int = 5,
) -> pd.DataFrame:
    """Frame-by-frame Euclidean speed of `landmarks`, summed and smoothed.

    Returns a DataFrame with columns:
        frame, timestamp, velocity, acceleration, motion_energy
    where `motion_energy` is the smoothed sum of per-landmark speeds.
    """
    wide = _wide(df)
    if wide.empty or len(wide) < 2:
        raise ValueError(
            "Not enough pose data to compute motion — the cropped video had "
            "fewer than 2 frames. Check that the crop window isn't too narrow."
        )
    fps = wide.attrs.get("fps", 30.0)
    dt = 1.0 / fps

    speeds: list[np.ndarray] = []
    for name in landmarks:
        cols = [f"{name}_x", f"{name}_y", f"{name}_z"]
        if any(c not in wide.columns for c in cols):
            continue
        coords = wide[cols].to_numpy(dtype=float)
        # If MediaPipe never detected this landmark, every value is NaN —
        # skip it rather than contributing a bogus zero-speed signal.
        if np.all(np.isnan(coords)):
            continue
        coords = pd.DataFrame(coords).ffill(limit=3).bfill(limit=3).to_numpy()
        displacement = np.diff(coords, axis=0, prepend=coords[:1])
        speed = np.linalg.norm(displacement, axis=1) / dt
        # Any remaining NaN (leading frames with no detection) → 0 speed.
        speeds.append(np.nan_to_num(speed, nan=0.0))

    if not speeds:
        raise ValueError(
            "Couldn't compute motion — MediaPipe didn't detect the wrists or "
            "ankles in any frame. Possible causes: the dancer isn't fully in "
            "frame, the crop window misses the actual choreography, the "
            "footage is too dark/blurry, or the lighting/contrast confuses "
            "the pose model. Try a different crop, a clearer video, or the "
            "'heavy' pose model variant."
        )

    motion = np.sum(np.stack(speeds, axis=0), axis=0)
    motion_smoothed = (
        pd.Series(motion)
        .rolling(smooth_window, center=True, min_periods=1)
        .mean()
        .to_numpy()
    )
    acceleration = np.gradient(motion_smoothed, dt)

    out = pd.DataFrame(
        {
            "frame": wide["frame"].to_numpy(),
            "timestamp": wide["timestamp"].to_numpy(),
            "velocity": motion_smoothed,
            "acceleration": acceleration,
            "motion_energy": motion_smoothed,
        }
    )
    out.attrs["fps"] = fps
    return out


# ---------------------------------------------------------------------------
# Boundary detection via dynamic kinematic thresholding
# ---------------------------------------------------------------------------


def find_pause_boundaries(
    motion: pd.DataFrame,
    low_velocity_percentile: float = 15.0,
) -> np.ndarray:
    """Boundary indices = the lowest-velocity frame inside each pause region.

    A "pause region" is a maximal run of consecutive frames whose smoothed
    motion energy sits below the dynamic threshold (the
    `low_velocity_percentile`-th percentile of the energy signal).
    """
    energy = motion["motion_energy"].to_numpy()
    n = energy.size
    if n < 3:
        return np.array([0, max(0, n - 1)])

    threshold = float(np.nanpercentile(energy, low_velocity_percentile))
    below = energy < threshold

    boundaries: list[int] = [0]
    i = 0
    while i < n:
        if below[i]:
            j = i
            while j < n and below[j]:
                j += 1
            # Boundary = the deepest point inside this pause region.
            boundaries.append(i + int(np.argmin(energy[i:j])))
            i = j
        else:
            i += 1
    boundaries.append(n - 1)
    return np.array(sorted(set(boundaries)))


# Duration tolerance around min/max — segments within this slack are kept as-is.
DURATION_TOLERANCE: float = 0.1


def _merge_short_segments(
    segs: list[StepSegment],
    min_duration: float,
) -> list[StepSegment]:
    """Merge any segment shorter than `min_duration` into a neighbor.

    Greedy pass: whenever the current accumulator is still under the
    threshold, absorb the next segment into it. After the pass, if the
    final segment is too short, fold it back into the previous one.
    """
    if not segs:
        return segs

    merged: list[StepSegment] = [segs[0]]
    threshold = min_duration - DURATION_TOLERANCE
    for s in segs[1:]:
        last = merged[-1]
        if last.duration < threshold:
            merged[-1] = StepSegment(
                index=last.index,
                start_time=last.start_time,
                end_time=s.end_time,
                start_frame=last.start_frame,
                end_frame=s.end_frame,
                duration=s.end_time - last.start_time,
                peak_motion=max(last.peak_motion, s.peak_motion),
            )
        else:
            merged.append(s)

    if len(merged) >= 2 and merged[-1].duration < threshold:
        prev, last = merged[-2], merged[-1]
        merged[-2] = StepSegment(
            index=prev.index,
            start_time=prev.start_time,
            end_time=last.end_time,
            start_frame=prev.start_frame,
            end_frame=last.end_frame,
            duration=last.end_time - prev.start_time,
            peak_motion=max(prev.peak_motion, last.peak_motion),
        )
        merged.pop()

    # Re-index after merging so callers get a clean 0..N-1 sequence.
    for i, s in enumerate(merged):
        s.index = i
    return merged


def _split_long_segments(
    segs: list[StepSegment],
    motion: pd.DataFrame,
    max_duration: float,
    min_duration: float,
) -> list[StepSegment]:
    """Recursively split any segment longer than `max_duration` at its
    lowest-energy internal frame, keeping both halves >= `min_duration`.

    Cut candidates are restricted to the window
        [seg.start + min_duration, seg.end - min_duration]
    so we never create a sub-minimum piece. If that window is empty (only
    possible when `max_duration < 2 * min_duration`), we cut at the midpoint
    as a last resort.
    """
    if not segs:
        return segs

    times = motion["timestamp"].to_numpy()
    energies = motion["motion_energy"].to_numpy()
    frames = motion["frame"].to_numpy()
    frame_to_idx = {int(f): i for i, f in enumerate(frames)}
    max_threshold = max_duration + DURATION_TOLERANCE

    def split_one(s: StepSegment) -> list[StepSegment]:
        if s.duration <= max_threshold:
            return [s]
        i0 = frame_to_idx.get(int(s.start_frame))
        i1 = frame_to_idx.get(int(s.end_frame))
        if i0 is None or i1 is None or i1 - i0 < 3:
            return [s]

        seg_times = times[i0 : i1 + 1]
        seg_energy = energies[i0 : i1 + 1]
        seg_frames = frames[i0 : i1 + 1]

        # Cut window keeps both halves >= min_duration.
        valid = (
            (seg_times - s.start_time >= min_duration)
            & (s.end_time - seg_times >= min_duration)
        )
        if valid.any():
            candidates = np.where(valid)[0]
            cut_local = int(candidates[np.argmin(seg_energy[candidates])])
        else:
            cut_local = (i1 - i0) // 2  # fallback: midpoint

        cut_time = float(seg_times[cut_local])
        cut_frame = int(seg_frames[cut_local])

        left = StepSegment(
            index=0,
            start_time=s.start_time,
            end_time=cut_time,
            start_frame=s.start_frame,
            end_frame=cut_frame,
            duration=cut_time - s.start_time,
            peak_motion=float(np.nanmax(seg_energy[: cut_local + 1])),
        )
        right = StepSegment(
            index=0,
            start_time=cut_time,
            end_time=s.end_time,
            start_frame=cut_frame,
            end_frame=s.end_frame,
            duration=s.end_time - cut_time,
            peak_motion=float(np.nanmax(seg_energy[cut_local:])),
        )
        return split_one(left) + split_one(right)

    out: list[StepSegment] = []
    for s in segs:
        out.extend(split_one(s))
    for i, s in enumerate(out):
        s.index = i
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def segment_steps(
    df: pd.DataFrame,
    crop_start: Optional[float] = None,
    crop_end: Optional[float] = None,
    smooth_window: int = 5,
    low_velocity_percentile: float = 15.0,
    min_segment_duration: float = 4.0,
    max_segment_duration: float = 8.0,
) -> list[StepSegment]:
    """Slice a pose DataFrame into variable-length dance-step segments.

    Args:
        df: Long-format DataFrame from `pose_extractor.extract_pose_landmarks`.
        crop_start: Seconds; rows before this are discarded.
        crop_end: Seconds; rows after this are discarded.
        smooth_window: Rolling-average window for the velocity signal (frames).
        low_velocity_percentile: 0–100. Frames with smoothed velocity below
            this percentile of the whole signal are treated as pauses.
        min_segment_duration: Seconds. Anything shorter is merged into a
            neighbor (with a ±DURATION_TOLERANCE slack).
        max_segment_duration: Seconds. Anything longer is split at its
            deepest internal motion-energy minimum (with the same slack).
    """
    cropped = _crop_dataframe(df, crop_start, crop_end)
    if cropped.empty:
        return []

    motion = compute_motion_signal(cropped, smooth_window=smooth_window)
    boundaries = find_pause_boundaries(motion, low_velocity_percentile)

    frames = motion["frame"].to_numpy()
    times = motion["timestamp"].to_numpy()
    energy = motion["motion_energy"].to_numpy()

    raw: list[StepSegment] = []
    for i in range(len(boundaries) - 1):
        a, b = int(boundaries[i]), int(boundaries[i + 1])
        if b <= a:
            continue
        seg_energy = energy[a : b + 1]
        raw.append(
            StepSegment(
                index=len(raw),
                start_time=float(times[a]),
                end_time=float(times[b]),
                start_frame=int(frames[a]),
                end_frame=int(frames[b]),
                duration=float(times[b] - times[a]),
                peak_motion=float(np.nanmax(seg_energy)) if seg_energy.size else 0.0,
            )
        )

    merged = _merge_short_segments(raw, min_segment_duration)
    return _split_long_segments(merged, motion, max_segment_duration, min_segment_duration)


def segments_to_dataframe(segments: list[StepSegment]) -> pd.DataFrame:
    """Convert step segments to a tidy DataFrame for export."""
    return pd.DataFrame([s.to_dict() for s in segments])


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Segment pose data into dance steps.")
    parser.add_argument("landmarks_csv", help="CSV produced by pose_extractor.py")
    parser.add_argument("-o", "--output", default="steps.csv")
    parser.add_argument("--crop-start", type=float, default=None)
    parser.add_argument("--crop-end", type=float, default=None)
    parser.add_argument("--percentile", type=float, default=15.0)
    parser.add_argument("--min-duration", type=float, default=4.0)
    parser.add_argument("--max-duration", type=float, default=8.0)
    args = parser.parse_args()

    df = pd.read_csv(args.landmarks_csv)
    df.attrs["fps"] = float(df.attrs.get("fps", 30.0))

    segments = segment_steps(
        df,
        crop_start=args.crop_start,
        crop_end=args.crop_end,
        low_velocity_percentile=args.percentile,
        min_segment_duration=args.min_duration,
        max_segment_duration=args.max_duration,
    )
    out = segments_to_dataframe(segments)
    out.to_csv(args.output, index=False)
    print(f"Found {len(segments)} step segments → {args.output}")
