# CLAUDE.md

Context for AI assistants (Claude Code, etc.) working in this repo. Update
this file when you add features, change architecture, or learn things
worth remembering across sessions.

## What this project is

Web app for practicing **dance choreography** and **vocal sections**.
Users upload a video or paste a YouTube URL → server detects segments →
user drills them with looping, slow-motion, a guided "workshop" mode,
mirror modes, webcam mirror, audio-only memory mode, etc. Multi-user with
accounts, library, sharing, and the ability to combine multiple library
entries into a new routine.

Live at https://dance-step-splitter.fly.dev (Fly.io).

## Stack

- **Backend**: FastAPI + uvicorn. Python 3.11 in the Docker image,
  Python 3.9 in the local venv.
- **Frontend**: Vanilla JS + Tailwind via CDN. Single-page app — no
  build step, no bundler. `frontend/index.html` + `frontend/app.js`.
- **Storage**: SQLite (stdlib `sqlite3`) for users, sessions, shares,
  share links, practice plans, practice log. Everything else is flat JSON
  + media files on disk under per-user folders.
- **Auth**: PBKDF2-HMAC-SHA256 (600k iterations) password hashing,
  HttpOnly session cookies, all in `backend/auth.py`.
- **Pose / segmentation**: MediaPipe Pose for dance; ffmpeg's
  `silencedetect` filter for singing.
- **Video tools**: ffmpeg (installed in Dockerfile), yt-dlp for downloads.
- **Deploy**: Fly.io. GitHub Actions auto-deploys on push to `main`.

## Repo layout

```
main.py                      # All FastAPI routes (~1500 lines)
backend/
  auth.py                    # SQLite schema + helpers (users, sessions,
                             #   shares, share-invites, plans, log)
  downloader.py              # yt-dlp wrapper with player-client fallback
  pose_extractor.py          # MediaPipe pose extraction (dance)
  segmenter.py               # Kinematic velocity-minima segmentation
  audio_segmenter.py         # ffmpeg-silence-based segmentation (singing)
  sequence_builder.py        # Convert StepSegment list → sequence.json
  tuner.py                   # Per-user grid-search for segmenter params
frontend/
  index.html                 # All markup, all CSS in inline <style>
  app.js                     # ~2700 lines, vanilla JS, all logic
  audio/1.wav … 8.wav        # Pre-rendered "one"…"eight" count voices
                             #   (macOS `say -v Samantha`)
data/                        # symlinked to /persistent/data on Fly
  users.db                   # SQLite auth + plans + log
  users/<id>/
    library.json             # User's library entries
    <video_id>.json          # Per-video segments
    <video_id>.pose.csv.gz   # Persisted pose data (for tuner)
    <video_id>.pose.meta.json
    sequence.json            # Most recent sequence (for /api/sequence)
downloads/                   # symlinked to /persistent/downloads on Fly
  users/<id>/
    <video_id>.mp4           # The actual video files
    rec-<uuid>.{webm,mp4}    # Self-recordings
scripts/
  rename_user.py             # Admin one-off
  migrate_to_fly.sh          # Local → Fly data migration
Dockerfile                   # python:3.11-slim + ffmpeg + opencv runtime deps
fly.toml                     # 1 vCPU, 1 GB RAM, 1 GB volume
.github/workflows/fly-deploy.yml
```

## Running locally

```bash
cd ~/dance-step-splitter
./venv/bin/python main.py    # serves on http://localhost:8000
```

Parse-check after edits:

```bash
node -e "new Function(require('fs').readFileSync('frontend/app.js','utf8')); console.log('app.js parses OK');"
./venv/bin/python -c "import main; print('main.py imports OK')" 2>&1 | grep -v Warning
```

## Deploying

```bash
git add -A && git commit -m "<message>" && git push origin main
```

GitHub Actions builds remotely on Fly's builder and deploys in ~2 min.
Hard-reload the browser after (`Cmd+Shift+R`) to bust the cached
`frontend/app.js`.

Direct: `fly deploy` (same thing, just bypasses GitHub).

## Conventions / things to know

- **No package.json / no build step on the frontend.** Don't add one
  unless the user explicitly asks. Edits go straight to `app.js`.
