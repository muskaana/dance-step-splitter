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
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

from backend import downloader, pose_extractor, segmenter, sequence_builder

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT / "frontend"
DATA_DIR = ROOT / "data"
DOWNLOADS_DIR = ROOT / "downloads"

for d in (DATA_DIR, DOWNLOADS_DIR):
    d.mkdir(exist_ok=True)

LIBRARY_PATH = DATA_DIR / "library.json"


def _read_library() -> list[dict]:
    if not LIBRARY_PATH.exists():
        return []
    try:
        return json.loads(LIBRARY_PATH.read_text())
    except json.JSONDecodeError:
        return []


def _write_library(items: list[dict]) -> None:
    LIBRARY_PATH.write_text(json.dumps(items, indent=2))


def _record_library_entry(
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
    items = _read_library()
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
        "segments_file": f"/data/{video_id}.json",
        "processed_at": now,
        "last_edited_at": None,
        "crop_start": crop_start,
        "crop_end": crop_end,
    }
    items.insert(0, entry)
    _write_library(items)
    return entry


def _update_library_entry(video_id: str, patch: dict) -> Optional[dict]:
    """Merge `patch` into the library entry with the given `video_id`."""
    items = _read_library()
    for i, item in enumerate(items):
        if item.get("video_id") == video_id:
            items[i] = {**item, **patch}
            _write_library(items)
            return items[i]
    return None

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
    quality: Literal["480p", "720p"] = "720p"
    model_variant: Literal["lite", "full", "heavy"] = "full"
    sample_every_n_frames: int = Field(default=1, ge=1, le=10)
    min_segment_duration: float = Field(default=4.0, gt=0)
    max_segment_duration: float = Field(default=8.0, gt=0)
    low_velocity_percentile: float = Field(default=15.0, gt=0, lt=100)
    smooth_window: int = Field(default=5, ge=1, le=60)
    cookies_from_browser: Optional[Literal["safari", "chrome", "firefox", "edge", "brave"]] = None
    start_time: Optional[float] = Field(default=None, ge=0)
    end_time: Optional[float] = Field(default=None, gt=0)


class ProcessResponse(BaseModel):
    video_id: str
    video_url: str
    duration: float
    segment_count: int
    segments: list[dict]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _run_pipeline(req: ProcessRequest) -> ProcessResponse:
    """Synchronous pipeline. Runs in a threadpool — see /api/process."""
    # 1. Download
    video_path, info = downloader.download_video(
        str(req.url),
        output_dir=DOWNLOADS_DIR,
        quality=req.quality,
        cookies_from_browser=req.cookies_from_browser,
    )
    video_id = video_path.stem
    title = (info.get("title") or video_id) if isinstance(info, dict) else video_id

    # 2. Extract pose landmarks (optionally cropped)
    pose_df = pose_extractor.extract_pose_landmarks(
        video_path,
        model_variant=req.model_variant,
        sample_every_n_frames=req.sample_every_n_frames,
        start_time=req.start_time,
        end_time=req.end_time,
    )

    # 3. Segment — dynamic kinematic thresholding, scoped to the crop window
    segments = segmenter.segment_steps(
        pose_df,
        crop_start=req.start_time,
        crop_end=req.end_time,
        smooth_window=req.smooth_window,
        low_velocity_percentile=req.low_velocity_percentile,
        min_segment_duration=req.min_segment_duration,
        max_segment_duration=req.max_segment_duration,
    )

    # 4. Build clean sequence + persist to data/sequence.json
    sequence, _ = sequence_builder.build_and_save(
        segments, filename="sequence.json", data_dir=DATA_DIR
    )

    # Also write a per-video copy for archival.
    sequence_builder.save_sequence(
        sequence, filename=f"{video_id}.json", data_dir=DATA_DIR
    )

    duration = float(pose_df["timestamp"].max()) if not pose_df.empty else 0.0
    video_url = f"/videos/{video_path.name}"

    _record_library_entry(
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
    )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.post("/api/process", response_model=ProcessResponse)
async def process_video(req: ProcessRequest) -> ProcessResponse:
    """Run the full pipeline and return the segment JSON for the UI."""
    try:
        return await asyncio.to_thread(_run_pipeline, req)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {e}")


