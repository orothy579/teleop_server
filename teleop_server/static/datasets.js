"use strict";
// Dataset browser: every recorded session (search, filter, label edit, delete) and one session's
// labels, quality, synchronized camera videos with the stored raw depth, and joint/base time series.
// Uses app.js globals ($, fmt, setStatus, css, fitCanvas, store, sessions, openLabelEditor).

const DS = { list: [], current: null, detail: null, frame: 0, playing: false, raf: 0, depthTimer: 0,
  depthAbort: null, depthUrls: {}, charts: {}, selected: new Set() };

async function dsJson(url, opts) {
  const r = await fetch(url, opts);
  const d = await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }));
  if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

function sessionDate(name) {
  const m = /^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/.exec(name);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : name;
}
function dur(s) {
  if (s == null || !isFinite(s)) return "–";
  return s < 60 ? `${fmt(s, 1)}초` : `${Math.floor(s / 60)}분 ${String(Math.round(s % 60)).padStart(2, "0")}초`;
}
const tclock = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${(s % 60).toFixed(1).padStart(4, "0")}`;
const bytes = (b) => b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.round(b / 1e3)} KB`;
const OUTCOME = { success: ["good", "✓", "성공"], failure: ["critical", "✕", "실패"] };
const outcomeOf = (x) => x.labels ? (OUTCOME[x.labels.outcome] || ["off", "–", x.labels.outcome]) : ["off", "–", "라벨 미입력"];
const recordOf = (x) => x.status === "completed" ? ["good", "✓", "녹화 완료"] : x.status === "failed" ? ["critical", "!", "녹화 실패"]
  : ["warning", "…", x.status || "상태 없음"];
const validOf = (x) => x.validation_passed === true ? ["good", "✓", "검증 통과"] : x.validation_passed === false
  ? ["critical", "!", "검증 실패"] : ["off", "–", "검증 기록 없음"];
const STOP = { user: "녹화 중지 버튼", "arm control stopped": "제어 중지 (녹화 중)" };

function statusEl(spec) {
  const el = document.createElement("span"), i = document.createElement("i"), t = document.createElement("span");
  el.className = "status"; el.append(i, t); setStatus(el, spec); return el;
}
function smallBtn(label, extra = "") {
  const b = document.createElement("button");
  b.type = "button"; b.className = `btn small ${extra}`.trim(); b.textContent = label; return b;
}
function kv(dl, rows) {  // rows: [label, value] — values are inserted as text (labels are user input)
  dl.replaceChildren();
  for (const [k, v] of rows) {
    if (v == null || v === "") continue;
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = k; dd.textContent = v; dl.append(dt, dd);
  }
}

// ---------- list ----------
function dsFiltered() {
  const q = $("#ds-search").value.trim().toLowerCase(), f = $("#ds-filter").value;
  return DS.list.filter((x) => {
    const L = x.labels || {};
    if (f === "success" && L.outcome !== "success") return false;
    if (f === "failure" && L.outcome !== "failure") return false;
    if (f === "unlabeled" && x.labels) return false;
    if (f === "failed" && x.status !== "failed") return false;
    return !q || [x.session, x.task, L.task_name, L.operator, L.environment, L.notes]
      .some((v) => (v || "").toLowerCase().includes(q));
  });
}

function renderList() {
  const rows = dsFiltered(), body = $("#ds-rows");
  body.replaceChildren();
  for (const x of rows) {
    const tr = document.createElement("tr");
    tr.tabIndex = 0; tr.dataset.session = x.session;
    const td = (cls, ...nodes) => { const c = document.createElement("td"); if (cls) c.className = cls; c.append(...nodes); tr.append(c); return c; };
    const pick = document.createElement("input");
    pick.type = "checkbox"; pick.checked = DS.selected.has(x.session);
    pick.setAttribute("aria-label", `${x.session} 선택`);
    pick.addEventListener("change", () => { pick.checked ? DS.selected.add(x.session) : DS.selected.delete(x.session); updateSelection(); });
    td("sel", pick);
    const name = document.createElement("span"), sub = document.createElement("span");
    name.className = "sname"; name.textContent = sessionDate(x.session);
    sub.className = "sdate"; sub.textContent = x.session + (x.synthetic ? " · 합성 센서" : "");
    td("", name, sub);
    td("num", dur(x.elapsed_s));
    td("num", String(x.frames ?? 0));
    td("", statusEl(recordOf(x)));
    td("", statusEl(outcomeOf(x)));
    const task = x.labels?.task_name || x.task || "";
    td("ds-task", task).title = task;
    td("", x.labels?.operator || "–");
    td("", x.labels?.environment || "–");
    const view = smallBtn("보기"), edit = smallBtn("라벨 수정"), del = smallBtn("삭제", "danger");
    view.addEventListener("click", () => openDetail(x.session));
    edit.addEventListener("click", () => editLabels(x));
    del.addEventListener("click", () => deleteSession(x));
    td("acts", view, edit, del);
    tr.addEventListener("click", (e) => { if (!e.target.closest("button, input, td.sel")) openDetail(x.session); });
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === tr) openDetail(x.session); });
    body.append(tr);
  }
  $("#ds-empty").hidden = rows.length > 0;
  updateSelection();
  const all = DS.list, n = (p) => all.filter(p).length;
  const total = all.filter((x) => x.status === "completed").reduce((a, x) => a + (x.elapsed_s || 0), 0);
  $("#ds-count").textContent = `${rows.length} / ${all.length}개 · 성공 ${n((x) => x.labels?.outcome === "success")}` +
    ` · 실패 ${n((x) => x.labels?.outcome === "failure")} · 라벨 미입력 ${n((x) => !x.labels)}` +
    ` · 녹화 실패 ${n((x) => x.status === "failed")} · 완료된 녹화 합계 ${dur(total)}`;
}

async function loadDatasets() {
  try {
    DS.list = (await dsJson("/api/datasets")).sessions;
    const names = new Set(DS.list.map((x) => x.session));
    for (const n of [...DS.selected]) if (!names.has(n)) DS.selected.delete(n);  // deleted meanwhile
    if (!DS.current) $("#ds-sub").textContent = "";
  } catch (e) { $("#ds-sub").textContent = `목록을 불러오지 못했습니다: ${e.message}`; }
  renderList();
}

function showList() {
  stopPlayback();
  DS.current = DS.detail = null;
  for (const v of document.querySelectorAll("#dd-videos video")) { v.removeAttribute("src"); v.load(); }
  $("#dd-videos").replaceChildren();
  for (const u of Object.values(DS.depthUrls)) URL.revokeObjectURL(u);
  DS.depthUrls = {};
  $("#ds-list-view").hidden = false; $("#ds-detail-view").hidden = true; $("#ds-back").hidden = true;
  $("#ds-title").textContent = "데이터셋"; $("#ds-sub").textContent = "";
}

