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

from backend import auth, downloader, pose_extractor, segmenter, sequence_builder, tuner

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
) -> dict:
    """Upsert a library entry. Most-recent processing of a given video_id wins."""
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

    # 2. Extract pose landmarks (optionally cropped) and persist for the tuner.
    pose_df = pose_extractor.extract_pose_landmarks(
        video_path,
        model_variant=req.model_variant,
        sample_every_n_frames=req.sample_every_n_frames,
        start_time=req.start_time,
        end_time=req.end_time,
    )
    _save_pose_data(user_id, video_id, pose_df)

    # 3. Tune segmenter params against the user's past manual edits, then
    #    segment with the chosen params. Defaults apply if there's no history.
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

    # 4. Build clean sequence + persist to the user's data folder
    sequence, _ = sequence_builder.build_and_save(
        segments, filename="sequence.json", data_dir=user_data
    )
    sequence_builder.save_sequence(
        sequence, filename=f"{video_id}.json", data_dir=user_data
    )

    duration = float(pose_df["timestamp"].max()) if not pose_df.empty else 0.0
    video_url = f"/videos/{user_id}/{video_path.name}"

    _record_library_entry(
        user_id=user_id,
        video_id=video_id,
        video_url=video_url,
        title=title,
        duration=duration,
        segment_count=len(sequence),
        source="youtube",
        source_url=str(req.url),
        crop_start=req.start_time,
        crop_end=req.end_time,
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
) -> ProcessResponse:
    """Same as `_run_pipeline` but skipping the download step."""
    user_data = user_data_dir(user_id)
    video_id = video_path.stem
    title = (
        Path(original_filename).stem if original_filename else video_id
    )

    pose_df = pose_extractor.extract_pose_landmarks(
        video_path,
        model_variant=model_variant,
        sample_every_n_frames=sample_every_n_frames,
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
    sequence, _ = sequence_builder.build_and_save(
        segments, filename="sequence.json", data_dir=user_data
    )
    sequence_builder.save_sequence(
        sequence, filename=f"{video_id}.json", data_dir=user_data
    )

    duration = float(pose_df["timestamp"].max()) if not pose_df.empty else 0.0
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
    )


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


