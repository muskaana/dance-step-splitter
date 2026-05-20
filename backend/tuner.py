"""Personalised segmenter param tuning from a user's manual edits.

Approach: for each previously-edited video we have the user's ground-truth
segment boundaries plus the original pose-extraction DataFrame. We grid-search
the segmenter's parameters, score each combination against the manual
boundaries, and return the params with the lowest aggregate error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from . import segmenter


# A small but meaningfully diverse grid. ~180 combinations on the full grid;
# we prune impossible cases (max <= min) so the real count is a bit lower.
PARAM_GRID: dict[str, list] = {
    "low_velocity_percentile": [10.0, 15.0, 20.0, 25.0, 30.0],
    "smooth_window": [3, 5, 7, 9],
    "min_segment_duration": [3.0, 4.0, 5.0],
    "max_segment_duration": [7.0, 8.0, 10.0],
}

# Penalty per missing/extra segment, in seconds. Tuned so a one-segment-off
# prediction is roughly as bad as a 0.5-second boundary shift.
COUNT_MISMATCH_PENALTY = 0.5


@dataclass(frozen=True)
class TuningExample:
    pose_df: pd.DataFrame
    ground_truth: list[dict]  # [{id, label, start, end}, ...]


@dataclass(frozen=True)
class TuningResult:
    params: dict
    score: float
    example_count: int


def _segment_boundary_times(segments: list[dict], key_start: str, key_end: str) -> list[float]:
    """Extract the sorted list of unique boundary timestamps from a segment list."""
    if not segments:
        return []
    bounds = {float(s[key_start]) for s in segments}
    bounds.add(float(segments[-1][key_end]))
    return sorted(bounds)


def score_prediction(predicted: list[segmenter.StepSegment], gt: list[dict]) -> float:
    """Lower = better. Mean nearest-neighbour distance between boundary sets,
    plus a small count-mismatch penalty.

    Predicted boundaries come from StepSegment (start_time/end_time).
    Ground-truth boundaries come from the persisted segment dicts (start/end).
    """
    if not gt or not predicted:
        # No way to score — treat as worst-case.
        return float("inf")

    pred_bounds = [s.start_time for s in predicted] + [predicted[-1].end_time]
    pred_bounds = sorted(set(pred_bounds))
    gt_bounds = _segment_boundary_times(gt, "start", "end")

    # Symmetric mean nearest-neighbour distance: for each GT boundary, find
    # the closest predicted boundary, and vice versa.
    def nearest_avg(a: list[float], b: list[float]) -> float:
        return sum(min(abs(x - y) for y in b) for x in a) / len(a)

    dist = 0.5 * (nearest_avg(gt_bounds, pred_bounds) + nearest_avg(pred_bounds, gt_bounds))
    count_penalty = abs(len(predicted) - len(gt)) * COUNT_MISMATCH_PENALTY
    return dist + count_penalty


def tune(examples: Iterable[TuningExample]) -> TuningResult | None:
    """Return the parameter combination that minimises mean score across examples.

    Returns None if no examples were provided.
    """
    examples = list(examples)
    if not examples:
        return None

    best: TuningResult | None = None
    for vp in PARAM_GRID["low_velocity_percentile"]:
        for sw in PARAM_GRID["smooth_window"]:
            for min_d in PARAM_GRID["min_segment_duration"]:
                for max_d in PARAM_GRID["max_segment_duration"]:
                    if max_d <= min_d:
                        continue
                    params = {
                        "low_velocity_percentile": vp,
                        "smooth_window": sw,
                        "min_segment_duration": min_d,
                        "max_segment_duration": max_d,
                    }
                    total = 0.0
                    for ex in examples:
                        predicted = segmenter.segment_steps(ex.pose_df, **params)
                        total += score_prediction(predicted, ex.ground_truth)
                    avg = total / len(examples)
                    if best is None or avg < best.score:
                        best = TuningResult(
                            params=params, score=avg, example_count=len(examples)
                        )
    return best
