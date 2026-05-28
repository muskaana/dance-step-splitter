"""FastAPI orchestrator for the Dance Step Splitter.

Wires the four backend modules into a single pipeline and exposes:
  - GET  /                  → frontend SPA (index.html)
  - GET  /app.js, /data/*   → frontend assets + generated sequence JSON
  - GET  /videos/<file>     → range-streamed MP4s from the downloads dir
  - POST /api/process       → run the full pipeline for a YouTube URL
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

from backend import (
    audio_segmenter,
    auth,
    beat_detector,
    downloader,
    pose_extractor,
    segmenter,
    sequence_builder,
    tuner,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "frontend"
DATA_DIR = ROOT / "data"
DOWNLOADS_DIR = ROOT / "downloads"

for d in (DATA_DIR, DOWNLOADS_DIR):
    # resolve() follows symlinks; needed when DATA_DIR / DOWNLOADS_DIR are
    # symlinked to a persistent-volume location that doesn't exist yet
    # (Fly.io mounts an empty volume over the build-time target).
    d.resolve().mkdir(parents=True, exist_ok=True)

auth.init_auth(DATA_DIR / "users.db")


# ---------------------------------------------------------------------------
# Per-user storage layout
# ---------------------------------------------------------------------------


def user_data_dir(user_id: int) -> Path:
    p = DATA_DIR / "users" / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_downloads_dir(user_id: int) -> Path:
    p = DOWNLOADS_DIR / "users" / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_library_path(user_id: int) -> Path:
    return user_data_dir(user_id) / "library.json"


def _read_library(user_id: int) -> list[dict]:
    path = user_library_path(user_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# Pose-data persistence (used by the personalised tuner)
# ---------------------------------------------------------------------------


def _pose_path(user_id: int, video_id: str) -> Path:
    return user_data_dir(user_id) / f"{video_id}.pose.csv.gz"


def _pose_meta_path(user_id: int, video_id: str) -> Path:
    return user_data_dir(user_id) / f"{video_id}.pose.meta.json"


def _save_pose_data(user_id: int, video_id: str, pose_df: "pd.DataFrame") -> None:
    """Persist the MediaPipe pose DataFrame so the tuner can re-segment it
    later. Cheap: gzipped CSV ~1-2 MB per video."""
    pose_df.to_csv(_pose_path(user_id, video_id), index=False, compression="gzip")
    fps = float(pose_df.attrs.get("fps", 30.0))
    _pose_meta_path(user_id, video_id).write_text(json.dumps({"fps": fps}))


def _load_pose_data(user_id: int, video_id: str):
    """Load a persisted pose DataFrame, restoring fps via the sidecar meta.
    Returns None if either the data or meta file is missing."""
    import pandas as pd

    pose_path = _pose_path(user_id, video_id)
    meta_path = _pose_meta_path(user_id, video_id)
    if not pose_path.exists() or not meta_path.exists():
        return None
    try:
        df = pd.read_csv(pose_path)
        meta = json.loads(meta_path.read_text())
        df.attrs["fps"] = float(meta.get("fps", 30.0))
        return df
    except Exception:
        return None


def _write_library(user_id: int, items: list[dict]) -> None:
    user_library_path(user_id).write_text(json.dumps(items, indent=2))


def _classify_source(url: str) -> str:
    """Map a remote URL to a short source tag for library entries.

    Frontend uses the tag to colour the badge on each library card.
    """
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "url"
    if host.endswith("youtube.com") or host == "youtu.be" or host.endswith(".youtu.be"):
        return "youtube"
    if host.endswith("instagram.com"):
        return "instagram"
    if host.endswith("tiktok.com"):
        return "tiktok"
    return "url"


def _record_library_entry(
    user_id: int,
    video_id: str,
    video_url: str,
    title: str,
    duration: float,
    segment_count: int,
    source: str,
    source_url: Optional[str] = None,
    crop_start: Optional[float] = None,
    crop_end: Optional[float] = None,
    kind: str = "dance",
    parent_video_id: Optional[str] = None,
    bpm: Optional[float] = None,
    beat_times: Optional[list[float]] = None,
) -> dict:
    """Upsert a library entry. Most-recent processing of a given video_id wins.

    `bpm` + `beat_times` are populated for dance entries that had beat detection
    enabled. They drive the count-voice scheduler on the client so 1-2-3-4-…
    lands on actual musical beats instead of evenly-spaced fractions of a loop.
    """
    items = _read_library(user_id)
    items = [i for i in items if i.get("video_id") != video_id]
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "video_id": video_id,
        "video_url": video_url,
        "title": title,
        "source": source,
        "source_url": source_url,
        "duration": duration,
        "segment_count": segment_count,
        "processed_at": now,
        "last_edited_at": None,
        "crop_start": crop_start,
        "crop_end": crop_end,
        "kind": kind,
        "parent_video_id": parent_video_id,
        # Optional beat data — only present when snap_to_beat was enabled and
        # librosa returned a usable result. ~4-8 bytes per beat, so a 4-minute
        # song at 120 BPM adds ~4 KB to library.json. Acceptable.
        "bpm": bpm,
        "beat_times": beat_times,
    }
    items.insert(0, entry)
    _write_library(user_id, items)
    return entry


def _update_library_entry(user_id: int, video_id: str, patch: dict) -> Optional[dict]:
    """Merge `patch` into the library entry with the given `video_id`."""
    items = _read_library(user_id)
    for i, item in enumerate(items):
        if item.get("video_id") == video_id:
            items[i] = {**item, **patch}
            _write_library(user_id, items)
            return items[i]
    return None


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def get_current_user(session: Optional[str] = Cookie(default=None)) -> auth.User:
    user = auth.user_from_session(session)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def resolve_entry_access(
    user_id: int, video_id: str
) -> tuple[int, str]:
    """Determine which owner's files back `video_id` for the requesting user
    and what permission level they have on it.

    Returns:
        (owner_user_id, permission) where permission ∈ {"owner", "edit", "view"}.

    Raises HTTPException 404 if the user has no access at all.
    """
    if any(i.get("video_id") == video_id for i in _read_library(user_id)):
        return user_id, "owner"
    share = auth.find_any_share_for_video(video_id, user_id)
    if share:
        return share.owner_id, share.permission
    raise HTTPException(status_code=404, detail=f"Unknown video_id: {video_id}")


def _normalize_video_url(entry: dict, owner_id: int) -> dict:
    """Rewrite legacy `/videos/<filename>` URLs to the per-owner format.
    Stored values may use the older format; the API always emits the new one."""
    url = entry.get("video_url", "")
    if url.startswith("/videos/"):
        parts = [p for p in url.split("/") if p]
        if len(parts) == 2:  # videos/<filename> — legacy
            return {**entry, "video_url": f"/videos/{owner_id}/{parts[1]}"}
    return entry

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Dance Step Splitter")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProcessRequest(BaseModel):
    url: HttpUrl
    kind: Literal["dance", "singing"] = "dance"
    quality: Literal["480p", "720p", "1080p"] = "720p"
    model_variant: Literal["lite", "full", "heavy"] = "full"
    sample_every_n_frames: int = Field(default=1, ge=1, le=10)
    min_segment_duration: float = Field(default=4.0, gt=0)
    max_segment_duration: float = Field(default=8.0, gt=0)
    low_velocity_percentile: float = Field(default=15.0, gt=0, lt=100)
    smooth_window: int = Field(default=5, ge=1, le=60)
    cookies_from_browser: Optional[Literal["safari", "chrome", "firefox", "edge", "brave"]] = None
    start_time: Optional[float] = Field(default=None, ge=0)
    end_time: Optional[float] = Field(default=None, gt=0)
    # Dance mode only — after motion-based segmentation, nudge each segment
    # boundary onto the nearest detected music beat (within ±0.4s by default).
    # Off by default so existing behaviour is preserved unless the user
    # opts in from the UI.
    snap_to_beat: bool = False


class TuningInfo(BaseModel):
    example_count: int
    params: dict


class ProcessResponse(BaseModel):
    video_id: str
    video_url: str
    duration: float
    segment_count: int
    segments: list[dict]
    tuning: Optional[TuningInfo] = None
    # Actual resolution we ended up with (after YouTube's player-client
    # fallback chain). Null for uploaded files since we have no metadata.
    height: Optional[int] = None
    # Beat-detection results, when snap_to_beat was requested. Null if
    # disabled or if beat detection produced no usable result.
    bpm: Optional[float] = None
    beat_snap_count: Optional[int] = None  # how many boundaries actually moved
    beat_times: Optional[list[float]] = None  # absolute seconds, used for counts


def _load_tuning_examples(
    user_id: int, max_examples: int = 5
) -> list[tuner.TuningExample]:
    """Find the user's most-recently-edited library entries with persisted pose
    data and return them as training examples for the tuner."""
    items = [i for i in _read_library(user_id) if i.get("last_edited_at")]
    items.sort(key=lambda i: i.get("last_edited_at", ""), reverse=True)

    examples: list[tuner.TuningExample] = []
    user_data = user_data_dir(user_id)
    for entry in items:
        if len(examples) >= max_examples:
            break
        video_id = entry.get("video_id")
        if not video_id:
            continue
        seg_path = user_data / f"{video_id}.json"
        pose_df = _load_pose_data(user_id, video_id)
        if pose_df is None or not seg_path.exists():
            continue
        try:
            ground_truth = json.loads(seg_path.read_text())
        except json.JSONDecodeError:
            continue
        if not ground_truth:
            continue
        examples.append(
            tuner.TuningExample(pose_df=pose_df, ground_truth=ground_truth)
        )
    return examples


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _run_pipeline(req: ProcessRequest, user_id: int) -> ProcessResponse:
    """Synchronous pipeline. Runs in a threadpool — see /api/process."""
    user_dl = user_downloads_dir(user_id)
    user_data = user_data_dir(user_id)

    # 1. Download.
    # If a Netscape cookies.txt is on the volume, prefer it — that's the cleanest
    # workaround for YouTube throttling cloud IPs.
    cookies_file = DATA_DIR / "cookies.txt"
    video_path, info = downloader.download_video(
        str(req.url),
        output_dir=user_dl,
        quality=req.quality,
        cookies_from_browser=req.cookies_from_browser,
        cookies_file=cookies_file if cookies_file.exists() else None,
    )
    video_id = video_path.stem
    title = (info.get("title") or video_id) if isinstance(info, dict) else video_id
    actual_height = info.get("height") if isinstance(info, dict) else None
    if isinstance(info, dict):
        print(
            f"[download] {video_id}: height={info.get('height')} "
            f"format={info.get('format_note')} "
            f"client={info.get('player_client')} "
            f"cookies={info.get('used_cookies')}"
        )

    # 2. Segment. Path forks on `kind`:
    #    - dance → pose extraction + kinematic segmenter (existing flow)
    #    - singing → audio silence-detection (no MediaPipe, much faster)
    tuning_result = None
    beat_info: Optional[dict] = None
    if req.kind == "singing":
        segments = audio_segmenter.segment_by_audio(
            video_path,
            crop_start=req.start_time,
            crop_end=req.end_time,
            min_segment_duration=max(req.min_segment_duration, 8.0),
            max_segment_duration=max(req.max_segment_duration, 30.0),
        )
        pose_df = None
    else:
        probed_duration = _probe_duration(video_path)
        effective_sample = _auto_sample_rate(
            probed_duration, req.start_time, req.end_time, req.sample_every_n_frames
        )
        if effective_sample != req.sample_every_n_frames:
            print(
                f"[pose] {video_id}: auto-sampling every {effective_sample} frames "
                f"(requested {req.sample_every_n_frames}, duration={probed_duration})"
            )
        pose_df = pose_extractor.extract_pose_landmarks(
            video_path,
            model_variant=req.model_variant,
            sample_every_n_frames=effective_sample,
            start_time=req.start_time,
            end_time=req.end_time,
        )
        _save_pose_data(user_id, video_id, pose_df)

        seg_params = {
            "smooth_window": req.smooth_window,
            "low_velocity_percentile": req.low_velocity_percentile,
            "min_segment_duration": req.min_segment_duration,
            "max_segment_duration": req.max_segment_duration,
        }
        tuning_result = tuner.tune(_load_tuning_examples(user_id))
        if tuning_result is not None:
            seg_params = {**seg_params, **tuning_result.params}
        segments = segmenter.segment_steps(
            pose_df,
            crop_start=req.start_time,
            crop_end=req.end_time,
            **seg_params,
        )

        # Beat-snapping post-pass — opt-in via the UI checkbox. Runs only for
        # dance mode because singing already uses musical-silence boundaries.
        if req.snap_to_beat and len(segments) >= 2:
            try:
                beat_result = beat_detector.detect_beats_subprocess(
                    video_path,
                    crop_start=req.start_time,
                    crop_end=req.end_time,
                )
            except Exception as exc:
                # Beat detection is best-effort — never let it fail the run.
                print(f"[beat] detection failed for {video_id}: {exc}")
                beat_result = None
            if beat_result is not None:
                before = [s.end_time for s in segments[:-1]]
                segments = segmenter.snap_to_beats(
                    segments,
                    beat_result.beat_times,
                    window=0.4,
                    min_kept_duration=max(1.5, seg_params["min_segment_duration"] * 0.5),
                )
                after = [s.end_time for s in segments[:-1]]
                moved = sum(
                    1 for a, b in zip(before, after) if abs(a - b) > 1e-3
                )
                beat_info = {
                    "bpm": beat_result.bpm,
                    "moved": moved,
                    "beat_times": beat_result.beat_times,
                }
                print(
                    f"[beat] {video_id}: bpm={beat_result.bpm:.1f} "
                    f"boundaries_moved={moved}/{len(before)}"
                )
            else:
                beat_info = None
        else:
            beat_info = None

    # 4. Build clean sequence + persist to the user's data folder
    sequence, _ = sequence_builder.build_and_save(
        segments, filename="sequence.json", data_dir=user_data
    )
    sequence_builder.save_sequence(
        sequence, filename=f"{video_id}.json", data_dir=user_data
    )

    if pose_df is not None and not pose_df.empty:
        duration = float(pose_df["timestamp"].max())
    else:
        # Singing path: probe the file directly since we don't have pose data.
        duration = float(_probe_duration(video_path) or 0)
    video_url = f"/videos/{user_id}/{video_path.name}"

    _record_library_entry(
        user_id=user_id,
        video_id=video_id,
        video_url=video_url,
        title=title,
        duration=duration,
        segment_count=len(sequence),
        source=_classify_source(str(req.url)),
        source_url=str(req.url),
        crop_start=req.start_time,
        crop_end=req.end_time,
        kind=req.kind,
        bpm=beat_info["bpm"] if beat_info else None,
        beat_times=beat_info["beat_times"] if beat_info else None,
    )

    return ProcessResponse(
        video_id=video_id,
        video_url=video_url,
        duration=duration,
        segment_count=len(sequence),
        segments=sequence,
        height=actual_height,
        tuning=(
            TuningInfo(
                example_count=tuning_result.example_count,
                params=tuning_result.params,
            )
            if tuning_result is not None
            else None
        ),
        bpm=beat_info["bpm"] if beat_info else None,
        beat_snap_count=beat_info["moved"] if beat_info else None,
        beat_times=beat_info["beat_times"] if beat_info else None,
    )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.post("/api/process", response_model=ProcessResponse)
async def process_video(
    req: ProcessRequest,
    user: auth.User = Depends(get_current_user),
) -> ProcessResponse:
    """Run the full pipeline and return the segment JSON for the UI."""
    try:
        return await asyncio.to_thread(_run_pipeline, req, user.id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}")


def _run_file_pipeline(
    user_id: int,
    video_path: Path,
    model_variant: str,
    sample_every_n_frames: int,
    min_segment_duration: float,
    max_segment_duration: float,
    low_velocity_percentile: float,
    smooth_window: int,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    original_filename: Optional[str] = None,
    kind: str = "dance",
    snap_to_beat: bool = False,
) -> ProcessResponse:
    """Same as `_run_pipeline` but skipping the download step."""
    user_data = user_data_dir(user_id)
    video_id = video_path.stem
    title = (
        Path(original_filename).stem if original_filename else video_id
    )

    tuning_result = None
    beat_info: Optional[dict] = None
    if kind == "singing":
        segments = audio_segmenter.segment_by_audio(
            video_path,
            crop_start=start_time,
            crop_end=end_time,
            min_segment_duration=max(min_segment_duration, 8.0),
            max_segment_duration=max(max_segment_duration, 30.0),
        )
        pose_df = None
    else:
        probed_duration = _probe_duration(video_path)
        effective_sample = _auto_sample_rate(
            probed_duration, start_time, end_time, sample_every_n_frames
        )
        if effective_sample != sample_every_n_frames:
            print(
                f"[pose] {video_id}: auto-sampling every {effective_sample} frames "
                f"(requested {sample_every_n_frames}, duration={probed_duration})"
            )
        pose_df = pose_extractor.extract_pose_landmarks(
            video_path,
            model_variant=model_variant,
            sample_every_n_frames=effective_sample,
            start_time=start_time,
            end_time=end_time,
        )
        _save_pose_data(user_id, video_id, pose_df)

        seg_params = {
            "smooth_window": smooth_window,
            "low_velocity_percentile": low_velocity_percentile,
            "min_segment_duration": min_segment_duration,
            "max_segment_duration": max_segment_duration,
        }
        tuning_result = tuner.tune(_load_tuning_examples(user_id))
        if tuning_result is not None:
            seg_params = {**seg_params, **tuning_result.params}
        segments = segmenter.segment_steps(
            pose_df,
            crop_start=start_time,
            crop_end=end_time,
            **seg_params,
        )

        if snap_to_beat and len(segments) >= 2:
            try:
                beat_result = beat_detector.detect_beats_subprocess(
                    video_path, crop_start=start_time, crop_end=end_time
                )
            except Exception as exc:
                print(f"[beat] detection failed for {video_id}: {exc}")
                beat_result = None
            if beat_result is not None:
                before = [s.end_time for s in segments[:-1]]
                segments = segmenter.snap_to_beats(
                    segments,
                    beat_result.beat_times,
                    window=0.4,
                    min_kept_duration=max(1.5, seg_params["min_segment_duration"] * 0.5),
                )
                moved = sum(
                    1
                    for a, b in zip(before, [s.end_time for s in segments[:-1]])
                    if abs(a - b) > 1e-3
                )
                beat_info = {
                    "bpm": beat_result.bpm,
                    "moved": moved,
                    "beat_times": beat_result.beat_times,
                }
                print(
                    f"[beat] {video_id}: bpm={beat_result.bpm:.1f} "
                    f"boundaries_moved={moved}/{len(before)}"
                )

    sequence, _ = sequence_builder.build_and_save(
        segments, filename="sequence.json", data_dir=user_data
    )
    sequence_builder.save_sequence(
        sequence, filename=f"{video_id}.json", data_dir=user_data
    )

    if pose_df is not None and not pose_df.empty:
        duration = float(pose_df["timestamp"].max())
    else:
        duration = float(_probe_duration(video_path) or 0)
    video_url = f"/videos/{user_id}/{video_path.name}"

    _record_library_entry(
        user_id=user_id,
        video_id=video_id,
        video_url=video_url,
        title=title,
        duration=duration,
        segment_count=len(sequence),
        source="upload",
        crop_start=start_time,
        crop_end=end_time,
        kind=kind,
        bpm=beat_info["bpm"] if beat_info else None,
        beat_times=beat_info["beat_times"] if beat_info else None,
    )

    return ProcessResponse(
        video_id=video_id,
        video_url=video_url,
        duration=duration,
        segment_count=len(sequence),
        segments=sequence,
        tuning=(
            TuningInfo(
                example_count=tuning_result.example_count,
                params=tuning_result.params,
            )
            if tuning_result is not None
            else None
        ),
        bpm=beat_info["bpm"] if beat_info else None,
        beat_snap_count=beat_info["moved"] if beat_info else None,
        beat_times=beat_info["beat_times"] if beat_info else None,
    )


def _auto_sample_rate(
    duration_s: Optional[float],
    crop_start: Optional[float],
    crop_end: Optional[float],
    requested: int,
) -> int:
    """Pick `sample_every_n_frames` to bound pose-extraction memory on Fly.

    The kinematic segmenter only cares about motion frequencies up to a few Hz
    (dance moves), so dropping to 10-15 fps barely affects accuracy. But the
    pose extractor's working set grows linearly with sampled frame count, and
    Fly's 1 GB shared-cpu-1x VM can't fit a 3+ min @ 30 fps extraction in RAM.

    Honour the caller if they explicitly asked for >1 (power-user override);
    otherwise auto-downsample longer clips.
    """
    if requested > 1:
        return requested
    span = duration_s or 0.0
    if crop_start is not None or crop_end is not None:
        s = crop_start if crop_start is not None else 0.0
        e = crop_end if crop_end is not None else (duration_s or 0.0)
        span = max(0.0, e - s)
    if span > 180:
        return 3
    if span > 90:
        return 2
    return 1


def _probe_duration(path: Path) -> Optional[float]:
    """Return media duration in seconds via ffprobe, or None if it fails."""
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
        return None


def _has_audio_stream(path: Path) -> bool:
    """Cheap ffprobe check — is there at least one audio stream in `path`?"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True, text=True, check=False,
        )
        return "audio" in (result.stdout or "")
    except Exception:
        return False