- **Tailwind is loaded from CDN.** Class strings must be literal — JIT
  works with arbitrary values like `lg:grid-cols-[1fr_280px]`. There's a
  console warning about CDN-in-production; we ignore it.
- **No tests.** Verification is manual + parse-checks + the in-line
  smoke tests embedded in `Bash` calls.
- **Tasks are CPU-heavy.** Pose extraction takes 3-5 min for a 5 min
  clip on Fly's shared-cpu-1x. Combining concatenates via ffmpeg, also
  slow. Tune patience accordingly.
- **YouTube throttling on Fly is real.** The downloader rotates through
  5 player-client combos. Even so, cloud-IP downloads frequently fall
  back to lower-resolution single-file streams. The UI warns about this.
- **Singing mode skips MediaPipe entirely** and uses ffmpeg's
  `silencedetect` filter for ~10× faster processing.
- **Per-user file paths.** Always use `user_data_dir(user_id)` and
  `user_downloads_dir(user_id)`, never raw `DATA_DIR / ...`. Sharing
  uses `resolve_entry_access(user_id, video_id)` to find the owner's
  files.
- **Library entries store `video_url` as `/videos/<owner_id>/<file>`.**
  The `/videos/{owner_id}/{filename}` route checks ownership or a valid
  share. Don't bypass this for cross-user access.
- **Segments are stored at absolute video-time** (not crop-relative).
  Crop bounds live on the library entry, not on segments.
- **Loop break is 0 between workshop reps but uses the user's `Break`
  setting between drills.** Manual loops always use the user's `Break`.

## Recent / current features

Latest additions, all working in prod:

- **Mobile layout pass**: viewport meta, safe-area insets, 40-44 px tap
  targets at ≤ 640 px.
- **Practice plans + tracker**: `practice_plans` + `practice_log` SQLite
  tables, Plans panel, 📊 Stats modal.
- **Self-recording**: 🔴 Record button → MediaRecorder on webcam →
  uploads to `/api/recordings` linked to the current video → Compare
  modal shows source + recording in sync.
- **Singing mode**: 🎤 Sing toggle, audio-energy segmentation, lyrics
  per segment + live "Now singing" panel, dance-only controls hidden.
- **Break overlay**: countdown over the video during loop breaks.
- **Combine library entries**: select N entries → splice each entry's
  segment clips into one new routine (preserves labels + lyrics).
- **Webcam Mirror**: mirrored selfie cam; gated behind Memory Mode.
- **Memory Mode**: blackout source video, audio keeps playing, custom
  playback bar with play/pause + scrubber.
- **Personalised segmenter tuning**: grid-search the segmenter against
  the user's past manual edits before processing new dance videos.
- **Sharing**: by username, or by share-link (auto-redeems after signup).
  View vs edit, ~15 s poll for collaborator updates.

## Things deliberately removed / NOT to add back

- **Karaoke / vocal-removal**. Tried ffmpeg center-channel subtraction
  (sounded bad) and Demucs (1 GB Docker bloat + minutes of CPU per song).
  Replaced with a Singing tip suggesting the user find a karaoke
  version on YouTube. Don't re-add unless the user explicitly asks for
  the heavy ML path.

## Known issues / TODO ideas

- Real-device mobile testing not done yet — DevTools simulation only.
- `recordLoopRep` only fires on loop wrap, so users who just play
  straight through don't get tracker entries (intentional, "deliberate
  practice" stats).
- Plan auto-advance is timer-based (entry duration + rest); doesn't
  pause when the user pauses.
- Recordings list does N+1 fetches when the library panel opens (one
  per card). Fine for now; batch later if it gets slow.

## Style notes for code changes

- Backend: type hints everywhere, `from __future__ import annotations`
  where needed (Python 3.9 doesn't like `X | None`).
- Frontend: vanilla `addEventListener`, no frameworks. DOM refs
  clustered at top of `app.js`. Functions roughly grouped by feature
  section with `/* ---------------- */` banner comments.
- Tailwind: prefer responsive utilities (`sm:`, `md:`, `lg:`) over
  custom media queries. Touch targets ≥ 40 px on mobile.

## When in doubt

Ask the user. They've iterated on this app extensively and have strong
opinions about UX. Prefer asking over guessing on UX decisions; default
to non-destructive engineering choices (no auto-deletion, no breaking
URL changes, etc.).