def _run_file_pipeline(
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
    segments = segmenter.segment_steps(
        pose_df,
        crop_start=start_time,
        crop_end=end_time,
        smooth_window=smooth_window,
        low_velocity_percentile=low_velocity_percentile,
        min_segment_duration=min_segment_duration,
        max_segment_duration=max_segment_duration,
    )
    sequence, _ = sequence_builder.build_and_save(
        segments, filename="sequence.json", data_dir=DATA_DIR
    )
    sequence_builder.save_sequence(
        sequence, filename=f"{video_id}.json", data_dir=DATA_DIR
    )

    duration = float(pose_df["timestamp"].max()) if not pose_df.empty else 0.0
    video_url = f"/videos/{video_path.name}"

    _record_library_entry(
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
    )


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
) -> ProcessResponse:
    """Run the pose / segment / sequence pipeline on a user-uploaded video."""
    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    # Use the original basename (sanitized) so /videos/<name> stays predictable.
    safe_stem = "".join(c for c in Path(file.filename or "video").stem if c.isalnum() or c in "-_") or "video"
    dest = DOWNLOADS_DIR / f"{safe_stem}{suffix}"

    # Stream the upload to disk in chunks so big files don't blow up memory.
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    try:
        return await asyncio.to_thread(
            _run_file_pipeline,
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
async def get_current_sequence() -> JSONResponse:
    """Return the most recently generated sequence, if any."""
    path = DATA_DIR / "sequence.json"
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
async def update_sequence(segments: list[SegmentItem]) -> JSONResponse:
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
    path = DATA_DIR / "sequence.json"
    with path.open("w") as f:
        json.dump(out, f, indent=2)
    return JSONResponse(out)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/library")
async def list_library() -> JSONResponse:
    """List all previously processed videos (most-recent first)."""
    return JSONResponse(_read_library())


@app.get("/api/library/{video_id}")
async def load_library_entry(video_id: str) -> JSONResponse:
    """Load a previously processed video's segments + metadata.

    Also rewrites `data/sequence.json` so the next page reload still sees the
    most recently opened sequence.
    """
    entry = next(
        (i for i in _read_library() if i.get("video_id") == video_id), None
    )
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown video_id: {video_id}")

    seg_path = DATA_DIR / f"{video_id}.json"
    if not seg_path.exists():
        raise HTTPException(
            status_code=404, detail=f"Segments file missing for {video_id}"
        )
    segments = json.loads(seg_path.read_text())

    video_filename = entry.get("video_url", "").split("/")[-1]
    video_exists = (DOWNLOADS_DIR / video_filename).exists() if video_filename else False

    # Mirror the loaded sequence into sequence.json for the next page reload.
    (DATA_DIR / "sequence.json").write_text(json.dumps(segments, indent=2))

    return JSONResponse({**entry, "segments": segments, "video_exists": video_exists})


class LibraryUpdate(BaseModel):
    segments: list[SegmentItem]
    crop_start: Optional[float] = Field(default=None, ge=0)
    crop_end: Optional[float] = Field(default=None, gt=0)


@app.put("/api/library/{video_id}")
async def update_library_entry(video_id: str, payload: LibraryUpdate) -> JSONResponse:
    """Save manual edits to a library entry's segments and/or crop bounds."""
    if not _read_library() or all(
        i.get("video_id") != video_id for i in _read_library()
    ):
        raise HTTPException(status_code=404, detail=f"Unknown video_id: {video_id}")

    # Reuse the same canonicalization (sort by start, renumber ids) as PUT /api/sequence.
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

    # Persist to both the per-video archive and the working sequence file.
    (DATA_DIR / f"{video_id}.json").write_text(json.dumps(out, indent=2))
    (DATA_DIR / "sequence.json").write_text(json.dumps(out, indent=2))

    updated = _update_library_entry(
        video_id,
        {
            "segment_count": len(out),
            "last_edited_at": datetime.now(timezone.utc).isoformat(),
            "crop_start": payload.crop_start,
            "crop_end": payload.crop_end,
        },
    )
    return JSONResponse({**(updated or {}), "segments": out})


@app.delete("/api/library/{video_id}")
async def delete_library_entry(video_id: str, purge_files: bool = False) -> JSONResponse:
    """Remove a library entry. Pass ?purge_files=true to also delete the MP4."""
    items = _read_library()
    new_items = [i for i in items if i.get("video_id") != video_id]
    if len(new_items) == len(items):
        raise HTTPException(status_code=404, detail=f"Unknown video_id: {video_id}")
    _write_library(new_items)

    # Always remove the per-video segments file; only remove the MP4 if asked.
    seg_path = DATA_DIR / f"{video_id}.json"
    if seg_path.exists():
        seg_path.unlink()
    if purge_files:
        for mp4 in DOWNLOADS_DIR.glob(f"{video_id}.*"):
            mp4.unlink(missing_ok=True)

    return JSONResponse({"ok": True, "remaining": len(new_items)})


# ---------------------------------------------------------------------------
# Static assets — frontend, generated JSON, and streamable MP4s
# ---------------------------------------------------------------------------

# Mounting StaticFiles gives us Range-request support for free, which is what
# the HTML5 <video> element needs to seek through an MP4.
app.mount("/videos", StaticFiles(directory=DOWNLOADS_DIR), name="videos")
app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")


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