def _normalize_clip(
    input_path: Path,
    output_path: Path,
    start: Optional[float] = None,
    end: Optional[float] = None,
) -> None:
    """Re-encode `input_path` into a known-good MP4 (H.264 yuv420p 30 fps,
    AAC 44.1 kHz stereo). Optionally trims via -ss/-to input seeking, which
    is more reliable than ffmpeg's `trim` filter for small clips.

    The result is a clip safe to stream-copy concat with other normalized
    clips — every output has identical codec/format parameters.
    """
    cmd: list[str] = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    # Input seeking (-ss before -i) is fast and "close enough" frame accurate
    # for our use; we don't need single-frame precision on dance clips.
    if start is not None and start > 0:
        cmd.extend(["-ss", f"{start:.3f}"])
    if end is not None and end > 0:
        cmd.extend(["-to", f"{end:.3f}"])
    cmd.extend(["-i", str(input_path)])

    cmd.extend(
        [
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30,format=yuv420p",
            "-af", "aresample=async=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path),
        ]
    )

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        msg = (result.stderr or "").strip().splitlines()[-3:]
        print(f"[normalize] failed cmd: {' '.join(cmd)[:1200]}")
        print(f"[normalize] stderr tail: {' | '.join(msg)}")
        raise RuntimeError(
            f"Normalizing {input_path.name} failed: " + " | ".join(msg)
        )


