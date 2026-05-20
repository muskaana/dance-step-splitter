/* Dance Step Splitter — frontend controller (with Workshop Mode). */

const video = document.getElementById("player");
const segmentGrid = document.getElementById("segment-grid");
const segmentCount = document.getElementById("segment-count");
const loopDisplay = document.getElementById("loop-display");
const clearBtn = document.getElementById("clear-btn");
const mirrorBtn = document.getElementById("mirror-btn");
const mirrorState = document.getElementById("mirror-state");
const speedControls = document.getElementById("speed-controls");
const videoFileInput = document.getElementById("video-file");

const workshopBtn = document.getElementById("workshop-btn");
const workshopBanner = document.getElementById("workshop-banner");
const workshopPhaseEl = document.getElementById("workshop-phase");
const workshopProgressEl = document.getElementById("workshop-progress");
const workshopRepsEl = document.getElementById("workshop-reps");
const prevDrillBtn = document.getElementById("prev-drill-btn");
const nextDrillBtn = document.getElementById("next-drill-btn");

const audioModeRow = document.getElementById("audio-mode");

const youtubeForm = document.getElementById("youtube-form");
const youtubeUrlInput = document.getElementById("youtube-url");
const youtubeQualitySelect = document.getElementById("youtube-quality");
const processBtn = document.getElementById("process-btn");
const processStatus = document.getElementById("process-status");

const libraryBtn = document.getElementById("library-btn");
const libraryPanel = document.getElementById("library-panel");
const libraryList = document.getElementById("library-list");
const libraryCount = document.getElementById("library-count");
const libraryCloseBtn = document.getElementById("library-close-btn");

const editSegmentsBtn = document.getElementById("edit-segments-btn");
const segmentEditor = document.getElementById("segment-editor");
const editorList = document.getElementById("editor-list");
const editorAddBtn = document.getElementById("editor-add-btn");
const editorSplitBtn = document.getElementById("editor-split-btn");
const editorSaveBtn = document.getElementById("editor-save-btn");
const editorCancelBtn = document.getElementById("editor-cancel-btn");
const editorStatus = document.getElementById("editor-status");

const cropStartInput = document.getElementById("crop-start");
const cropEndInput = document.getElementById("crop-end");
const cropStartRange = document.getElementById("crop-start-range");
const cropEndRange = document.getElementById("crop-end-range");
const cropDuration = document.getElementById("crop-duration");
const cropProcessBtn = document.getElementById("crop-process-btn");
const cropPreviewStartBtn = document.getElementById("crop-preview-start-btn");
const cropPreviewEndBtn = document.getElementById("crop-preview-end-btn");
const cropResetBtn = document.getElementById("crop-reset-btn");

/** Track the most recent file the user picked so the Process Video button
    can re-run the file pipeline with updated crop bounds. */
let pendingFile = null;

/** ID of the currently-loaded library entry (null until a video has been
    processed or opened from the library). Editor saves route here. */
let currentVideoId = null;

/** Pending crop bounds to apply once `loadedmetadata` fires — used when
    restoring a library entry that was processed with a crop. */
let pendingCrop = null;

/* ---------------------------------------------------------------- */
/* State                                                             */
/* ---------------------------------------------------------------- */

let allSegments = [];
let selectedSegments = [];        // manual selection
let loopBounds = null;            // active loop window (manual OR workshop)
const LOOP_EPS = 0.015;

/** Workshop state */
const workshop = {
  active: false,
  plan: [],            // [{ type, segmentIds, label, targetReps }]
  phaseIdx: 0,
  repsRemaining: 0,
};

/** Repetition targets per phase type (req 3). Tweak here to taste. */
const REPS_BY_TYPE = { drill: 3, combine: 2 };

/* ---------------------------------------------------------------- */
/* Data loading                                                      */
/* ---------------------------------------------------------------- */