async function openDatasets() {
  const d = $("#ds-dialog");
  showList();
  if (!d.open) d.showModal();
  await loadDatasets();
  $("#ds-search").focus();
}

// ---------- edit / delete ----------
function editLabels(x) {
  // The label pop-up edits sessions from its own (recent) list; add older ones on demand.
  if (!sessions.some((s) => s.session === x.session)) {
    sessions.push({ session: x.session, frames: x.frames, status: x.status, task: x.task, labels: x.labels });
  }
  openLabelEditor(x.session);
}

async function deleteSession(x) {
  const msg = `세션 ${x.session}을(를) 삭제할까요?\n${sessionDate(x.session)} · ${dur(x.elapsed_s)} · ${x.frames ?? 0} 프레임` +
    (x.labels ? ` · 라벨: ${outcomeOf(x)[2]}` : "") +
    "\n\n데이터는 dataset/.trash 폴더로 옮겨집니다. 그 폴더를 비우기 전까지는 되살릴 수 있습니다.";
  if (!confirm(msg)) return;
  try { await dsJson(`/api/datasets/${encodeURIComponent(x.session)}`, { method: "DELETE" }); }
  catch (e) { alert(`삭제하지 못했습니다: ${e.message}`); return; }
  if (DS.current === x.session) showList();
  await loadDatasets();
}

// Labels saved or a session deleted (any browser): refresh what is shown.
window.datasetsChanged = async (m) => {
  if (!$("#ds-dialog").open) return;
  await loadDatasets();
  if (DS.current && m?.session === DS.current) {
    if (m.deleted) showList(); else openDetail(DS.current, { keepFrame: true });
  }
};

// ---------- detail ----------
async function openDetail(name, { keepFrame = false } = {}) {
  const frame = keepFrame ? DS.frame : 0;
  if (!keepFrame) stopPlayback();
  DS.current = name;
  $("#ds-list-view").hidden = true; $("#ds-detail-view").hidden = false; $("#ds-back").hidden = false;
  $("#ds-title").textContent = sessionDate(name); $("#ds-sub").textContent = name;
  let d;
  try { d = (await dsJson(`/api/datasets/${encodeURIComponent(name)}`)).session; }
  catch (e) { $("#ds-sub").textContent = `${name} · 불러오지 못했습니다: ${e.message}`; return; }
  if (DS.current !== name) return;  // the user moved on meanwhile
  DS.detail = d;
  renderDetailText(d);
  renderLegends();
  if (!keepFrame) renderMedia(d);
  requestAnimationFrame(() => setFrame(frame, { seekVideos: !keepFrame }));
}

function renderDetailText(d) {
  setStatus($("#dd-status"), recordOf(d));
  setStatus($("#dd-outcome"), outcomeOf(d));
  setStatus($("#dd-valid"), validOf(d));
  const err = $("#dd-error");
  err.hidden = !d.error; err.textContent = d.error ? `오류: ${d.error}` : "";
  const q = d.camera_quality, g = d.capture_gaps || {}, v = d.validation || {};
  const skipped = Object.entries(g.skipped || {}).map(([k, n]) => `${k} ${n}`).join(", ");
  kv($("#dd-info"), [
    ["녹화 시작", d.start_unix_ns ? new Date(d.start_unix_ns / 1e6).toLocaleString("ko-KR") : sessionDate(d.session)],
    ["길이", `${dur(d.elapsed_s)} · ${d.frames ?? 0} 프레임 · ${d.fps} fps` +
      (v.measured_sample_hz ? ` (측정 ${fmt(v.measured_sample_hz, 1)} Hz)` : "")],
    ["데이터셋 작업", d.task],
    ["녹화 당시 작업", d.recorded_task && d.recorded_task !== d.task ? d.recorded_task : null],
    ["종료", d.stop_reason ? (STOP[d.stop_reason] || d.stop_reason) : null],
    ["누락 주기", (g.skipped_cycles || g.missing_cycles) ? `${(g.skipped_cycles || 0) + (g.missing_cycles || 0)}` +
      ` (${fmt(100 * (1 - (g.recorded_fraction ?? 1)), 1)}%)${skipped ? ` · ${skipped}` : ""}` : "없음"],
    ["카메라 최대 지연", q ? `${q.camera_max_age_ms.map((x) => fmt(x, 0)).join(" / ")} ms` : null],
    ["반복 프레임", q ? q.camera_repeated_frames.join(" / ") : null],
    ["RGB-depth 시각차 최대", q ? `${q.max_rgb_depth_dt_ms.map((x) => fmt(x, 3)).join(" / ")} ms` : null],
    ["녹화 장소", d.source === "jetson" ? "Jetson에서 녹화 → DGX로 이동" : "DGX"],
  ]);
  const L = d.labels;
  kv($("#dd-labels"), L ? [
    ["결과", OUTCOME[L.outcome]?.[2] || L.outcome], ["작업명", L.task_name], ["환경", L.environment],
    ["작업자", L.operator], ["설명", L.notes || "–"],
    ["저장", `${(L.labeled_at || "").slice(0, 19).replace("T", " ")} · 수정 ${L.revision ?? 1}회`],
  ] : [["상태", "라벨 미입력 — 위의 '라벨 수정'으로 입력하세요."]]);
  const s = d.sizes || {}, depthN = (d.cameras || []).map((c) => c.depth_frames);
  kv($("#dd-files"), [
    ["위치", `dataset/${d.session}/`], ["전체", s.total ? bytes(s.total) : null],
    ["RGB 원본 (rgb_mp4)", s.rgb_mp4 ? bytes(s.rgb_mp4) : null],
    ["depth 원본 PNG (depth_raw)", s.depth_raw ? `${bytes(s.depth_raw)} · 카메라별 ${depthN.join(" / ")}장` : null],
    ["LeRobot 데이터셋 (dataset)", s.dataset ? bytes(s.dataset) : null],
    ["기타 (로그·메타데이터)", s.other ? bytes(s.other) : null],
  ]);
}

