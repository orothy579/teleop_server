"use strict";
// Web "AI 제어": models found under the inference folder (each with inference.yaml), loaded once into
// a worker that stays in memory, then started/stopped instantly. Uses app.js globals ($, fmt,
// setStatus, post, sources, store). Names here start with "ai" to stay apart from the other scripts.

const AIS = { snap: null, selected: null, available: false, busy: false, logs: {}, runs: {}, lastSeen: null,
  dismissed: new Set(), alerted: new Set(), audio: null, title: document.title,
  pick: { model: null, runs: new Set(), errors: new Set() } };  // logs ticked for "선택한 로그 받기"
try { for (const k of JSON.parse(sessionStorage.getItem("teleop.aiDismissed") || "[]")) AIS.dismissed.add(k); } catch { /* none */ }
const AI_STATE = {
  off: ["off", "–", "꺼짐"], starting: ["warning", "…", "시작 중"], loading: ["warning", "…", "모델 로딩 중"],
  ready: ["good", "✓", "준비됨"], running: ["ai", "●", "AI 제어 중"], stopping: ["warning", "…", "정지 중"],
  error: ["critical", "!", "오류"], failed: ["critical", "!", "워커 종료됨"],
};
const aiClock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

function aiStatusEl(spec) {
  const el = document.createElement("span"), i = document.createElement("i"), t = document.createElement("span");
  el.className = "status"; el.append(i, t); setStatus(el, spec); return el;
}

function aiModel(id) { return AIS.snap?.models?.find((m) => m.id === id); }
// "pi05_pink_bag_20261012 · 030000 · 10-13 04:07": training run, step, when the checkpoint was saved.
function aiCheckpointLabel(m) {
  return (m.checkpoint_info || []).find((c) => c.path === m.checkpoint)?.label || m.name;
}

async function aiPost(body) {
  const r = await fetch("/api/ai", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }));
  if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

function aiMsg(text, err = false) { const m = $("#ai-msg"); m.textContent = text || ""; m.classList.toggle("err", err); }

// ---------- status in the control card and the banner ----------
function aiChanged(snap) {
  if (!snap || !snap.models) return;
  AIS.snap = snap;
  renderAIGroup();
  renderAIBanner();
  if ($("#ai-dialog").open) renderAIDialog();
}
window.aiChanged = aiChanged;

function renderAIGroup() {
  const snap = AIS.snap, models = snap?.models || [];
  const active = snap?.active, loading = models.find((m) => ["starting", "loading"].includes(m.state));
  const ready = models.filter((m) => m.state === "ready"), alert = aiAlerts()[0];
  setStatus($("#ai-state"), active ? (active.waiting ? ["warning", "!", "입력 지연 · 자세 유지하며 기다리는 중"]
      : ["ai", "●", active.execute ? "AI가 팔을 제어 중" : "AI 예측만 (팔 그대로)"])
    : alert ? ["critical", "!", alert.title]
    : models.some((m) => m.state === "stopping") ? AI_STATE.stopping
    : loading ? ["warning", "…", `모델 로딩 중 · ${loading.name}`]
    : ready.length ? ["good", "✓", `준비됨 · ${ready.map((m) => aiCheckpointLabel(m)).join(", ")}`]
    : models.length ? ["off", "–", "모델 꺼짐"] : ["off", "–", "모델 없음"]);
  $("#btn-ai-stop").hidden = !active;
}

const AI_END = { done: "AI 제어 끝", stopped: "AI 정지함", error: "AI 제어가 오류로 끝났습니다" };
const aiLogUrl = (model, path) => `/api/ai/runs/${encodeURIComponent(model)}/${encodeURIComponent(String(path).split("/").pop())}`;
const aiErrorUrl = (model, file) => `/api/ai/errors/${encodeURIComponent(model)}/${encodeURIComponent(file)}`;
const AI_ERROR_TITLE = { run: "⚠ AI 제어가 오류로 끝났습니다", load: "⚠ AI 모델 오류", crash: "⚠ AI 워커가 비정상 종료됨" };
const AI_ERROR_KIND = { run: "실행 오류", load: "로딩 실패", crash: "워커 종료" };

function aiLastRun() {  // the most recently finished run of any model
  return (AIS.snap?.models || []).flatMap((m) => m.worker?.last_run ? [{ ...m.worker.last_run, model: m.id }] : [])
    .sort((a, b) => (b.finished_unix || 0) - (a.finished_unix || 0))[0];
}