async function loadSegments() {
  try {
    const res = await fetch("/data/sequence.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    console.warn("Falling back to sample segments:", err);
    return [
      { id: 1, label: "Segment 1", start: 0.0, end: 3.4 },
      { id: 2, label: "Segment 2", start: 3.4, end: 6.8 },
      { id: 3, label: "Segment 3", start: 6.8, end: 10.2 },
      { id: 4, label: "Segment 4", start: 10.2, end: 13.6 },
    ];
  }
}

/* ---------------------------------------------------------------- */
/* Segment grid rendering + manual selection                         */
/* ---------------------------------------------------------------- */

function renderSegments(segments) {
  segmentGrid.innerHTML = "";
  if (!segments.length) {
    segmentGrid.innerHTML =
      '<p class="col-span-full text-sm text-slate-500 text-center py-8">No segments available.</p>';
    return;
  }

  for (const seg of segments) {
    const btn = document.createElement("button");
    btn.className =
      "segment-btn px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm font-medium text-left hover:bg-slate-50 transition-all";
    btn.dataset.id = String(seg.id);
    btn.innerHTML = `
      <div class="font-semibold">${seg.label}</div>
      <div class="text-xs text-slate-500 font-mono">${seg.start.toFixed(2)}s – ${seg.end.toFixed(2)}s</div>
    `;
    btn.addEventListener("click", () => {
      if (workshop.active) return; // manual selection disabled in workshop mode
      toggleSegment(seg, btn);
    });
    segmentGrid.appendChild(btn);
  }
}

function toggleSegment(seg, btn) {
  const idx = selectedSegments.findIndex((s) => s.id === seg.id);
  if (idx >= 0) {
    selectedSegments.splice(idx, 1);
    btn.classList.remove("active");
  } else {
    selectedSegments.push(seg);
    btn.classList.add("active");
  }
  onManualSelectionChanged();
}

function clearSelection() {
  selectedSegments = [];
  document
    .querySelectorAll(".segment-btn.active")
    .forEach((b) => b.classList.remove("active"));
  onManualSelectionChanged();
}

function onManualSelectionChanged() {
  if (workshop.active) return; // workshop owns loopBounds
  loopBounds = calculateLoopBounds();
  resetCountBeat();
  updateLoopDisplay();
  segmentCount.textContent = `${selectedSegments.length} selected`;
  clearBtn.disabled = selectedSegments.length === 0;

  if (loopBounds) {
    if (video.currentTime < loopBounds.start || video.currentTime >= loopBounds.end) {
      video.currentTime = loopBounds.start;
    }
    if (video.paused) video.play().catch(() => {});
  }
}

/* ---------------------------------------------------------------- */
/* Loop math (manual selection)                                      */
/* ---------------------------------------------------------------- */

/**
 * Compute loop bounds from the active manual selection.
 * Earliest selected start → latest selected end. Null if empty.
 */
function calculateLoopBounds() {
  if (selectedSegments.length === 0) return null;
  let start = Infinity;
  let end = -Infinity;
  for (const seg of selectedSegments) {
    if (seg.start < start) start = seg.start;
    if (seg.end > end) end = seg.end;
  }
  return { start, end };
}

function updateLoopDisplay() {
  if (!loopBounds) {
    loopDisplay.textContent = "— → —";
    return;
  }
  loopDisplay.textContent = `${loopBounds.start.toFixed(2)}s → ${loopBounds.end.toFixed(2)}s`;
}

/* ---------------------------------------------------------------- */
/* Workshop plan generator                                           */
/* ---------------------------------------------------------------- */

/**
 * Generate the chunking-and-chaining lesson plan for N segments.
 *
 *   N=1: Drill 1
 *   N=2: Drill 1, Drill 2, Combine 1+2
 *   N=3: Drill 1, Drill 2, Combine 1+2, Drill 3, Combine 1+2+3
 *   ...
 *
 * Each phase has a target rep count from `REPS_BY_TYPE`.
 */
function buildWorkshopPlan(totalSegments) {
  const plan = [];
  for (let i = 1; i <= totalSegments; i++) {
    plan.push({
      type: "drill",
      segmentIds: [i],
      label: `Drilling Segment ${i}`,
      targetReps: REPS_BY_TYPE.drill,
    });
    if (i >= 2) {
      const ids = Array.from({ length: i }, (_, k) => k + 1);
      plan.push({
        type: "combine",
        segmentIds: ids,
        label: `Combine ${ids.join("+")}`,
        targetReps: REPS_BY_TYPE.combine,
      });
    }
  }
  return plan;
}

/* ---------------------------------------------------------------- */
/* Workshop engine                                                   */
/* ---------------------------------------------------------------- */

function startWorkshop() {
  if (!allSegments.length) return;

  clearSelection();
  workshop.active = true;
  workshop.plan = buildWorkshopPlan(allSegments.length);
  workshop.phaseIdx = 0;

  workshopBtn.textContent = "Stop Workshop";
  workshopBtn.classList.remove("bg-indigo-600", "hover:bg-indigo-700");
  workshopBtn.classList.add("bg-rose-600", "hover:bg-rose-700");
  workshopBanner.classList.remove("hidden");
  setSegmentsLocked(true);
  enterPhase(0);

  video.play().catch(() => {});
}

function stopWorkshop() {
  workshop.active = false;
  workshop.plan = [];
  workshop.phaseIdx = 0;
  workshop.repsRemaining = 0;

  workshopBtn.textContent = "Start Workshop";
  workshopBtn.classList.remove("bg-rose-600", "hover:bg-rose-700");
  workshopBtn.classList.add("bg-indigo-600", "hover:bg-indigo-700");
  workshopBanner.classList.add("hidden");
  setSegmentsLocked(false);

  loopBounds = null;
  highlightWorkshopSegments([]);
  updateLoopDisplay();
}

function enterPhase(idx) {
  if (idx < 0 || idx >= workshop.plan.length) {
    finishWorkshop();
    return;
  }
  workshop.phaseIdx = idx;
  const phase = workshop.plan[idx];
  workshop.repsRemaining = phase.targetReps;

  loopBounds = boundsForPhase(phase);
  resetCountBeat();
  workshopPhaseEl.textContent = phase.label;
  workshopProgressEl.textContent = `Phase ${idx + 1} of ${workshop.plan.length}`;
  workshopRepsEl.textContent = String(workshop.repsRemaining);
  workshopRepsEl.classList.remove("rep-pulse");
  updateLoopDisplay();
  highlightWorkshopSegments(phase.segmentIds);
  prevDrillBtn.disabled = idx === 0;
  nextDrillBtn.disabled = false;

  if (loopBounds) {
    video.currentTime = loopBounds.start;
    if (video.paused) video.play().catch(() => {});
  }
}

function finishWorkshop() {
  workshopPhaseEl.textContent = "Workshop complete! 🎉";
  workshopProgressEl.textContent = `Finished ${workshop.plan.length} phases`;
  workshopRepsEl.textContent = "0";
  loopBounds = null;
  highlightWorkshopSegments([]);
  // Auto-disable workshop after a beat so the user sees the message.
  setTimeout(() => {
    if (workshop.active) stopWorkshop();
  }, 1500);
}

function boundsForPhase(phase) {
  const segs = phase.segmentIds
    .map((id) => allSegments.find((s) => s.id === id))
    .filter(Boolean);
  if (!segs.length) return null;
  let start = Infinity;
  let end = -Infinity;
  for (const s of segs) {
    if (s.start < start) start = s.start;
    if (s.end > end) end = s.end;
  }
  return { start, end };
}

function nextDrill() {
  if (!workshop.active) return;
  enterPhase(workshop.phaseIdx + 1);
}

function previousDrill() {
  if (!workshop.active) return;
  enterPhase(Math.max(0, workshop.phaseIdx - 1));
}

function setSegmentsLocked(locked) {
  document.querySelectorAll(".segment-btn").forEach((b) => {
    b.classList.toggle("locked", locked);
  });
}

function highlightWorkshopSegments(ids) {
  const idSet = new Set(ids.map(String));
  document.querySelectorAll(".segment-btn").forEach((b) => {
    b.classList.toggle("workshop-current", idSet.has(b.dataset.id));
  });
}

/* ---------------------------------------------------------------- */
/* Voice counts (Web Audio API)                                      */
/* ---------------------------------------------------------------- */

const counts = {
  mode: "audio",           // "audio" | "counts"
  ctx: null,               // AudioContext (lazy)
  gain: null,              // master GainNode for counts
  voices: [],              // [{ kind: 'buffer'|'speech', payload }] x 8
  loaded: false,
  loadingPromise: null,
  lastBeatIdx: -1,         // most recently fired beat in the current loop
  schedulerRunning: false,
};

const COUNT_WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight"];
const SPEECH_AVAILABLE = typeof window !== "undefined" && "speechSynthesis" in window;

const VIDEO_VOLUME_BY_MODE = { audio: 1.0, counts: 0.05 };

function ensureAudioCtx() {
  if (counts.ctx) return counts.ctx;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  counts.ctx = new Ctor();
  counts.gain = counts.ctx.createGain();
  counts.gain.gain.value = 1.0;
  counts.gain.connect(counts.ctx.destination);
  return counts.ctx;
}

/** Build a short synthesized beep buffer for count `n` (1..8). */
function synthesizeCountBuffer(n) {
  const ctx = ensureAudioCtx();
  const sr = ctx.sampleRate;
  const dur = 0.18;
  const buf = ctx.createBuffer(1, Math.floor(sr * dur), sr);
  const ch = buf.getChannelData(0);
  // Pitch ramps up across counts so they're distinguishable: ~440 → ~880 Hz.
  const freq = 440 * Math.pow(2, (n - 1) / 7);
  for (let i = 0; i < ch.length; i++) {
    const t = i / sr;
    const env = Math.min(1, t * 60) * Math.exp(-t * 9); // quick attack + decay
    ch[i] = env * Math.sin(2 * Math.PI * freq * t);
  }
  return buf;
}

const COUNT_AUDIO_EXTS = ["wav", "mp3", "m4a", "ogg"];

/**
 * Resolve the voice source for count `n` (1..8), preferring quality:
 *   1. Pre-rendered audio file at `audio/<n>.{wav,mp3,m4a,ogg}` — reliable
 *      AudioBuffer playback, immune to the Chrome/Safari SpeechSynthesis
 *      degradation that happens with repeated cancel+speak calls.
 *   2. Web Speech API spoken word (works without files, OS voice).
 *   3. Synthesized beep (last resort if SpeechSynthesis is unavailable).
 */
async function loadCountVoice(n) {
  const ctx = ensureAudioCtx();
  for (const ext of COUNT_AUDIO_EXTS) {
    try {
      const res = await fetch(`audio/${n}.${ext}`, { cache: "force-cache" });
      if (!res.ok) continue;
      const arr = await res.arrayBuffer();
      const buffer = await ctx.decodeAudioData(arr);
      return { kind: "buffer", buffer };
    } catch {
      /* try next extension */
    }
  }
  if (SPEECH_AVAILABLE) {
    return { kind: "speech", text: COUNT_WORDS[n - 1] };
  }
  return { kind: "buffer", buffer: synthesizeCountBuffer(n) };
}

async function loadAllCounts() {
  if (counts.loaded) return;
  if (counts.loadingPromise) return counts.loadingPromise;
  counts.loadingPromise = (async () => {
    ensureAudioCtx();
    counts.voices = await Promise.all(
      [1, 2, 3, 4, 5, 6, 7, 8].map(loadCountVoice)
    );
    // Warm up the speech engine on macOS/Chrome — first utterance otherwise
    // has noticeable latency.
    if (SPEECH_AVAILABLE && counts.voices.some((v) => v.kind === "speech")) {
      try {
        const warm = new SpeechSynthesisUtterance(" ");
        warm.volume = 0;
        window.speechSynthesis.speak(warm);
      } catch {}
    }
    counts.loaded = true;
  })();
  return counts.loadingPromise;
}

function playCount(beatIdx) {
  const voice = counts.voices[beatIdx];
  if (!voice) return;

  if (voice.kind === "speech") {
    try {
      // Cancel any in-flight utterance so fast tempos don't queue up backlog.
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(voice.text);
      // Count voice plays at natural pitch/speed regardless of video.playbackRate.
      // Only the *spacing between counts* tracks playbackRate (the scheduler
      // is driven off video.currentTime, so that's automatic).
      u.rate = 1.0;
      u.pitch = 1.0;
      u.volume = 1.0;
      window.speechSynthesis.speak(u);
    } catch (err) {
      console.warn("SpeechSynthesis failed:", err);
    }
    return;
  }

  if (!counts.ctx) return;
  const src = counts.ctx.createBufferSource();
  src.buffer = voice.buffer;
  src.playbackRate.value = 1.0;
  src.connect(counts.gain);
  src.start(0);
}

/**
 * rAF-driven scheduler. Drives off `video.currentTime`, which already
 * advances at the playback rate — so the wall-clock interval between counts
 * scales perfectly: 0.5x video = 2× spacing, 2x video = ½ spacing.
 *
 * Splits the active loop window into 8 evenly spaced intervals and fires
 * count N when the playhead crosses interval N.
 */
function scheduleTick() {
  if (!counts.schedulerRunning) return;

  if (
    counts.mode === "counts" &&
    counts.loaded &&
    loopBounds &&
    !video.paused &&
    !video.ended
  ) {
    const duration = loopBounds.end - loopBounds.start;
    if (duration > 0) {
      const beatDur = duration / 8;
      const elapsed = video.currentTime - loopBounds.start;
      let idx = Math.floor(elapsed / beatDur);
      if (idx >= 0 && idx < 8) {
        // Detect wrap (loop snapped back) or seek-backward.
        if (idx < counts.lastBeatIdx) counts.lastBeatIdx = -1;
        if (idx !== counts.lastBeatIdx) {
          playCount(idx);
          counts.lastBeatIdx = idx;
        }
      }
    }
  }

  requestAnimationFrame(scheduleTick);
}

function startCountsScheduler() {
  if (counts.schedulerRunning) return;
  counts.schedulerRunning = true;
  counts.lastBeatIdx = -1;
  requestAnimationFrame(scheduleTick);
}

async function setAudioMode(mode) {
  counts.mode = mode;
  video.volume = VIDEO_VOLUME_BY_MODE[mode];

  document.querySelectorAll(".audio-mode-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
  });

  if (mode === "counts") {
    ensureAudioCtx();
    // AudioContext may start suspended (autoplay policy) — resume on user gesture.
    if (counts.ctx.state === "suspended") {
      try { await counts.ctx.resume(); } catch {}
    }
    await loadAllCounts();
    counts.lastBeatIdx = -1;
    startCountsScheduler();
  }
}

