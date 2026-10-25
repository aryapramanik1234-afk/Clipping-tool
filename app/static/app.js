"use strict";

const state = {
  page: location.hash.replace("#", "") || "overview",
  videos: [],
  clips: [],
  calendar: [],
  analytics: { counts: {}, metrics: [], available: false },
  usage: {},
  health: {},
  settings: {},
  clipFilter: "all",
  librarySearch: "",
  calendarOffset: 0,
  editingClip: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function fmtBytes(bytes = 0) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(0)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

function fmtDuration(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return "—";
  const s = Math.max(0, Math.floor(Number(seconds)));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
  return `${m}:${String(r).padStart(2, "0")}`;
}

function fmtDate(value, options = {}) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", ...options }).format(date);
}

function fmtDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(date);
}

function statusText(status) {
  const labels = {
    queued: "Queued", validating: "Validating", transcribing: "Transcribing",
    finding_highlights: "Finding moments", rendering: "Rendering clips", ready: "Ready for review",
    needs_setup: "Setup needed", no_highlights: "No candidates", failed: "Needs attention",
    paused: "Paused", needs_review: "Needs review", approved: "Approved",
    rejected: "Rejected", scheduled_local: "Local calendar", published: "Published",
    local_only: "Local plan", cancelled: "Cancelled",
  };
  return labels[status] || String(status || "Unknown").replaceAll("_", " ");
}

function statusClass(status) {
  if (["queued", "validating", "transcribing", "finding_highlights", "rendering"].includes(status)) return "status-processing";
  if (status === "ready") return "status-ready";
  if (["needs_review", "paused"].includes(status)) return status === "paused" ? "status-paused" : "status-review";
  if (["needs_setup", "failed", "no_highlights"].includes(status)) return `status-${status}`;
  return `status-${status}`;
}

function statusBadge(status) {
  return `<span class="status-badge ${statusClass(status)}">${escapeHtml(statusText(status))}</span>`;
}

function progressFor(status) {
  return ({ queued: 8, validating: 18, transcribing: 39, finding_highlights: 63, rendering: 82, ready: 100 })[status] || 0;
}

