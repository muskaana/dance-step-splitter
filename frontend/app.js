/* Dance Step Splitter — frontend controller (with Workshop Mode). */

const video = document.getElementById("player");
const segmentGrid = document.getElementById("segment-grid");
const segmentCount = document.getElementById("segment-count");
const mirrorBtn = document.getElementById("mirror-btn");
const memoryBtn = document.getElementById("memory-btn");
const memoryState = document.getElementById("memory-state");
const memoryOverlay = document.getElementById("memory-overlay");
const mirrorState = document.getElementById("mirror-state");
const speedControls = document.getElementById("speed-controls");
const loopBreakInput = document.getElementById("loop-break");
const videoFileInput = document.getElementById("video-file");

const workshopBtn = document.getElementById("workshop-btn");
const workshopBanner = document.getElementById("workshop-banner");
const workshopPhaseEl = document.getElementById("workshop-phase");
const workshopProgressEl = document.getElementById("workshop-progress");
const workshopRepsEl = document.getElementById("workshop-reps");
const prevDrillBtn = document.getElementById("prev-drill-btn");
const nextDrillBtn = document.getElementById("next-drill-btn");

const audioModeRow = document.getElementById("audio-mode");

const sourceToggle = document.getElementById("source-toggle");
const sourceYoutubeRow = document.getElementById("source-youtube");
const sourceUploadRow = document.getElementById("source-upload");
const youtubeUrlInput = document.getElementById("youtube-url");
const youtubeQualitySelect = document.getElementById("youtube-quality");
const uploadLabel = document.getElementById("upload-label");
const uploadFileList = document.getElementById("upload-file-list");
const processVideoBtn = document.getElementById("process-video-btn");
const processSubtitle = document.getElementById("process-subtitle");
const cropRow = document.getElementById("crop-row");
const cropHint = document.getElementById("crop-hint");

const addVideoCard = document.getElementById("add-video-card");
const addVideoBody = document.getElementById("add-video-body");
const addVideoCollapseBtn = document.getElementById("add-video-collapse-btn");
const addVideoTagline = document.getElementById("add-video-tagline");

const quickSegments = document.getElementById("quick-segments");
const quickSegmentsBtn = document.getElementById("quick-segments-btn");
const quickSegmentsLabel = document.getElementById("quick-segments-label");
const quickSegmentsMenu = document.getElementById("quick-segments-menu");
const restartSegmentBtn = document.getElementById("restart-segment-btn");

const emptyState = document.getElementById("empty-state");
const emptyStateLibraryLink = document.getElementById("empty-state-library-link");

const processBanner = document.getElementById("process-banner");
const processBannerPhase = document.getElementById("process-banner-phase");
const processBannerDetail = document.getElementById("process-banner-detail");
const processBannerTime = document.getElementById("process-banner-time");
const processBannerSpinner = document.getElementById("process-banner-spinner");
const processBannerClose = document.getElementById("process-banner-close");
const processBannerTitle = document.getElementById("process-banner-title");

const libraryBtn = document.getElementById("library-btn");
const libraryPanel = document.getElementById("library-panel");
const libraryList = document.getElementById("library-list");
const libraryCount = document.getElementById("library-count");
const libraryCloseBtn = document.getElementById("library-close-btn");

const editSegmentsBtn = document.getElementById("edit-segments-btn");
const shareCurrentBtn = document.getElementById("share-controls-btn");
const workshopHelpBtn = document.getElementById("workshop-help-btn");
const workshopHelpPopover = document.getElementById("workshop-help-popover");
const workshopHelpClose = document.getElementById("workshop-help-close");
const segmentEditor = document.getElementById("segment-editor");
const editorList = document.getElementById("editor-list");
const editorAddBtn = document.getElementById("editor-add-btn");
const editorSplitBtn = document.getElementById("editor-split-btn");
const editorSaveBtn = document.getElementById("editor-save-btn");
const editorCancelBtn = document.getElementById("editor-cancel-btn");
const editorStatus = document.getElementById("editor-status");

const authOverlay = document.getElementById("auth-overlay");
const authForm = document.getElementById("auth-form");
const authUsername = document.getElementById("auth-username");
const authPassword = document.getElementById("auth-password");
const authSubmit = document.getElementById("auth-submit");
const authError = document.getElementById("auth-error");
const authTabs = document.querySelectorAll(".auth-tab");
const userBadge = document.getElementById("user-badge");
const userNameEl = document.getElementById("user-name");
const renameUserBtn = document.getElementById("rename-user-btn");
const userRenameError = document.getElementById("user-rename-error");
const logoutBtn = document.getElementById("logout-btn");
const appRoot = document.getElementById("app-root");

const tutorialOverlay = document.getElementById("tutorial-overlay");
const tutorialBtn = document.getElementById("tutorial-btn");
const tutorialClose = document.getElementById("tutorial-close");
const tutorialDismiss = document.getElementById("tutorial-dismiss");

const shareModal = document.getElementById("share-modal");
const shareClose = document.getElementById("share-close");
const shareSubtitle = document.getElementById("share-subtitle");
const shareForm = document.getElementById("share-form");
const shareUsernameEl = document.getElementById("share-username");
const shareError = document.getElementById("share-error");
const shareList = document.getElementById("share-list");
const shareLinkCreateBtn = document.getElementById("share-link-create");
const shareLinksList = document.getElementById("share-links-list");

const cropStartInput = document.getElementById("crop-start");
const cropEndInput = document.getElementById("crop-end");
const cropStartRange = document.getElementById("crop-start-range");
const cropEndRange = document.getElementById("crop-end-range");
const cropDuration = document.getElementById("crop-duration");
const cropPreviewStartBtn = document.getElementById("crop-preview-start-btn");
const cropPreviewEndBtn = document.getElementById("crop-preview-end-btn");
const cropResetBtn = document.getElementById("crop-reset-btn");

/** Files the user has chosen for upload. Single entry = standard
    `/api/process-file` flow. Two or more = `/api/process-files` which
    ffmpeg-concatenates them into one routine. */
let pendingFiles = [];

/** ID of the currently-loaded library entry (null until a video has been
    processed or opened from the library). Editor saves route here. */
let currentVideoId = null;
/** Permission level on the currently-loaded video: "owner", "edit", or "view".
    Drives whether the segment editor is reachable. */
let currentPermission = "owner";

/** Polling state for collaborative edit sync. While a video is open we ping
    /api/library/{video_id} every 15s; if `last_edited_at` changed since we
    last looked, we re-render the segments. */
let entryPollHandle = null;
let lastEditedAtKnown = null;

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
    const res = await fetch("/api/sequence", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    console.warn("Couldn't load saved sequence:", err);
    return [];
  }
}

/* ---------------------------------------------------------------- */
/* Segment grid rendering + manual selection                         */
/* ---------------------------------------------------------------- */

/** Show/hide editing affordances based on the current video's permission.
    "owner" and "edit" → Edit button enabled. "view" → hidden + banner shown. */
function applyPermissionGating() {
  const canEdit = currentPermission === "owner" || currentPermission === "edit";
  const isOwner = currentPermission === "owner";
  // Edit button in the segments header.
  if (editSegmentsBtn) {
    editSegmentsBtn.classList.toggle("hidden", !canEdit);
  }
  // Share-current button: only owners can manage sharing, and only when
  // there's actually a loaded video.
  if (shareCurrentBtn) {
    shareCurrentBtn.classList.toggle("hidden", !(isOwner && currentVideoId));
  }
  // If the editor was open when permission changes (e.g. the user re-opened
  // a view-only entry), bail out so the viewer can't keep typing.
  if (!canEdit && typeof editing !== "undefined" && editing) {
    exitEditMode();
  }
  // Surface a small inline indicator next to the segments title so viewers
  // know why the Edit button is missing.
  const headerEl = document.querySelector("#segment-grid")?.parentElement;
  let badge = document.getElementById("view-only-badge");
  if (currentPermission === "view") {
    if (!badge && headerEl) {
      badge = document.createElement("span");
      badge.id = "view-only-badge";
      badge.className =
        "text-[10px] uppercase tracking-wide bg-slate-200 text-slate-700 px-1.5 py-0.5 rounded ml-2";
      badge.textContent = "View only";
      headerEl.querySelector("h2")?.appendChild(badge);
    }
  } else if (badge) {
    badge.remove();
  }
}