/* Reset the beat tracker whenever the active loop window changes so the
   next playthrough fires count 1 cleanly. */
function resetCountBeat() {
  counts.lastBeatIdx = -1;
}

/* ---------------------------------------------------------------- */
/* Looping playback — the single source of truth for loop snap-back  */
/* ---------------------------------------------------------------- */

video.addEventListener("timeupdate", () => {
  if (!loopBounds) return;
  if (video.currentTime >= loopBounds.end - LOOP_EPS) {
    video.currentTime = loopBounds.start;
    resetCountBeat();

    if (workshop.active) {
      // Each wrap = one completed rep.
      workshop.repsRemaining -= 1;
      workshopRepsEl.textContent = String(Math.max(0, workshop.repsRemaining));
      workshopRepsEl.classList.remove("rep-pulse");
      // Force reflow so the animation restarts.
      void workshopRepsEl.offsetWidth;
      workshopRepsEl.classList.add("rep-pulse");

      if (workshop.repsRemaining <= 0) {
        enterPhase(workshop.phaseIdx + 1);
      }
    }
  }
});

/* ---------------------------------------------------------------- */
/* Speed + mirror + workshop button wiring                           */
/* ---------------------------------------------------------------- */

speedControls.addEventListener("click", (e) => {
  const btn = e.target.closest(".speed-btn");
  if (!btn) return;
  video.playbackRate = parseFloat(btn.dataset.speed);
  document
    .querySelectorAll(".speed-btn")
    .forEach((b) => b.classList.toggle("active", b === btn));
});