function renderMedia(d) {
  const root = $("#dd-videos");
  root.replaceChildren();
  for (const cam of d.cameras || []) {
    const fig = document.createElement("figure"), cap = document.createElement("figcaption");
    fig.className = "ds-cam"; fig.dataset.cam = cam.name;
    const name = document.createElement("span"), info = document.createElement("span");
    name.textContent = cam.name.replace("camera", "카메라 "); info.className = "dinfo";
    cap.append(name, info);
    if (cam.video) {
      const v = document.createElement("video");
      v.className = "ds-rgb"; v.muted = true; v.playsInline = true; v.preload = "auto";
      v.src = `/api/datasets/${encodeURIComponent(d.session)}/video/${cam.name}`;
      v.setAttribute("aria-label", `${name.textContent} 영상`);
      v.addEventListener("ended", stopPlayback);
      fig.append(v);
    } else {
      const p = document.createElement("div");
      p.className = "nodata-box"; p.textContent = "영상 없음"; fig.append(p);
    }
    if (cam.depth_frames) {
      const img = document.createElement("img");
      img.className = "ds-depth"; img.alt = `${name.textContent} depth`;
      img.hidden = !$("#dd-depth-on").checked;
      fig.append(img);
    }
    fig.append(cap);
    root.append(fig);
  }
  const seek = $("#dd-seek");
  seek.max = String(Math.max(0, (d.frames || 1) - 1));
  $("#dd-play").disabled = !(d.cameras || []).some((c) => c.video);
  const hasDepth = (d.cameras || []).some((c) => c.depth_frames);
  $("#dd-depth-on").disabled = !hasDepth;
  $("#dd-media-note").textContent = !d.frames ? "저장된 프레임이 없습니다."
    : "RGB: Jetson이 녹화한 원본 H.264 영상 · depth: 카메라 원본 uint16 (밝을수록 가까움, 0~1 m, 검정 = 측정 없음)";
  $("#dd-plots").hidden = !d.series;
  if (hasDepth) prepareDepthVideos(d);
}

// Depth as 30 fps videos made on the DGX (cached): until they are ready, single stored frames are shown.
async function prepareDepthVideos(d) {
  const base = `/api/datasets/${encodeURIComponent(d.session)}/depth_preview`;
  let method = "POST";
  while (DS.detail === d) {
    let cams;
    try { cams = (await dsJson(base, { method })).cameras; } catch { return; }
    method = "GET";
    if (DS.detail && DS.detail !== d) return;
    let pending = false;
    for (const [name, st] of Object.entries(cams)) {
      const fig = document.querySelector(`#dd-videos figure[data-cam="${name}"]`);
      if (!fig) continue;
      if (st.ready) attachDepthVideo(d, fig, name);
      else if (st.error) fig.querySelector(".dinfo").textContent = "depth 영상을 만들지 못했습니다 (프레임별 이미지 사용)";
      else { pending = true; fig.querySelector(".dinfo").textContent = `depth 영상 준비 중 ${st.total ? fmt(100 * st.done / st.total, 0) : 0}%`; }
    }
    if (!pending) return;
    await new Promise((r) => setTimeout(r, 500));
  }
}

function attachDepthVideo(d, fig, name) {
  if (fig.querySelector("video.ds-depthvid")) return;
  const v = document.createElement("video"), img = fig.querySelector("img.ds-depth");
  v.className = "ds-depthvid"; v.muted = true; v.playsInline = true; v.preload = "auto";
  v.setAttribute("aria-label", `${name} depth 영상`);
  v.src = `/api/datasets/${encodeURIComponent(d.session)}/depth_video/${name}`;
  v.hidden = !$("#dd-depth-on").checked;
  v.currentTime = (DS.frame + 0.5) / d.fps;
  fig.insertBefore(v, img || fig.querySelector("figcaption"));
  if (img) img.hidden = true;  // the video replaces the single frames
  fig.querySelector(".dinfo").textContent = "depth 30 fps";
  if (DS.playing) v.addEventListener("loadeddata", () => { v.currentTime = ddVideos()[0]?.currentTime || 0; v.play().catch(() => {}); }, { once: true });
  if (!DS.playing) scheduleDepth();
}

// ---------- playback (camera 1 drives, the others follow) ----------
// RGB first (camera 1 leads), then the depth videos; all follow the same clock.
const ddVideos = () => [...document.querySelectorAll("#dd-videos video.ds-rgb"), ...document.querySelectorAll("#dd-videos video.ds-depthvid")];

function setFrame(i, { seekVideos = true } = {}) {
  const d = DS.detail;
  if (!d) return;
  const last = Math.max(0, (d.frames || 1) - 1);
  i = Math.min(Math.max(0, Math.round(i)), last);
  DS.frame = i;
  $("#dd-seek").value = String(i);
  $("#dd-time").textContent = `${tclock(i / d.fps)} / ${tclock(last / d.fps)} · 프레임 ${i + 1} / ${d.frames || 0}`;
  if (seekVideos) for (const v of ddVideos()) v.currentTime = (i + 0.5) / d.fps;  // middle of frame i
  drawCharts();
  renderValues();
  scheduleDepth();
}

function play() {
  const vids = ddVideos();
  if (!vids.length || !DS.detail) return;
  if (DS.frame >= DS.detail.frames - 1) setFrame(0);
  DS.playing = true;
  for (const v of vids) v.play().catch(() => {});
  $("#dd-play").textContent = "일시정지"; $("#dd-play").setAttribute("aria-label", "일시정지");
  const step = () => {
    if (!DS.playing) return;
    const [lead, ...rest] = ddVideos(), fps = DS.detail.fps;
    if (lead) {
      for (const v of rest) if (Math.abs(v.currentTime - lead.currentTime) > 1.5 / fps) v.currentTime = lead.currentTime;
      const i = Math.floor(lead.currentTime * fps);
      if (i !== DS.frame) setFrame(i, { seekVideos: false });
    }
    DS.raf = requestAnimationFrame(step);
  };
  DS.raf = requestAnimationFrame(step);
}

function stopPlayback() {
  DS.playing = false;
  cancelAnimationFrame(DS.raf);
  for (const v of ddVideos()) v.pause();
  $("#dd-play").textContent = "재생"; $("#dd-play").setAttribute("aria-label", "재생");
}

// Stored depth of the current frame (image until the depth video is ready, and the measured
// fraction / median distance): right away when paused, at most twice a second while playing.
function scheduleDepth() {
  const d = DS.detail;
  if (!d || !$("#dd-depth-on").checked) return;
  const withVideo = document.querySelectorAll("#dd-videos video.ds-depthvid").length;
  if (DS.playing && withVideo === (d.cameras || []).filter((c) => c.depth_frames).length) return;  // videos play it
  clearTimeout(DS.depthTimer);
  const due = DS.playing ? Math.max(0, 500 - (performance.now() - (DS.depthAt || 0))) : 60;
  DS.depthTimer = setTimeout(loadDepth, due);
}