function renderSegments(segments) {
  segmentGrid.innerHTML = "";
  if (!segments.length) {
    segmentGrid.innerHTML =
      '<p class="text-sm text-slate-500 text-center py-8">No segments available.</p>';
    applySegmentsLayout(0);
    renderQuickSegments([]);
    return;
  }

  for (const seg of segments) {
    const btn = document.createElement("button");
    btn.className =
      "segment-btn flex items-center justify-between gap-2 px-2.5 py-1.5 rounded-md border border-slate-300 bg-white text-left hover:bg-slate-50 transition-all";
    btn.dataset.id = String(seg.id);
    btn.innerHTML = `
      <span class="text-xs font-semibold truncate min-w-0">${seg.label}</span>
      <span class="text-[10px] text-slate-500 font-mono whitespace-nowrap">${seg.start.toFixed(1)}–${seg.end.toFixed(1)}s</span>
    `;
    btn.addEventListener("click", () => {
      if (workshop.active) return; // manual selection disabled in workshop mode
      toggleSegment(seg, btn);
    });
    segmentGrid.appendChild(btn);
  }
  applySegmentsLayout(segments.length);
  renderQuickSegments(segments);
}

// `applySegmentsLayout` was needed when segments lived in a right sidebar
// whose width adapted to segment count. Now segments live below the video
// in a full-width grid, so this is a no-op kept for backward-compat with the
// two existing call sites (cheaper than removing them all).
function applySegmentsLayout(_count) {
  /* no-op */
}

/* ---------------------------------------------------------------- */
/* Collapsible Add-a-video card                                      */
/* ---------------------------------------------------------------- */

let addVideoCollapsed = false;

function setAddVideoCollapsed(collapsed) {
  addVideoCollapsed = collapsed;
  addVideoBody.classList.toggle("hidden", collapsed);
  sourceToggle.classList.toggle("hidden", collapsed);
  addVideoCollapseBtn.textContent = collapsed ? "+" : "−";
  addVideoCollapseBtn.title = collapsed ? "Expand to add another video" : "Collapse";
  addVideoTagline.textContent = collapsed
    ? "Click + to add another video."
    : "Paste a YouTube link or upload a file, optionally crop, then process.";
}

addVideoCollapseBtn.addEventListener("click", () =>
  setAddVideoCollapsed(!addVideoCollapsed)
);

/* ---------------------------------------------------------------- */
/* Quick-segment dropdown (overlays the video)                       */
/* ---------------------------------------------------------------- */

function renderQuickSegments(segments) {
  if (!segments || !segments.length) {
    quickSegments.classList.add("hidden");
    quickSegmentsMenu.classList.add("hidden");
    restartSegmentBtn.classList.add("hidden");
    return;
  }
  quickSegments.classList.remove("hidden");
  restartSegmentBtn.classList.remove("hidden");

  quickSegmentsMenu.innerHTML = "";
  for (const seg of segments) {
    const item = document.createElement("button");
    item.dataset.id = String(seg.id);
    item.className =
      "quick-seg w-full text-left text-xs px-2.5 py-1.5 hover:bg-indigo-50 flex items-center gap-2 border-b border-slate-100 last:border-b-0";
    item.innerHTML = `
      <span class="quick-check w-3.5 h-3.5 rounded-sm border border-slate-300 flex-shrink-0 flex items-center justify-center text-[10px] text-white"></span>
      <span class="font-medium truncate min-w-0 flex-1">${escapeAttr(seg.label)}</span>
      <span class="text-[10px] text-slate-500 font-mono whitespace-nowrap">${seg.start.toFixed(1)}–${seg.end.toFixed(1)}s</span>
    `;
    item.addEventListener("click", (e) => {
      e.stopPropagation(); // keep menu open so the user can pick several
      quickToggleSegment(seg);
    });
    quickSegmentsMenu.appendChild(item);
  }
  updateQuickSegmentsState();
}

function updateQuickSegmentsState() {
  // Reflect the currently-active selection in both the button label and
  // an "active" highlight inside the dropdown.
  if (workshop.active) {
    quickSegmentsLabel.textContent = "Workshop running";
  } else if (selectedSegments.length === 0) {
    quickSegmentsLabel.textContent = "Pick segment";
  } else if (selectedSegments.length === 1) {
    quickSegmentsLabel.textContent = selectedSegments[0].label;
  } else {
    quickSegmentsLabel.textContent = `${selectedSegments.length} selected`;
  }
  const activeIds = new Set(selectedSegments.map((s) => String(s.id)));
  quickSegmentsMenu.querySelectorAll(".quick-seg").forEach((el) => {
    const isActive = activeIds.has(el.dataset.id);
    el.classList.toggle("bg-indigo-50", isActive);
    el.classList.toggle("text-indigo-700", isActive);
    const check = el.querySelector(".quick-check");
    if (check) {
      check.classList.toggle("bg-indigo-600", isActive);
      check.classList.toggle("border-indigo-600", isActive);
      check.textContent = isActive ? "✓" : "";
    }
  });
}

function quickToggleSegment(seg) {
  if (workshop.active) return; // workshop owns the loop
  // Multi-select: same semantics as the sidebar — toggle this segment in/out.
  const idx = selectedSegments.findIndex((s) => s.id === seg.id);
  if (idx >= 0) {
    selectedSegments.splice(idx, 1);
    document
      .querySelectorAll(`.segment-btn[data-id="${seg.id}"]`)
      .forEach((b) => b.classList.remove("active"));
  } else {
    selectedSegments.push(seg);
    document
      .querySelectorAll(`.segment-btn[data-id="${seg.id}"]`)
      .forEach((b) => b.classList.add("active"));
  }
  onManualSelectionChanged();
}

quickSegmentsBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  quickSegmentsMenu.classList.toggle("hidden");
});

document.addEventListener("click", (e) => {
  if (!quickSegments.contains(e.target)) {
    quickSegmentsMenu.classList.add("hidden");
  }
});

restartSegmentBtn.addEventListener("click", () => {
  // loopBounds is set whenever a segment / combination / workshop phase is
  // active — jump to its start. Otherwise rewind the video to 0.
  cancelLoopBreak();
  const target = loopBounds ? loopBounds.start : 0;
  try {
    video.currentTime = target;
    resetCountBeat();
    if (video.paused) video.play().catch(() => {});
  } catch {
    /* video not ready yet */
  }
});

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
  cancelLoopBreak();
  onManualSelectionChanged();
}

function onManualSelectionChanged() {
  if (workshop.active) return; // workshop owns loopBounds
  loopBounds = calculateLoopBounds();
  resetCountBeat();
  segmentCount.textContent = `${selectedSegments.length} selected`;
  if (typeof updateQuickSegmentsState === "function") updateQuickSegmentsState();

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

// The visible "Loop" status card was removed — keep a no-op so existing
// call sites (workshop transitions, library load) don't need to be touched.
function updateLoopDisplay() {}

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
  if (typeof updateQuickSegmentsState === "function") updateQuickSegmentsState();

  video.play().catch(() => {});
}