mirrorBtn.addEventListener("click", () => {
  const isMirrored = video.classList.toggle("mirrored");
  mirrorState.textContent = isMirrored ? "On" : "Off";
  mirrorBtn.classList.toggle("bg-indigo-50", isMirrored);
  mirrorBtn.classList.toggle("border-indigo-300", isMirrored);
});

clearBtn.addEventListener("click", clearSelection);
workshopBtn.addEventListener("click", () => {
  workshop.active ? stopWorkshop() : startWorkshop();
});
nextDrillBtn.addEventListener("click", nextDrill);
prevDrillBtn.addEventListener("click", previousDrill);

audioModeRow.addEventListener("click", (e) => {
  const btn = e.target.closest(".audio-mode-btn");
  if (!btn) return;
  setAudioMode(btn.dataset.mode);
});

/* ---------------------------------------------------------------- */
/* Local video file picker                                           */
/* ---------------------------------------------------------------- */

/* ---------------------------------------------------------------- */
/* Crop UI — Dance Start / End                                       */
/* ---------------------------------------------------------------- */

function clampCropToDuration(duration, preset = null) {
  if (!isFinite(duration) || duration <= 0) return;
  for (const el of [cropStartInput, cropEndInput, cropStartRange, cropEndRange]) {
    el.max = duration.toFixed(2);
  }
  // Default to the full video range, or honor an explicit preset (e.g.
  // restored from a library entry).
  const start = preset && preset.start != null ? Math.max(0, preset.start) : 0;
  const end = preset && preset.end != null
    ? Math.min(duration, preset.end)
    : duration;
  cropStartInput.value = start.toFixed(2);
  cropStartRange.value = start.toString();
  cropEndInput.value = end.toFixed(2);
  cropEndRange.value = end.toString();
  updateCropDurationLabel();
}