// Errors stay on screen (red, ⚠) until "확인", with a beep and "⚠" in the tab title, so they are
// noticed while the operator watches the robot. The server names each error with a key (one alert per
// browser tab) and saves a report for it (traceback, run, worker log, robot state): "오류 기록 받기".
function aiAlerts() {
  const out = [];
  for (const m of AIS.snap?.models || []) {
    for (const e of m.current_errors || []) {
      const r = m.worker?.last_run;
      const text = e.kind === "run" && r
        ? [`${fmt(r.elapsed_s || 0, 1)}초`, `chunk ${r.chunks || 0}`, e.message].filter(Boolean).join(" · ") : e.message;
      out.push({ key: e.key, title: AI_ERROR_TITLE[e.kind] || "⚠ AI 오류", text,
        log: e.file ? aiErrorUrl(m.id, e.file) : e.kind === "run" && r?.log ? aiLogUrl(m.id, r.log) : null,
        logLabel: e.file ? "오류 기록 받기" : "로그 받기" });
    }
  }
  return out.filter((a) => !AIS.dismissed.has(a.key));
}

function aiBeep() {  // two short tones; the audio context was unlocked by a click on an AI button
  try {
    const ctx = AIS.audio || (AIS.audio = new (window.AudioContext || window.webkitAudioContext)());
    for (const [i, f] of [[0, 880], [1, 660]]) {
      const o = ctx.createOscillator(), g = ctx.createGain(), t = ctx.currentTime + i * 0.22;
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.2);
      o.connect(g).connect(ctx.destination);
      o.start(t); o.stop(t + 0.21);
    }
  } catch { /* no audio */ }
}

function aiUnlockAudio() {
  try { AIS.audio = AIS.audio || new (window.AudioContext || window.webkitAudioContext)(); AIS.audio.resume(); } catch { /* none */ }
}

// While the followers hold the pose an AI run left them in, the operator chooses: "AI 재개" (the same
// model and task again, from this pose) or "원격조작 전환" (app.js returnToTeleop).
function aiResumable() {
  const last = aiLastRun(), m = last && aiModel(last.model);
  return last && m?.state === "ready" && last.task ? { last, m } : null;
}

async function aiResume() {
  const r = aiResumable();
  if (!r) { showMsg("AI 재개: 모델이 준비되지 않았습니다. 'AI 제어…'에서 모델을 로드하세요."); return; }
  const seconds = Number(r.last.seconds) || 30;
  if (!confirm(`'${r.m.name}'이(가) 지금 자세에서 같은 작업을 ${fmt(seconds, 0)}초 동안 다시 실행합니다.\n"${r.last.task}"\n` +
    "주변에 사람과 물체가 없는지 확인하세요. 멈추려면 위쪽의 'AI 정지'를 누르세요.\nAI 제어를 재개할까요?")) return;
  showMsg("");
  try {
    aiChanged(await aiPost({ action: "run", model: r.m.id, task: r.last.task, seconds, execute: true,
      return_after: $("#ai-return").checked }));
  } catch (e) { showMsg(`AI 재개 실패: ${e.message}`); }
}

function aiDismiss() {
  for (const a of aiAlerts()) AIS.dismissed.add(a.key);
  try { sessionStorage.setItem("teleop.aiDismissed", JSON.stringify([...AIS.dismissed].slice(-200))); } catch { /* private */ }
  renderAIBanner(); renderAIGroup();
}