function timeRange(clip) {
  return `${fmtDuration(clip.start_seconds)} – ${fmtDuration(clip.end_seconds)}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { ...(options.body && !(options.body instanceof FormData) ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let error = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      const detail = payload.detail;
      error = typeof detail === "string" ? detail : detail?.message || error;
    } catch (_ignored) {}
    throw new Error(error);
  }
  if (response.status === 204) return null;
  return response.json();
}

async function loadData() {
  try {
    const [videos, clips, calendar, analytics, usage, health, settings] = await Promise.all([
      api("/api/videos"), api("/api/clips"), api("/api/calendar"),
      api("/api/analytics"), api("/api/usage"), api("/api/health"), api("/api/settings"),
    ]);
    state.videos = videos;
    state.clips = clips;
    state.calendar = calendar;
    state.analytics = analytics;
    state.usage = usage;
    state.health = health;
    state.settings = settings;
    updateChrome();
    renderPage();
  } catch (error) {
    showToast(`Could not reach the local app: ${error.message}`, "error");
    renderDisconnected();
  }
}

function updateChrome() {
  const reviewCount = state.clips.filter((clip) => clip.status === "needs_review").length;
  const libraryCount = state.videos.length;
  const reviewBadge = $("#review-count");
  const libraryBadge = $("#library-count");
  if (reviewBadge) reviewBadge.textContent = reviewCount;
  if (libraryBadge) libraryBadge.textContent = libraryCount;
  const storageSize = $("#storage-size");
  const meter = $("#storage-meter-fill");
  if (storageSize) storageSize.textContent = `${fmtBytes((state.usage.source_bytes || 0) + (state.usage.output_bytes || 0))} used`;
  if (meter) {
    const used = (state.usage.source_bytes || 0) + (state.usage.output_bytes || 0);
    const max = state.health.max_upload_bytes || 1;
    meter.style.width = `${Math.max(0.5, Math.min(100, used / max * 100))}%`;
  }
  const pageName = ({ overview: "Overview", library: "Library", review: "Review queue", calendar: "Calendar", analytics: "Analytics", settings: "Settings" })[state.page] || "Overview";
  $("#page-title").textContent = pageName;
  $$(".nav-link").forEach((link) => link.classList.toggle("active", link.dataset.page === state.page));
}

function pageHeading(eyebrow, title, subtitle, action = "") {
  return `<div class="page-heading"><div class="page-heading-copy"><div class="eyebrow"><span class="eyebrow-dot"></span>${escapeHtml(eyebrow)}</div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(subtitle)}</p></div>${action}</div>`;
}

function setupBanner() {
  const missing = [];
  if (!state.health.ffmpeg) missing.push("FFmpeg");
  if (!state.health.ffprobe) missing.push("FFprobe");
  if (!state.health.transcription) missing.push("the optional faster-whisper package/model");
  if (!missing.length) return "";
  return `<div class="setup-banner"><div class="banner-icon">⚙</div><div><strong>One-time setup before the first render</strong><p>Install ${escapeHtml(missing.join(", "))}. Uploaded files are stored locally; this app will not send them to an AI or storage service.</p></div><button class="secondary-button" data-action="navigate" data-page="settings">View setup</button></div>`;
}

function statCard(label, value, foot, icon, tone = "") {
  return `<article class="stat-card"><div class="stat-top"><span class="stat-label">${escapeHtml(label)}</span><span class="stat-icon ${tone}">${icon}</span></div><div class="stat-value">${escapeHtml(value)}</div><div class="stat-foot">${escapeHtml(foot)}</div></article>`;
}

function heroUpload() {
  return `<section class="hero-card"><div class="hero-content"><div class="hero-kicker"><span></span> YOUR LOCAL CONTENT STUDIO</div><h2>Turn long videos into moments worth sharing.</h2><p>Find clear, self-contained highlights, review every cut, and export vertical clips with synced captions.</p><div class="drop-panel" id="hero-dropzone"><span class="drop-icon">⇧</span><span class="drop-copy"><strong>Drop a video to start a new batch</strong><small>MP4, MOV, MKV or WEBM · max ${fmtBytes(state.health.max_upload_bytes || 2 * 1024 ** 3)}</small></span><button class="secondary-button" data-action="open-upload">Choose video</button></div><div class="hero-bottom"><span class="hero-note">● Local-first&nbsp; · &nbsp;Manual approval&nbsp; · &nbsp;No paid AI APIs</span></div></div><div class="hero-decoration" aria-hidden="true"><div class="phone"></div><div class="play">▶</div><span class="spark">✦</span><span class="spark small">✦</span></div></section>`;
}

function processCard() {
  const stages = [
    ["01", "Transcribe", state.health.transcription ? "Local model" : "Install model", state.health.transcription ? "done" : "locked"],
    ["02", "Select highlights", "Explainable ranking", ""],
    ["03", "Create vertical clips", "9:16 · H.264", state.health.ffmpeg ? "" : "locked"],
    ["04", "Review & approve", "Always in your hands", "done"],
  ];
  return `<section class="process-card"><div class="card-top-line"><h3>How your clips get made</h3><span class="tiny-badge">MVP PIPELINE</span></div><p class="process-intro">A transparent workflow. Nothing is auto-published.</p><div class="pipeline-list">${stages.map(([num, title, meta, cls]) => `<div class="pipeline-item ${cls}"><span class="pipeline-icon">${num}</span><span class="pipeline-text">${title}</span><span class="pipeline-meta">${meta}</span></div>`).join("")}</div></section>`;
}

function recentVideoRow(video) {
  const duration = video.duration_seconds ? fmtDuration(video.duration_seconds) : "Duration pending";
  const clips = Number(video.clip_count || 0);
  const progress = progressFor(video.status);
  const isProcessing = ["queued", "validating", "transcribing", "finding_highlights", "rendering"].includes(video.status);
  return `<div class="video-row"><div class="video-thumb"><span class="thumb-symbol">▧</span>${video.duration_seconds ? `<span class="thumb-duration">${duration}</span>` : ""}</div><div class="video-info"><strong title="${escapeHtml(video.filename)}">${escapeHtml(video.filename)}</strong><small>${fmtBytes(video.size_bytes)}<span>·</span>${clips} ${clips === 1 ? "clip" : "clips"}<span>·</span>${fmtDate(video.created_at)}</small>${isProcessing ? `<div class="progress-track"><span style="width:${progress}%"></span></div>` : ""}</div>${statusBadge(video.status)}</div>`;
}

function overviewPage() {
  const counts = state.analytics.counts || {};
  const review = state.clips.filter((clip) => clip.status === "needs_review").length;
  const planned = state.calendar.length;
  const videos = [...state.videos].slice(0, 4);
  const activity = videos.length ? videos.map(recentVideoRow).join("") : `<div class="empty-state"><div class="empty-illustration">▧</div><h4>Your first source video goes here</h4><p>Upload a video you own or have permission to edit. The local pipeline will create candidates for your review.</p><button class="secondary-button" data-action="open-upload">Upload a video</button></div>`;
  return `${pageHeading("CONTENT WORKSPACE", "Make every moment count.", "A thoughtful, approval-first workflow for turning long videos into Shorts.", `<button class="primary-button" data-action="open-upload"><span class="button-icon">＋</span> New source video</button>`)}
    ${setupBanner()}
    <section class="stats-grid">
      ${statCard("Source videos", String(counts.videos || 0), "Stored on this device", "▧", "blue")}
      ${statCard("Ready to review", String(review), "Your approval required", "✓", "amber")}
      ${statCard("Calendar plans", String(planned), "Local reminders only", "◷", "")}
      ${statCard("AI API spend", "₹0", "Local tools · no API calls", "⌁", "mint")}
    </section>
    <section class="hero-grid">${heroUpload()}${processCard()}</section>
    <section class="content-grid">
      <article class="panel"><div class="panel-header"><div class="panel-heading"><h3>Recent source videos</h3><p>Processing status and candidate count</p></div><button class="panel-link" data-action="navigate" data-page="library">Open library&nbsp; →</button></div><div class="video-list">${activity}</div></article>
      <article class="panel side-note"><div class="note-icon">⌂</div><h3>Your workspace stays local</h3><p>Source files, transcripts, and exports are stored in the app's data folder. There are no connected accounts or cloud AI services in this prototype.</p><div class="note-points"><div class="note-point"><span class="note-check">✓</span>Only process content you own or are authorized to use.</div><div class="note-point"><span class="note-check">✓</span>Check context, crop, subtitles, and rights before publishing.</div><div class="note-point"><span class="note-check">✓</span>Publishing stays manual until an official account connection is configured.</div></div></article>
    </section>`;
}

function thumbnailMarkup(clip, extraClass = "") {
  const thumb = clip.thumbnail_url ? `<img src="${escapeHtml(clip.thumbnail_url)}" alt="Cover frame for ${escapeHtml(clip.title)}" loading="lazy">` : `<span class="preview-play">▶</span>`;
  return `<div class="clip-preview ${extraClass}" data-action="open-clip" data-clip-id="${escapeHtml(clip.id)}">${thumb}${clip.thumbnail_url ? `<span class="preview-play">▶</span>` : ""}<span class="clip-score">Review score · ${Math.round(Number(clip.score) || 0)}</span><span class="preview-duration">${fmtDuration(clip.duration_seconds)}</span></div>`;
}

function clipCard(clip) {
  const footer = clip.status === "needs_review"
    ? `<button class="secondary-button" data-action="open-clip" data-clip-id="${escapeHtml(clip.id)}">Review clip</button><button class="quiet-button" data-action="quick-reject" data-clip-id="${escapeHtml(clip.id)}">Reject</button>`
    : `<button class="secondary-button" data-action="open-clip" data-clip-id="${escapeHtml(clip.id)}">Edit details</button><a class="quiet-button" href="${escapeHtml(clip.export_url)}" download>Export</a>`;
  return `<article class="clip-card">${thumbnailMarkup(clip)}<div class="clip-card-body"><div class="clip-card-meta">${statusBadge(clip.status)}<span class="clip-time">${timeRange(clip)}</span></div><h3>${escapeHtml(clip.title)}</h3><p class="clip-card-desc">${escapeHtml(clip.caption || clip.transcript || "")}</p><div class="clip-card-footer">${footer}</div></div></article>`;
}

function clipsPage() {
  const filter = state.clipFilter;
  const clips = state.clips.filter((clip) => filter === "all" || clip.status === filter);
  const filters = [["all", "All clips"], ["needs_review", "Needs review"], ["approved", "Approved"], ["scheduled_local", "Calendar"]];
  return `${pageHeading("CLIP LIBRARY", "Your best moments, organized.", "Open any clip to preview it, check its transcript, and edit the publishing copy.", `<button class="primary-button" data-action="open-upload"><span class="button-icon">＋</span> Add video</button>`)}
    <div class="section-title-row"><div><h2>Generated clips</h2><p>${state.clips.length} candidate${state.clips.length === 1 ? "" : "s"} · scores only prioritize human review, not predict performance</p></div><div class="filter-tabs">${filters.map(([key, label]) => `<button class="filter-tab ${filter === key ? "active" : ""}" data-action="clip-filter" data-filter="${key}">${label}</button>`).join("")}</div></div>
    ${clips.length ? `<div class="clip-grid">${clips.map(clipCard).join("")}</div>` : `<section class="panel"><div class="empty-state"><div class="empty-illustration">✂</div><h4>${state.clips.length ? "No clips in this view" : "No candidate clips yet"}</h4><p>${state.clips.length ? "Try another filter, or upload a video to make a batch." : "Upload a source file to generate a few timestamped candidates. Every clip stays in draft until you approve it."}</p><button class="secondary-button" data-action="open-upload">Upload a source video</button></div></section>`}`;
}

function libraryPage() {
  const query = state.librarySearch.trim().toLowerCase();
  const videos = state.videos.filter((video) => !query || video.filename.toLowerCase().includes(query));
  const rows = videos.map((video) => `<article class="library-item"><div class="video-thumb"><span class="thumb-symbol">▧</span>${video.duration_seconds ? `<span class="thumb-duration">${fmtDuration(video.duration_seconds)}</span>` : ""}</div><div class="library-main"><h3 title="${escapeHtml(video.filename)}">${escapeHtml(video.filename)}</h3><p>${fmtBytes(video.size_bytes)} &nbsp;·&nbsp; ${video.clip_count || 0} candidates &nbsp;·&nbsp; Added ${fmtDate(video.created_at)}</p>${video.error ? `<p class="error-copy">${escapeHtml(video.error)}</p>` : ""}</div><div class="library-item-actions">${statusBadge(video.status)}${["needs_setup", "failed", "no_highlights"].includes(video.status) ? `<button class="secondary-button" data-action="retry-video" data-video-id="${escapeHtml(video.id)}">${video.status === "no_highlights" ? "Try shorter clips" : "Retry"}</button>` : ""}</div></article>`).join("");
  return `${pageHeading("SOURCE LIBRARY", "A clean home for every source.", "Your uploaded files and their processing history stay in the local workspace.", `<button class="primary-button" data-action="open-upload"><span class="button-icon">＋</span> Add video</button>`)}
    <div class="library-toolbar"><div><h2 style="font-size:13px;margin:0">Source videos</h2><p style="font-size:9px;color:#959baa;margin:4px 0 0">${state.videos.length} saved locally</p></div><label class="search-box"><span aria-hidden="true">⌕</span><input id="library-search" type="search" placeholder="Search videos…" value="${escapeHtml(state.librarySearch)}" /></label></div>
    <div class="library-list">${rows || `<section class="panel"><div class="empty-state"><div class="empty-illustration">▧</div><h4>${query ? "No matching videos" : "Your library is ready"}</h4><p>${query ? "Try a different search." : "Upload your first authorized source video to begin."}</p>${query ? "" : `<button class="secondary-button" data-action="open-upload">Upload a video</button>`}</div></section>`}</div>`;
}

function reviewPage() {
  const clips = state.clips.filter((clip) => clip.status === "needs_review");
  const rows = clips.map((clip) => `<article class="review-row">${thumbnailMarkup(clip)}<div class="review-copy"><h3>${escapeHtml(clip.title)}</h3><p>${escapeHtml(clip.transcript)}</p><div class="review-meta">${statusBadge(clip.status)}<span class="clip-time">${timeRange(clip)}</span><span class="clip-time">${escapeHtml(clip.video_filename || "Source video")}</span><span class="clip-time">Selection score ${Math.round(Number(clip.score) || 0)} / 100</span></div></div><div class="review-actions"><button class="primary-button" data-action="open-clip" data-clip-id="${escapeHtml(clip.id)}">Review & approve</button><button class="quiet-button" data-action="quick-reject" data-clip-id="${escapeHtml(clip.id)}">Reject candidate</button></div></article>`).join("");
  return `${pageHeading("QUALITY GATE", "Every clip gets a human check.", "Verify the transcript, context, framing, and rights before a clip leaves draft.")}
    <section class="panel"><div class="panel-header"><div class="panel-heading"><h3>Waiting for your review</h3><p>${clips.length} clip${clips.length === 1 ? "" : "s"} require an explicit decision</p></div><span class="tiny-badge">APPROVAL REQUIRED</span></div><div class="review-list" style="padding:14px">${rows || `<div class="empty-state"><div class="empty-illustration">✓</div><h4>Nothing waiting for approval</h4><p>When processing finishes, candidate clips will appear here. They will never publish automatically.</p><button class="secondary-button" data-action="open-upload">Add a source video</button></div>`}</div></section>`;
}

function calendarPage() {
  const now = new Date();
  const firstOfMonth = new Date(now.getFullYear(), now.getMonth() + state.calendarOffset, 1);
  const monthTitle = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" }).format(firstOfMonth);
  const start = new Date(firstOfMonth.getFullYear(), firstOfMonth.getMonth(), 1 - firstOfMonth.getDay());
  const activeEvents = state.calendar.map((event) => ({ ...event, date: new Date(event.scheduled_at) }));
  const cells = [];
  for (let i = 0; i < 42; i += 1) {
    const date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    const isOutside = date.getMonth() !== firstOfMonth.getMonth();
    const isToday = date.toDateString() === now.toDateString();
    const events = activeEvents.filter((event) => event.date.toDateString() === date.toDateString());
    cells.push(`<div class="calendar-day ${isOutside ? "outside" : ""} ${isToday ? "today" : ""}"><span class="calendar-number">${date.getDate()}</span>${events.slice(0, 2).map((event) => `<span class="calendar-event" data-action="open-clip" data-clip-id="${escapeHtml(event.clip_id)}" title="${escapeHtml(event.title)}">▶ ${escapeHtml(event.title)}<small>Local plan · ${event.date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</small></span>`).join("")}</div>`);
  }
  const scheduleRows = [...state.calendar].sort((a, b) => new Date(a.scheduled_at) - new Date(b.scheduled_at)).map((event) => {
    const date = new Date(event.scheduled_at);
    return `<div class="schedule-item"><div class="schedule-date"><strong>${date.getDate()}</strong><small>${new Intl.DateTimeFormat(undefined, { month: "short" }).format(date).toUpperCase()}</small></div><div class="schedule-copy"><strong>${escapeHtml(event.title)}</strong><small>Local reminder · ${fmtDateTime(event.scheduled_at)}</small></div><button class="icon-button" data-action="unschedule" data-schedule-id="${escapeHtml(event.id)}" title="Remove calendar plan">×</button></div>`;
  }).join("");
  const empty = `<div class="empty-state" style="padding:17px 10px"><p>No local calendar plans yet. Approve a clip, then add a reminder from its review panel.</p></div>`;
  return `${pageHeading("PUBLISHING PLAN", "A calmer way to stay consistent.", "Plan your cadence here. These entries are local reminders only—not scheduled YouTube uploads.", `<button class="secondary-button" data-action="navigate" data-page="review">Review clips</button>`)}
    <div class="setup-banner"><div class="banner-icon">▶</div><div><strong>YouTube is not connected</strong><p>Calendar entries do not publish or schedule a YouTube upload. Export approved clips and use YouTube Studio until an official OAuth integration is configured.</p></div></div>
    <section class="calendar-layout"><article class="panel calendar-card"><div class="calendar-title"><button class="icon-button" data-action="calendar-prev" aria-label="Previous month">‹</button><h3>${escapeHtml(monthTitle)}</h3><button class="icon-button" data-action="calendar-next" aria-label="Next month">›</button></div><div class="calendar-grid"><div class="calendar-weekday">Sun</div><div class="calendar-weekday">Mon</div><div class="calendar-weekday">Tue</div><div class="calendar-weekday">Wed</div><div class="calendar-weekday">Thu</div><div class="calendar-weekday">Fri</div><div class="calendar-weekday">Sat</div>${cells.join("")}</div></article><article class="panel"><div class="panel-header"><div class="panel-heading"><h3>Upcoming plans</h3><p>Saved to this local workspace</p></div><span class="tiny-badge">NOT PUBLISHED</span></div><div class="schedule-list" style="padding:12px">${scheduleRows || empty}</div></article></section>`;
}

function analyticsPage() {
  const counts = state.analytics.counts || {};
  const cards = [
    ["Candidates created", String(counts.candidates || 0), "Local processing", "▧", "blue"],
    ["Approved", String(counts.approved || 0), "Human-reviewed clips", "✓", "mint"],
    ["Local plans", String(counts.planned || 0), "Not actual uploads", "◷", "amber"],
    ["Published", String(counts.published || 0), "No publisher connected", "↗", ""],
  ].map((item) => statCard(...item)).join("");
  return `${pageHeading("PERFORMANCE", "Use real results to improve.", "No vanity predictions. Performance metrics appear only after a platform account is connected and authorized.")}
    <section class="analytics-cards">${cards}</section>
    <div class="panel analytics-note"><span class="analytics-note-icon">⌁</span><div><h3>Performance data is not connected yet</h3><p>${escapeHtml(state.analytics.note || "Connect the YouTube Analytics API after OAuth setup to view real views, likes, comments, average view duration, and eligible retention metrics. Until then, this page does not invent performance numbers.")}</p></div></div>
    <section class="panel" style="margin-top:14px"><div class="metrics-empty"><div class="empty-illustration">⌁</div><h3>Real analytics, when you're ready</h3><p>After the first approved test publication, add read-only YouTube Analytics authorization and compare actual results by clip, topic, hook, and duration.</p></div></section>`;
}