async function loadDepth() {
  const d = DS.detail, i = DS.frame;
  if (!d) return;
  DS.depthAt = performance.now();
  DS.depthAbort?.abort();
  const ctl = DS.depthAbort = new AbortController();
  await Promise.all((d.cameras || []).filter((c) => c.depth_frames).map(async (cam) => {
    const fig = document.querySelector(`#dd-videos figure[data-cam="${cam.name}"]`);
    const img = fig?.querySelector("img.ds-depth"), info = fig?.querySelector(".dinfo");
    if (!img) return;
    const hasVideo = !!fig.querySelector("video.ds-depthvid");
    if (hasVideo && DS.playing) return;
    if (i >= cam.depth_frames) { info.textContent = "depth 없음"; return; }
    try {
      const r = await fetch(`/api/datasets/${encodeURIComponent(d.session)}/depth/${cam.name}/${i}`, { signal: ctl.signal });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      if (ctl.signal.aborted || DS.detail !== d) return;
      if (!hasVideo) {
        const old = DS.depthUrls[cam.name];
        img.src = DS.depthUrls[cam.name] = URL.createObjectURL(blob);
        if (old) URL.revokeObjectURL(old);
      }
      const valid = Number(r.headers.get("X-Depth-Valid")), med = r.headers.get("X-Depth-Median-M");
      info.textContent = `depth 측정 ${fmt(100 * valid, 0)}%` + (med ? ` · 중앙값 ${fmt(Number(med), 2)} m` : "");
    } catch (e) { if (e.name !== "AbortError") info.textContent = "depth 불러오기 실패"; }
  }));
}

// ---------- charts ----------
const SERIES = (k) => css(`--series-${k}`);
function nearestSample(S, frame) {  // index into the downsampled series
  let lo = 0, hi = S.frame.length - 1;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (S.frame[mid] < frame) lo = mid + 1; else hi = mid; }
  return lo > 0 && Math.abs(S.frame[lo - 1] - frame) < Math.abs(S.frame[lo] - frame) ? lo - 1 : lo;
}
function niceStep(span, n = 4) {
  const raw = span / n, p = 10 ** Math.floor(Math.log10(raw)), f = raw / p;
  return p * (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10);
}

function chartSpecs() {
  const S = DS.detail?.series;
  if (!S || !S.follower) return [];
  const names = S.joint_names, col = (j, k = 1) => S.follower.map((row) => row[j] * k);
  const joints = (base) => [0, 1, 2, 3, 4, 5].map((j) => ({ name: `joint ${j}`, full: names[base + j], values: col(base + j), color: j + 1 }));
  return [
    { id: "left", unit: "rad", digits: 3, series: joints(0) },
    { id: "right", unit: "rad", digits: 3, series: joints(7) },
    { id: "grip", unit: "mm", digits: 1, series: [
      { name: "왼쪽", full: names[6], values: col(6, 1000), color: 1 },
      { name: "오른쪽", full: names[13], values: col(13, 1000), color: 2 }] },
  ];
}

function drawCharts() {
  const S = DS.detail?.series;
  if (!S) return;
  const k = nearestSample(S, DS.frame);
  for (const spec of chartSpecs()) drawLineChart(spec, S, k);
  drawBase(S, k);
}

function drawLineChart(spec, S, cur) {
  const canvas = $(`#dd-c-${spec.id}`), { ctx, w, h } = fitCanvas(canvas);
  const pad = { l: 46, r: 10, t: 8, b: 20 }, t = S.t, tmax = Math.max(t[t.length - 1], 1e-3);
  let lo = Infinity, hi = -Infinity;
  for (const s of spec.series) for (const v of s.values) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  if (hi - lo < 1e-6) { lo -= 0.5; hi += 0.5; }
  const step = niceStep(hi - lo), y0 = Math.floor(lo / step) * step, y1 = Math.ceil(hi / step) * step;
  const X = (tt) => pad.l + (w - pad.l - pad.r) * tt / tmax, Y = (v) => h - pad.b - (h - pad.t - pad.b) * (v - y0) / (y1 - y0);
  DS.charts[spec.id] = { spec, X, Y, pad, w, h, tmax };
  ctx.clearRect(0, 0, w, h);
  ctx.font = "11px system-ui, -apple-system, 'Segoe UI', sans-serif";
  ctx.lineWidth = 1; ctx.strokeStyle = css("--grid"); ctx.fillStyle = css("--muted");
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let v = y0; v <= y1 + step / 2; v += step) {
    const y = Math.round(Y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(fmt(v, step < 0.1 ? 2 : step < 1 ? 1 : 0), pad.l - 6, y);
  }
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  const ts = niceStep(tmax, 5);
  for (let tt = 0; tt <= tmax + 1e-9; tt += ts) ctx.fillText(`${fmt(tt, ts < 1 ? 1 : 0)}s`, X(tt), h - pad.b + 5);
  ctx.strokeStyle = css("--axis");
  ctx.beginPath(); ctx.moveTo(pad.l, h - pad.b + 0.5); ctx.lineTo(w - pad.r, h - pad.b + 0.5); ctx.stroke();
  ctx.lineWidth = 2; ctx.lineJoin = ctx.lineCap = "round";
  for (const s of spec.series) {
    ctx.strokeStyle = SERIES(s.color);
    ctx.beginPath();
    s.values.forEach((v, i) => (i ? ctx.lineTo(X(t[i]), Y(v)) : ctx.moveTo(X(t[i]), Y(v))));
    ctx.stroke();
  }
  // Current frame.
  const xc = Math.round(X(t[cur])) + 0.5;
  ctx.lineWidth = 1.5; ctx.strokeStyle = css("--ink-2");
  ctx.beginPath(); ctx.moveTo(xc, pad.t); ctx.lineTo(xc, h - pad.b); ctx.stroke();
  // Hover crosshair with a dot (2px surface ring) on every series.
  const hv = DS.charts[spec.id].hover;
  if (hv != null) {
    const xh = Math.round(X(t[hv])) + 0.5;
    ctx.lineWidth = 1; ctx.strokeStyle = css("--muted");
    ctx.beginPath(); ctx.moveTo(xh, pad.t); ctx.lineTo(xh, h - pad.b); ctx.stroke();
    for (const s of spec.series) {
      ctx.fillStyle = SERIES(s.color); ctx.strokeStyle = css("--surface"); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(X(t[hv]), Y(s.values[hv]), 4, 0, 7); ctx.fill(); ctx.stroke();
    }
  }
}

function baseGeometry(S, w, h) {
  const B = S.base, pad = 14;
  let x0 = Math.min(...B.y), x1 = Math.max(...B.y), y0 = Math.min(...B.x), y1 = Math.max(...B.x);
  const span = Math.max(0.5, x1 - x0, y1 - y0) * 1.15, cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  const s = Math.min(w - 2 * pad, h - 2 * pad) / span;
  // ROS x forward drawn up, y left drawn left (as in the live view).
  return { X: (i) => w / 2 - (B.y[i] - cx) * s, Y: (i) => h / 2 - (B.x[i] - cy) * s, s, span, cx, cy };
}