def _concat_videos(
    input_paths: list[Path],
    output_path: Path,
    trims: Optional[list[tuple[Optional[float], Optional[float]]]] = None,
) -> None:
    """Concatenate N videos into one MP4 in a single ffmpeg pass.

    Optional per-input trim: `trims[i]` is a `(start, end)` pair in seconds,
    either bound may be `None` for "no trim on this end". This lets callers
    apply each source's crop_start / crop_end while concatenating, so the
    output only contains the meaningful portion of each clip.

    Each input's audio is normalized to 44.1 kHz stereo via `aformat` so the
    concat filter doesn't choke on mismatched sample rates / channel layouts.

    Uses `ultrafast` preset — the output is fine for playback and follow-on
    pose extraction, and slower presets cost CPU we don't have on Fly's
    shared instance.

    Requires every input to have an audio track.
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

    n = len(input_paths)
    cmd: list[str] = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for p in input_paths:
        cmd.extend(["-i", str(p)])

    def _trim_clause(start: Optional[float], end: Optional[float], audio: bool) -> str:
        bits: list[str] = []
        if start is not None:
            bits.append(f"start={start:.3f}")
        if end is not None:
            bits.append(f"end={end:.3f}")
        if not bits:
            return ""
        name = "atrim" if audio else "trim"
        return f"{name}={':'.join(bits)},"

    parts: list[str] = []
    for i in range(n):
        ts, te = trims[i]
        parts.append(
            f"[{i}:v]"
            f"{_trim_clause(ts, te, audio=False)}"
            f"setpts=PTS-STARTPTS,"
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30,format=yuv420p"
            f"[v{i}];"
        )
        parts.append(
            f"[{i}:a]"
            f"{_trim_clause(ts, te, audio=True)}"
            f"asetpts=PTS-STARTPTS,"
            f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"aresample=async=1"
            f"[a{i}];"
        )
    chain = "".join(f"[v{i}][a{i}]" for i in range(n))
    filter_str = "".join(parts) + f"{chain}concat=n={n}:v=1:a=1[v][a]"

    cmd.extend(
        [
            "-filter_complex", filter_str,
            "-map", "[v]",
            "-map", "[a]",
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
        raise RuntimeError("ffmpeg concat failed: " + " | ".join(msg))


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
        out.append(
            {
                "id": i,
                "label": s.label or f"Segment {i}",
                "start": round(s.start, 2),
                "end": round(s.end, 2),
            }
        )
    (user_data_dir(user.id) / "sequence.json").write_text(json.dumps(out, indent=2))
    return JSONResponse(out)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


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
                **_normalize_video_url(entry, user.id),
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
                **_normalize_video_url(owner_entry, share.owner_id),
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
        out.append(
            {
                "id": i,
                "label": s.label or f"Segment {i}",
                "start": round(s.start, 2),
                "end": round(s.end, 2),
            }
        )

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

    # 2. ffmpeg concat into a new MP4 owned by the requesting user.
    #    Pass each source's crop window as a trim — only the cropped portion
    #    of each clip ends up in the combined output.
    user_dl = user_downloads_dir(user.id)
    video_id = f"combined-{uuid.uuid4().hex[:10]}"
    dest = user_dl / f"{video_id}.mp4"
    trims: list[tuple[Optional[float], Optional[float]]] = [
        (s["crop_start"], s["crop_end"]) for s in sources
    ]
    try:
        await asyncio.to_thread(
            _concat_videos, [s["path"] for s in sources], dest, trims
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Combine failed: {e}")

    # 3. Stitch segments. Each source's segments are stored at absolute
    #    timestamps within the *original* clip. After trimming away the
    #    leading crop_start seconds and placing the trimmed clip at the
    #    current cumulative offset, every segment timestamp shifts by
    #    (-crop_start + cumulative).
    probed_total = 0.0
    cumulative = 0.0
    combined_segments: list[dict] = []
    for src in sources:
        crop_start = float(src["crop_start"]) if src["crop_start"] is not None else 0.0
        crop_end = src["crop_end"]
        # Trimmed-clip duration: prefer the explicit crop window, fall back
        # to the cached full-clip duration if no crop is set.
        if crop_end is not None:
            trimmed_dur = float(crop_end) - crop_start
        else:
            trimmed_dur = float(src["duration"]) - crop_start

        for s in src["segments"]:
            new_start = round(float(s["start"]) - crop_start + cumulative, 2)
            new_end = round(float(s["end"]) - crop_start + cumulative, 2)
            # Skip anything that falls outside this clip's window in the
            # combined timeline.
            if new_end <= cumulative or new_start >= cumulative + trimmed_dur:
                continue
            # Clamp to the cropped clip's boundaries.
            new_start = max(cumulative, new_start)
            new_end = min(cumulative + trimmed_dur, new_end)
            combined_segments.append(
                {
                    "id": 0,
                    "label": s.get("label") or "Segment",
                    "start": new_start,
                    "end": new_end,
                }
            )
        cumulative += trimmed_dur
        probed_total += trimmed_dur

    # Re-probe the actual file duration to catch tiny drift from fps
    # normalization (uses precise, may differ from sum by milliseconds).
    precise = await asyncio.to_thread(_probe_duration, dest)
    if precise and precise > 0:
        probed_total = precise

    # Drop any segment that extends past the real duration (defensive).
    combined_segments = [
        s for s in combined_segments if s["end"] <= probed_total + 0.5
    ]
    for i, s in enumerate(combined_segments, start=1):
        s["id"] = i

    # 4. Persist the stitched segments + library entry. No pose extraction.
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
        duration=probed_total,
        segment_count=len(combined_segments),
        source="upload",
        crop_start=None,
        crop_end=None,
    )

    return ProcessResponse(
        video_id=video_id,
        video_url=video_url,
        duration=probed_total,
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