function settingRow(icon, title, description, control = "") {
  return `<div class="setting-row"><span class="setting-icon">${icon}</span><span class="setting-copy"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(description)}</small></span>${control}</div>`;
}

function settingsPage() {
  const paused = Boolean(state.settings.processing_paused);
  const tools = [
    ["FFmpeg", state.health.ffmpeg, "Required for 9:16 video rendering"],
    ["FFprobe", state.health.ffprobe, "Checks video duration, streams, and dimensions"],
    ["faster-whisper", state.health.transcription, "Optional local speech-to-text model"],
  ];
  return `${pageHeading("WORKSPACE SETTINGS", "A small, secure setup by design.", "This prototype keeps source media on disk and requires a person to approve each clip.")}
    <div class="settings-grid"><div class="settings-stack">
      <section class="settings-card"><h3>Processing controls</h3><p>Pause locally automated work at any time. It does not interrupt a render already running, but stops the next pipeline step.</p>
        ${settingRow("Ⅱ", paused ? "Processing is paused" : "Processing is active", "New uploads will wait until you resume. No automatic publishing is enabled.", `<label class="switch" aria-label="Pause processing"><input id="pause-switch" type="checkbox" ${paused ? "checked" : ""}><span class="switch-track"></span></label>`)}
        ${settingRow("⌂", "Private local storage", `${state.settings.storage_directory || "data/"} · source and generated files are not uploaded by this app.`)}
      </section>
      <section class="settings-card"><h3>Local tool readiness</h3><p>Install missing tools on the same computer that runs this server, then retry a video.</p>${tools.map(([name, ready, description]) => settingRow(ready ? "✓" : "!", name, description, `<span class="connection-status" style="${ready ? "background:#e8f9f3;color:#399874" : ""}">${ready ? "READY" : "MISSING"}</span>`)).join("")}
        <div class="field-hint" style="margin-top:12px">See the README for setup commands. The speech model downloads only when first selected; CPU processing can be slow.</div>
      </section>
      <section class="settings-card"><h3>Output defaults</h3><p>Current export profile for the local YouTube Shorts-first MVP.</p><div class="spec-list"><div class="spec-row"><span>Frame</span><span>9:16 · 1080 × 1920</span></div><div class="spec-row"><span>Video / audio</span><span>H.264 / AAC · MP4</span></div><div class="spec-row"><span>Captions</span><span>SRT sidecar + burn-in when supported</span></div><div class="spec-row"><span>Reframing</span><span>Centered crop · inspect manually</span></div><div class="spec-row"><span>Clip length</span><span>10–180 seconds · default 20–45</span></div></div></section>
    </div><div class="settings-stack">
      <section class="settings-card"><h3>Publishing connections</h3><p>No account OAuth or platform credentials are stored in this prototype.</p><div class="integration-row"><span class="integration-logo">▶</span><span class="integration-copy"><strong>YouTube Shorts</strong><small>Official API integration is not configured. Export approved clips and upload with YouTube Studio.</small></span><span class="connection-status">NOT CONNECTED</span></div><div class="field-hint">A real publisher needs Google Cloud OAuth credentials, user consent, secure token storage, and a first verified test upload. Never paste account passwords here.</div></section>
      <section class="settings-card"><h3>Usage & budget</h3><p>Usage measured by files stored in this workspace.</p><div class="spec-list"><div class="spec-row"><span>Source media</span><span>${fmtBytes(state.usage.source_bytes || 0)}</span></div><div class="spec-row"><span>Generated assets</span><span>${fmtBytes(state.usage.output_bytes || 0)}</span></div><div class="spec-row"><span>Video files</span><span>${state.usage.video_count || 0}</span></div><div class="spec-row"><span>Recurring software cost</span><span>₹0 in this local MVP</span></div></div><div class="cost-highlight"><strong>₹0 API spend</strong><small>FFmpeg, SQLite, and optional local transcription run on your computer. Hardware, electricity, internet, backups, and model download are outside this estimate.</small></div></section>
      <section class="settings-card"><h3>Rights and safety</h3><p>The upload form asks you to confirm you own or have permission to edit the source. This is an attestation, not an automated rights check. URL import, third-party downloading, and auto-publishing are intentionally off.</p></section>
    </div></div>`;
}

function renderDisconnected() {
  $("#app-view").innerHTML = `${pageHeading("LOCAL SERVER", "The studio is offline.", "Start the FastAPI server from the repository, then refresh this tab.")}
    <section class="panel"><div class="empty-state"><div class="empty-illustration">⌁</div><h4>Waiting for the local API</h4><p>Run <code>uvicorn app.main:app --host 127.0.0.1 --port 8000</code> in the project folder.</p><button class="secondary-button" data-action="refresh">Try again</button></div></section>`;
}

function renderPage() {
  const page = ({ overview: overviewPage, clips: clipsPage, library: libraryPage, review: reviewPage, calendar: calendarPage, analytics: analyticsPage, settings: settingsPage })[state.page] || overviewPage;
  $("#app-view").innerHTML = page();
  updateChrome();
}

function showToast(message, type = "") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  $("#toast-root").appendChild(toast);
  window.setTimeout(() => toast.remove(), 3900);
}

function navigate(page) {
  if (!["overview", "clips", "library", "review", "calendar", "analytics", "settings"].includes(page)) page = "overview";
  state.page = page;
  if (location.hash !== `#${page}`) history.replaceState(null, "", `#${page}`);
  $("#sidebar").classList.remove("open");
  renderPage();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function openUploadModal(prefillFile = null) {
  const modalRoot = $("#modal-root");
  modalRoot.innerHTML = `<div class="modal-backdrop" id="modal-backdrop"><section class="modal-card" role="dialog" aria-modal="true" aria-labelledby="upload-title"><div class="modal-header"><div><h2 id="upload-title">Add a source video</h2><p>One video at a time · local processing · manual approval required</p></div><button class="modal-close" data-action="close-modal" aria-label="Close">×</button></div><div class="modal-body upload-modal-body"><div class="upload-dropzone" id="modal-dropzone"><div class="upload-symbol">⇧</div><strong id="selected-filename">Drop an authorized video here</strong><p>MP4, MOV, M4V, MKV, WEBM or AVI · up to ${fmtBytes(state.health.max_upload_bytes || 2 * 1024 ** 3)}</p><button class="secondary-button" data-action="choose-file">Browse files</button><input id="video-file" type="file" accept="video/mp4,video/quicktime,video/x-m4v,video/x-matroska,video/webm,video/x-msvideo" hidden /></div><div class="duration-fields"><label><span class="field-label">Minimum clip length <small>seconds</small></span><input class="form-input" id="min-duration" type="number" min="10" max="180" value="20" /></label><label><span class="field-label">Maximum clip length <small>seconds · max 180</small></span><input class="form-input" id="max-duration" type="number" min="10" max="180" value="45" /></label></div><label class="rights-row"><input id="rights-attested" type="checkbox" /><span>I own this source video or have permission to process and publish clips from it.</span></label><div class="field-hint">Authorized URL and cloud-folder ingestion are not enabled. Use an upload you have the right to edit. The app does not download platform URLs.</div><div class="upload-progress" id="upload-progress"><div class="progress-head"><span id="progress-label">Uploading…</span><span id="progress-percent">0%</span></div><div class="progress-track"><span id="progress-fill"></span></div></div><div class="upload-modal-actions"><button class="secondary-button" data-action="close-modal">Cancel</button><button class="primary-button" id="start-upload" data-action="start-upload" disabled>Upload & process</button></div></div></section></div>`;
  const input = $("#video-file");
  input.addEventListener("change", () => setSelectedFile(input.files?.[0]));
  $("#rights-attested").addEventListener("change", updateUploadButton);
  const dropzone = $("#modal-dropzone");
  ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.add("drag-over"); }));
  ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.remove("drag-over"); }));
  dropzone.addEventListener("drop", (event) => setSelectedFile(event.dataTransfer?.files?.[0]));
  if (prefillFile) setSelectedFile(prefillFile);
  setTimeout(() => $("#modal-backdrop")?.addEventListener("click", (event) => { if (event.target.id === "modal-backdrop") closeModal(); }), 0);
}