function stopWorkshop() {
  cancelLoopBreak();
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
  if (typeof updateQuickSegmentsState === "function") updateQuickSegmentsState();
  // Pause playback so the video doesn't keep running once the user explicitly
  // ended the workshop session.
  try { video.pause(); } catch {}
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

let loopBreakTimer = null;

function cancelLoopBreak() {
  if (loopBreakTimer) {
    clearTimeout(loopBreakTimer);
    loopBreakTimer = null;
  }
}

function currentLoopBreakSeconds() {
  const v = parseFloat(loopBreakInput.value);
  if (!isFinite(v) || v < 0) return 0;
  return Math.min(v, 30);
}

/** Snap the playhead back to the loop start, respecting the user-configured
    break delay. During the break we pause the video; after the timeout
    elapses we resume playback. */
function snapLoopBack(afterMs) {
  if (afterMs <= 0) {
    video.currentTime = loopBounds.start;
    resetCountBeat();
    if (video.paused) video.play().catch(() => {});
    return;
  }
  video.pause();
  loopBreakTimer = setTimeout(() => {
    loopBreakTimer = null;
    // Loop bounds may have changed during the break (user cleared / picked
    // a different segment / workshop stopped) — bail out cleanly.
    if (!loopBounds) return;
    video.currentTime = loopBounds.start;
    resetCountBeat();
    video.play().catch(() => {});
  }, afterMs);
}

video.addEventListener("timeupdate", () => {
  if (!loopBounds) return;
  if (loopBreakTimer) return; // already in a break, ignore further fires
  if (video.currentTime >= loopBounds.end - LOOP_EPS) {
    const breakMs = currentLoopBreakSeconds() * 1000;

    if (workshop.active) {
      // Decrement the rep counter immediately — the rep just finished even
      // if we're about to pause for the break.
      workshop.repsRemaining -= 1;
      workshopRepsEl.textContent = String(Math.max(0, workshop.repsRemaining));
      workshopRepsEl.classList.remove("rep-pulse");
      void workshopRepsEl.offsetWidth;
      workshopRepsEl.classList.add("rep-pulse");

      if (workshop.repsRemaining <= 0) {
        // Phase done — break, then advance. `enterPhase` itself starts
        // playback at the new phase's start.
        if (breakMs > 0) {
          video.pause();
          loopBreakTimer = setTimeout(() => {
            loopBreakTimer = null;
            enterPhase(workshop.phaseIdx + 1);
          }, breakMs);
        } else {
          enterPhase(workshop.phaseIdx + 1);
        }
        return;
      }
    }

    snapLoopBack(breakMs);
  }
});

// Any state change that invalidates the current loop should also abort an
// in-flight break.
video.addEventListener("seeking", cancelLoopBreak);

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

memoryBtn.addEventListener("click", () => {
  // `invisible` (visibility:hidden) keeps audio playing while making the
  // video element invisible — exactly what we want for an audio-only
  // memory drill.
  const on = video.classList.toggle("invisible");
  memoryState.textContent = on ? "On" : "Off";
  memoryBtn.classList.toggle("bg-indigo-50", on);
  memoryBtn.classList.toggle("border-indigo-300", on);
  memoryOverlay.classList.toggle("hidden", !on);
  memoryOverlay.classList.toggle("flex", on);
});

workshopBtn.addEventListener("click", () => {
  workshop.active ? stopWorkshop() : startWorkshop();
});
nextDrillBtn.addEventListener("click", nextDrill);
prevDrillBtn.addEventListener("click", previousDrill);

// "?" popover next to the workshop button.
workshopHelpBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  workshopHelpPopover.classList.toggle("hidden");
});
workshopHelpClose.addEventListener("click", () => {
  workshopHelpPopover.classList.add("hidden");
});
document.addEventListener("click", (e) => {
  if (
    !workshopHelpPopover.classList.contains("hidden") &&
    !workshopHelpPopover.contains(e.target) &&
    e.target !== workshopHelpBtn
  ) {
    workshopHelpPopover.classList.add("hidden");
  }
});

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
  } else {
    const dur = Math.max(0, crop.end - crop.start);
    cropDuration.textContent = `${dur.toFixed(2)}s · ${crop.start.toFixed(2)}s → ${crop.end.toFixed(2)}s`;
  }
  // Keep the action-row subtitle in sync with the crop selection.
  if (typeof updateProcessSubtitle === "function") updateProcessSubtitle();
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
  // A valid-duration video is loaded — drop the "load a file first" hint and
  // let the actual inputs do the talking.
  if (cropHint) cropHint.classList.add("hidden");
});

/* ---------------------------------------------------------------- */
/* Process Video — single button that respects the crop selection    */
/* ---------------------------------------------------------------- */

/* ---------- Sticky processing banner ---------- */

let bannerTimerHandle = null;
let bannerStartedAt = 0;

function formatElapsed(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function phaseForElapsed(seconds) {
  // We don't have real progress signals from the server, so approximate by
  // typical pipeline durations.
  if (seconds < 20) return "Downloading";
  if (seconds < 60) return "Extracting pose";
  return "Segmenting";
}

function startProcessingBanner(detail) {
  bannerStartedAt = Date.now();
  processBanner.classList.remove(
    "hidden",
    "bg-emerald-600",
    "bg-rose-600",
    "bg-indigo-600"
  );
  processBanner.classList.add("bg-indigo-600", "flex");
  processBannerSpinner.classList.remove("hidden");
  processBannerClose.classList.add("hidden");
  processBannerTitle.textContent = "Processing video…";
  processBannerPhase.classList.remove("hidden");
  processBannerPhase.textContent = "Downloading";
  processBannerDetail.textContent =
    detail || "This usually takes a few minutes. You can keep this tab open.";
  processBannerTime.textContent = "0:00";

  if (bannerTimerHandle) clearInterval(bannerTimerHandle);
  bannerTimerHandle = setInterval(() => {
    const elapsed = (Date.now() - bannerStartedAt) / 1000;
    processBannerTime.textContent = formatElapsed(elapsed);
    processBannerPhase.textContent = phaseForElapsed(elapsed);
  }, 1000);
}

function stopProcessingBanner() {
  if (bannerTimerHandle) {
    clearInterval(bannerTimerHandle);
    bannerTimerHandle = null;
  }
}

function showProcessingSuccess(message) {
  stopProcessingBanner();
  processBanner.classList.remove("bg-indigo-600", "bg-rose-600");
  processBanner.classList.add("bg-emerald-600", "flex");
  processBanner.classList.remove("hidden");
  processBannerSpinner.classList.add("hidden");
  processBannerPhase.classList.add("hidden");
  processBannerClose.classList.remove("hidden");
  processBannerTitle.textContent = "✓ " + message;
  processBannerDetail.textContent = "";
  processBannerTime.textContent = "";
  setTimeout(() => {
    if (!processBanner.classList.contains("bg-emerald-600")) return;
    processBanner.classList.add("hidden");
    processBanner.classList.remove("flex");
  }, 4500);
}

function showProcessingError(message) {
  stopProcessingBanner();
  processBanner.classList.remove("bg-indigo-600", "bg-emerald-600");
  processBanner.classList.add("bg-rose-600", "flex");
  processBanner.classList.remove("hidden");
  processBannerSpinner.classList.add("hidden");
  processBannerPhase.classList.add("hidden");
  processBannerClose.classList.remove("hidden");
  processBannerTitle.textContent = "Failed";
  processBannerDetail.textContent = message;
  processBannerTime.textContent = "";
}

function hideProcessingBanner() {
  stopProcessingBanner();
  processBanner.classList.add("hidden");
  processBanner.classList.remove("flex");
}

processBannerClose.addEventListener("click", hideProcessingBanner);

/* ---------- Empty state ---------- */

function showEmptyState() {
  emptyState.classList.remove("hidden");
  emptyState.classList.add("flex");
  video.classList.add("hidden");
}

function hideEmptyState() {
  emptyState.classList.add("hidden");
  emptyState.classList.remove("flex");
  video.classList.remove("hidden");
}

emptyStateLibraryLink.addEventListener("click", async () => {
  await refreshLibrary();
  libraryPanel.classList.remove("hidden");
});

video.addEventListener("loadeddata", hideEmptyState);

/* ---------- Source toggle ---------- */

let currentSource = "youtube"; // "youtube" | "upload"

function setSource(source) {
  currentSource = source;
  document.querySelectorAll(".source-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.source === source);
  });
  sourceYoutubeRow.classList.toggle("hidden", source !== "youtube");
  sourceUploadRow.classList.toggle("hidden", source !== "upload");
  updateProcessSubtitle();
}