/**
 * Returns the user's active crop window, or `null` if no real crop is set.
 *
 * "No crop" means either:
 *   - the video metadata hasn't loaded yet (so we can't tell what a valid
 *     end-time would be), or
 *   - the inputs cover the entire video (the default after loadedmetadata).
 *
 * Callers must treat `null` as "process the whole video" and omit
 * `start_time`/`end_time` from API payloads.
 */
function getCropBounds() {
  const duration = video.duration;
  if (!isFinite(duration) || duration <= 0) return null;
  const start = parseFloat(cropStartInput.value);
  const end = parseFloat(cropEndInput.value);
  if (!isFinite(start) || !isFinite(end) || end <= start) return null;
  // Treat near-full ranges as "no crop" so we don't ship spurious bounds.
  if (start <= 0.05 && end >= duration - 0.05) return null;
  return { start, end };
}

function updateCropDurationLabel() {
  const crop = getCropBounds();
  if (!crop) {
    cropDuration.textContent = "Full video";
    return;
  }
  const dur = Math.max(0, crop.end - crop.start);
  cropDuration.textContent = `Selected: ${dur.toFixed(2)}s (${crop.start.toFixed(2)}s → ${crop.end.toFixed(2)}s)`;
}

function syncCropPair(numberEl, rangeEl, source) {
  // Keep number + range inputs in sync; clamp so start < end.
  const val = parseFloat(source.value);
  if (!isFinite(val)) return;
  numberEl.value = val.toFixed(2);
  rangeEl.value = val;

  // Enforce ordering.
  const start = parseFloat(cropStartInput.value);
  const end = parseFloat(cropEndInput.value);
  if (end <= start) {
    if (source === cropStartInput || source === cropStartRange) {
      cropEndInput.value = (start + 0.1).toFixed(2);
      cropEndRange.value = (start + 0.1).toString();
    } else {
      cropStartInput.value = Math.max(0, end - 0.1).toFixed(2);
      cropStartRange.value = Math.max(0, end - 0.1).toString();
    }
  }
  updateCropDurationLabel();
}

cropStartInput.addEventListener("input", () => syncCropPair(cropStartInput, cropStartRange, cropStartInput));
cropStartRange.addEventListener("input", () => syncCropPair(cropStartInput, cropStartRange, cropStartRange));
cropEndInput.addEventListener("input", () => syncCropPair(cropEndInput, cropEndRange, cropEndInput));
cropEndRange.addEventListener("input", () => syncCropPair(cropEndInput, cropEndRange, cropEndRange));

cropPreviewStartBtn.addEventListener("click", () => {
  cropStartInput.value = video.currentTime.toFixed(2);
  cropStartRange.value = video.currentTime;
  syncCropPair(cropStartInput, cropStartRange, cropStartInput);
});
cropPreviewEndBtn.addEventListener("click", () => {
  cropEndInput.value = video.currentTime.toFixed(2);
  cropEndRange.value = video.currentTime;
  syncCropPair(cropEndInput, cropEndRange, cropEndInput);
});
cropResetBtn.addEventListener("click", () => clampCropToDuration(video.duration));

video.addEventListener("loadedmetadata", () => {
  const preset = pendingCrop;
  pendingCrop = null;
  clampCropToDuration(video.duration, preset);
});

/* ---------------------------------------------------------------- */
/* Process Video — single button that respects the crop selection    */
/* ---------------------------------------------------------------- */

function setProcessing(isProcessing, message) {
  cropProcessBtn.disabled = isProcessing;
  processBtn.disabled = isProcessing;
  if (message) {
    processStatus.textContent = message;
    processStatus.classList.toggle("text-rose-600", false);
    processStatus.classList.toggle("text-slate-500", true);
  }
}

function showProcessError(msg) {
  processStatus.textContent = `Failed: ${msg}`;
  processStatus.classList.remove("text-slate-500");
  processStatus.classList.add("text-rose-600");
}

