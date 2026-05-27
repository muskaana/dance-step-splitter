"""Frame-by-frame pose extraction with MediaPipe Pose (Tasks API).

Produces a long-format Pandas DataFrame with one row per (frame, landmark):
columns = [frame, timestamp, landmark_id, landmark_name, x, y, z, visibility].
"""

from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Literal

import cv2
import mediapipe as mp
import numpy as np
import pandas as pd
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision as mp_vision

ModelVariant = Literal["lite", "full", "heavy"]

# 33 MediaPipe Pose landmarks, in canonical index order.
LANDMARK_NAMES: list[str] = [
    "nose",
    "left_eye_inner", "left_eye", "left_eye_outer",
    "right_eye_inner", "right_eye", "right_eye_outer",
    "left_ear", "right_ear",
    "mouth_left", "mouth_right",
    "left_shoulder", "right_shoulder",
    "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
    "left_pinky", "right_pinky",
    "left_index", "right_index",
    "left_thumb", "right_thumb",
    "left_hip", "right_hip",
    "left_knee", "right_knee",
    "left_ankle", "right_ankle",
    "left_heel", "right_heel",
    "left_foot_index", "right_foot_index",
]
NUM_LANDMARKS = len(LANDMARK_NAMES)  # 33

_MODEL_URLS: dict[ModelVariant, str] = {
    "lite": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task",
    "full": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task",
    "heavy": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task",
}

_DEFAULT_MODEL_DIR = Path.home() / ".cache" / "mediapipe-models"


def ensure_model(variant: ModelVariant = "full", model_dir: Path | None = None) -> Path:
    """Download the requested PoseLandmarker model if not already cached."""
    model_dir = model_dir or _DEFAULT_MODEL_DIR
    model_dir.mkdir(parents=True, exist_ok=True)
    path = model_dir / f"pose_landmarker_{variant}.task"
    if not path.exists():
        url = _MODEL_URLS[variant]
        urllib.request.urlretrieve(url, path)
    return path


def _build_landmarker(
    model_path: Path,
    min_detection_confidence: float,
    min_tracking_confidence: float,
) -> mp_vision.PoseLandmarker:
    options = mp_vision.PoseLandmarkerOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=str(model_path)),
        running_mode=mp_vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=min_detection_confidence,
        min_pose_presence_confidence=min_detection_confidence,
        min_tracking_confidence=min_tracking_confidence,
        output_segmentation_masks=False,
    )
    return mp_vision.PoseLandmarker.create_from_options(options)