function renderAIBanner() {
  const banner = $("#ai-banner"), active = AIS.snap?.active, link = $("#ai-banner-log");
  const policy = sources.arms?.state === "running" ? sources.arms?.info?.policy : null;
  const back = sources.arms?.info?.return, auto = AIS.snap?.auto_return;
  const alerts = aiAlerts();
  for (const a of alerts) {
    if (!AIS.alerted.has(a.key)) { AIS.alerted.add(a.key); aiBeep(); }
  }
  document.title = alerts.length ? `⚠ AI 오류 · ${AIS.title}` : AIS.title;
  let cls = "", title = "", text = "", log = null, logLabel = "로그 받기", ok = false;
  if (active) {
    cls = active.waiting ? "warn" : "";
    title = active.waiting ? "⚠ 입력 지연 — 자세 유지하며 기다리는 중" : active.execute ? "AI 제어 중" : "AI 예측만 (팔은 움직이지 않음)";
    text = `${active.checkpoint_label || active.name} · ` +
      (active.started_unix ? `${aiClock(active.elapsed_s ?? 0)} / ${aiClock(active.seconds)}` : "관측 기다리는 중") +
      ` · chunk ${active.chunks || 0}` + (active.prediction_s ? ` · 추론 ${fmt(active.prediction_s, 2)}초` : "") +
      (active.waiting ? " · 입력 지연, 자세 유지하며 기다리는 중" : active.stalls ? ` · 입력 지연 ${active.stalls}회 넘김` : "") +
      ` · "${active.task}"`;
  } else if (policy?.mode === "returning") {
    title = "준비 자세로 돌아가는 중";
    text = `팔 4대가 함께 움직입니다${back ? ` · ${Math.round((back.progress || 0) * 100)}% / 약 ${fmt(back.duration_s, 0)}초` : ""}` +
      " · 끝나면 팔로워가 다시 리더를 따라갑니다";
  } else if (alerts.length) {
    const a = alerts[0];
    cls = "error"; title = a.title; text = a.text; log = a.log || null; logLabel = a.logLabel; ok = true;
  } else {
    // The last run's result: while the arms still hold its pose, or for a minute after it ended.
    const last = aiLastRun();
    if (last && last.status !== "error" && (policy?.mode === "hold" || Date.now() / 1000 - (last.finished_unix || 0) < 60)) {
      const status = last.status || (last.error ? "error" : last.stopped_by_user ? "stopped" : "done");
      const failed = auto && !auto.ok && auto.time_unix >= (last.finished_unix || 0) ? auto.error : null;
      cls = status === "error" ? "error" : failed ? "warn" : "done";
      title = AI_END[status] || AI_END.done;
      text = [`${fmt(last.elapsed_s || 0, 1)}초`, `chunk ${last.chunks || 0}`,
        last.stalls ? `입력 지연 ${last.stalls}회` : "", last.error || "",
        failed ? `자동 복귀 실패: ${failed}` : policy?.mode === "hold" ? "자세 유지 중" : policy?.mode === "teleop" ? "원격조작 재개됨" : ""]
        .filter(Boolean).join(" · ");
      if (last.log) log = aiLogUrl(last.model, last.log);
    }
  }
  banner.hidden = !title;
  banner.className = `ai-banner ${cls}`.trim();
  $("#ai-banner-title").textContent = title;
  $("#ai-banner-text").textContent = text;
  $("#ai-banner-stop").hidden = !active;
  const choose = !active && policy?.mode === "hold";  // AI 재개 or 원격조작 전환
  $("#ai-banner-return").hidden = !choose || !title;
  $("#ai-banner-resume").hidden = !choose || !title || !aiResumable();
  $("#btn-ai-resume").hidden = !choose || !aiResumable();
  $("#ai-banner-ok").hidden = !ok;
  link.hidden = !log;
  if (log) { link.href = log; link.textContent = logLabel; }
  // A run just ended: refresh the run history shown in the dialog.
  const lastKey = aiLastRun()?.finished_unix || null;
  if (lastKey !== AIS.lastSeen) { AIS.lastSeen = lastKey; if ($("#ai-dialog").open) aiLoadRuns(); }
}
setInterval(renderAIBanner, 500);  // elapsed time and the arms' policy mode (app.js sources)

// ---------- dialog ----------
function aiSelected() { return aiModel(AIS.selected) || (AIS.snap?.models || []).find((m) => !m.error); }