sourceToggle.addEventListener("click", (e) => {
  const btn = e.target.closest(".source-btn");
  if (!btn) return;
  e.preventDefault();
  // If user starts a new action while a stale done/error banner is up, clear it.
  if (
    processBanner.classList.contains("bg-emerald-600") ||
    processBanner.classList.contains("bg-rose-600")
  ) {
    hideProcessingBanner();
  }
  setSource(btn.dataset.source);
});

/* ---------- Process subtitle (live preview of what will run) ---------- */

function updateProcessSubtitle() {
  let prefix;
  if (currentSource === "youtube") {
    const url = youtubeUrlInput.value.trim();
    if (!url) {
      processSubtitle.textContent = "Paste a YouTube URL to enable.";
      processVideoBtn.disabled = true;
      return;
    }
    prefix = `YouTube ${youtubeQualitySelect.value}`;
  } else {
    if (!pendingFiles.length) {
      processSubtitle.textContent = "Choose a video file to enable.";
      processVideoBtn.disabled = true;
      return;
    }
    if (pendingFiles.length === 1) {
      prefix = `"${pendingFiles[0].name}"`;
    } else {
      prefix = `${pendingFiles.length} clips combined`;
    }
  }
  const crop = getCropBounds();
  // Combined uploads don't support pre-crop — they get re-encoded as one
  // continuous video, and the user can re-process with crop afterward.
  const isCombined = currentSource === "upload" && pendingFiles.length > 1;
  const cropStr = isCombined
    ? "no crop"
    : crop
    ? `crop ${crop.start.toFixed(2)}s → ${crop.end.toFixed(2)}s`
    : "full video";
  processSubtitle.textContent = `Will process ${prefix} · ${cropStr}.`;
  processVideoBtn.disabled = false;
}

youtubeUrlInput.addEventListener("input", updateProcessSubtitle);
youtubeQualitySelect.addEventListener("change", updateProcessSubtitle);

/* ---------- API helpers ---------- */

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

async function processFiles(files) {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  const res = await fetch("/api/process-files", { method: "POST", body: form });
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
  hideEmptyState();
  allSegments = data.segments;
  currentVideoId = data.video_id || null;
  // A freshly processed video is always owned by the requesting user.
  currentPermission = "owner";
  applyPermissionGating();
  renderSegments(allSegments);
  refreshLibrary();
  // Start watching for collaborator edits on the video we just made.
  startEntryPoll(currentVideoId, null);

  let msg = `Done — ${data.segment_count} segments from ${data.duration.toFixed(1)}s.`;
  if (data.height) {
    msg += ` Got ${data.height}p.`;
    if (data.height < 480) {
      msg += " YouTube throttled the source — upload the file directly for better quality.";
    }
  }
  if (data.tuning && data.tuning.example_count > 0) {
    msg += ` ✨ Tuned from your last ${data.tuning.example_count} edited ${data.tuning.example_count === 1 ? "video" : "videos"}.`;
  }
  showProcessingSuccess(msg);
  // Reduce clutter once the user has a real video on screen.
  setAddVideoCollapsed(true);
}

/* ---------- Process Video click handler ---------- */

processVideoBtn.addEventListener("click", async () => {
  if (currentSource === "youtube") {
    const url = youtubeUrlInput.value.trim();
    if (!url) return;
  } else if (!pendingFiles.length) {
    return;
  }

  if (workshop.active) stopWorkshop();
  clearSelection();

  const crop = getCropBounds();
  const isCombined = currentSource === "upload" && pendingFiles.length > 1;
  const msg = isCombined
    ? `Combining ${pendingFiles.length} clips and processing.`
    : crop
    ? `Processing crop ${crop.start.toFixed(2)}s → ${crop.end.toFixed(2)}s of your video.`
    : "Processing the full video.";
  startProcessingBanner(msg);
  processVideoBtn.disabled = true;

  try {
    let data;
    if (currentSource === "youtube") {
      data = await processYouTube(youtubeUrlInput.value.trim(), crop);
    } else if (isCombined) {
      data = await processFiles(pendingFiles);
    } else {
      data = await processFile(pendingFiles[0], crop);
    }
    applyProcessResponse(data);
  } catch (err) {
    console.error(err);
    showProcessingError(err.message);
  } finally {
    updateProcessSubtitle(); // re-enables button if inputs still valid
  }
});

/* ---------- File picker ---------- */

function renderUploadFileList() {
  uploadFileList.innerHTML = "";
  if (pendingFiles.length === 0) {
    uploadFileList.classList.add("hidden");
    uploadLabel.textContent = "Choose one or more video files…";
    cropRow.classList.remove("hidden");
    return;
  }
  uploadFileList.classList.remove("hidden");
  uploadLabel.textContent =
    pendingFiles.length === 1
      ? "Replace file…"
      : `Add more (${pendingFiles.length} selected)`;

  // Combined uploads can't be pre-cropped (the merged video doesn't exist
  // yet) — hide the crop row while >1 file is queued.
  cropRow.classList.toggle("hidden", pendingFiles.length > 1);

  pendingFiles.forEach((file, idx) => {
    const li = document.createElement("li");
    li.className =
      "flex items-center gap-2 px-2 py-1 rounded-md bg-slate-50 border border-slate-200 text-xs";
    const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
    li.innerHTML = `
      <span class="text-slate-400 font-mono w-5 text-right">${idx + 1}.</span>
      <span class="flex-1 min-w-0 truncate" title="${escapeAttr(file.name)}">${escapeAttr(file.name)}</span>
      <span class="text-slate-500 font-mono whitespace-nowrap">${sizeMb} MB</span>
      <button class="upload-remove text-rose-500 hover:text-rose-700 text-sm leading-none" title="Remove">×</button>
    `;
    li.querySelector(".upload-remove").addEventListener("click", () => {
      pendingFiles.splice(idx, 1);
      onPendingFilesChanged();
    });
    uploadFileList.appendChild(li);
  });
}

function onPendingFilesChanged() {
  currentVideoId = null;
  // Preview shows the first file so the crop UI works for single-file uploads.
  if (video.src && video.src.startsWith("blob:")) URL.revokeObjectURL(video.src);
  if (pendingFiles.length === 1) {
    video.src = URL.createObjectURL(pendingFiles[0]);
    video.load();
  } else if (pendingFiles.length === 0) {
    showEmptyState();
  }
  renderUploadFileList();
  hideProcessingBanner();
  updateProcessSubtitle();
}

videoFileInput.addEventListener("change", (e) => {
  const newFiles = Array.from(e.target.files || []);
  if (!newFiles.length) return;
  // Cap at 8 to match the backend; warn if the user goes over.
  pendingFiles = pendingFiles.concat(newFiles).slice(0, 8);
  e.target.value = ""; // allow re-selecting the same file
  onPendingFilesChanged();
});