function drawBase(S, cur) {
  const canvas = $("#dd-c-base"), { ctx, w, h } = fitCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const B = S.base;
  if (!B) { $("#dd-l-base").textContent = "베이스 데이터 없음"; return; }
  const g = baseGeometry(S, w, h);
  DS.charts.base = { g };
  const step = niceStep(g.span, 4);
  ctx.lineWidth = 1; ctx.strokeStyle = css("--grid");
  ctx.beginPath();
  for (let k = -10; k <= 10; k++) {  // grid lines through world multiples of `step`
    const px = w / 2 - (Math.round(g.cx / step + k) * step - g.cx) * g.s, py = h / 2 - (Math.round(g.cy / step + k) * step - g.cy) * g.s;
    ctx.moveTo(Math.round(px) + 0.5, 0); ctx.lineTo(Math.round(px) + 0.5, h);
    ctx.moveTo(0, Math.round(py) + 0.5); ctx.lineTo(w, Math.round(py) + 0.5);
  }
  ctx.stroke();
  ctx.lineWidth = 2; ctx.lineJoin = ctx.lineCap = "round"; ctx.strokeStyle = SERIES(1);
  ctx.beginPath();
  B.x.forEach((_, i) => (i ? ctx.lineTo(g.X(i), g.Y(i)) : ctx.moveTo(g.X(i), g.Y(i))));
  ctx.stroke();
  // Start: hollow ring. Current: filled dot with a surface ring and a heading tick.
  ctx.lineWidth = 2; ctx.strokeStyle = SERIES(1); ctx.fillStyle = css("--surface");
  ctx.beginPath(); ctx.arc(g.X(0), g.Y(0), 5, 0, 7); ctx.fill(); ctx.stroke();
  const x = g.X(cur), y = g.Y(cur), yaw = B.yaw[cur];
  ctx.strokeStyle = SERIES(1); ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - Math.sin(yaw) * 14, y - Math.cos(yaw) * 14); ctx.stroke();
  ctx.fillStyle = SERIES(1); ctx.strokeStyle = css("--surface");
  ctx.beginPath(); ctx.arc(x, y, 5, 0, 7); ctx.fill(); ctx.stroke();
  const moved = Math.hypot(B.x[B.x.length - 1] - B.x[0], B.y[B.y.length - 1] - B.y[0]);
  $("#dd-l-base").textContent = `○ 시작 · ● 현재 위치(선 = 향하는 방향) · 격자 ${fmt(step, step < 1 ? 1 : 0)} m · 시작→끝 ${fmt(moved, 2)} m`;
}

function renderLegends() {
  for (const spec of chartSpecs()) {
    const el = $(`#dd-l-${spec.id}`);
    el.replaceChildren();
    for (const s of spec.series) {
      const item = document.createElement("span"), key = document.createElement("span");
      key.className = "key"; key.style.background = SERIES(s.color);
      item.append(key, document.createTextNode(s.name)); item.title = s.full || s.name;
      el.append(item);
    }
  }
}

function renderValues() {
  const S = DS.detail?.series, box = $("#dd-values");
  if (!S || !S.follower) { box.textContent = "–"; return; }
  const k = nearestSample(S, DS.frame), F = S.follower[k], Ld = S.leader?.[k];
  const tbl = document.createElement("table"), head = tbl.createTHead().insertRow();
  for (const c of [`프레임 ${S.frame[k] + 1} (가장 가까운 표본)`, "왼팔 팔로워", "왼팔 리더", "오른팔 팔로워", "오른팔 리더"]) {
    const th = document.createElement("th"); th.textContent = c; head.append(th);
  }
  const body = tbl.createTBody();
  for (let j = 0; j < 7; j++) {
    const grip = j === 6, kx = grip ? 1000 : 1, d = grip ? 1 : 3, r = body.insertRow();
    r.insertCell().textContent = grip ? "gripper (mm)" : `joint ${j} (rad)`;
    for (const v of [F[j], Ld?.[j], F[7 + j], Ld?.[7 + j]]) r.insertCell().textContent = v == null ? "–" : fmt(v * kx, d);
  }
  if (S.base) {
    const B = S.base, r = body.insertRow();
    r.insertCell().textContent = "베이스 x, y (m) · yaw (°) · vx (m/s) · wz (rad/s)";
    const c = r.insertCell(); c.colSpan = 4;
    c.textContent = `${fmt(B.x[k], 3)}, ${fmt(B.y[k], 3)} · ${fmt(B.yaw[k] * 180 / Math.PI, 1)}° · ${fmt(B.vx[k], 3)} · ${fmt(B.wz[k], 3)}`;
  }
  box.replaceChildren(tbl);
}

// Hover (crosshair + one tooltip with every series) and click-to-seek on the charts.
function tip(e, rows, title) {
  const el = $("#ds-tip");
  el.replaceChildren();
  const t = document.createElement("div"); t.textContent = title; el.append(t);
  for (const [color, value, name] of rows) {
    const row = document.createElement("div"), key = document.createElement("span"), b = document.createElement("b");
    row.className = "ds-tip-row"; key.className = "key"; key.style.background = color;
    b.textContent = value; row.append(key, b, document.createTextNode(name)); el.append(row);
  }
  el.hidden = false;
  el.style.left = `${Math.min(e.clientX + 14, innerWidth - el.offsetWidth - 8)}px`;
  el.style.top = `${Math.max(8, e.clientY - el.offsetHeight - 10)}px`;
}