def extract_pose_landmarks(
    video_path: str | Path,
    model_variant: ModelVariant = "full",
    min_detection_confidence: float = 0.5,
    min_tracking_confidence: float = 0.5,
    sample_every_n_frames: int = 1,
    model_path: str | Path | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
) -> pd.DataFrame:
    """Run MediaPipe Pose on every frame of a video and return a DataFrame.

    Args:
        video_path: Path to a local video file.
        model_variant: "lite", "full", or "heavy" — accuracy/speed trade-off.
        min_detection_confidence: Threshold for initial pose detection.
        min_tracking_confidence: Threshold for landmark tracking.
        sample_every_n_frames: Process 1 of every N frames (1 = all).
        model_path: Optional explicit path to a .task model file.

    Returns:
        Long-format DataFrame with columns:
            frame, timestamp, landmark_id, landmark_name, x, y, z, visibility.
        x and y are normalized to [0, 1] (image coordinates). z is normalized
        depth relative to the hips (smaller = closer to camera).
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    resolved_model = Path(model_path) if model_path else ensure_model(model_variant)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    # Crop bounds — skip seeking + decoding outside [start_time, end_time].
    start_frame = int(max(0, (start_time or 0.0) * fps))
    end_frame = int((end_time * fps)) if end_time is not None else None
    if start_frame > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    # Pre-allocate a typed numpy buffer instead of growing a Python list of
    # dicts. A list of 200K small dicts costs ~160 MB of object overhead; the
    # equivalent float32 array is ~26 MB — important on the 1 GB Fly VM.
    upper_bound = end_frame if end_frame is not None else total_frames
    sampled_estimate = max(
        1, ((upper_bound - start_frame) + sample_every_n_frames - 1) // sample_every_n_frames
    )
    capacity = sampled_estimate + 16  # small headroom for fps rounding
    buf_frame = np.zeros(capacity, dtype=np.int32)
    buf_time = np.zeros(capacity, dtype=np.float32)
    # (n_samples, 33, 4) — x, y, z, visibility per landmark.
    buf_lm = np.zeros((capacity, NUM_LANDMARKS, 4), dtype=np.float32)
    count = 0

    try:
        landmarker = _build_landmarker(
            resolved_model, min_detection_confidence, min_tracking_confidence
        )
        try:
            frame_idx = start_frame
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if end_frame is not None and frame_idx >= end_frame:
                    break

                if (frame_idx - start_frame) % sample_every_n_frames == 0:
                    timestamp = frame_idx / fps
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                    result = landmarker.detect_for_video(mp_image, int(timestamp * 1000))

                    # Grow the buffer if our estimate was too low — handles
                    # off-by-one cases where total_frames was inaccurate.
                    if count >= capacity:
                        new_cap = capacity * 2
                        buf_frame = np.resize(buf_frame, new_cap)
                        buf_time = np.resize(buf_time, new_cap)
                        new_lm = np.zeros((new_cap, NUM_LANDMARKS, 4), dtype=np.float32)
                        new_lm[:capacity] = buf_lm
                        buf_lm = new_lm
                        capacity = new_cap

                    buf_frame[count] = frame_idx
                    buf_time[count] = timestamp

                    if result.pose_landmarks:
                        landmarks = result.pose_landmarks[0]
                        for lm_id, lm in enumerate(landmarks):
                            buf_lm[count, lm_id, 0] = lm.x
                            buf_lm[count, lm_id, 1] = lm.y
                            buf_lm[count, lm_id, 2] = lm.z
                            buf_lm[count, lm_id, 3] = getattr(lm, "visibility", 1.0)
                    else:
                        # Missing detection — mark as NaN coords, zero visibility.
                        buf_lm[count, :, 0:3] = np.nan
                        buf_lm[count, :, 3] = 0.0

                    count += 1

                frame_idx += 1
        finally:
            landmarker.close()
    finally:
        cap.release()

    if count == 0:
        df = pd.DataFrame(columns=["frame", "timestamp", "landmark_id", "landmark_name", "x", "y", "z", "visibility"])
    else:
        # Expand the (n, 33, 4) buffer into long format only at the end —
        # avoids holding both representations simultaneously.
        n = count
        repeats = np.tile(np.arange(NUM_LANDMARKS), n)
        df = pd.DataFrame(
            {
                "frame": np.repeat(buf_frame[:n], NUM_LANDMARKS),
                "timestamp": np.repeat(buf_time[:n], NUM_LANDMARKS),
                "landmark_id": repeats,
                "landmark_name": [LANDMARK_NAMES[i] for i in repeats],
                "x": buf_lm[:n, :, 0].reshape(-1),
                "y": buf_lm[:n, :, 1].reshape(-1),
                "z": buf_lm[:n, :, 2].reshape(-1),
                "visibility": buf_lm[:n, :, 3].reshape(-1),
            }
        )
        # Free the big intermediate buffer before returning.
        del buf_frame, buf_time, buf_lm

    df.attrs["fps"] = fps
    df.attrs["video_path"] = str(video_path)
    return df


def to_wide_format(df: pd.DataFrame) -> pd.DataFrame:
    """Pivot the long-format pose DataFrame into wide format.

    One row per frame; columns are <landmark>_x, <landmark>_y, <landmark>_z,
    <landmark>_visibility, plus `frame` and `timestamp`.
    """
    wide = df.pivot_table(
        index=["frame", "timestamp"],
        columns="landmark_name",
        values=["x", "y", "z", "visibility"],
    )
    wide.columns = [f"{name}_{coord}" for coord, name in wide.columns]
    wide = wide.reset_index().sort_values("frame").reset_index(drop=True)
    wide.attrs.update(df.attrs)
    return wide


def save_landmarks(df: pd.DataFrame, output_path: str | Path) -> Path:
    """Save the pose DataFrame to CSV or Parquet (inferred from extension)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()
    if suffix == ".parquet":
        df.to_parquet(output_path, index=False)
    else:
        df.to_csv(output_path, index=False)
    return output_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract MediaPipe Pose landmarks.")
    parser.add_argument("video", help="Path to input video")
    parser.add_argument("-o", "--output", default="pose_landmarks.csv")
    parser.add_argument("--variant", choices=["lite", "full", "heavy"], default="full")
    parser.add_argument("--every", type=int, default=1, help="Sample every N frames")
    args = parser.parse_args()

    df = extract_pose_landmarks(
        args.video, model_variant=args.variant, sample_every_n_frames=args.every
    )
    save_landmarks(df, args.output)
    print(f"Wrote {len(df)} rows to {args.output} (fps={df.attrs.get('fps')})")