function renderAIDialog() {
  const snap = AIS.snap, root = $("#ai-models");
  $("#ai-root").textContent = snap?.root ? `모델 폴더: ${snap.root}` : "";
  const sel = aiSelected();
  AIS.selected = sel?.id || null;
  root.replaceChildren();
  if (!snap?.models?.length) {
    const p = document.createElement("p");
    p.className = "tx-hint";
    p.textContent = `${snap?.root || "inference"} 안에 inference.yaml이 있는 모델 폴더가 없습니다.`;
    root.append(p);
  }
  for (const m of snap?.models || []) {
    const card = document.createElement("div"), head = document.createElement("div");
    card.className = "ai-model"; card.setAttribute("role", "radio"); card.dataset.model = m.id;
    card.setAttribute("aria-checked", String(m.id === AIS.selected)); card.tabIndex = 0;
    head.className = "head";
    const radio = document.createElement("input"), name = document.createElement("span");
    radio.type = "radio"; radio.name = "ai-model"; radio.checked = m.id === AIS.selected; radio.tabIndex = -1;
    radio.setAttribute("aria-label", m.name);
    name.className = "name"; name.textContent = m.name;
    head.append(radio, name, aiStatusEl(AI_STATE[m.state] || ["off", "–", m.state || "?"]));
    card.append(head);
    if (m.error) { const e = document.createElement("div"); e.className = "err"; e.textContent = m.error; card.append(e); }
    const row = document.createElement("div");
    row.className = "row";
    if (m.checkpoints?.length) {
      const label = document.createElement("label"), select = document.createElement("select");
      label.textContent = "체크포인트 ";
      for (const c of m.checkpoints) {
        const o = document.createElement("option"), info = (m.checkpoint_info || []).find((x) => x.path === c);
        o.value = c; o.textContent = info?.label || c; select.append(o);
      }
      select.value = m.checkpoint || m.default_checkpoint || m.checkpoints[0];
      select.dataset.role = "checkpoint";
      select.disabled = ["running", "stopping"].includes(m.state);
      label.append(select); row.append(label);
    }
    const loaded = ["starting", "loading", "ready", "running", "stopping", "error"].includes(m.state);
    const load = document.createElement("button");
    load.type = "button"; load.className = "btn small primary"; load.textContent = "모델 로드";
    load.hidden = loaded && m.state !== "error"; load.disabled = !!m.error || AIS.busy;
    load.addEventListener("click", (e) => { e.stopPropagation(); aiLoad(m.id, card.querySelector("select")?.value); });
    const unload = document.createElement("button");
    unload.type = "button"; unload.className = "btn small"; unload.textContent = "언로드";
    unload.hidden = !loaded && m.state !== "failed"; unload.disabled = AIS.busy || ["running", "stopping"].includes(m.state);
    unload.addEventListener("click", (e) => { e.stopPropagation(); aiUnload(m); });
    const logs = document.createElement("button");
    logs.type = "button"; logs.className = "btn small"; logs.textContent = AIS.logs[m.id] ? "로그 닫기" : "로그";
    logs.addEventListener("click", (e) => { e.stopPropagation(); aiLogs(m.id); });
    row.append(load, unload, logs);
    card.append(row);
    const info = document.createElement("div");
    info.className = "info"; info.textContent = aiInfo(m);
    card.append(info);
    if (AIS.logs[m.id]) { const pre = document.createElement("pre"); pre.textContent = AIS.logs[m.id].join("\n") || "(로그 없음)"; card.append(pre); }
    const pick = () => { AIS.selected = m.id; store.set("teleop.aiModel", m.id); aiDefaults(m, true); renderAIDialog(); aiLoadRuns(); };
    card.addEventListener("click", (e) => { if (!e.target.closest("button, select")) pick(); });
    card.addEventListener("keydown", (e) => { if ((e.key === " " || e.key === "Enter") && e.target === card) { e.preventDefault(); pick(); } });
    root.append(card);
  }
  renderAIRuns();
  renderAIErrors();
  const active = snap?.active, armsOk = sources.arms?.state === "running";
  const returning = armsOk && sources.arms?.info?.policy?.mode === "returning";
  $("#ai-start").hidden = !!active;
  $("#ai-stop").hidden = !active;
  $("#ai-start").disabled = AIS.busy || !sel || sel.state !== "ready" || !armsOk || returning;
  $("#ai-start").title = !armsOk ? "먼저 '제어 시작'으로 팔을 연결하세요." : returning ? "팔이 준비 자세로 돌아가는 중입니다."
    : sel?.state !== "ready" ? "선택한 모델을 먼저 로드하세요." : "";
  $("#ai-return").disabled = !$("#ai-execute").checked;
  if (!AIS.busy && !$("#ai-msg").classList.contains("err")) {
    aiMsg(active ? "AI 제어 중입니다. 'AI 정지'를 누르면 팔이 현재 자세를 유지합니다."
      : !armsOk ? "AI 제어를 시작하려면 먼저 '제어 시작'으로 팔을 연결하세요."
      : sel && sel.state !== "ready" ? "선택한 모델을 로드하면 시작할 수 있습니다." : "");
  }
}