let selectedFile = null;
function setSelectedFile(file) {
  if (!file) return;
  selectedFile = file;
  const label = $("#selected-filename");
  if (label) label.textContent = `${file.name} · ${fmtBytes(file.size)}`;
  updateUploadButton();
}

function updateUploadButton() {
  const button = $("#start-upload");
  if (button) button.disabled = !(selectedFile && $("#rights-attested")?.checked);
}

function closeModal() {
  $("#modal-root").innerHTML = "";
  selectedFile = null;
  state.editingClip = null;
}

function startUpload() {
  const min = Number($("#min-duration")?.value || 20);
  const max = Number($("#max-duration")?.value || 45);
  if (!selectedFile) return showToast("Choose a video file first.", "error");
  if (!$("#rights-attested")?.checked) return showToast("Confirm your rights to use this source video.", "error");
  if (min < 10 || max > 180 || max < min) return showToast("Choose valid clip lengths from 10–180 seconds.", "error");

  const form = new FormData();
  form.append("file", selectedFile);
  form.append("min_clip_seconds", String(min));
  form.append("max_clip_seconds", String(max));
  form.append("rights_attested", "true");
  const file = selectedFile;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/videos");
  const progress = $("#upload-progress");
  progress?.classList.add("visible");
  $("#start-upload").disabled = true;
  $("#progress-label").textContent = `Uploading ${file.name}`;
  xhr.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    const percent = Math.round(event.loaded / event.total * 100);
    $("#progress-percent").textContent = `${percent}%`;
    $("#progress-fill").style.width = `${percent}%`;
  });
  xhr.addEventListener("load", async () => {
    let payload = {};
    try { payload = JSON.parse(xhr.responseText); } catch (_ignored) {}
    if (xhr.status >= 200 && xhr.status < 300) {
      closeModal();
      showToast("Upload received. Processing will run locally.", "success");
      await loadData();
      navigate("library");
    } else {
      const detail = payload.detail;
      const message = typeof detail === "string" ? detail : detail?.message || `Upload failed (${xhr.status}).`;
      showToast(message, "error");
      updateUploadButton();
    }
  });
  xhr.addEventListener("error", () => { showToast("Upload failed. Check the local server and available disk space.", "error"); updateUploadButton(); });
  xhr.send(form);
}