async function processYouTube(url, crop) {
  const body = { url, quality: youtubeQualitySelect.value };
  if (crop) {
    body.start_time = crop.start;
    body.end_time = crop.end;
  }
  const res = await fetch("/api/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

async function processFile(file, crop) {
  const form = new FormData();
  form.append("file", file);
  if (crop) {
    form.append("start_time", String(crop.start));
    form.append("end_time", String(crop.end));
  }
  const res = await fetch("/api/process-file", { method: "POST", body: form });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function applyProcessResponse(data) {
  if (video.src && video.src.startsWith("blob:")) URL.revokeObjectURL(video.src);
  video.src = data.video_url;
  video.load();
  allSegments = data.segments;
  currentVideoId = data.video_id || null;
  renderSegments(allSegments);
  refreshLibrary();
  processStatus.textContent =
    `Done — ${data.segment_count} segments from ${data.duration.toFixed(1)}s of cropped video.`;
}

cropProcessBtn.addEventListener("click", async () => {
  const crop = getCropBounds();
  const url = youtubeUrlInput.value.trim();

  if (!url && !pendingFile) {
    showProcessError("Paste a YouTube URL or pick a local file first.");
    return;
  }

  if (workshop.active) stopWorkshop();
  clearSelection();
  const cropMsg = crop
    ? `crop ${crop.start.toFixed(2)}s → ${crop.end.toFixed(2)}s`
    : "full video";
  setProcessing(
    true,
    `Processing ${cropMsg}… this can take a few minutes.`
  );

  try {
    const data = url
      ? await processYouTube(url, crop)
      : await processFile(pendingFile, crop);
    applyProcessResponse(data);
  } catch (err) {
    console.error(err);
    showProcessError(err.message);
  } finally {
    setProcessing(false);
  }
});

youtubeForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = youtubeUrlInput.value.trim();
  if (!url) return;

  // Tear down any active workshop / selection so the new video starts clean.
  if (workshop.active) stopWorkshop();
  clearSelection();

  processBtn.disabled = true;
  processStatus.textContent =
    "Downloading, extracting pose, segmenting… this can take a few minutes.";
  processStatus.classList.remove("text-rose-600");
  processStatus.classList.add("text-slate-500");

  try {
    const crop = getCropBounds();
    const body = { url, quality: youtubeQualitySelect.value };
    if (crop) {
      body.start_time = crop.start;
      body.end_time = crop.end;
    }
    const res = await fetch("/api/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();

    // Swap in the new video and segments.
    if (video.src && video.src.startsWith("blob:")) URL.revokeObjectURL(video.src);
    video.src = data.video_url;
    video.load();

    allSegments = data.segments;
    renderSegments(allSegments);

    processStatus.textContent = `Done — ${data.segment_count} segments from ${data.duration.toFixed(1)}s of video.`;
  } catch (err) {
    console.error(err);
    processStatus.textContent = `Failed: ${err.message}`;
    processStatus.classList.remove("text-slate-500");
    processStatus.classList.add("text-rose-600");
  } finally {
    processBtn.disabled = false;
  }
});

videoFileInput.addEventListener("change", (e) => {
  const file = e.target.files?.[0];
  if (!file) return;

  // Preview the picked file locally so the user can see it and set crop
  // bounds before kicking off the (slow) pipeline. Actual processing is
  // triggered by the "Process Video" button.
  if (video.src && video.src.startsWith("blob:")) URL.revokeObjectURL(video.src);
  video.src = URL.createObjectURL(file);
  video.load();

  pendingFile = file;
  currentVideoId = null; // cleared until processing completes
  processStatus.textContent = `Loaded "${file.name}" — adjust crop and click Process Video.`;
  processStatus.classList.remove("text-rose-600");
  processStatus.classList.add("text-slate-500");
});

/* ---------------------------------------------------------------- */
/* Segment editor                                                    */
/* ---------------------------------------------------------------- */

/** Working copy used while editing — committed to allSegments on save. */
let editorDraft = [];
let editing = false;

function enterEditMode() {
  if (workshop.active) stopWorkshop();
  clearSelection();
  editing = true;
  // Deep-copy so cancelling discards changes.
  editorDraft = allSegments.map((s) => ({ ...s }));
  segmentGrid.classList.add("hidden");
  segmentEditor.classList.remove("hidden");
  segmentEditor.classList.add("flex");
  editSegmentsBtn.textContent = "Editing…";
  editSegmentsBtn.disabled = true;
  editorStatus.textContent = "";
  renderEditor();
}

function exitEditMode() {
  editing = false;
  segmentEditor.classList.add("hidden");
  segmentEditor.classList.remove("flex");
  segmentGrid.classList.remove("hidden");
  editSegmentsBtn.textContent = "Edit";
  editSegmentsBtn.disabled = false;
}

const AUTO_MIN_DURATION = 4.0;
const AUTO_MAX_DURATION = 8.0;
const AUTO_DUR_TOLERANCE = 0.1;

function durationStatus(seg) {
  const d = seg.end - seg.start;
  if (d < AUTO_MIN_DURATION - AUTO_DUR_TOLERANCE)
    return { kind: "short", label: `${d.toFixed(2)}s · short` };
  if (d > AUTO_MAX_DURATION + AUTO_DUR_TOLERANCE)
    return { kind: "long", label: `${d.toFixed(2)}s · long` };
  return { kind: "ok", label: `${d.toFixed(2)}s` };
}

function renderEditor() {
  editorList.innerHTML = "";
  if (!editorDraft.length) {
    editorList.innerHTML =
      '<p class="text-sm text-slate-500 text-center py-6">No segments. Add one with the playhead button below.</p>';
    return;
  }

  editorDraft.forEach((seg, idx) => {
    const row = document.createElement("div");
    const status = durationStatus(seg);
    const statusClass =
      status.kind === "ok"
        ? "text-slate-400"
        : status.kind === "short"
        ? "text-amber-600"
        : "text-rose-600";
    row.className =
      "flex items-center gap-2 p-2 rounded-lg border border-slate-200 bg-slate-50";
    row.innerHTML = `
      <span class="text-xs font-mono text-slate-500 w-6 text-center">${idx + 1}</span>
      <input type="text" class="ed-label flex-1 min-w-0 px-2 py-1 text-xs rounded border border-slate-300 bg-white" value="${escapeAttr(seg.label)}" />
      <input type="number" step="0.05" min="0" class="ed-start w-20 px-2 py-1 text-xs font-mono rounded border border-slate-300 bg-white" value="${seg.start.toFixed(2)}" />
      <span class="text-slate-400 text-xs">→</span>
      <input type="number" step="0.05" min="0" class="ed-end w-20 px-2 py-1 text-xs font-mono rounded border border-slate-300 bg-white" value="${seg.end.toFixed(2)}" />
      <span class="ed-status text-xs font-mono ${statusClass} w-24 text-right" title="Auto-detection prefers 4–8s; manual overrides allowed">${status.label}</span>
      <button class="ed-play text-xs px-2 py-1 rounded border border-slate-300 hover:bg-slate-100" title="Preview this segment">▶</button>
      <button class="ed-snap-start text-xs px-1.5 py-1 rounded border border-slate-300 hover:bg-slate-100" title="Snap start to playhead">⇤</button>
      <button class="ed-snap-end text-xs px-1.5 py-1 rounded border border-slate-300 hover:bg-slate-100" title="Snap end to playhead">⇥</button>
      <button class="ed-delete text-xs px-2 py-1 rounded border border-rose-300 text-rose-600 hover:bg-rose-50" title="Delete">🗑</button>
    `;

    const labelEl = row.querySelector(".ed-label");
    const startEl = row.querySelector(".ed-start");
    const endEl = row.querySelector(".ed-end");

    const statusEl = row.querySelector(".ed-status");
    function refreshStatus() {
      const s = durationStatus(seg);
      statusEl.textContent = s.label;
      statusEl.classList.remove("text-slate-400", "text-amber-600", "text-rose-600");
      statusEl.classList.add(
        s.kind === "ok" ? "text-slate-400" : s.kind === "short" ? "text-amber-600" : "text-rose-600"
      );
    }

    labelEl.addEventListener("input", () => { seg.label = labelEl.value; });
    startEl.addEventListener("input", () => {
      const v = parseFloat(startEl.value);
      if (isFinite(v)) { seg.start = v; refreshStatus(); }
    });
    endEl.addEventListener("input", () => {
      const v = parseFloat(endEl.value);
      if (isFinite(v)) { seg.end = v; refreshStatus(); }
    });

    row.querySelector(".ed-play").addEventListener("click", () => {
      video.currentTime = seg.start;
      loopBounds = { start: seg.start, end: seg.end };
      resetCountBeat();
      updateLoopDisplay();
      video.play().catch(() => {});
    });
    row.querySelector(".ed-snap-start").addEventListener("click", () => {
      seg.start = +video.currentTime.toFixed(2);
      startEl.value = seg.start.toFixed(2);
      refreshStatus();
    });
    row.querySelector(".ed-snap-end").addEventListener("click", () => {
      seg.end = +video.currentTime.toFixed(2);
      endEl.value = seg.end.toFixed(2);
      refreshStatus();
    });
    row.querySelector(".ed-delete").addEventListener("click", () => {
      editorDraft.splice(idx, 1);
      renderEditor();
    });

    editorList.appendChild(row);
  });
}

function escapeAttr(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

function addSegmentAtPlayhead() {
  const t = video.currentTime || 0;
  const end = Math.min((video.duration || t + 2), t + 2);
  editorDraft.push({
    id: editorDraft.length + 1,
    label: `Segment ${editorDraft.length + 1}`,
    start: +t.toFixed(2),
    end: +end.toFixed(2),
  });
  editorDraft.sort((a, b) => a.start - b.start);
  renderEditor();
}

function splitAtPlayhead() {
  const t = video.currentTime;
  // Find the segment containing t.
  const idx = editorDraft.findIndex((s) => t > s.start && t < s.end);
  if (idx < 0) {
    editorStatus.textContent = "Playhead isn't inside a segment — nothing to split.";
    return;
  }
  const orig = editorDraft[idx];
  const cut = +t.toFixed(2);
  const left = { ...orig, end: cut, label: orig.label };
  const right = {
    ...orig,
    start: cut,
    label: orig.label.replace(/(\s*\(b\))?$/, "") + " (b)",
  };
  editorDraft.splice(idx, 1, left, right);
  renderEditor();
  editorStatus.textContent = `Split at ${cut.toFixed(2)}s.`;
}

async function saveEditorDraft() {
  editorStatus.textContent = "Saving…";
  try {
    const segments = editorDraft.map((s) => ({
      id: s.id || 0,
      label: s.label || "",
      start: Number(s.start),
      end: Number(s.end),
    }));

    let saved;
    if (currentVideoId) {
      // Persist to the library entry — also captures the current crop bounds
      // so re-opening the entry restores the exact view the user saved.
      const crop = getCropBounds();
      const body = JSON.stringify({
        segments,
        crop_start: crop ? crop.start : null,
        crop_end: crop ? crop.end : null,
      });
      const res = await fetch(
        `/api/library/${encodeURIComponent(currentVideoId)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body,
        }
      );
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      saved = data.segments;
      await refreshLibrary();
    } else {
      // Untitled working sequence — write to sequence.json only.
      const res = await fetch("/api/sequence", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(segments),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || `HTTP ${res.status}`);
      }
      saved = await res.json();
    }

    allSegments = saved;
    renderSegments(allSegments);
    exitEditMode();
    editorStatus.textContent = "";
  } catch (err) {
    editorStatus.textContent = `Failed: ${err.message}`;
  }
}

editSegmentsBtn.addEventListener("click", enterEditMode);
editorCancelBtn.addEventListener("click", exitEditMode);
editorSaveBtn.addEventListener("click", saveEditorDraft);
editorAddBtn.addEventListener("click", addSegmentAtPlayhead);
editorSplitBtn.addEventListener("click", splitAtPlayhead);

/* ---------------------------------------------------------------- */
/* Library — previously processed videos                             */
/* ---------------------------------------------------------------- */

let libraryCache = [];

function formatRelativeTime(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hr ago`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} d ago`;
  return new Date(iso).toLocaleDateString();
}

async function refreshLibrary() {
  try {
    const res = await fetch("/api/library");
    libraryCache = await res.json();
  } catch {
    libraryCache = [];
  }
  libraryCount.textContent = libraryCache.length ? `(${libraryCache.length})` : "";
  renderLibrary();
}

function renderLibrary() {
  libraryList.innerHTML = "";
  if (!libraryCache.length) {
    libraryList.innerHTML =
      '<p class="col-span-full text-xs text-slate-500 text-center py-6">No videos processed yet.</p>';
    return;
  }

  for (const entry of libraryCache) {
    const card = document.createElement("div");
    card.className =
      "p-3 rounded-lg border border-slate-200 bg-slate-50 hover:bg-white hover:border-indigo-300 transition cursor-pointer";
    const dur = entry.duration ? `${entry.duration.toFixed(1)}s` : "—";
    const sourceBadge =
      entry.source === "youtube"
        ? '<span class="text-[10px] uppercase tracking-wide bg-rose-100 text-rose-700 px-1.5 py-0.5 rounded">YouTube</span>'
        : '<span class="text-[10px] uppercase tracking-wide bg-sky-100 text-sky-700 px-1.5 py-0.5 rounded">Upload</span>';
    const editedBadge = entry.last_edited_at
      ? `<span class="text-[10px] uppercase tracking-wide bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded" title="Edited ${formatRelativeTime(entry.last_edited_at)}">edited</span>`
      : "";
    const cropNote =
      entry.crop_start != null || entry.crop_end != null
        ? `<span class="text-[10px] text-slate-400">· crop ${(entry.crop_start ?? 0).toFixed(1)}–${(entry.crop_end ?? entry.duration ?? 0).toFixed(1)}s</span>`
        : "";

    card.innerHTML = `
      <div class="flex items-start justify-between gap-2">
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2 mb-0.5">
            ${sourceBadge}
            ${editedBadge}
            <span class="text-xs text-slate-400">${formatRelativeTime(entry.processed_at)}</span>
          </div>
          <div class="text-sm font-semibold text-slate-900 truncate" title="${escapeAttr(entry.title)}">${escapeAttr(entry.title)}</div>
          <div class="text-xs text-slate-500 font-mono mt-0.5">
            ${entry.segment_count} segments · ${dur} ${cropNote}
          </div>
        </div>
        <button
          class="lib-delete text-xs text-rose-500 hover:text-rose-700"
          title="Remove from library"
        >🗑</button>
      </div>
      <div class="flex gap-2 mt-2">
        <button class="lib-open flex-1 text-xs px-2 py-1 rounded-md border border-slate-300 bg-white hover:bg-slate-100">
          Open
        </button>
        <button class="lib-workshop flex-1 text-xs px-2 py-1 rounded-md bg-indigo-600 text-white font-semibold hover:bg-indigo-700">
          Start Workshop
        </button>
      </div>
    `;
    card.querySelector(".lib-open").addEventListener("click", (e) => {
      e.stopPropagation();
      loadLibraryEntry(entry.video_id, { autoStartWorkshop: false });
    });
    card.querySelector(".lib-workshop").addEventListener("click", (e) => {
      e.stopPropagation();
      loadLibraryEntry(entry.video_id, { autoStartWorkshop: true });
    });
    card.querySelector(".lib-delete").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Remove "${entry.title}" from library?`)) return;
      await fetch(`/api/library/${encodeURIComponent(entry.video_id)}`, {
        method: "DELETE",
      });
      await refreshLibrary();
    });
    libraryList.appendChild(card);
  }
}