def _concat_videos(
    input_paths: list[Path],
    output_path: Path,
    trims: Optional[list[tuple[Optional[float], Optional[float]]]] = None,
) -> None:
    """Concatenate N videos into one MP4.

    Two-pass approach: each input is independently re-encoded into a
    normalized intermediate (correct codec, format, frame rate, audio
    params, optionally trimmed). The intermediates are then concat-demuxed
    with `-c copy` — fast, stream-only, and rock-solid because every input
    is now known to have identical parameters.

    This is more reliable than a single-pass filter graph: any failure
    localizes to one clip's normalization step, where the error message
    tells you exactly which file was the problem instead of a cryptic
    `-22` from libx264 with no input identification.

    Optional per-input trim: `trims[i] = (start, end)` in seconds; either
    bound may be `None`.
    """
    if not input_paths:
        raise ValueError("No input videos to concatenate")
    if trims is None:
        trims = [(None, None)] * len(input_paths)
    if len(trims) != len(input_paths):
        raise ValueError("trims length must match input_paths length")

    missing_audio = [p.name for p in input_paths if not _has_audio_stream(p)]
    if missing_audio:
        raise RuntimeError(
            "These clips have no audio track and can't be combined: "
            + ", ".join(missing_audio)
            + ". Re-record with sound or add a silent track via an external tool."
        )

    work_dir = output_path.parent / f".concat-{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []

    try:
        for i, (src, (ts, te)) in enumerate(zip(input_paths, trims)):
            norm = work_dir / f"norm_{i:02d}.mp4"
            _normalize_clip(src, norm, ts, te)
            normalized.append(norm)

        manifest = work_dir / "concat.txt"
        manifest.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in normalized) + "\n"
        )

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(manifest),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            msg = (result.stderr or "").strip().splitlines()[-3:]
            print(f"[concat] failed cmd: {' '.join(cmd)[:1200]}")
            print(f"[concat] stderr tail: {' | '.join(msg)}")
            raise RuntimeError("ffmpeg concat failed: " + " | ".join(msg))
    finally:
        for p in normalized:
            p.unlink(missing_ok=True)
        (work_dir / "concat.txt").unlink(missing_ok=True)
        try:
            work_dir.rmdir()
        except OSError:
            pass