function openClipEditor(clipId) {
  const clip = state.clips.find((item) => item.id === clipId);
  if (!clip) return showToast("This clip is no longer available.", "error");
  state.editingClip = clip;
  const editable = !["rejected", "published"].includes(clip.status);
  const tags = (clip.hashtags || []).join(" ");
  const scheduledDefault = new Date(Date.now() + 24 * 60 * 60 * 1000);
  scheduledDefault.setHours(9, 0, 0, 0);
  const scheduleValue = new Date(scheduledDefault.getTime() - scheduledDefault.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const video = clip.video_url ? `<video controls playsinline preload="metadata" src="${escapeHtml(clip.video_url)}" poster="${escapeHtml(clip.thumbnail_url || "")}"></video>` : `<span class="preview-empty">Rendered preview unavailable</span>`;
  const canSchedule = ["needs_review", "approved", "scheduled_local"].includes(clip.status);
  const statusTextHtml = clip.status === "scheduled_local" ? `<div class="field-hint">This is a local reminder only. It does not upload or schedule a YouTube post.</div>` : "";
  $("#modal-root").innerHTML = `<div class="modal-backdrop" id="modal-backdrop"><section class="modal-card" role="dialog" aria-modal="true" aria-labelledby="editor-title"><div class="modal-header"><div><h2 id="editor-title">Review this candidate</h2><p>${escapeHtml(clip.video_filename || "Source video")} · ${timeRange(clip)} · heuristic review score ${Math.round(Number(clip.score) || 0)} / 100</p></div><button class="modal-close" data-action="close-modal" aria-label="Close">×</button></div><div class="modal-body"><div class="editor-grid"><div><div class="editor-preview">${video}</div><div class="field-hint" style="margin-top:7px">Watch the full clip and verify framing, audio, captions, and surrounding context. Center crop only; speaker tracking is not enabled.</div></div><div class="editor-fields"><label><span class="field-label">Title <small id="title-count">${clip.title.length}/100</small></span><input class="form-input" id="clip-title" maxlength="100" value="${escapeHtml(clip.title)}" ${editable ? "" : "disabled"} /></label><label><span class="field-label">Description</span><textarea class="form-textarea" id="clip-caption" maxlength="5000" ${editable ? "" : "disabled"}>${escapeHtml(clip.caption || "")}</textarea><span class="field-hint">Auto-suggested from the transcript. Keep it accurate to the original.</span></label><label><span class="field-label">Hashtags <small>space separated</small></span><input class="form-input hashtag-input" id="clip-hashtags" value="${escapeHtml(tags)}" ${editable ? "" : "disabled"} /></label><div><span class="field-label">Transcript excerpt</span><div class="form-input" style="max-height:86px;overflow:auto;color:#727b8c">${escapeHtml(clip.transcript)}</div><span class="field-hint">Local speech recognition can be wrong. Correct it before export if needed.</span></div>${statusTextHtml}${canSchedule ? `<div><button class="secondary-button" data-action="toggle-schedule">${clip.status === "scheduled_local" ? "Change local reminder" : "Add local calendar reminder"}</button><div class="schedule-inline" id="schedule-inline"><label><span class="field-label">Reminder date and time <small>your device timezone</small></span><div class="schedule-controls"><input class="form-input" id="schedule-time" type="datetime-local" value="${scheduleValue}" /><button class="primary-button" data-action="save-schedule">Approve & add</button></div></label><div class="schedule-disclaimer">This saves a local planning entry only. YouTube publishing is not connected.</div></div></div>` : ""}</div></div><div class="editor-toolbar"><div class="editor-toolbar-left"><a class="secondary-button" href="${escapeHtml(clip.export_url)}" download>↓ Export bundle</a>${clip.subtitle_url ? `<a class="quiet-button" href="${escapeHtml(clip.subtitle_url)}" download>Download SRT</a>` : ""}</div><div class="editor-toolbar-right">${clip.status === "needs_review" ? `<button class="danger-button" data-action="reject-clip" data-clip-id="${escapeHtml(clip.id)}">Reject</button><button class="primary-button" data-action="approve-clip" data-clip-id="${escapeHtml(clip.id)}">Save & approve</button>` : `<button class="secondary-button" data-action="save-clip" data-clip-id="${escapeHtml(clip.id)}" ${editable ? "" : "disabled"}>Save changes</button>`}</div></div></div></section></div>`;
  const backdrop = $("#modal-backdrop");
  backdrop.addEventListener("click", (event) => { if (event.target === backdrop) closeModal(); });
  $("#clip-title")?.addEventListener("input", (event) => { $("#title-count").textContent = `${event.target.value.length}/100`; });
}

async function saveEditorFields() {
  const clip = state.editingClip;
  if (!clip) return null;
  const title = $("#clip-title")?.value.trim() || "";
  const caption = $("#clip-caption")?.value.trim() || "";
  const hashtags = ($("#clip-hashtags")?.value || "").split(/[\s,]+/).filter(Boolean);
  if (!title) throw new Error("Add a truthful title before saving.");
  const updated = await api(`/api/clips/${encodeURIComponent(clip.id)}`, {
    method: "PATCH", body: JSON.stringify({ title, caption, hashtags }),
  });
  Object.assign(clip, updated);
  return clip;
}

async function approveClip(clipId) {
  try {
    await saveEditorFields();
    await api(`/api/clips/${encodeURIComponent(clipId)}/review`, { method: "POST", body: JSON.stringify({ action: "approve" }) });
    closeModal();
    showToast("Clip approved. It remains a draft until you publish it.", "success");
    await loadData();
  } catch (error) { showToast(error.message, "error"); }
}

async function rejectClip(clipId) {
  try {
    await api(`/api/clips/${encodeURIComponent(clipId)}/review`, { method: "POST", body: JSON.stringify({ action: "reject" }) });
    closeModal();
    showToast("Candidate rejected.");
    await loadData();
  } catch (error) { showToast(error.message, "error"); }
}

async function addSchedule() {
  const clip = state.editingClip;
  const input = $("#schedule-time");
  if (!clip || !input?.value) return showToast("Choose a date and time.", "error");
  try {
    await saveEditorFields();
    const current = state.clips.find((item) => item.id === clip.id);
    if (current?.status !== "approved" && current?.status !== "scheduled_local") {
      await api(`/api/clips/${encodeURIComponent(clip.id)}/review`, { method: "POST", body: JSON.stringify({ action: "approve" }) });
    }
    const date = new Date(input.value);
    await api(`/api/clips/${encodeURIComponent(clip.id)}/schedule`, {
      method: "POST", body: JSON.stringify({ platform: "youtube", scheduled_at: date.toISOString() }),
    });
    closeModal();
    showToast("Added to your local calendar. This does not publish to YouTube.", "success");
    await loadData();
    navigate("calendar");
  } catch (error) { showToast(error.message, "error"); }
}

async function rejectFromCard(clipId) {
  if (!window.confirm("Reject this candidate clip? It will remain in the library as rejected.")) return;
  await rejectClip(clipId);
}

async function retryVideo(videoId) {
  try {
    const video = state.videos.find((item) => item.id === videoId);
    const options = { method: "POST" };
    if (video?.status === "no_highlights") {
      options.body = JSON.stringify({ min_clip_seconds: 10, max_clip_seconds: 30 });
    }
    await api(`/api/videos/${encodeURIComponent(videoId)}/retry`, options);
    showToast(video?.status === "no_highlights" ? "Retry queued with a 10–30 second range." : "Retry queued. Check that FFmpeg and local transcription are installed.", "success");
    await loadData();
  } catch (error) { showToast(error.message, "error"); }
}

async function unschedule(scheduleId) {
  if (!window.confirm("Remove this local calendar reminder? The clip will stay approved.")) return;
  try {
    await api(`/api/calendar/${encodeURIComponent(scheduleId)}`, { method: "DELETE" });
    showToast("Local reminder removed.");
    await loadData();
  } catch (error) { showToast(error.message, "error"); }
}

async function pauseProcessing(value) {
  try {
    const settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify({ processing_paused: value }) });
    state.settings = settings;
    showToast(value ? "Processing paused. Current render will finish its active step." : "Processing resumed.", "success");
    await loadData();
  } catch (error) { showToast(error.message, "error"); }
}