/* ---------------------------------------------------------------- */
/* Segment editor                                                    */
/* ---------------------------------------------------------------- */

/** Working copy used while editing — committed to allSegments on save. */
let editorDraft = [];
let editing = false;

function enterEditMode() {
  // Defence-in-depth: even if the Edit button leaked through somehow, refuse
  // to open the editor when the user has view-only access.
  if (currentPermission === "view") {
    alert("This video is shared with you as view-only — only the owner can edit segments.");
    return;
  }
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

/** Per-row input + status references, keyed by the seg object. Lets us
    update a neighbouring row's inputs without re-rendering the whole list
    (which would blow away focus while the user is still typing). */
let editorRowRefs = new Map();

/** Match adjacent segments when one's boundary changes:
    - changing seg.start → previous segment's end snaps to the same value
    - changing seg.end   → next segment's start snaps to the same value
    Adjacency is determined by sorted start time so reordering edits behave. */
function linkAdjacent(seg, side) {
  const sorted = [...editorDraft].sort((a, b) => a.start - b.start);
  const idx = sorted.indexOf(seg);
  if (idx < 0) return;

  let neighbor = null;
  let field = null;
  if (side === "start" && idx > 0) {
    neighbor = sorted[idx - 1];
    field = "end";
    neighbor.end = seg.start;
  } else if (side === "end" && idx < sorted.length - 1) {
    neighbor = sorted[idx + 1];
    field = "start";
    neighbor.start = seg.end;
  }
  if (!neighbor) return;

  // Reflect the change in the neighbour's row UI without re-rendering.
  const refs = editorRowRefs.get(neighbor);
  if (!refs) return;
  if (field === "start") refs.startEl.value = neighbor.start.toFixed(2);
  if (field === "end") refs.endEl.value = neighbor.end.toFixed(2);
  refs.refreshStatus();
}

function renderEditor() {
  editorList.innerHTML = "";
  editorRowRefs = new Map();
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

    editorRowRefs.set(seg, { startEl, endEl, refreshStatus });

    labelEl.addEventListener("input", () => { seg.label = labelEl.value; });
    startEl.addEventListener("input", () => {
      const v = parseFloat(startEl.value);
      if (isFinite(v)) {
        seg.start = v;
        refreshStatus();
        linkAdjacent(seg, "start");
      }
    });
    endEl.addEventListener("input", () => {
      const v = parseFloat(endEl.value);
      if (isFinite(v)) {
        seg.end = v;
        refreshStatus();
        linkAdjacent(seg, "end");
      }
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
      linkAdjacent(seg, "start");
    });
    row.querySelector(".ed-snap-end").addEventListener("click", () => {
      seg.end = +video.currentTime.toFixed(2);
      endEl.value = seg.end.toFixed(2);
      refreshStatus();
      linkAdjacent(seg, "end");
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
      // Record our own edit so the next poll doesn't flag it as remote.
      lastEditedAtKnown = data.last_edited_at || lastEditedAtKnown;
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

// Share the currently-loaded video — reuses the same modal the library uses.
shareCurrentBtn.addEventListener("click", () => {
  if (!currentVideoId) return;
  // Find the library entry for the current video so the modal can show its
  // title. Fall back to a stub if the entry isn't cached yet.
  const entry =
    libraryCache.find((i) => i.video_id === currentVideoId) || {
      video_id: currentVideoId,
      title: "this video",
    };
  openShareModal(entry);
});
editorCancelBtn.addEventListener("click", exitEditMode);
editorSaveBtn.addEventListener("click", saveEditorDraft);
editorAddBtn.addEventListener("click", addSegmentAtPlayhead);
editorSplitBtn.addEventListener("click", splitAtPlayhead);

/* ---------------------------------------------------------------- */
/* Library — previously processed videos                             */
/* ---------------------------------------------------------------- */

let libraryCache = [];

/** Combine-mode state: when true, library cards turn into selectable tiles
    and clicking adds/removes the entry from the ordered selection list. */
let libraryCombineMode = false;
let libraryCombineSelection = []; // ordered video_ids

const libraryCombineBtn = document.getElementById("library-combine-btn");
const libraryCombineBar = document.getElementById("library-combine-bar");
const libraryCombineConfirm = document.getElementById("library-combine-confirm");
const libraryCombineCancel = document.getElementById("library-combine-cancel");
const libraryCombineHint = document.getElementById("library-combine-hint");

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
    const isOwner = entry.permission === "owner";
    const canEdit = entry.permission === "owner" || entry.permission === "edit";
    const sourceBadge =
      entry.source === "youtube"
        ? '<span class="text-[10px] uppercase tracking-wide bg-rose-100 text-rose-700 px-1.5 py-0.5 rounded">YouTube</span>'
        : '<span class="text-[10px] uppercase tracking-wide bg-sky-100 text-sky-700 px-1.5 py-0.5 rounded">Upload</span>';
    const editedBadge = entry.last_edited_at
      ? `<span class="text-[10px] uppercase tracking-wide bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded" title="Edited ${formatRelativeTime(entry.last_edited_at)}">edited</span>`
      : "";
    const sharedByBadge = !isOwner && entry.shared_by_username
      ? `<span class="text-[10px] uppercase tracking-wide bg-emerald-100 text-emerald-700 px-1.5 py-0.5 rounded" title="Shared by ${escapeAttr(entry.shared_by_username)}">${entry.permission === "edit" ? "shared · edit" : "shared · view"}</span>`
      : "";
    const cropNote =
      entry.crop_start != null || entry.crop_end != null
        ? `<span class="text-[10px] text-slate-400">· crop ${(entry.crop_start ?? 0).toFixed(1)}–${(entry.crop_end ?? entry.duration ?? 0).toFixed(1)}s</span>`
        : "";

    const renameBtnHtml = isOwner
      ? '<button class="lib-rename text-xs text-slate-400 hover:text-slate-700 flex-shrink-0" title="Rename">✎</button>'
      : "";
    // Top-right column of each card: a labelled Share button on its own row,
    // delete icon underneath. Owner-only.
    const ownerActionsHtml = isOwner
      ? `
        <button class="lib-share px-2.5 py-1 text-xs rounded-md border border-indigo-200 bg-white text-indigo-700 font-semibold hover:bg-indigo-50 whitespace-nowrap">Share</button>
        <button class="lib-delete text-xs text-rose-500 hover:text-rose-700" title="Remove from library">🗑</button>`
      : "";

    card.innerHTML = `
      <div class="flex items-start justify-between gap-2">
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2 mb-0.5 flex-wrap">
            ${sourceBadge}
            ${sharedByBadge}
            ${editedBadge}
            <span class="text-xs text-slate-400">${formatRelativeTime(entry.processed_at)}</span>
          </div>
          <div class="flex items-center gap-1 min-w-0">
            <span class="lib-title text-sm font-semibold text-slate-900 truncate outline-none"
                  contenteditable="false"
                  title="${escapeAttr(entry.title)}">${escapeAttr(entry.title)}</span>
            ${renameBtnHtml}
          </div>
          <div class="text-xs text-slate-500 font-mono mt-0.5">
            ${entry.segment_count} segments · ${dur} ${cropNote}
          </div>
        </div>
        <div class="flex flex-col items-end gap-1">
          ${ownerActionsHtml}
        </div>
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
    // In combine mode, the whole card toggles selection and the inner
    // buttons are inert. Otherwise the inner buttons drive the usual flows.
    if (libraryCombineMode) {
      const order = libraryCombineSelection.indexOf(entry.video_id);
      if (order >= 0) {
        card.classList.add("ring-2", "ring-indigo-500");
        const badge = document.createElement("div");
        badge.className =
          "absolute top-1 left-1 w-6 h-6 rounded-full bg-indigo-600 text-white text-xs font-bold flex items-center justify-center shadow";
        badge.textContent = String(order + 1);
        card.classList.add("relative");
        card.appendChild(badge);
      }
      card.classList.add("cursor-pointer", "select-none");
      card.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleCombineSelection(entry.video_id);
      });
    } else {
      card.querySelector(".lib-open").addEventListener("click", (e) => {
        e.stopPropagation();
        loadLibraryEntry(entry.video_id, { autoStartWorkshop: false });
      });
      card.querySelector(".lib-workshop").addEventListener("click", (e) => {
        e.stopPropagation();
        loadLibraryEntry(entry.video_id, { autoStartWorkshop: true });
      });
    }
    const titleEl = card.querySelector(".lib-title");
    const renameBtn = card.querySelector(".lib-rename");

    function startRename(e) {
      e?.stopPropagation();
      if (!isOwner) return; // viewers and editors can't rename
      titleEl.contentEditable = "true";
      titleEl.classList.add("border", "border-indigo-400", "px-1", "rounded", "bg-white");
      titleEl.focus();
      // Select all so the user can just start typing to replace.
      const range = document.createRange();
      range.selectNodeContents(titleEl);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    if (renameBtn) renameBtn.addEventListener("click", startRename);
    if (isOwner) titleEl.addEventListener("dblclick", startRename);

    async function commitRename() {
      titleEl.contentEditable = "false";
      titleEl.classList.remove("border", "border-indigo-400", "px-1", "rounded", "bg-white");
      const newTitle = titleEl.textContent.trim();
      if (!newTitle || newTitle === entry.title) {
        titleEl.textContent = entry.title;
        return;
      }
      try {
        const res = await fetch(`/api/library/${encodeURIComponent(entry.video_id)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: newTitle }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        await refreshLibrary();
      } catch (err) {
        console.error(err);
        titleEl.textContent = entry.title;
      }
    }
    titleEl.addEventListener("blur", commitRename);
    titleEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        titleEl.blur();
      } else if (e.key === "Escape") {
        e.preventDefault();
        titleEl.textContent = entry.title;
        titleEl.blur();
      }
    });

    const deleteBtn = card.querySelector(".lib-delete");
    if (deleteBtn) {
      deleteBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`Remove "${entry.title}" from library?`)) return;
        await fetch(`/api/library/${encodeURIComponent(entry.video_id)}`, {
          method: "DELETE",
        });
        await refreshLibrary();
      });
    }

    const shareBtn = card.querySelector(".lib-share");
    if (shareBtn) {
      shareBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openShareModal(entry);
      });
    }
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
    pendingFiles = [];
    renderUploadFileList();
    currentVideoId = data.video_id;
    currentPermission = data.permission || "owner";
    applyPermissionGating();
    youtubeUrlInput.value = data.source_url || "";
    // Watch for upstream edits (collaborator or owner) on this entry.
    startEntryPoll(currentVideoId, data.last_edited_at || null);

    allSegments = data.segments;
    renderSegments(allSegments);

    libraryPanel.classList.add("hidden");
    hideEmptyState();
    setAddVideoCollapsed(true);
    showProcessingSuccess(`Loaded "${data.title}" — ${data.segment_count} segments.`);

    if (autoStartWorkshop) {
      // Wait one tick so the video element has a chance to begin loading.
      setTimeout(() => startWorkshop(), 0);
    }
  } catch (err) {
    console.error(err);
    showProcessingError(`Failed to load: ${err.message}`);
  }
}