function bindCharts() {
  for (const id of ["left", "right", "grip"]) {
    const canvas = $(`#dd-c-${id}`);
    const at = (e) => {
      const c = DS.charts[id], S = DS.detail?.series;
      if (!c || !S) return null;
      const r = canvas.getBoundingClientRect(), tt = (e.clientX - r.left - c.pad.l) / (c.w - c.pad.l - c.pad.r) * c.tmax;
      return nearestSample(S, Math.round(Math.min(Math.max(tt, 0), c.tmax) * DS.detail.fps));
    };
    canvas.addEventListener("pointermove", (e) => {
      const k = at(e); if (k == null) return;
      const c = DS.charts[id], S = DS.detail.series;
      c.hover = k; drawCharts();
      tip(e, c.spec.series.map((s) => [SERIES(s.color), fmt(s.values[k], c.spec.digits), s.name]),
        `${fmt(S.t[k], 2)}s · 프레임 ${S.frame[k] + 1} · ${c.spec.unit}`);
    });
    canvas.addEventListener("pointerleave", () => { if (DS.charts[id]) DS.charts[id].hover = null; $("#ds-tip").hidden = true; drawCharts(); });
    canvas.addEventListener("click", (e) => { const k = at(e); if (k != null) { stopPlayback(); setFrame(DS.detail.series.frame[k]); } });
  }
  const base = $("#dd-c-base");
  const nearest = (e) => {
    const S = DS.detail?.series, g = DS.charts.base?.g;
    if (!S?.base || !g) return null;
    const r = base.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
    let best = null, bd = 24 * 24;  // 24 px hit radius
    S.base.x.forEach((_, i) => { const d = (g.X(i) - px) ** 2 + (g.Y(i) - py) ** 2; if (d < bd) { bd = d; best = i; } });
    return best;
  };
  base.addEventListener("pointermove", (e) => {
    const k = nearest(e), S = DS.detail?.series;
    if (k == null) { $("#ds-tip").hidden = true; return; }
    const B = S.base;
    tip(e, [[SERIES(1), `${fmt(B.x[k], 3)}, ${fmt(B.y[k], 3)} m`, "x, y"], [SERIES(1), `${fmt(B.yaw[k] * 180 / Math.PI, 1)}°`, "yaw"]],
      `${fmt(S.t[k], 2)}s · 프레임 ${S.frame[k] + 1}`);
  });
  base.addEventListener("pointerleave", () => { $("#ds-tip").hidden = true; });
  base.addEventListener("click", (e) => { const k = nearest(e); if (k != null) { stopPlayback(); setFrame(DS.detail.series.frame[k]); } });
}

// ---------- wiring ----------
function bindDatasets() {
  $("#btn-datasets").addEventListener("click", openDatasets);
  $("#ds-close").addEventListener("click", () => $("#ds-dialog").close());
  $("#ds-back").addEventListener("click", () => { showList(); renderList(); });
  $("#ds-dialog").addEventListener("close", () => { showList(); $("#ds-tip").hidden = true; });
  $("#ds-search").addEventListener("input", renderList);
  $("#ds-filter").addEventListener("change", renderList);
  $("#dd-play").addEventListener("click", () => (DS.playing ? stopPlayback() : play()));
  $("#dd-seek").addEventListener("input", (e) => { stopPlayback(); setFrame(Number(e.target.value)); });
  $("#dd-depth-on").addEventListener("change", (e) => {
    for (const fig of document.querySelectorAll("#dd-videos figure")) {
      const v = fig.querySelector("video.ds-depthvid"), img = fig.querySelector("img.ds-depth");
      if (v) v.hidden = !e.target.checked;
      if (img) img.hidden = !e.target.checked || !!v;
    }
    if (e.target.checked) scheduleDepth();
  });
  $("#dd-edit").addEventListener("click", () => { const x = DS.list.find((s) => s.session === DS.current) || DS.detail; if (x) editLabels(x); });
  $("#dd-delete").addEventListener("click", () => { const x = DS.list.find((s) => s.session === DS.current) || DS.detail; if (x) deleteSession(x); });
  bindCharts();
  new ResizeObserver(() => { if (DS.detail) drawCharts(); }).observe($("#dd-plots"));
  const legends = () => { if (DS.detail) { renderLegends(); drawCharts(); } };
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", legends);
}

// ---------- send to another computer (SSH) ----------
const TX = { targets: [], status: { state: "idle" }, token: null, path: null };
const txBusy = () => ["connecting", "sending"].includes(TX.status?.state);
const TX_STATE = { queued: ["off", "–", "대기"], sending: ["warning", "…", "보내는 중"], verifying: ["warning", "…", "검증 중"],
  done: ["good", "✓", "완료"], skipped: ["off", "–", "건너뜀 (이미 있음)"], failed: ["critical", "!", "실패"],
  cancelled: ["off", "–", "취소됨"] };

function selectedSessions() { return DS.list.filter((x) => DS.selected.has(x.session)).map((x) => x.session); }

function updateSelection() {
  const rows = dsFiltered(), all = $("#ds-all"), n = DS.selected.size;
  const on = rows.filter((x) => DS.selected.has(x.session)).length;
  all.checked = rows.length > 0 && on === rows.length;
  all.indeterminate = on > 0 && on < rows.length;
  $("#ds-selected").textContent = `선택 ${n}개`;
  $("#ds-send").disabled = $("#ds-send-quick").disabled = !n || txBusy();
}

async function loadTransfer() {
  try {
    const d = await dsJson("/api/transfer");
    TX.targets = d.targets || [];
    renderQuick();
    onTransfer(d.transfer);
  } catch (e) { $("#tx-inline").textContent = `전송 상태를 불러오지 못했습니다: ${e.message}`; }
}

function renderQuick() {
  const b = $("#ds-send-quick"), t = TX.targets[0];
  b.hidden = !t;
  if (t) { b.textContent = `${t.name}(으)로 바로 보내기`; b.title = `${t.user}@${t.host}:${t.port} → ${t.remote_dir}`; }
  updateSelection();
}

function fillPresets(selectName) {
  const sel = $("#tx-preset");
  sel.replaceChildren();
  for (const [i, t] of TX.targets.entries()) {
    const o = document.createElement("option");
    o.value = String(i); o.textContent = `${t.name} (${t.user}@${t.host}:${t.port})`; sel.append(o);
  }
  const blank = document.createElement("option");
  blank.value = "new"; blank.textContent = "새 대상…"; sel.append(blank);
  const i = TX.targets.findIndex((t) => t.name === selectName);
  sel.value = i >= 0 ? String(i) : TX.targets.length ? "0" : "new";
  fillTarget(sel.value === "new" ? null : TX.targets[Number(sel.value)]);
}

function fillTarget(t) {
  $("#tx-name").value = t?.name || ""; $("#tx-host").value = t?.host || ""; $("#tx-port").value = String(t?.port || 22);
  $("#tx-user").value = t?.user || ""; $("#tx-dir").value = t?.remote_dir || "teleop_datasets";
  $("#tx-checkkey").checked = t ? !!t.check_host_key : true;
}

function formTarget() {
  return { name: $("#tx-name").value.trim(), host: $("#tx-host").value.trim(), port: Number($("#tx-port").value),
           user: $("#tx-user").value.trim(), remote_dir: $("#tx-dir").value.trim(), check_host_key: $("#tx-checkkey").checked };
}

function txMsg(text, err = false) { const m = $("#tx-msg"); m.textContent = text || ""; m.classList.toggle("err", err); }