const AI_RUN_STATE = { done: ["good", "✓", "끝"], stopped: ["off", "–", "정지함"], error: ["critical", "!", "오류"],
  unknown: ["off", "?", "이전 형식"] };

async function aiLoadRuns() {
  const m = aiSelected();
  if (!m) return;
  try { AIS.runs[m.id] = (await aiPost({ action: "runs", model: m.id })).runs; } catch { /* listed on the next try */ }
  renderAIRuns();
}

// ---------- choosing logs to download together ----------
function aiPicked(kind) {  // the ticked files of the selected model (cleared when another model is chosen)
  const id = aiSelected()?.id || null;
  if (AIS.pick.model !== id) AIS.pick = { model: id, runs: new Set(), errors: new Set() };
  return AIS.pick[kind];
}

function aiPickBox(kind, file) {
  const box = document.createElement("input");
  box.type = "checkbox"; box.checked = aiPicked(kind).has(file); box.dataset.file = file;
  box.setAttribute("aria-label", `${file} 선택`);
  box.addEventListener("change", () => { aiPicked(kind)[box.checked ? "add" : "delete"](file); renderAIPick(); });
  return box;
}

function aiPickAll(kind, files, all) {  // the list's "전체 선택" box follows the ticks
  const picked = aiPicked(kind);
  for (const f of [...picked]) if (!files.includes(f)) picked.delete(f);  // gone from the list
  const n = files.filter((f) => picked.has(f)).length;
  all.checked = files.length > 0 && n === files.length;
  all.indeterminate = n > 0 && n < files.length;
  all.disabled = !files.length;
}

function renderAIPick() {
  const runs = aiPicked("runs").size, errors = aiPicked("errors").size;
  $("#ai-pick-count").textContent = runs + errors
    ? `${runs + errors}개 선택 (실행 ${runs} · 오류 ${errors})` : "로그를 체크해서 고르면 한 번에 받을 수 있습니다.";
  $("#ai-pick-download").disabled = !(runs + errors) || AIS.downloading;
  $("#ai-pick-download").textContent = AIS.downloading ? "묶는 중…" : "선택한 로그 받기 (.zip)";
  $("#ai-pick-clear").hidden = !(runs + errors);
  const runFiles = ((aiSelected() && AIS.runs[aiSelected().id]) || []).map((r) => r.file);
  aiPickAll("runs", runFiles, $("#ai-runs-all"));
  aiPickAll("errors", (aiSelected()?.errors || []).map((e) => e.file), $("#ai-errors-all"));
}

function aiSave(blob, name) {  // hand a downloaded file to the browser
  const url = URL.createObjectURL(blob), a = document.createElement("a");
  a.href = url; a.download = name; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

async function aiDownloadPicked() {
  const m = aiSelected();
  if (!m) return;
  const q = new URLSearchParams();
  for (const f of aiPicked("runs")) q.append("run", f);
  for (const f of aiPicked("errors")) q.append("error", f);
  AIS.downloading = true; renderAIPick();
  try {
    const r = await fetch(`/api/ai/bundle/${encodeURIComponent(m.id)}?${q}`);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
    const name = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "")?.[1] || `${m.id}_ai_logs.zip`;
    aiSave(await r.blob(), name);
    aiMsg(`${name} 받음`);
  } catch (e) { aiMsg(`로그 받기 실패: ${e.message}`, true); }
  AIS.downloading = false; renderAIPick();
}

function renderAIRuns() {
  const m = aiSelected(), list = $("#ai-runs"), runs = (m && AIS.runs[m.id]) || [];
  list.replaceChildren();
  $("#ai-runs-box").hidden = !m;
  renderAIPick();
  if (!runs.length) {
    const li = document.createElement("li"); li.className = "empty"; li.textContent = "기록이 없습니다."; list.append(li); return;
  }
  for (const r of runs) {
    const li = document.createElement("li"), head = document.createElement("div"), a = document.createElement("a");
    const when = /robot_run_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/.exec(r.file);
    head.className = "run-head";
    head.append(aiPickBox("runs", r.file));
    const t = document.createElement("span");
    t.textContent = `${when ? `${when[2]}-${when[3]} ${when[4]}:${when[5]}:${when[6]}` : r.file} · ` +
      `${r.elapsed_s != null ? `${fmt(r.elapsed_s, 1)}초 · ` : ""}chunk ${r.chunks ?? 0}` +
      (r.stalls ? ` · 입력 지연 ${r.stalls}회` : "") + (r.execute === false ? " · 예측만" : "");
    a.href = aiLogUrl(m.id, r.file); a.textContent = "로그 받기"; a.className = "mini"; a.setAttribute("download", "");
    head.append(aiStatusEl(AI_RUN_STATE[r.status] || AI_RUN_STATE.unknown), t, a);
    li.append(head);
    if (r.error) { const e = document.createElement("div"); e.className = "err"; e.textContent = r.error; li.append(e); }
    list.append(li);
  }
}

