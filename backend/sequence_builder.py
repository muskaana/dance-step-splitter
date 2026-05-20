"""Convert raw segmenter output into a clean, UI-friendly JSON sequence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence, Union

from .segmenter import StepSegment

SegmentInput = Union[StepSegment, dict]

DEFAULT_DATA_DIR = Path("data")


def build_sequence(
    segments: Sequence[SegmentInput],
    label_prefix: str = "Segment",
    round_to: int = 2,
) -> list[dict]:
    """Map raw time segments into clean movement-block dicts.

    Args:
        segments: Iterable of `StepSegment` objects or dicts containing at
            minimum `start_time`/`end_time` (or `start`/`end`) keys.
        label_prefix: Prefix used for the generated label, e.g. "Segment 1".
        round_to: Decimal places to round timestamps to.

    Returns:
        List of dicts shaped as:
            {"id": int, "label": str, "start": float, "end": float}
    """
    sequence: list[dict] = []
    for i, seg in enumerate(segments, start=1):
        start, end = _extract_bounds(seg)
        sequence.append(
            {
                "id": i,
                "label": f"{label_prefix} {i}",
                "start": round(float(start), round_to),
                "end": round(float(end), round_to),
            }
        )
    return sequence


def _extract_bounds(seg: SegmentInput) -> tuple[float, float]:
    if isinstance(seg, StepSegment):
        return seg.start_time, seg.end_time
    if isinstance(seg, dict):
        if "start_time" in seg and "end_time" in seg:
            return float(seg["start_time"]), float(seg["end_time"])
        if "start" in seg and "end" in seg:
            return float(seg["start"]), float(seg["end"])
    raise TypeError(f"Unrecognized segment input: {seg!r}")


def save_sequence(
    sequence: Iterable[dict],
    filename: str = "sequence.json",
    data_dir: str | Path = DEFAULT_DATA_DIR,
) -> Path:
    """Write a sequence to `<data_dir>/<filename>` and return the path."""
    out_dir = Path(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(list(sequence), f, indent=2)
    return out_path


def build_and_save(
    segments: Sequence[SegmentInput],
    filename: str = "sequence.json",
    data_dir: str | Path = DEFAULT_DATA_DIR,
    label_prefix: str = "Segment",
    round_to: int = 2,
) -> tuple[list[dict], Path]:
    """Convenience: build the sequence and persist it in one call."""
    sequence = build_sequence(segments, label_prefix=label_prefix, round_to=round_to)
    path = save_sequence(sequence, filename=filename, data_dir=data_dir)
    return sequence, path


if __name__ == "__main__":
    import argparse
    import pandas as pd

    parser = argparse.ArgumentParser(description="Build a clean JSON sequence from segments.")
    parser.add_argument("segments_csv", help="CSV produced by segmenter.py")
    parser.add_argument("-o", "--filename", default="sequence.json")
    parser.add_argument("-d", "--data-dir", default=str(DEFAULT_DATA_DIR))
    args = parser.parse_args()

    df = pd.read_csv(args.segments_csv)
    rows = df.to_dict(orient="records")
    seq, path = build_and_save(rows, filename=args.filename, data_dir=args.data_dir)
    print(f"Wrote {len(seq)} segments to {path}")