document.addEventListener("click", async (event) => {
  const nav = event.target.closest("[data-page]");
  if (nav && nav.classList.contains("nav-link")) {
    event.preventDefault();
    navigate(nav.dataset.page);
    return;
  }
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  try {
    if (action === "navigate") navigate(button.dataset.page);
    else if (action === "open-upload") openUploadModal();
    else if (action === "choose-file") $("#video-file")?.click();
    else if (action === "start-upload") startUpload();
    else if (action === "close-modal") closeModal();
    else if (action === "open-clip") openClipEditor(button.dataset.clipId);
    else if (action === "approve-clip") await approveClip(button.dataset.clipId);
    else if (action === "reject-clip") await rejectClip(button.dataset.clipId);
    else if (action === "quick-reject") await rejectFromCard(button.dataset.clipId);
    else if (action === "save-clip") {
      await saveEditorFields(); closeModal(); showToast("Clip details saved.", "success"); await loadData();
    } else if (action === "toggle-schedule") $("#schedule-inline")?.classList.toggle("open");
    else if (action === "save-schedule") await addSchedule();
    else if (action === "retry-video") await retryVideo(button.dataset.videoId);
    else if (action === "unschedule") await unschedule(button.dataset.scheduleId);
    else if (action === "clip-filter") { state.clipFilter = button.dataset.filter; renderPage(); }
    else if (action === "calendar-prev") { state.calendarOffset -= 1; renderPage(); }
    else if (action === "calendar-next") { state.calendarOffset += 1; renderPage(); }
    else if (action === "refresh") await loadData();
  } catch (error) {
    showToast(error.message || "Something went wrong.", "error");
  }
});