function openSend(quick = false) {
  const names = selectedSessions();
  if (!names.length && !txBusy()) return;
  fillPresets(quick ? TX.targets[0]?.name : undefined);
  $("#tx-password").value = "";
  $("#tx-check").textContent = ""; $("#tx-check").classList.remove("err");
  txMsg("");
  $("#tx-summary").textContent = txBusy() ? "" : `선택한 세션 ${names.length}개`;
  renderTransfer();
  const d = $("#tx-dialog");
  if (!d.open) d.showModal();
  (quick || TX.targets.length ? $("#tx-password") : $("#tx-host")).focus();
}

async function txPost(body) {
  return dsJson("/api/transfer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

async function testTarget() {
  const box = $("#tx-check");
  box.classList.remove("err"); box.textContent = "연결하는 중…";
  $("#tx-test").disabled = true;
  try {
    const c = (await txPost({ action: "test", target: formTarget(), password: $("#tx-password").value,
                              sessions: selectedSessions(), token: TX.token })).check;
    setConnected(c.token);
    box.textContent = `연결됨: ${c.who} · 받을 폴더 ${c.path} · 여유 ${bytes(c.free_bytes)}` +
      (c.total_bytes ? ` · 보낼 크기 ${bytes(c.send_bytes)}` : "") +
      (c.existing.length ? ` · 이미 있는 세션 ${c.existing.length}개는 건너뜀` : "") +
      (c.send_bytes > c.free_bytes ? " · ⚠ 여유 공간이 부족합니다" : "");
    box.classList.toggle("err", c.send_bytes > c.free_bytes);
  } catch (e) { box.textContent = e.message; box.classList.add("err"); if (/비밀번호|로그인/.test(e.message)) setConnected(null); }
  $("#tx-test").disabled = false;
}

async function startSend(e) {
  e.preventDefault();
  if (txBusy()) return;
  const names = selectedSessions(), t = formTarget();
  if (!names.length) { txMsg("보낼 세션을 목록에서 고르세요.", true); return; }
  if (!$("#tx-password").value && !TX.token && !confirm("비밀번호 없이 연결합니다 (SSH 키가 등록된 경우). 계속할까요?")) return;
  $("#tx-start").disabled = true;
  txMsg("연결하는 중…");
  try {
    const d = await txPost({ action: "start", target: t, password: $("#tx-password").value, sessions: names,
                             rate_mbps: Number($("#tx-rate").value), token: TX.token });
    $("#tx-password").value = "";  // used once by the DGX to log in; not kept here either
    TX.token = null; $("#tx-browser").hidden = true;  // the transfer took over the connection
    setConnected(null);
    txMsg("");
    onTransfer(d.transfer);
  } catch (err) { txMsg(err.message, true); }
  $("#tx-start").disabled = false;
}

function eta(s) {
  if (s == null || !isFinite(s)) return "–";
  return s < 90 ? `${Math.round(s)}초` : `${Math.floor(s / 60)}분 ${Math.round(s % 60)}초`;
}

function onTransfer(st) {
  if (!st) return;
  TX.status = st;
  const busy = txBusy(), sess = st.sessions || [];
  const pct = st.total_bytes ? 100 * Math.min(1, st.sent_bytes / st.total_bytes) : 0;
  const n = (k) => sess.filter((x) => x.state === k).length;
  const badge = $("#tx-badge");
  const recent = busy || !st.finished_unix || Date.now() / 1000 - st.finished_unix < 600;  // finished: shown 10 min
  const counts = [n("done") && `보냄 ${n("done")}`, n("skipped") && `이미 있음 ${n("skipped")}`,
                  n("failed") && `실패 ${n("failed")}`].filter(Boolean).join(" · ");
  const text = !recent ? "" : busy ? `전송 ${st.state === "connecting" ? "연결 중" : `${fmt(pct, 0)}%`} → ${st.target?.name}`
    : st.state === "done" ? `전송 완료 → ${st.target?.name} (${counts})` : st.state === "failed" ? `전송 실패 → ${st.target?.name} (${counts || st.error})`
    : st.state === "cancelled" ? "전송 취소됨" : "";
  badge.hidden = !text; badge.textContent = text;
  $("#tx-inline").textContent = text;
  updateSelection();
  if ($("#tx-dialog").open) renderTransfer();
}

function renderTransfer() {
  const st = TX.status || { state: "idle" }, busy = txBusy(), has = st.state !== "idle";
  for (const el of $("#tx-setup").querySelectorAll("input, select, textarea, button")) el.disabled = busy;
  $("#tx-test").hidden = $("#tx-start").hidden = busy;
  $("#tx-cancel").hidden = !busy;
  $("#tx-progress").hidden = !has;
  if (!has) return;
  const sess = st.sessions || [], pct = st.total_bytes ? Math.min(1, st.sent_bytes / st.total_bytes) : 0;
  const finished = sess.filter((x) => ["done", "skipped", "failed", "cancelled"].includes(x.state)).length;
  $("#tx-where").textContent = `${st.target.user}@${st.target.host}:${st.target.port}` +
    (st.remote ? ` (${st.remote.who}) → ${st.remote.path}` : ` → ${st.target.remote_dir}`) +
    (st.rate_limit_mbps ? ` · 속도 제한 ${st.rate_limit_mbps} Mbit/s` : "");
  $("#tx-bar").value = pct;
  const head = { connecting: "연결하는 중…", sending: "보내는 중", done: "끝났습니다", failed: "실패했습니다", cancelled: "취소했습니다" }[st.state] || st.state;
  $("#tx-numbers").textContent = `${head} · 세션 ${finished}/${sess.length} · ${bytes(st.sent_bytes || 0)} / ${bytes(st.total_bytes || 0)}` +
    (busy && st.rate_bps ? ` · ${fmt(st.rate_bps * 8 / 1e6, 0)} Mbit/s · 남은 시간 ${eta(st.eta_s)}` : "") +
    (st.error ? ` · ${st.error}` : "");
  const list = $("#tx-list");
  list.replaceChildren();
  for (const x of sess) {
    const li = document.createElement("li"), spec = TX_STATE[x.state] || ["off", "–", x.state];
    li.textContent = `${sessionDate(x.session)} (${x.session}) · ${bytes(x.bytes)}` +
      (x.state === "sending" && x.bytes ? ` · ${fmt(100 * x.sent / x.bytes, 0)}%` : "");
    li.append(statusEl(x.error && x.state === "failed" ? ["critical", "!", `실패: ${x.error}`] : spec));
    list.append(li);
  }
}

// ---------- browsing the receiving computer's folders (one login, reused for sending) ----------
function setConnected(token) {
  TX.token = token || null;
  const pw = $("#tx-password");
  if (TX.token) { pw.value = ""; pw.placeholder = "연결되어 있음 — 다시 입력할 필요 없음"; }
  else pw.placeholder = "이번 전송에만 쓰고 저장하지 않습니다";
}

async function dropConnection() {
  const token = TX.token;
  setConnected(null);
  $("#tx-browser").hidden = true;
  if (token) { try { await txPost({ action: "close_browse", token }); } catch { /* already closed */ } }
}

const parentOf = (p) => (p === "/" ? "/" : p.replace(/\/[^/]*\/?$/, "") || "/");
const joinPath = (p, name) => `${p.replace(/\/$/, "")}/${name}`;

function renderListing(L) {
  TX.path = L.path;
  $("#txb-path").value = L.path;
  $("#txb-info").textContent = `${L.writable ? "쓰기 가능" : "⚠ 쓰기 권한 없음"} · 여유 ${bytes(L.free_bytes)}` +
    (L.sessions ? ` · 녹화 세션 폴더 ${L.sessions}개 있음 (데이터셋 폴더로 보임)` : "");
  $("#txb-pick").disabled = !L.writable;
  const list = $("#txb-list");
  list.replaceChildren();
  for (const name of L.dirs) {
    const li = document.createElement("li"), b = document.createElement("button");
    b.type = "button"; b.textContent = name; b.title = joinPath(L.path, name);
    b.addEventListener("click", () => browseTo(joinPath(L.path, name)));
    li.append(b); list.append(li);
  }
  if (!L.dirs.length) {
    const li = document.createElement("li"); li.className = "empty";
    li.textContent = L.sessions ? "하위 폴더는 녹화 세션뿐입니다." : "하위 폴더가 없습니다."; list.append(li);
  }
}

async function browseTo(path, action = "browse", name) {
  const pw = $("#tx-password").value;
  if (!TX.token && !pw && !confirm("비밀번호 없이 연결합니다 (SSH 키가 등록된 경우). 계속할까요?")) return;
  $("#tx-browser").hidden = false;
  $("#txb-info").textContent = TX.token ? "불러오는 중…" : "연결하는 중…";
  try {
    const d = await txPost({ action, target: formTarget(), password: pw, token: TX.token, path, name });
    setConnected(d.listing.token);
    renderListing(d.listing);
    if (action === "mkdir") { $("#txb-new").value = ""; txMsg(`'${d.listing.path}' 폴더를 만들었습니다.`); }
  } catch (e) {
    $("#txb-info").textContent = e.message;
    if (e.message.includes("비밀번호") || e.message.includes("로그인")) { setConnected(null); $("#tx-password").focus(); }
  }
}

function pickFolder() {
  if (!TX.path) return;
  $("#tx-dir").value = TX.path;
  $("#tx-browser").hidden = true;
  $("#tx-check").textContent = "";
  txMsg(`받을 폴더: ${TX.path} — 다음에도 쓰려면 '대상 저장'을 누르세요.`);
}

window.transferChanged = onTransfer;

function bindTransfer() {
  $("#ds-all").addEventListener("change", (e) => {
    for (const x of dsFiltered()) e.target.checked ? DS.selected.add(x.session) : DS.selected.delete(x.session);
    renderList();
  });
  $("#ds-send").addEventListener("click", () => openSend(false));
  $("#ds-send-quick").addEventListener("click", () => openSend(true));
  $("#tx-badge").addEventListener("click", () => { if (TX.status.state !== "idle") openSend(); });
  $("#tx-preset").addEventListener("change", (e) => fillTarget(e.target.value === "new" ? null : TX.targets[Number(e.target.value)]));
  $("#tx-parse").addEventListener("click", async () => {
    try {
      const t = (await txPost({ action: "parse_config", text: $("#tx-config").value })).target;
      fillTarget({ ...formTarget(), ...t, remote_dir: $("#tx-dir").value || "teleop_datasets" });
      $("#tx-preset").value = "new";
      txMsg("설정을 채웠습니다. '대상 저장'을 누르면 다음에도 쓸 수 있습니다.");
    } catch (e) { txMsg(e.message, true); }
  });
  $("#tx-save").addEventListener("click", async () => {
    try {
      const d = await txPost({ action: "save_target", target: formTarget() });
      TX.targets = d.targets; fillPresets(d.target.name); renderQuick();
      txMsg(`'${d.target.name}' 대상을 저장했습니다 (비밀번호 제외).`);
    } catch (e) { txMsg(e.message, true); }
  });
  $("#tx-delete").addEventListener("click", async () => {
    const name = $("#tx-name").value.trim();
    if (!name || !confirm(`저장된 대상 '${name}'을(를) 지울까요? (받는 컴퓨터의 데이터는 그대로입니다)`)) return;
    try { const d = await txPost({ action: "delete_target", name }); TX.targets = d.targets; fillPresets(); renderQuick(); txMsg("대상을 지웠습니다."); }
    catch (e) { txMsg(e.message, true); }
  });
  $("#tx-test").addEventListener("click", testTarget);
  $("#tx-browse").addEventListener("click", () => browseTo($("#tx-dir").value.trim() || "~"));
  $("#txb-up").addEventListener("click", () => TX.path && browseTo(parentOf(TX.path)));
  $("#txb-home").addEventListener("click", () => browseTo("~"));
  $("#txb-go").addEventListener("click", () => browseTo($("#txb-path").value.trim() || "~"));
  $("#txb-path").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); browseTo(e.target.value.trim() || "~"); } });
  $("#txb-mkdir").addEventListener("click", () => {
    const name = $("#txb-new").value.trim();
    if (name && TX.path) browseTo(TX.path, "mkdir", name);
  });
  $("#txb-new").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#txb-mkdir").click(); } });
  $("#txb-pick").addEventListener("click", pickFolder);
  $("#txb-close").addEventListener("click", () => { $("#tx-browser").hidden = true; });
  // Another computer or account: the open connection no longer applies.
  for (const id of ["#tx-host", "#tx-port", "#tx-user", "#tx-checkkey"]) $(id).addEventListener("change", dropConnection);
  $("#tx-preset").addEventListener("change", dropConnection);
  $("#tx-form").addEventListener("submit", startSend);
  $("#tx-cancel").addEventListener("click", async () => {
    if (!confirm("전송을 취소할까요? 이미 끝난 세션은 받는 쪽에 그대로 남습니다.")) return;
    try { await txPost({ action: "cancel" }); } catch (e) { txMsg(e.message, true); }
  });
  $("#tx-close").addEventListener("click", () => $("#tx-dialog").close());
  $("#tx-dialog").addEventListener("close", () => { $("#tx-password").value = ""; dropConnection(); });
}

bindDatasets();
bindTransfer();
loadTransfer();
window.__teleop.datasets = DS;
window.__teleop.transfer = TX;