@app.post("/api/process-files", response_model=ProcessResponse)
async def process_video_files(
    files: list[UploadFile] = File(...),
    model_variant: Literal["lite", "full", "heavy"] = Form("full"),
    sample_every_n_frames: int = Form(1),
    min_segment_duration: float = Form(4.0),
    max_segment_duration: float = Form(8.0),
    low_velocity_percentile: float = Form(15.0),
    smooth_window: int = Form(5),
    user: auth.User = Depends(get_current_user),
) -> ProcessResponse:
    """Concatenate several uploaded clips into one MP4 and process the result.

    Crop bounds are intentionally NOT accepted here — the user can re-process
    the resulting library entry with crop bounds afterwards if they want.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")
    if len(files) > 8:
        raise HTTPException(
            status_code=400,
            detail="At most 8 clips can be combined at once.",
        )

    user_dl = user_downloads_dir(user.id)
    tmp_dir = user_dl / f".tmp-{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    input_paths: list[Path] = []
    try:
        # Stream every upload to a temp file (chunked so large clips don't OOM).
        for i, file in enumerate(files):
            suffix = Path(file.filename or f"clip{i}.mp4").suffix or ".mp4"
            tmp_in = tmp_dir / f"in_{i:02d}{suffix}"
            with tmp_in.open("wb") as f:
                while chunk := await file.read(1024 * 1024):
                    f.write(chunk)
            input_paths.append(tmp_in)

        # Build a stable name for the combined video so the library is happy.
        first_stem = (
            "".join(c for c in Path(files[0].filename or "combined").stem
                    if c.isalnum() or c in "-_")
            or "combined"
        )
        video_id = f"combined-{first_stem[:24]}-{uuid.uuid4().hex[:6]}"
        dest = user_dl / f"{video_id}.mp4"

        # Re-encode everything into one MP4. Runs in a thread to keep the
        # event loop free during the ffmpeg call (which can take a while).
        await asyncio.to_thread(_concat_videos, input_paths, dest)

        # Reuse the existing single-file pipeline on the merged video.
        original_name = " + ".join(f.filename or "clip" for f in files)
        return await asyncio.to_thread(
            _run_file_pipeline,
            user.id,
            dest,
            model_variant,
            sample_every_n_frames,
            min_segment_duration,
            max_segment_duration,
            low_velocity_percentile,
            smooth_window,
            None,
            None,
            original_name,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Combine + process failed: {e}")
    finally:
        # Best-effort cleanup of temp inputs.
        for p in input_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            tmp_dir.rmdir()
        except Exception:
            pass


@app.post("/api/process-file", response_model=ProcessResponse)
async def process_video_file(
    file: UploadFile = File(...),
    model_variant: Literal["lite", "full", "heavy"] = Form("full"),
    sample_every_n_frames: int = Form(1),
    min_segment_duration: float = Form(4.0),
    max_segment_duration: float = Form(8.0),
    low_velocity_percentile: float = Form(15.0),
    smooth_window: int = Form(5),
    start_time: Optional[float] = Form(None),
    end_time: Optional[float] = Form(None),
    kind: Literal["dance", "singing"] = Form("dance"),
    snap_to_beat: bool = Form(False),
    user: auth.User = Depends(get_current_user),
) -> ProcessResponse:
    """Run the pose / segment / sequence pipeline on a user-uploaded video."""
    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    safe_stem = "".join(c for c in Path(file.filename or "video").stem if c.isalnum() or c in "-_") or "video"
    dest = user_downloads_dir(user.id) / f"{safe_stem}{suffix}"

    # Stream the upload to disk in chunks so big files don't blow up memory.
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    try:
        return await asyncio.to_thread(
            _run_file_pipeline,
            user.id,
            dest,
            model_variant,
            sample_every_n_frames,
            min_segment_duration,
            max_segment_duration,
            low_velocity_percentile,
            smooth_window,
            start_time,
            end_time,
            file.filename,
            kind,
            snap_to_beat,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}")


@app.get("/api/sequence")
async def get_current_sequence(
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    """Return the most recently generated sequence for the current user."""
    path = user_data_dir(user.id) / "sequence.json"
    if not path.exists():
        return JSONResponse([])
    with path.open() as f:
        return JSONResponse(json.load(f))


class SegmentItem(BaseModel):
    id: int = 0  # Server-assigned on save, ignored on input.
    label: str = ""
    start: float = Field(..., ge=0)
    end: float = Field(..., gt=0)
    lyrics: Optional[str] = None  # Singing-mode only; flows through unchanged.


@app.put("/api/sequence")
async def update_sequence(
    segments: list[SegmentItem],
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    """Persist a user-edited segment list, sorted by start time and re-numbered."""
    sorted_segs = sorted(segments, key=lambda s: s.start)
    out = []
    for i, s in enumerate(sorted_segs, start=1):
        if s.end <= s.start:
            raise HTTPException(
                status_code=400,
                detail=f"Segment {i}: end ({s.end}) must be greater than start ({s.start}).",
            )
        item = {
            "id": i,
            "label": s.label or f"Segment {i}",
            "start": round(s.start, 2),
            "end": round(s.end, 2),
        }
        if s.lyrics is not None and s.lyrics.strip():
            item["lyrics"] = s.lyrics
        out.append(item)
    (user_data_dir(user.id) / "sequence.json").write_text(json.dumps(out, indent=2))
    return JSONResponse(out)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Self-recordings (practice video the user filmed of themselves)
# ---------------------------------------------------------------------------


@app.post("/api/recordings")
async def upload_recording(
    file: UploadFile = File(...),
    parent_video_id: str = Form(...),
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    """Save a self-recording (webm/mp4 from the user's webcam) and link it
    to the source library entry they were practicing along to.

    The recording is added to the user's library as a separate entry with
    `kind="recording"` and `parent_video_id` pointing at the source.
    """
    # Confirm parent entry exists + user has access (owner or shared).
    try:
        resolve_entry_access(user.id, parent_video_id)
    except HTTPException:
        raise HTTPException(
            status_code=404,
            detail=f"Parent video '{parent_video_id}' not found in your library.",
        )

    suffix = Path(file.filename or "rec.webm").suffix or ".webm"
    if suffix.lower() not in (".webm", ".mp4", ".mov", ".mkv"):
        suffix = ".webm"
    rec_id = f"rec-{uuid.uuid4().hex[:10]}"
    dest = user_downloads_dir(user.id) / f"{rec_id}{suffix}"

    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    # Probe duration; if it fails we still record a 0-duration entry so the
    # upload isn't lost.
    duration = float(_probe_duration(dest) or 0)

    parent_entry = next(
        (i for i in _read_library(user.id) if i.get("video_id") == parent_video_id),
        None,
    )
    parent_title = parent_entry.get("title") if parent_entry else parent_video_id
    title = f"Recording of {parent_title} · {datetime.now(timezone.utc).strftime('%b %-d %H:%M')}"

    # No segments on recordings — they're free-form practice captures.
    user_data = user_data_dir(user.id)
    (user_data / f"{rec_id}.json").write_text("[]")

    video_url = f"/videos/{user.id}/{dest.name}"
    _record_library_entry(
        user_id=user.id,
        video_id=rec_id,
        video_url=video_url,
        title=title,
        duration=duration,
        segment_count=0,
        source="recording",
        kind="dance",
        parent_video_id=parent_video_id,
    )
    return JSONResponse(
        {
            "video_id": rec_id,
            "video_url": video_url,
            "duration": duration,
            "title": title,
            "parent_video_id": parent_video_id,
        }
    )


@app.get("/api/library/{video_id}/recordings")
async def list_recordings_for(
    video_id: str,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    """List the user's self-recordings linked to the given source entry."""
    out = [
        i
        for i in _read_library(user.id)
        if i.get("parent_video_id") == video_id
    ]
    return JSONResponse(out)


# ---------------------------------------------------------------------------
# Practice plans + activity tracker
# ---------------------------------------------------------------------------


class PlanItem(BaseModel):
    video_id: str
    rest_seconds: float = Field(default=0.0, ge=0)


class PlanRequest(BaseModel):
    name: str
    items: list[PlanItem]


class PracticeLogEntry(BaseModel):
    video_id: str
    segment_id: Optional[int] = None
    duration_seconds: float = Field(..., gt=0)


def _plan_to_dict(plan: auth.PracticePlan) -> dict:
    return {
        "id": plan.id,
        "name": plan.name,
        "items": plan.items,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }


@app.get("/api/plans")
async def list_practice_plans(
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    return JSONResponse([_plan_to_dict(p) for p in auth.list_plans(user.id)])


@app.post("/api/plans")
async def create_practice_plan(
    payload: PlanRequest,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    plan = auth.create_plan(
        user.id, payload.name, [i.model_dump() for i in payload.items]
    )
    return JSONResponse(_plan_to_dict(plan))


@app.put("/api/plans/{plan_id}")
async def update_practice_plan(
    plan_id: int,
    payload: PlanRequest,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    plan = auth.update_plan(
        user.id, plan_id, payload.name, [i.model_dump() for i in payload.items]
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return JSONResponse(_plan_to_dict(plan))


@app.delete("/api/plans/{plan_id}")
async def delete_practice_plan(
    plan_id: int,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    ok = auth.delete_plan(user.id, plan_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Plan not found")
    return JSONResponse({"ok": True})


@app.post("/api/practice-log")
async def record_practice_entry(
    payload: PracticeLogEntry,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    auth.record_practice(
        user.id, payload.video_id, payload.segment_id, payload.duration_seconds
    )
    return JSONResponse({"ok": True})


@app.get("/api/stats")
async def get_practice_stats(
    days: int = 7,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    days = max(1, min(days, 365))
    return JSONResponse(auth.stats_for_user(user.id, days))


def _strip_list_only_fields(entry: dict) -> dict:
    """Drop heavyweight fields that the list view doesn't need.

    `beat_times` can be a few hundred floats per video — fine on the per-entry
    endpoint, but multiplied across a user's whole library it bloats the list
    payload. Frontend re-fetches the entry detail when actually loading a
    video, so we only keep `bpm` (small, useful for badges) in list view.
    """
    return {k: v for k, v in entry.items() if k != "beat_times"}


@app.get("/api/library")
async def list_library(
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    """List all previously processed videos — both owned and shared with me."""
    out: list[dict] = []

    # Owned entries.
    for entry in _read_library(user.id):
        out.append(
            {
                **_strip_list_only_fields(_normalize_video_url(entry, user.id)),
                "permission": "owner",
                "owner_id": user.id,
            }
        )

    # Entries shared with this user.
    for share in auth.shares_for_recipient(user.id):
        owner_entries = _read_library(share.owner_id)
        owner_entry = next(
            (i for i in owner_entries if i.get("video_id") == share.video_id),
            None,
        )
        if not owner_entry:
            continue  # Share points at a deleted entry — skip silently.
        owner = auth.get_user(share.owner_id)
        out.append(
            {
                **_strip_list_only_fields(_normalize_video_url(owner_entry, share.owner_id)),
                "permission": share.permission,
                "owner_id": share.owner_id,
                "shared_by_username": owner.username if owner else None,
            }
        )

    return JSONResponse(out)


@app.get("/api/library/{video_id}")
async def load_library_entry(
    video_id: str,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    """Load a video the user owns OR has been granted view/edit access to."""
    owner_id, permission = resolve_entry_access(user.id, video_id)
    owner_data = user_data_dir(owner_id)
    owner_dl = user_downloads_dir(owner_id)

    entry = next(
        (i for i in _read_library(owner_id) if i.get("video_id") == video_id),
        None,
    )
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown video_id: {video_id}")

    seg_path = owner_data / f"{video_id}.json"
    if not seg_path.exists():
        raise HTTPException(
            status_code=404, detail=f"Segments file missing for {video_id}"
        )
    segments = json.loads(seg_path.read_text())

    video_filename = entry.get("video_url", "").split("/")[-1]
    video_exists = (owner_dl / video_filename).exists() if video_filename else False

    # Mirror to the requesting user's own sequence file so /api/sequence works.
    user_data_dir(user.id).joinpath("sequence.json").write_text(
        json.dumps(segments, indent=2)
    )

    return JSONResponse(
        {
            **_normalize_video_url(entry, owner_id),
            "segments": segments,
            "video_exists": video_exists,
            "permission": permission,
            "owner_id": owner_id,
        }
    )


class LibraryUpdate(BaseModel):
    segments: list[SegmentItem]
    crop_start: Optional[float] = Field(default=None, ge=0)
    crop_end: Optional[float] = Field(default=None, gt=0)


@app.put("/api/library/{video_id}")
async def update_library_entry(
    video_id: str,
    payload: LibraryUpdate,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    """Save manual edits — allowed for owner or anyone with 'edit' permission."""
    owner_id, permission = resolve_entry_access(user.id, video_id)
    if permission == "view":
        raise HTTPException(
            status_code=403,
            detail="You have view-only access to this video. Ask the owner for edit access.",
        )

    sorted_segs = sorted(payload.segments, key=lambda s: s.start)
    out = []
    for i, s in enumerate(sorted_segs, start=1):
        if s.end <= s.start:
            raise HTTPException(
                status_code=400,
                detail=f"Segment {i}: end ({s.end}) must be greater than start ({s.start}).",
            )
        item = {
            "id": i,
            "label": s.label or f"Segment {i}",
            "start": round(s.start, 2),
            "end": round(s.end, 2),
        }
        if s.lyrics is not None and s.lyrics.strip():
            item["lyrics"] = s.lyrics
        out.append(item)

    # Persist edits onto the OWNER's archive (shared edits go to the original).
    owner_data = user_data_dir(owner_id)
    (owner_data / f"{video_id}.json").write_text(json.dumps(out, indent=2))
    # The viewer also keeps a local sequence.json so the UI loads cleanly.
    user_data_dir(user.id).joinpath("sequence.json").write_text(
        json.dumps(out, indent=2)
    )

    updated = _update_library_entry(
        owner_id,
        video_id,
        {
            "segment_count": len(out),
            "last_edited_at": datetime.now(timezone.utc).isoformat(),
            "crop_start": payload.crop_start,
            "crop_end": payload.crop_end,
        },
    )
    return JSONResponse({**(updated or {}), "segments": out})


class CopySegmentsRequest(BaseModel):
    source_video_id: str


@app.post("/api/library/{video_id}/copy-segments-from")
async def copy_segments_from(
    video_id: str,
    payload: CopySegmentsRequest,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    """Replace the target entry's segments with the source entry's boundaries.

    Copies only start/end times — labels are reset to generic ones, lyrics
    are not carried over. Use case: same song, different recording — saves
    re-doing all the boundary editing.

    The caller must own/have-edit on the target; they must have at least
    view access on the source.
    """
    target_owner_id, target_permission = resolve_entry_access(user.id, video_id)
    if target_permission == "view":
        raise HTTPException(
            status_code=403,
            detail="You have view-only access to this video — can't replace its segments.",
        )

    source_owner_id, _ = resolve_entry_access(user.id, payload.source_video_id)

    source_seg_path = user_data_dir(source_owner_id) / f"{payload.source_video_id}.json"
    if not source_seg_path.exists():
        raise HTTPException(
            status_code=404, detail="Source segments file missing."
        )
    try:
        source_segments = json.loads(source_seg_path.read_text())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Source segments JSON corrupt.")
    if not isinstance(source_segments, list) or not source_segments:
        raise HTTPException(status_code=400, detail="Source has no segments to copy.")

    # Clip to target's known duration so we don't end up with segments past
    # the end of a shorter target video.
    target_entry = next(
        (i for i in _read_library(target_owner_id) if i.get("video_id") == video_id),
        None,
    )
    target_duration = float(target_entry.get("duration") or 0.0) if target_entry else 0.0

    out: list[dict] = []
    for s in source_segments:
        start = float(s.get("start", 0))
        end = float(s.get("end", 0))
        if target_duration > 0:
            start = min(start, target_duration)
            end = min(end, target_duration)
        if end <= start:
            continue  # zero or negative-length segment after clipping → drop
        out.append({
            "id": len(out) + 1,
            "label": f"Segment {len(out) + 1}",
            "start": round(start, 2),
            "end": round(end, 2),
        })

    if not out:
        raise HTTPException(
            status_code=400,
            detail="Nothing to copy — every source segment falls past the target's duration.",
        )

    target_data_dir = user_data_dir(target_owner_id)
    (target_data_dir / f"{video_id}.json").write_text(json.dumps(out, indent=2))
    # The viewer's local sequence mirror, so /api/sequence stays current.
    user_data_dir(user.id).joinpath("sequence.json").write_text(json.dumps(out, indent=2))

    updated = _update_library_entry(
        target_owner_id,
        video_id,
        {
            "segment_count": len(out),
            "last_edited_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return JSONResponse(
        {
            **(updated or {}),
            "segments": out,
            "copied_from": payload.source_video_id,
            "copied_count": len(out),
            "dropped_count": len(source_segments) - len(out),
        }
    )


class LibraryPatch(BaseModel):
    """Partial update — only fields the client sets are touched."""
    title: Optional[str] = None
    crop_start: Optional[float] = Field(default=None, ge=0)
    crop_end: Optional[float] = Field(default=None, gt=0)


@app.patch("/api/library/{video_id}")
async def patch_library_entry(
    video_id: str,
    payload: LibraryPatch,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    """Rename a video or update its crop bounds (owner only)."""
    items = _read_library(user.id)
    if all(i.get("video_id") != video_id for i in items):
        raise HTTPException(
            status_code=404,
            detail="Only the video's owner can rename it or change its crop bounds.",
        )

    patch = payload.model_dump(exclude_unset=True)
    if "title" in patch:
        title = (patch["title"] or "").strip()[:200]
        patch["title"] = title or f"Video {video_id}"

    if not patch:
        return JSONResponse(next(i for i in items if i["video_id"] == video_id))

    patch["last_edited_at"] = datetime.now(timezone.utc).isoformat()
    updated = _update_library_entry(user.id, video_id, patch)
    return JSONResponse(updated or {})


# ---------------------------------------------------------------------------
# Sharing endpoints
# ---------------------------------------------------------------------------


class ShareRequest(BaseModel):
    username: str
    permission: Literal["view", "edit"] = "view"


def _require_ownership(user_id: int, video_id: str) -> None:
    if all(i.get("video_id") != video_id for i in _read_library(user_id)):
        raise HTTPException(
            status_code=403,
            detail="Only the video's owner can manage its sharing.",
        )


@app.get("/api/library/{video_id}/shares")
async def list_shares(
    video_id: str,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    _require_ownership(user.id, video_id)
    shares = auth.shares_for_video(user.id, video_id)
    return JSONResponse(
        [
            {
                "shared_with_id": s.shared_with_id,
                "shared_with_username": s.shared_with_username,
                "permission": s.permission,
                "created_at": s.created_at,
            }
            for s in shares
        ]
    )


@app.post("/api/library/{video_id}/shares")
async def create_share_endpoint(
    video_id: str,
    payload: ShareRequest,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    _require_ownership(user.id, video_id)
    recipient = auth.find_user_by_username(payload.username)
    if not recipient:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No account named '{payload.username}'. "
                "If they haven't signed up yet, use a share link instead — "
                "they'll be prompted to create an account when they click it."
            ),
        )
    try:
        auth.create_share(user.id, video_id, recipient.id, payload.permission)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(
        {
            "shared_with_id": recipient.id,
            "shared_with_username": recipient.username,
            "permission": payload.permission,
        }
    )


@app.delete("/api/library/{video_id}/shares/{shared_with_id}")
async def revoke_share(
    video_id: str,
    shared_with_id: int,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    _require_ownership(user.id, video_id)
    removed = auth.delete_share(user.id, video_id, shared_with_id)
    if not removed:
        raise HTTPException(status_code=404, detail="No such share")
    return JSONResponse({"ok": True})


# --- Share links ---------------------------------------------------------


class ShareLinkRequest(BaseModel):
    permission: Literal["view", "edit"] = "view"


def _absolute_share_url(request: Request, token: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/?share={token}"


@app.post("/api/library/{video_id}/share-links")
async def create_share_link(
    video_id: str,
    payload: ShareLinkRequest,
    request: Request,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    _require_ownership(user.id, video_id)
    invite = auth.create_share_invite(user.id, video_id, payload.permission)
    return JSONResponse(
        {
            "token": invite.token,
            "permission": invite.permission,
            "url": _absolute_share_url(request, invite.token),
            "created_at": invite.created_at,
        }
    )


@app.get("/api/library/{video_id}/share-links")
async def list_share_links(
    video_id: str,
    request: Request,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    _require_ownership(user.id, video_id)
    invites = auth.list_share_invites(user.id, video_id)
    return JSONResponse(
        [
            {
                "token": i.token,
                "permission": i.permission,
                "url": _absolute_share_url(request, i.token),
                "created_at": i.created_at,
            }
            for i in invites
        ]
    )


@app.delete("/api/library/{video_id}/share-links/{token}")
async def revoke_share_link(
    video_id: str,
    token: str,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    _require_ownership(user.id, video_id)
    removed = auth.delete_share_invite(user.id, token)
    if not removed:
        raise HTTPException(status_code=404, detail="No such share link")
    return JSONResponse({"ok": True})


@app.get("/api/share/{token}/preview")
async def preview_share_invite(token: str) -> JSONResponse:
    """Public — used by the landing page to say 'X wants to share Y with you'."""
    invite = auth.find_share_invite(token)
    if not invite:
        raise HTTPException(status_code=404, detail="This share link is invalid or has been revoked.")
    owner = auth.get_user(invite.owner_id)
    title = None
    for entry in _read_library(invite.owner_id):
        if entry.get("video_id") == invite.video_id:
            title = entry.get("title")
            break
    return JSONResponse(
        {
            "video_id": invite.video_id,
            "permission": invite.permission,
            "owner_username": owner.username if owner else None,
            "title": title,
        }
    )


@app.post("/api/share/{token}/accept")
async def accept_share_invite(
    token: str,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    """Redeem a share link as the currently-authenticated user."""
    invite = auth.find_share_invite(token)
    if not invite:
        raise HTTPException(status_code=404, detail="This share link is invalid or has been revoked.")
    if invite.owner_id == user.id:
        raise HTTPException(
            status_code=400,
            detail="This is your own video — no need to redeem the link.",
        )
    try:
        auth.create_share(invite.owner_id, invite.video_id, user.id, invite.permission)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(
        {
            "video_id": invite.video_id,
            "permission": invite.permission,
        }
    )


class CombineLibraryRequest(BaseModel):
    video_ids: list[str]
    model_variant: Literal["lite", "full", "heavy"] = "full"


@app.post("/api/library/combine", response_model=ProcessResponse)
async def combine_library_entries(
    payload: CombineLibraryRequest,
    user: auth.User = Depends(get_current_user),
) -> ProcessResponse:
    """Concatenate several library entries into a new routine owned by the
    requesting user, **preserving each source's segment markers** so the
    user's curated splits carry over (offset by cumulative clip duration).

    No pose extraction or re-segmentation runs — combining is purely an
    ffmpeg concat plus a segment-stitching operation. This is fast (no
    multi-minute MediaPipe pass) and respects the user's existing edits.
    """
    if len(payload.video_ids) < 2:
        raise HTTPException(
            status_code=400, detail="Pick at least 2 videos to combine."
        )
    if len(payload.video_ids) > 8:
        raise HTTPException(
            status_code=400, detail="At most 8 videos can be combined at once."
        )

    # 1. Resolve every video_id to its file path + cached segments, and
    #    probe each clip's actual duration so segment offsets align tightly.
    sources: list[dict] = []
    for vid in payload.video_ids:
        owner_id, _perm = resolve_entry_access(user.id, vid)
        owner_lib = _read_library(owner_id)
        entry = next((i for i in owner_lib if i.get("video_id") == vid), None)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Unknown video: {vid}")

        filename = entry.get("video_url", "").split("/")[-1]
        path = user_downloads_dir(owner_id) / filename if filename else None
        if not path or not path.is_file():
            raise HTTPException(
                status_code=410,
                detail=f"Video file for '{entry.get('title', vid)}' is no longer on disk.",
            )

        seg_path = user_data_dir(owner_id) / f"{vid}.json"
        try:
            segments = json.loads(seg_path.read_text()) if seg_path.exists() else []
        except json.JSONDecodeError:
            segments = []

        sources.append(
            {
                "path": path,
                "title": entry.get("title") or vid,
                "segments": segments,
                "duration": entry.get("duration") or 0.0,
                "crop_start": entry.get("crop_start"),
                "crop_end": entry.get("crop_end"),
            }
        )

    # Validate that we actually have segments to splice.
    if all(not s["segments"] for s in sources):
        raise HTTPException(
            status_code=400,
            detail="None of the chosen videos have segments yet. Process them first.",
        )

    # 2. Extract each segment as its own normalized clip, then concat them.
    #    This sidesteps every crop / trim-filter pitfall: we're just splicing
    #    standalone clips, not trimming inside a complex filter graph.
    user_dl = user_downloads_dir(user.id)
    video_id = f"combined-{uuid.uuid4().hex[:10]}"
    dest = user_dl / f"{video_id}.mp4"

    work_dir = user_dl / f".combine-{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    segment_clips: list[Path] = []
    combined_segments: list[dict] = []
    cumulative = 0.0

    try:
        for src in sources:
            for seg in src["segments"]:
                seg_start = float(seg["start"])
                seg_end = float(seg["end"])
                if seg_end <= seg_start:
                    continue
                seg_duration = seg_end - seg_start

                clip_path = work_dir / f"seg_{len(segment_clips):03d}.mp4"
                try:
                    await asyncio.to_thread(
                        _normalize_clip, src["path"], clip_path, seg_start, seg_end
                    )
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            f"Failed extracting segment '{seg.get('label', '?')}' "
                            f"from '{src['title']}': {e}"
                        ),
                    )
                segment_clips.append(clip_path)

                combined_segments.append(
                    {
                        "id": len(combined_segments) + 1,
                        "label": seg.get("label") or "Segment",
                        "start": round(cumulative, 2),
                        "end": round(cumulative + seg_duration, 2),
                    }
                )
                cumulative += seg_duration

        if not segment_clips:
            raise HTTPException(
                status_code=400, detail="No usable segments found in the selected videos."
            )

        # Concat-demux the segment clips. All have identical codec/format
        # thanks to _normalize_clip, so `-c copy` works flawlessly.
        manifest = work_dir / "concat.txt"
        manifest.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in segment_clips) + "\n"
        )
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(manifest),
            "-c", "copy",
            "-movflags", "+faststart",
            str(dest),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            msg = (result.stderr or "").strip().splitlines()[-3:]
            print(f"[combine] concat failed: {' | '.join(msg)}")
            raise HTTPException(
                status_code=500, detail="ffmpeg concat failed: " + " | ".join(msg)
            )
    finally:
        for p in segment_clips:
            p.unlink(missing_ok=True)
        try:
            (work_dir / "concat.txt").unlink(missing_ok=True)
            work_dir.rmdir()
        except OSError:
            pass

    # 3. Probe actual output duration for the library record.
    precise = await asyncio.to_thread(_probe_duration, dest)
    total_duration = precise if precise and precise > 0 else cumulative

    # 4. Persist segments + library entry. No pose extraction needed.
    user_data = user_data_dir(user.id)
    (user_data / f"{video_id}.json").write_text(
        json.dumps(combined_segments, indent=2)
    )
    (user_data / "sequence.json").write_text(
        json.dumps(combined_segments, indent=2)
    )

    combined_title = " + ".join(s["title"] for s in sources)[:200]
    video_url = f"/videos/{user.id}/{dest.name}"
    _record_library_entry(
        user_id=user.id,
        video_id=video_id,
        video_url=video_url,
        title=combined_title,
        duration=total_duration,
        segment_count=len(combined_segments),
        source="upload",
        crop_start=None,
        crop_end=None,
    )

    return ProcessResponse(
        video_id=video_id,
        video_url=video_url,
        duration=total_duration,
        segment_count=len(combined_segments),
        segments=combined_segments,
    )


@app.delete("/api/library/{video_id}")
async def delete_library_entry(
    video_id: str,
    purge_files: bool = False,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    """Remove a library entry. Pass ?purge_files=true to also delete the MP4."""
    items = _read_library(user.id)
    new_items = [i for i in items if i.get("video_id") != video_id]
    if len(new_items) == len(items):
        raise HTTPException(status_code=404, detail=f"Unknown video_id: {video_id}")
    _write_library(user.id, new_items)

    seg_path = user_data_dir(user.id) / f"{video_id}.json"
    if seg_path.exists():
        seg_path.unlink()
    # Drop the persisted pose data + meta so it doesn't leak into the tuner.
    _pose_path(user.id, video_id).unlink(missing_ok=True)
    _pose_meta_path(user.id, video_id).unlink(missing_ok=True)
    if purge_files:
        for mp4 in user_downloads_dir(user.id).glob(f"{video_id}.*"):
            mp4.unlink(missing_ok=True)

    return JSONResponse({"ok": True, "remaining": len(new_items)})


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------


class CredentialsRequest(BaseModel):
    username: str
    password: str


def _build_session_response(user: auth.User, request: Request) -> JSONResponse:
    """Mint a session for `user` and attach it as a cookie to the response."""
    token = auth.create_session(user.id)
    resp = JSONResponse({"id": user.id, "username": user.username})
    resp.set_cookie(
        key=auth.SESSION_COOKIE,
        value=token,
        max_age=int(auth.SESSION_LIFETIME.total_seconds()),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return resp


def _claim_legacy_library(user_id: int) -> None:
    """One-shot migration: if there's pre-auth data at the root of DATA_DIR /
    DOWNLOADS_DIR (from before per-user isolation), move it into this user's
    folder. Only runs when this is the *first* account on the server."""
    if auth.count_users() != 1:
        return
    legacy_lib = DATA_DIR / "library.json"
    if not legacy_lib.exists():
        return

    user_data = user_data_dir(user_id)
    user_dl = user_downloads_dir(user_id)

    # Move library.json
    (user_data / "library.json").write_text(legacy_lib.read_text())
    legacy_lib.unlink()

    # Move per-video segments JSONs (everything in DATA_DIR root that's a .json
    # except the auth DB, library, sequence).
    for p in DATA_DIR.iterdir():
        if p.is_dir() or p.name in ("users.db", "sequence.json"):
            continue
        if p.suffix == ".json":
            (user_data / p.name).write_text(p.read_text())
            p.unlink()

    # Move MP4s (and anything else that looks like a video file).
    for p in DOWNLOADS_DIR.iterdir():
        if p.is_dir():
            continue
        if p.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv"):
            p.rename(user_dl / p.name)


@app.post("/api/auth/signup")
async def signup(payload: CredentialsRequest, request: Request) -> JSONResponse:
    try:
        user = auth.create_user(payload.username, payload.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        _claim_legacy_library(user.id)
    except Exception as e:
        # Don't block signup if migration has issues — surface it in logs only.
        print(f"Legacy migration failed for user {user.id}: {e}")
    return _build_session_response(user, request)


@app.post("/api/auth/login")
async def login(payload: CredentialsRequest, request: Request) -> JSONResponse:
    user = auth.authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    return _build_session_response(user, request)


@app.post("/api/auth/logout")
async def logout(session: Optional[str] = Cookie(default=None)) -> JSONResponse:
    auth.delete_session(session)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
async def me(user: auth.User = Depends(get_current_user)) -> JSONResponse:
    return JSONResponse({"id": user.id, "username": user.username})


class UsernameUpdate(BaseModel):
    username: str


@app.patch("/api/auth/me")
async def update_my_username(
    payload: UsernameUpdate,
    user: auth.User = Depends(get_current_user),
) -> JSONResponse:
    try:
        updated = auth.update_username(user.id, payload.username)
    except auth.AuthError as e:
        # 409 Conflict for collisions, 400 for validation errors.
        status = 409 if "taken" in str(e).lower() else 400
        raise HTTPException(status_code=status, detail=str(e))
    return JSONResponse({"id": updated.id, "username": updated.username})


# ---------------------------------------------------------------------------
# Video serving — replaces the old /videos static mount with per-user auth.
# ---------------------------------------------------------------------------


@app.get("/videos/{owner_id}/{filename}")
async def serve_video(
    owner_id: int,
    filename: str,
    user: auth.User = Depends(get_current_user),
) -> FileResponse:
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Owner always allowed; everyone else needs a share for this video_id.
    if user.id != owner_id:
        video_id = Path(filename).stem
        if auth.find_share(owner_id, video_id, user.id) is None:
            raise HTTPException(status_code=403, detail="No access to this video.")

    path = user_downloads_dir(owner_id) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(path)


# Back-compat: redirect the pre-share single-segment URL to the owner-prefixed
# form, assuming the current user owns it (the only case where the old URL
# could possibly resolve correctly).
@app.get("/videos/{filename}")
async def serve_video_legacy(
    filename: str,
    user: auth.User = Depends(get_current_user),
) -> FileResponse:
    return await serve_video(user.id, filename, user)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


# Serve the frontend bundle last so `/api/*` and the explicit mounts above win.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


# ---------------------------------------------------------------------------
# Uvicorn entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