document.addEventListener("change", (event) => {
  if (event.target.id === "pause-switch") pauseProcessing(event.target.checked);
});

document.addEventListener("input", (event) => {
  if (event.target.id === "library-search") {
    const selectionStart = event.target.selectionStart;
    state.librarySearch = event.target.value;
    renderPage();
    const replacement = $("#library-search");
    replacement?.focus();
    replacement?.setSelectionRange(selectionStart, selectionStart);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModal();
});

$("#mobile-menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$("#platform-chip").addEventListener("click", () => showToast("YouTube is not connected. Export a reviewed clip and upload it through YouTube Studio."));

// Let the main overview accept a dropped source without an extra modal step.
document.addEventListener("dragover", (event) => {
  if (event.target.closest("#hero-dropzone")) { event.preventDefault(); $("#hero-dropzone")?.classList.add("drag-over"); }
});
document.addEventListener("dragleave", (event) => {
  if (event.target.closest("#hero-dropzone")) $("#hero-dropzone")?.classList.remove("drag-over");
});
document.addEventListener("drop", (event) => {
  if (!event.target.closest("#hero-dropzone")) return;
  event.preventDefault();
  $("#hero-dropzone")?.classList.remove("drag-over");
  const file = event.dataTransfer?.files?.[0];
  if (file) openUploadModal(file);
});

loadData();
window.setInterval(async () => {
  const hasWork = state.videos.some((video) => ["queued", "validating", "transcribing", "finding_highlights", "rendering"].includes(video.status));
  if (hasWork && !$("#modal-root").innerHTML) await loadData();
}, 5000);