async function loadLibraryEntry(videoId, { autoStartWorkshop = false } = {}) {
  try {
    const res = await fetch(`/api/library/${encodeURIComponent(videoId)}`);
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();

    if (data.video_exists === false) {
      if (!confirm("The video file is no longer on disk. Open segments anyway?")) return;
    }

    if (workshop.active) stopWorkshop();
    clearSelection();

    // Stash crop so the loadedmetadata handler restores it (instead of
    // resetting to the full-video default).
    pendingCrop =
      data.crop_start != null || data.crop_end != null
        ? { start: data.crop_start, end: data.crop_end }
        : null;

    if (video.src && video.src.startsWith("blob:")) URL.revokeObjectURL(video.src);
    video.src = data.video_url;
    video.load();
    pendingFile = null;
    currentVideoId = data.video_id;
    youtubeUrlInput.value = data.source_url || "";

    allSegments = data.segments;
    renderSegments(allSegments);

    libraryPanel.classList.add("hidden");
    processStatus.textContent = `Loaded "${data.title}" — ${data.segment_count} segments.`;
    processStatus.classList.remove("text-rose-600");
    processStatus.classList.add("text-slate-500");

    if (autoStartWorkshop) {
      // Wait one tick so the video element has a chance to begin loading.
      setTimeout(() => startWorkshop(), 0);
    }
  } catch (err) {
    console.error(err);
    processStatus.textContent = `Failed to load: ${err.message}`;
    processStatus.classList.remove("text-slate-500");
    processStatus.classList.add("text-rose-600");
  }
}

libraryBtn.addEventListener("click", async () => {
  await refreshLibrary();
  libraryPanel.classList.toggle("hidden");
});
libraryCloseBtn.addEventListener("click", () => libraryPanel.classList.add("hidden"));

/* ---------------------------------------------------------------- */
/* Boot                                                              */
/* ---------------------------------------------------------------- */

(async () => {
  video.volume = VIDEO_VOLUME_BY_MODE[counts.mode]; // default: On Audio @ 1.0
  allSegments = await loadSegments();
  renderSegments(allSegments);
  refreshLibrary(); // populate count badge in header
})();

/* Exposed for testing / console use. */
window.__dss = {
  buildWorkshopPlan,
  calculateLoopBounds,
  workshop,
  counts,
  setAudioMode,
  get allSegments() { return allSegments; },
};