/** Poll handle that refreshes the library every 8s while the panel is open,
    so new shares from other users appear without a manual reload. */
let libraryPollHandle = null;

function startLibraryPoll() {
  if (libraryPollHandle) return;
  libraryPollHandle = setInterval(refreshLibrary, 8000);
}

function stopLibraryPoll() {
  if (libraryPollHandle) {
    clearInterval(libraryPollHandle);
    libraryPollHandle = null;
  }
}

libraryBtn.addEventListener("click", async () => {
  await refreshLibrary();
  const wasHidden = libraryPanel.classList.toggle("hidden");
  if (!wasHidden) startLibraryPoll();
  else stopLibraryPoll();
});
libraryCloseBtn.addEventListener("click", () => {
  libraryPanel.classList.add("hidden");
  stopLibraryPoll();
  if (libraryCombineMode) exitLibraryCombineMode();
});

/* ---------------------------------------------------------------- */
/* Library combine mode — select N entries, merge into a new routine */
/* ---------------------------------------------------------------- */

function enterLibraryCombineMode() {
  libraryCombineMode = true;
  libraryCombineSelection = [];
  libraryCombineBar.classList.remove("hidden");
  libraryCombineBtn.textContent = "Combining…";
  libraryCombineBtn.disabled = true;
  updateLibraryCombineConfirm();
  renderLibrary();
}

function exitLibraryCombineMode() {
  libraryCombineMode = false;
  libraryCombineSelection = [];
  libraryCombineBar.classList.add("hidden");
  libraryCombineBtn.textContent = "Combine…";
  libraryCombineBtn.disabled = false;
  renderLibrary();
}

function toggleCombineSelection(videoId) {
  const idx = libraryCombineSelection.indexOf(videoId);
  if (idx >= 0) libraryCombineSelection.splice(idx, 1);
  else libraryCombineSelection.push(videoId);
  updateLibraryCombineConfirm();
  renderLibrary();
}

function updateLibraryCombineConfirm() {
  const n = libraryCombineSelection.length;
  libraryCombineConfirm.disabled = n < 2;
  libraryCombineConfirm.textContent =
    n === 0 ? "Combine 0 selected" : `Combine ${n} selected`;
  libraryCombineHint.textContent =
    n < 2
      ? "Click clips in the order you want them combined."
      : `Will merge in this order: ${libraryCombineSelection
          .map((id, i) => {
            const entry = libraryCache.find((e) => e.video_id === id);
            return `${i + 1}. ${entry ? entry.title : id}`;
          })
          .join(" → ")}`;
}

libraryCombineBtn.addEventListener("click", enterLibraryCombineMode);
libraryCombineCancel.addEventListener("click", exitLibraryCombineMode);