// Saved error reports of the selected model (newest first), each downloadable as JSON.
function renderAIErrors() {
  const m = aiSelected(), list = $("#ai-errors"), errors = m?.errors || [];
  $("#ai-errors-box").hidden = !m;
  $("#ai-errors-count").textContent = errors.length ? ` (${errors.length}${errors.length >= 50 ? "+" : ""})` : "";
  list.replaceChildren();
  renderAIPick();
  if (!errors.length) {
    const li = document.createElement("li"); li.className = "empty"; li.textContent = "저장된 오류가 없습니다."; list.append(li); return;
  }
  for (const e of errors) {
    const li = document.createElement("li"), head = document.createElement("div"), t = document.createElement("span");
    const a = document.createElement("a"), d = new Date((e.time_unix || 0) * 1000), p2 = (n) => String(n).padStart(2, "0");
    head.className = "run-head";
    head.append(aiPickBox("errors", e.file));
    t.textContent = `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
    a.href = aiErrorUrl(m.id, e.file); a.textContent = "받기"; a.className = "mini"; a.setAttribute("download", "");
    head.append(aiStatusEl(["critical", "!", AI_ERROR_KIND[e.kind] || "오류"]), t, a);
    const msg = document.createElement("div");
    msg.className = "err"; msg.textContent = e.message || "";
    li.append(head, msg);
    list.append(li);
  }
}

function aiInfo(m) {
  const w = m.worker;
  if (m.state === "starting" || m.state === "loading") {
    const t = m.waiting_s ?? (w?.load_started_unix ? Date.now() / 1000 - w.load_started_unix : 0);
    return `불러오는 중 ${aiClock(t)} (π₀.₅는 보통 약 2분)`;
  }
  if (w?.state === "error") return w.error || "오류";
  if (m.state === "failed") return m.unit_info?.summary || "워커가 비정상 종료했습니다.";
  if (!w) return "";
  return [w.load_s ? `로딩 ${fmt(w.load_s, 0)}초` : "", w.gpu_max_gib ? `GPU ${fmt(w.gpu_max_gib, 1)} GiB` : ""]
    .filter(Boolean).join(" · ");
}

function aiDefaults(m, force = false) {
  const d = { ...(m?.defaults || {}), ...(m?.worker?.defaults || {}) };
  const task = $("#ai-task");
  if (force || !task.value) task.value = store.get(`teleop.aiTask.${m?.id}`) || d.task || "";
  if (force || !$("#ai-seconds").dataset.touched) $("#ai-seconds").value = String(d.seconds || 30);
}

async function aiRefresh() {
  try {
    const r = await fetch("/api/ai");
    if (r.status === 404) return;  // single-PC server: no AI control
    const d = await r.json();
    AIS.available = true;
    $("#ai-group").hidden = false;
    aiChanged(d);
  } catch { /* offline */ }
}

function aiOpen() {
  AIS.selected = AIS.selected || store.get("teleop.aiModel") || null;
  aiMsg("");
  const d = $("#ai-dialog");
  renderAIDialog();
  aiDefaults(aiSelected());
  if (!d.open) d.showModal();
  aiRefresh();
  aiLoadRuns();
}

async function aiAct(fn) {
  AIS.busy = true; renderAIDialog();
  try { await fn(); } catch (e) { aiMsg(e.message, true); }
  AIS.busy = false; renderAIDialog();
}

function aiLoad(id, checkpoint) {
  aiMsg("");
  return aiAct(async () => {
    aiChanged(await aiPost({ action: "load", model: id, checkpoint }));
    aiMsg("모델을 불러오는 중입니다. 창을 닫아도 계속됩니다.");
  });
}

function aiUnload(m) {
  if (!confirm(`'${m.name}' 모델을 언로드할까요? GPU 메모리를 비우며, 다시 쓰려면 처음부터 불러와야 합니다.`)) return;
  aiMsg("");
  return aiAct(async () => aiChanged(await aiPost({ action: "unload", model: m.id })));
}

async function aiLogs(id) {
  if (AIS.logs[id]) { delete AIS.logs[id]; renderAIDialog(); return; }
  try { AIS.logs[id] = (await aiPost({ action: "logs", model: id })).lines; } catch (e) { aiMsg(e.message, true); }
  renderAIDialog();
}

async function aiStart() {
  const m = aiSelected(), task = $("#ai-task").value.trim(), seconds = Number($("#ai-seconds").value);
  const execute = $("#ai-execute").checked, returnAfter = execute && $("#ai-return").checked;
  if (!m || !task) { aiMsg("모델과 작업 문장을 확인하세요.", true); return; }
  const ask = execute
    ? `'${m.name}'이(가) 양팔을 ${seconds}초 동안 직접 움직입니다.\n리더 추종과 베이스 조작은 꺼집니다.\n` +
      (returnAfter ? "끝나면 팔 4대(리더 포함)가 준비 자세로 돌아간 뒤 원격조작이 다시 됩니다.\n" : "") +
      "주변에 사람과 물체가 없는지 확인하세요. 멈추려면 위쪽의 'AI 정지'를 누르세요.\nAI 제어를 시작할까요?"
    : `'${m.name}'의 예측만 ${seconds}초 동안 실행합니다. 팔은 움직이지 않고 원격조작은 그대로입니다.\n시작할까요?`;
  if (!confirm(ask)) return;
  store.set(`teleop.aiTask.${m.id}`, task);
  aiMsg("");
  await aiAct(async () => aiChanged(await aiPost({ action: "run", model: m.id, task, seconds, execute, return_after: returnAfter })));
}

async function aiStop() {
  try { aiChanged(await aiPost({ action: "stop" })); } catch (e) { aiMsg(e.message, true); alert(`AI 정지 실패: ${e.message}`); }
}

function bindAI() {
  $("#btn-ai").addEventListener("click", aiOpen);
  for (const id of ["#btn-ai-stop", "#ai-banner-stop", "#ai-stop"]) $(id).addEventListener("click", aiStop);
  $("#ai-banner-ok").addEventListener("click", aiDismiss);
  $("#ai-banner-return").addEventListener("click", () => window.returnToTeleop());
  for (const id of ["#ai-banner-resume", "#btn-ai-resume"]) {
    $(id).addEventListener("click", aiUnlockAudio);
    $(id).addEventListener("click", aiResume);
  }
  $("#ai-return").checked = store.get("teleop.aiReturn") === "1";  // default: hold, then choose
  $("#ai-pick-download").addEventListener("click", aiDownloadPicked);
  $("#ai-pick-clear").addEventListener("click", () => { aiPicked("runs").clear(); aiPicked("errors").clear(); renderAIDialog(); });
  for (const [kind, all] of [["runs", "#ai-runs-all"], ["errors", "#ai-errors-all"]]) {
    $(all).addEventListener("change", (e) => {
      const files = kind === "runs" ? ((aiSelected() && AIS.runs[aiSelected().id]) || []).map((r) => r.file)
        : (aiSelected()?.errors || []).map((x) => x.file);
      const picked = aiPicked(kind);
      for (const f of files) picked[e.target.checked ? "add" : "delete"](f);
      renderAIDialog();
    });
  }
  $("#ai-return").addEventListener("change", (e) => store.set("teleop.aiReturn", e.target.checked ? "1" : "0"));
  $("#ai-execute").addEventListener("change", () => { $("#ai-return").disabled = !$("#ai-execute").checked; });
  for (const id of ["#btn-ai", "#ai-start"]) $(id).addEventListener("click", aiUnlockAudio);
  $("#ai-start").addEventListener("click", aiStart);
  $("#ai-close").addEventListener("click", () => $("#ai-dialog").close());
  $("#ai-seconds").addEventListener("input", (e) => { e.target.dataset.touched = "1"; });
  $("#ai-dialog").addEventListener("close", () => { AIS.logs = {}; });
}

bindAI();
aiRefresh();
window.__teleop.ai = AIS;