libraryCombineConfirm.addEventListener("click", async () => {
  if (libraryCombineSelection.length < 2) return;
  const selectedIds = [...libraryCombineSelection];
  libraryCombineConfirm.disabled = true;
  libraryPanel.classList.add("hidden");
  exitLibraryCombineMode();

  startProcessingBanner(
    `Combining ${selectedIds.length} library videos and processing.`
  );
  processVideoBtn.disabled = true;
  try {
    const res = await fetch("/api/library/combine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_ids: selectedIds }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    applyProcessResponse(data);
  } catch (err) {
    console.error(err);
    showProcessingError(err.message);
  } finally {
    updateProcessSubtitle();
  }
});

/* ---------------------------------------------------------------- */
/* Per-entry sync — polls the open video's segments every 15s so a   */
/* collaborator's edits (or owner's edits on a viewer's screen)      */
/* appear without a manual refresh.                                  */
/* ---------------------------------------------------------------- */

function stopEntryPoll() {
  if (entryPollHandle) {
    clearInterval(entryPollHandle);
    entryPollHandle = null;
  }
}

function startEntryPoll(videoId, initialLastEditedAt) {
  stopEntryPoll();
  if (!videoId) return;
  lastEditedAtKnown = initialLastEditedAt || null;
  entryPollHandle = setInterval(
    () => checkRemoteEntryUpdates(videoId),
    15000
  );
}

async function checkRemoteEntryUpdates(videoId) {
  // Stale poll — a different entry is now loaded.
  if (videoId !== currentVideoId) {
    stopEntryPoll();
    return;
  }
  // Don't disrupt active loop owners.
  if (workshop.active) return;
  // Don't trash the user's open-but-unsaved edit draft.
  if (typeof editing !== "undefined" && editing) return;

  try {
    const res = await fetch(`/api/library/${encodeURIComponent(videoId)}`);
    if (!res.ok) return;
    const data = await res.json();
    const remoteEdited = data.last_edited_at || null;
    if (remoteEdited === lastEditedAtKnown) return;

    lastEditedAtKnown = remoteEdited;
    allSegments = data.segments;
    renderSegments(allSegments);
    showProcessingSuccess(
      currentPermission === "owner"
        ? "Segments updated by a collaborator."
        : "Segments updated by the owner."
    );
  } catch {
    /* network blip — try again next tick */
  }
}

/* ---------------------------------------------------------------- */
/* Tutorial overlay                                                  */
/* ---------------------------------------------------------------- */

const TUTORIAL_SEEN_KEY = "dss.tutorial_seen";

function showTutorial() {
  tutorialOverlay.classList.remove("hidden");
  tutorialOverlay.classList.add("flex");
}

function hideTutorial() {
  tutorialOverlay.classList.add("hidden");
  tutorialOverlay.classList.remove("flex");
  try { localStorage.setItem(TUTORIAL_SEEN_KEY, "1"); } catch {}
}

function maybeShowTutorialForNewUser(libraryItems) {
  // Auto-open exactly once per browser, and only when the library is empty
  // (so returning users with content don't get nagged).
  let seen = false;
  try { seen = localStorage.getItem(TUTORIAL_SEEN_KEY) === "1"; } catch {}
  if (!seen && libraryItems.length === 0) showTutorial();
}

tutorialBtn.addEventListener("click", showTutorial);
tutorialClose.addEventListener("click", hideTutorial);
tutorialDismiss.addEventListener("click", hideTutorial);
tutorialOverlay.addEventListener("click", (e) => {
  if (e.target === tutorialOverlay) hideTutorial();
});

/* ---------------------------------------------------------------- */
/* Share modal                                                       */
/* ---------------------------------------------------------------- */

function openShareModal(entry) {
  shareVideoId = entry.video_id;
  shareSubtitle.textContent = `"${entry.title}" — invite others by email.`;
  shareUsernameEl.value = "";
  shareError.classList.add("hidden");
  shareList.innerHTML =
    '<p class="text-xs text-slate-400 text-center py-2">Loading shares…</p>';
  shareModal.classList.remove("hidden");
  shareModal.classList.add("flex");
  refreshShares();
  shareUsernameEl.focus();
}

function closeShareModal() {
  shareVideoId = null;
  shareModal.classList.add("hidden");
  shareModal.classList.remove("flex");
}

async function refreshShares() {
  if (!shareVideoId) return;
  try {
    const [sharesRes, linksRes] = await Promise.all([
      fetch(`/api/library/${encodeURIComponent(shareVideoId)}/shares`),
      fetch(`/api/library/${encodeURIComponent(shareVideoId)}/share-links`),
    ]);
    if (!sharesRes.ok) throw new Error(`HTTP ${sharesRes.status}`);
    if (!linksRes.ok) throw new Error(`HTTP ${linksRes.status}`);
    renderShareList(await sharesRes.json());
    renderShareLinks(await linksRes.json());
  } catch (err) {
    shareList.innerHTML = `<p class="text-xs text-rose-600 text-center py-2">${err.message}</p>`;
  }
}

function renderShareLinks(links) {
  shareLinksList.innerHTML = "";
  if (!links.length) {
    shareLinksList.innerHTML =
      '<p class="text-xs text-slate-400 text-center py-1">No active links.</p>';
    return;
  }
  for (const link of links) {
    const row = document.createElement("div");
    row.className =
      "flex items-center gap-2 px-2 py-1.5 rounded-md bg-white border border-slate-200 text-xs";
    const permClass =
      link.permission === "edit"
        ? "bg-amber-100 text-amber-700"
        : "bg-slate-200 text-slate-700";
    row.innerHTML = `
      <span class="px-1.5 py-0.5 rounded ${permClass} text-[10px] uppercase tracking-wide flex-shrink-0">${link.permission}</span>
      <span class="link-url min-w-0 flex-1 truncate font-mono text-slate-600" title="${escapeAttr(link.url)}">${escapeAttr(link.url)}</span>
      <button class="link-copy text-indigo-600 hover:text-indigo-800 flex-shrink-0" title="Copy">⧉</button>
      <button class="link-revoke text-rose-500 hover:text-rose-700 flex-shrink-0" title="Revoke">🗑</button>
    `;
    row.querySelector(".link-copy").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(link.url);
        const btn = row.querySelector(".link-copy");
        const orig = btn.textContent;
        btn.textContent = "✓";
        setTimeout(() => (btn.textContent = orig), 1200);
      } catch {
        // Fallback: select the URL span so the user can manually copy.
        const sel = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(row.querySelector(".link-url"));
        sel.removeAllRanges();
        sel.addRange(range);
      }
    });
    row.querySelector(".link-revoke").addEventListener("click", async () => {
      await fetch(
        `/api/library/${encodeURIComponent(shareVideoId)}/share-links/${encodeURIComponent(link.token)}`,
        { method: "DELETE" }
      );
      await refreshShares();
    });
    shareLinksList.appendChild(row);
  }
}

shareLinkCreateBtn.addEventListener("click", async () => {
  shareError.classList.add("hidden");
  const permission = (document.querySelector('input[name="share-perm"]:checked')?.value || "view");
  try {
    const res = await fetch(
      `/api/library/${encodeURIComponent(shareVideoId)}/share-links`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ permission }),
      }
    );
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    // Auto-copy the freshly minted URL for the most common workflow.
    try { await navigator.clipboard.writeText(data.url); } catch {}
    await refreshShares();
  } catch (err) {
    shareError.textContent = err.message;
    shareError.classList.remove("hidden");
  }
});

function renderShareList(shares) {
  shareList.innerHTML = "";
  if (!shares.length) {
    shareList.innerHTML =
      '<p class="text-xs text-slate-400 text-center py-2">Not shared with anyone yet.</p>';
    return;
  }
  for (const s of shares) {
    const row = document.createElement("div");
    row.className =
      "flex items-center justify-between gap-2 px-2 py-1.5 rounded-md bg-slate-50 border border-slate-200 text-xs";
    const permClass =
      s.permission === "edit"
        ? "bg-amber-100 text-amber-700"
        : "bg-slate-200 text-slate-700";
    row.innerHTML = `
      <div class="min-w-0 flex-1 truncate">${escapeAttr(s.shared_with_username)}</div>
      <span class="px-1.5 py-0.5 rounded ${permClass} text-[10px] uppercase tracking-wide">${s.permission}</span>
      <button class="share-revoke text-rose-500 hover:text-rose-700" title="Revoke">🗑</button>
    `;
    row.querySelector(".share-revoke").addEventListener("click", async () => {
      await fetch(
        `/api/library/${encodeURIComponent(shareVideoId)}/shares/${s.shared_with_id}`,
        { method: "DELETE" }
      );
      await refreshShares();
    });
    shareList.appendChild(row);
  }
}

function showShareError(message, { offerLink = false } = {}) {
  shareError.classList.remove("hidden", "text-rose-600");
  shareError.classList.add("text-rose-600");
  shareError.innerHTML = "";
  shareError.appendChild(document.createTextNode(message));
  if (offerLink) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className =
      "ml-2 underline text-indigo-600 hover:text-indigo-800";
    btn.textContent = "Generate share link instead";
    btn.addEventListener("click", () => {
      shareError.classList.add("hidden");
      shareLinkCreateBtn.focus();
      shareLinkCreateBtn.click();
    });
    shareError.appendChild(btn);
  }
}

shareForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  shareError.classList.add("hidden");
  const permission = (document.querySelector('input[name="share-perm"]:checked')?.value || "view");
  try {
    const res = await fetch(
      `/api/library/${encodeURIComponent(shareVideoId)}/shares`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: shareUsernameEl.value.trim(), permission }),
      }
    );
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      // 404 = unknown username; surface the "generate a link instead" affordance.
      if (res.status === 404) {
        showShareError(detail.detail || "That username doesn't exist.", { offerLink: true });
        return;
      }
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    shareUsernameEl.value = "";
    await refreshShares();
  } catch (err) {
    showShareError(err.message);
  }
});

shareClose.addEventListener("click", closeShareModal);
shareModal.addEventListener("click", (e) => {
  if (e.target === shareModal) closeShareModal();
});

/* ---------------------------------------------------------------- */
/* Share-link redemption (?share=token)                              */
/* ---------------------------------------------------------------- */

const PENDING_SHARE_KEY = "dss.pending_share_token";

function getPendingShareToken() {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("share");
  if (fromUrl) {
    try { sessionStorage.setItem(PENDING_SHARE_KEY, fromUrl); } catch {}
    // Clean the token out of the URL so refreshes don't reapply it.
    params.delete("share");
    const cleanQs = params.toString();
    window.history.replaceState(
      {},
      "",
      window.location.pathname + (cleanQs ? "?" + cleanQs : "") + window.location.hash
    );
    return fromUrl;
  }
  try { return sessionStorage.getItem(PENDING_SHARE_KEY); } catch { return null; }
}

function clearPendingShareToken() {
  try { sessionStorage.removeItem(PENDING_SHARE_KEY); } catch {}
}

async function fetchSharePreview(token) {
  try {
    const res = await fetch(`/api/share/${encodeURIComponent(token)}/preview`);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

async function redeemPendingShareIfAny() {
  const token = getPendingShareToken();
  if (!token) return;
  try {
    const res = await fetch(`/api/share/${encodeURIComponent(token)}/accept`, {
      method: "POST",
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    clearPendingShareToken();
    showProcessingSuccess(
      `Added shared video to your library (${data.permission} access).`
    );
  } catch (err) {
    clearPendingShareToken();
    console.warn("Couldn't redeem share token:", err);
  }
}

/* ---------------------------------------------------------------- */
/* Auth                                                              */
/* ---------------------------------------------------------------- */


let shareVideoId = null;

let authMode = "signin"; // "signin" | "signup"

function setAuthMode(mode) {
  authMode = mode;
  authTabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === mode));
  authSubmit.textContent = mode === "signin" ? "Sign in" : "Create account";
  authPassword.autocomplete = mode === "signin" ? "current-password" : "new-password";
  authError.classList.add("hidden");
}

async function showAuthOverlay() {
  authOverlay.classList.remove("hidden");
  authOverlay.classList.add("flex");
  appRoot.classList.add("hidden");
  userBadge.classList.add("hidden");
  userBadge.classList.remove("inline-flex");

  // If the user landed via a share link, tell them why they're seeing the
  // auth overlay and default the tab to "Sign up" (most likely a new user).
  const token = getPendingShareToken();
  if (token) {
    const preview = await fetchSharePreview(token);
    if (preview && preview.title) {
      authError.classList.remove("hidden", "text-rose-600");
      authError.classList.add("text-slate-600");
      authError.textContent =
        `Sign in or sign up to accept "${preview.title}" from ${preview.owner_username} (${preview.permission} access).`;
      setAuthMode("signup");
    }
  }
}

function hideAuthOverlay() {
  authOverlay.classList.add("hidden");
  authOverlay.classList.remove("flex");
  appRoot.classList.remove("hidden");
  userBadge.classList.remove("hidden");
  userBadge.classList.add("inline-flex");
}

async function checkAuth() {
  try {
    const res = await fetch("/api/auth/me", { cache: "no-store" });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

authTabs.forEach((tab) =>
  tab.addEventListener("click", (e) => {
    e.preventDefault();
    setAuthMode(tab.dataset.tab);
  })
);

authForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  authError.classList.add("hidden");
  authSubmit.disabled = true;
  const endpoint = authMode === "signin" ? "/api/auth/login" : "/api/auth/signup";
  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: authUsername.value.trim(),
        password: authPassword.value,
      }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    // Reload so the app boots cleanly with the new session cookie.
    window.location.reload();
  } catch (err) {
    authError.textContent = err.message;
    authError.classList.remove("hidden");
    authSubmit.disabled = false;
  }
});

logoutBtn.addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.reload();
});

function startUsernameEdit() {
  userRenameError.classList.add("hidden");
  userNameEl.contentEditable = "true";
  userNameEl.classList.add("border", "border-indigo-400", "px-1", "rounded", "bg-white");
  userNameEl.dataset.original = userNameEl.textContent;
  userNameEl.focus();
  const range = document.createRange();
  range.selectNodeContents(userNameEl);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

function endUsernameEdit() {
  userNameEl.contentEditable = "false";
  userNameEl.classList.remove("border", "border-indigo-400", "px-1", "rounded", "bg-white");
}

function showRenameError(msg) {
  userRenameError.textContent = msg;
  userRenameError.classList.remove("hidden");
}

async function commitUsernameEdit() {
  const original = userNameEl.dataset.original || "";
  const next = userNameEl.textContent.trim();

  endUsernameEdit();

  if (!next || next === original) {
    userNameEl.textContent = original;
    return;
  }

  try {
    const res = await fetch("/api/auth/me", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: next }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    userNameEl.textContent = data.username;
    userRenameError.classList.add("hidden");
    // Library may also display this username on shared cards — refresh it.
    refreshLibrary();
  } catch (err) {
    userNameEl.textContent = original;
    showRenameError(err.message);
  }
}

renameUserBtn.addEventListener("click", startUsernameEdit);
userNameEl.addEventListener("dblclick", startUsernameEdit);
userNameEl.addEventListener("blur", () => {
  if (userNameEl.contentEditable === "true") commitUsernameEdit();
});
userNameEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    userNameEl.blur();
  } else if (e.key === "Escape") {
    e.preventDefault();
    userNameEl.textContent = userNameEl.dataset.original || userNameEl.textContent;
    endUsernameEdit();
  }
});

/* ---------------------------------------------------------------- */
/* Boot                                                              */
/* ---------------------------------------------------------------- */

(async () => {
  // Run this early so a share token in the URL gets stashed into sessionStorage
  // before any reload (e.g. after a fresh sign-up).
  getPendingShareToken();

  const user = await checkAuth();
  if (!user) {
    await showAuthOverlay();
    setAuthMode(getPendingShareToken() ? "signup" : "signin");
    return; // don't boot the rest of the app until logged in
  }
  hideAuthOverlay();
  userNameEl.textContent = user.username;

  // Redeem any pending share now that we're authenticated.
  await redeemPendingShareIfAny();

  video.volume = VIDEO_VOLUME_BY_MODE[counts.mode]; // default: On Audio @ 1.0
  setSource("youtube");
  updateProcessSubtitle();
  // Empty-state shows by default since no video src is set; it'll hide when
  // either a library entry is loaded or the user picks a local file.
  showEmptyState();
  allSegments = await loadSegments();
  renderSegments(allSegments);
  await refreshLibrary();
  maybeShowTutorialForNewUser(libraryCache);
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
