"use strict";
// Teleop Monitor client. Server timestamps are host unix ns; the browser converts its own
// clock to server time with an NTP-style min-RTT offset estimate so cross-machine latency is valid.

const THR = { good: 100, warn: 200 };      // end-to-end p95 thresholds (ms)
const STALE_MS = 1000;
const WINDOW_MS = 10000;
const HISTORY_S = 60;
const CAMS = ["camera1", "camera2", "camera3"];
const ARM_SIDES = [["left", "왼팔"], ["right", "오른팔"]];
const ARM_SENSORS = ["leader_left", "follower_left", "leader_right", "follower_right"];
const SENSORS = [...CAMS, ...ARM_SENSORS, "odom"];
const LABELS = { camera1: "카메라 1", camera2: "카메라 2", camera3: "카메라 3", leader_left: "리더 왼팔",
  follower_left: "팔로워 왼팔", leader_right: "리더 오른팔", follower_right: "팔로워 오른팔", odom: "베이스 odometry" };
const JOINTS = ["joint 0", "joint 1", "joint 2", "joint 3", "joint 4", "joint 5", "gripper"];

const $ = (sel, root = document) => root.querySelector(sel);
// ?skew_ms=N shifts this browser's clock (test hook for the clock-offset estimation).
const SKEW_MS = Number(new URLSearchParams(location.search).get("skew_ms") || 0);
const wallNow = () => performance.timeOrigin + performance.now() + SKEW_MS;
const clock = { offset: null, rtt: null, samples: [] };
const serverNow = () => wallNow() + (clock.offset ?? 0);
const fmt = (v, d = 1) => (v == null || !isFinite(v) ? "–" : v.toFixed(d));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

class Rolling {
  constructor(win = WINDOW_MS) { this.win = win; this.t = []; this.v = []; }
  add(v, t = wallNow()) { this.t.push(t); this.v.push(v); this.trim(t); }
  trim(now) { let i = 0; while (i < this.t.length && now - this.t[i] > this.win) i++; if (i) { this.t.splice(0, i); this.v.splice(0, i); } }
  stats() {
    this.trim(wallNow());
    if (!this.v.length) return null;
    const s = [...this.v].sort((a, b) => a - b);
    const q = (p) => s[Math.min(s.length - 1, Math.floor(p * (s.length - 1) + 0.5))];
    return { n: s.length, p50: q(0.5), p95: q(0.95), max: s[s.length - 1], last: this.v[this.v.length - 1] };
  }
  rate() { this.trim(wallNow()); const n = this.t.length; return n > 1 ? (n - 1) * 1000 / (this.t[n - 1] - this.t[0]) : 0; }
}

const S = {};  // per-sensor client state
for (const name of SENSORS) {
  S[name] = { network: new Rolling(), render: new Rolling(), e2e: new Rolling(), arrivals: new Rolling(),
    last: null, history: [], server: null, source: null, drawn: 0 };
}
const sources = {};  // source status by name
const latest = {};   // latest telemetry message by sensor
window.__teleop = { clock, S, sources, latest, get record() { return recState; }, get base() { return baseState; },
  get joy() { return joy; }, get layout() { return camLayout; } };

// ---------- layout ----------
function buildCams() {
  const root = $("#cams");
  for (const name of CAMS) {
    const el = document.createElement("div");
    el.className = "cam card";
    el.innerHTML = `<canvas width="640" height="480" aria-label="${LABELS[name]} 영상"></canvas>
      <canvas class="depth-inset" width="320" height="240" aria-label="${LABELS[name]} depth 미리보기" hidden></canvas>
      <div class="nodata">영상 없음</div>
      <div class="bar"><b>${LABELS[name]}</b><span><span class="fps">– fps</span> · 종단 <span class="lat">–</span> ms
        <button class="mini" type="button" aria-pressed="false" title="depth 미리보기 (가까움 = 밝음, 검정 = 측정 없음)">깊이</button></span></div>`;
    el.dataset.cam = name;
    el.setAttribute("aria-label", LABELS[name]);
    const handle = document.createElement("div");
    handle.className = "rs-handle";
    handle.title = "끌어서 크기 조절";
    el.appendChild(handle);
    root.appendChild(el);
    S[name].el = el;
    S[name].ctx = $("canvas", el).getContext("2d");
    const inset = $(".depth-inset", el), btn = $(".mini", el);
    S[name].depthCtx = inset.getContext("2d");
    btn.addEventListener("click", () => {
      const on = btn.getAttribute("aria-pressed") !== "true";
      btn.setAttribute("aria-pressed", String(on)); inset.hidden = !on;
    });
  }
}

function buildArms() {
  const root = $("#arms");
  for (const [side, label] of ARM_SIDES) {
    const el = document.createElement("div");
    el.className = "arm card";
    const rows = JOINTS.map((j, i) => `<tr><td>${j}</td><td data-r="leader" data-i="${i}">–</td>
      <td data-r="follower" data-i="${i}">–</td><td class="delta" data-r="delta" data-i="${i}">–</td></tr>`).join("");
    el.innerHTML = `<h3>${label}<span data-k="rate">리더 – Hz · 팔로워 – Hz</span></h3>
      <table class="joints"><thead><tr><th scope="col">관절</th><th scope="col">리더</th><th scope="col">팔로워</th>
      <th scope="col" title="리더 − 팔로워">차이</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="err" data-k="err"></div>`;
    root.appendChild(el);
    el.dataset.side = side;
  }
}

const ODOM_FIELDS = [["x", "x (m)"], ["y", "y (m)"], ["yaw", "yaw (°)"], ["vx", "vx (m/s)"], ["vy", "vy (m/s)"],
  ["wz", "wz (rad/s)"], ["frames", "frame"], ["age", "수신 지연 (ms)"]];
function buildOdom() {
  $("#odom-kv").innerHTML = ODOM_FIELDS.map(([k, l]) => `<dt>${l}</dt><dd data-k="${k}">–</dd>`).join("");
}

function buildLatency() {
  $("#thr-good").textContent = `${THR.good} ms`;
  $("#thr-warn").textContent = `${THR.warn} ms`;
  const body = $("#lat tbody");
  for (const name of SENSORS) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<th scope="row">${LABELS[name]}</th><td><span class="status" data-s="off"><i>–</i><span>꺼짐</span></span></td>
      <td class="num" data-k="hz">–</td><td class="num" data-k="sensor">–</td><td class="num" data-k="server">–</td>
      <td class="num" data-k="network">–</td><td class="num" data-k="render">–</td><td class="num e2e" data-k="p50">–</td>
      <td class="num" data-k="p95">–</td><td class="num" data-k="max">–</td><td class="num" data-k="drop">–</td>
      <td><canvas class="spark" width="170" height="30" aria-label="${LABELS[name]} 종단 지연 추이"></canvas></td>`;
    body.appendChild(tr);
    S[name].row = tr;
    const spark = $("canvas", tr);
    spark.addEventListener("mousemove", (e) => sparkHover(name, spark, e));
    spark.addEventListener("mouseleave", () => { $("#tooltip").hidden = true; S[name].hover = null; drawSpark(name); });
  }
}

// ---------- websocket ----------
let ws;
function connect() {
  const conn = $("#conn");
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  conn.dataset.state = "connecting"; conn.textContent = "연결 중";
  ws.onopen = () => {
    conn.dataset.state = "open"; conn.textContent = "연결됨";
    clock.samples = [];
    for (let i = 0; i < 5; i++) setTimeout(ping, i * 150);
  };
  ws.onclose = () => { conn.dataset.state = "closed"; conn.textContent = "연결 끊김"; setTimeout(connect, 1000); };
  ws.onmessage = (ev) => {
    const tRecv = serverNow();
    if (typeof ev.data === "string") onText(JSON.parse(ev.data), tRecv);
    else onBinary(ev.data, tRecv);
  };
}
function ping() { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "ping", t0: wallNow() })); }
setInterval(ping, 2000);

function onPong(m) {
  const t1 = wallNow();
  const rtt = t1 - m.t0;
  clock.samples.push({ rtt, offset: m.server_ns / 1e6 - (m.t0 + t1) / 2 });
  if (clock.samples.length > 20) clock.samples.shift();
  const best = clock.samples.reduce((a, b) => (b.rtt < a.rtt ? b : a));
  clock.offset = best.offset; clock.rtt = rtt;
  $("#rtt").textContent = `${fmt(rtt)} ms`;
  $("#offset").textContent = `${fmt(clock.offset)} ms`;
}

function onText(m, tRecv) {
  switch (m.type) {
    case "pong": return onPong(m);
    case "hello":
      for (const s of m.sources) sources[s.name] = s;
      recState = m.record; baseState = m.base; myId = m.client_id; renderBase();
      renderSources(); return renderControl();
    case "status": sources[m.name] = m; renderSources(); return renderControl();
    case "record": recState = m; renderControl(); return onRecordState(m);
    case "labels": return loadSessions();
    case "base_ctl": baseState = m; return renderBase();
    case "base_reject": $("#joy-read").textContent = `거부됨: ${m.reason}`; return;
    case "stats":
      for (const s of m.sources) sources[s.name] = s;
      for (const [name, st] of Object.entries(m.sensors)) if (S[name]) S[name].server = st;
      renderSources(); return renderControl();
    case "arm": case "odom": return onTelemetry(m, tRecv);
  }
}

function arrive(name, m, tRecv) {
  // Ack on receipt: the server keeps at most a few unacknowledged messages per stream in flight.
  if (m.key && ws.readyState === 1) ws.send(JSON.stringify({ type: "ack", key: m.key }));
  const st = S[name];
  st.arrivals.add(1);
  st.last = wallNow();
  st.network.add(tRecv - m.t_sent_ns / 1e6);
}

// Cameras: decode off the main path; if a decode is in flight keep only the newest pending frame.
const camBusy = {}, camPending = {};
function onBinary(buf, tRecv) {
  const n = new DataView(buf).getUint32(0, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, n)));
  const jpg = new Blob([new Uint8Array(buf, 4 + n)], { type: "image/jpeg" });
  if (header.type === "depth_preview") return drawDepth(header, jpg);
  if (!S[header.sensor]) return;
  arrive(header.sensor, header, tRecv);
  const item = { header, jpg, tRecv };
  if (camBusy[header.sensor]) { camPending[header.sensor] = item; return; }
  drawFrame(item);
}
async function drawFrame({ header, jpg, tRecv }) {
  const name = header.sensor, st = S[name];
  camBusy[name] = true;
  try {
    const bmp = await createImageBitmap(jpg);
    const c = st.ctx.canvas;
    if (c.width !== bmp.width || c.height !== bmp.height) { c.width = bmp.width; c.height = bmp.height; }
    st.ctx.drawImage(bmp, 0, 0);
    bmp.close();
    const tDraw = serverNow();
    st.render.add(tDraw - tRecv);
    if (header.t_capture_ns) st.e2e.add(tDraw - header.t_capture_ns / 1e6);
    st.drawn++;
    $(".nodata", st.el).hidden = true;
  } catch (e) { console.warn(e); }
  camBusy[name] = false;
  const next = camPending[name];
  if (next) { camPending[name] = null; drawFrame(next); }
}

async function drawDepth(header, jpg) {
  const st = S[header.camera];
  if (!st || $(".depth-inset", st.el).hidden) return;
  try {
    const bmp = await createImageBitmap(jpg);
    st.depthCtx.drawImage(bmp, 0, 0, 320, 240);
    bmp.close();
    st.depthFrames = (st.depthFrames || 0) + 1;
  } catch (e) { console.warn(e); }
}

// Telemetry: coalesce DOM updates into one animation frame; latency measured at paint time.
const dirty = new Set();
let rafQueued = false;
function onTelemetry(m, tRecv) {
  if (!S[m.sensor]) return;
  arrive(m.sensor, m, tRecv);
  latest[m.sensor] = { m, tRecv };
  dirty.add(m.sensor);
  if (!rafQueued) { rafQueued = true; requestAnimationFrame(paintTelemetry); }
}
function paintTelemetry() {
  rafQueued = false;
  const tDraw = serverNow();
  for (const name of dirty) {
    if (!latest[name]) continue;  // cleared after the arms stopped
    const { m, tRecv } = latest[name];
    S[name].render.add(tDraw - tRecv);
    if (m.t_source_ns) S[name].e2e.add(tDraw - m.t_source_ns / 1e6);
    S[name].drawn++;
  }
  if ([...dirty].some((n) => n !== "odom")) renderArms();
  if (dirty.has("odom")) renderOdom();
  dirty.clear();
}

// ---------- panels ----------
function renderArms() {
  for (const el of document.querySelectorAll(".arm")) {
    const side = el.dataset.side;
    const L = latest[`leader_${side}`]?.m, F = latest[`follower_${side}`]?.m;
    for (let i = 0; i < JOINTS.length; i++) {
      const grip = i === JOINTS.length - 1, k = grip ? 1000 : 1, d = grip ? 1 : 3;
      const lv = L?.pos?.[i], fv = F?.pos?.[i];
      $(`[data-r="leader"][data-i="${i}"]`, el).textContent = lv == null ? "–" : fmt(lv * k, d);
      $(`[data-r="follower"][data-i="${i}"]`, el).textContent = fv == null ? "–" : fmt(fv * k, d);
      const cell = $(`[data-r="delta"][data-i="${i}"]`, el);
      if (lv == null || fv == null) { cell.textContent = "–"; cell.classList.remove("big"); continue; }
      const delta = (lv - fv) * k;
      cell.textContent = fmt(delta, d);
      cell.classList.toggle("big", Math.abs(delta) > (grip ? 5 : 0.1));
    }
  }
}

const traj = [];
function renderOdom() {
  const m = latest.odom.m;
  const [qx, qy, qz, qw] = m.q;
  const yaw = Math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz));
  const kv = $("#odom-kv");
  const set = (k, v) => { $(`[data-k="${k}"]`, kv).textContent = v; };
  set("x", fmt(m.p[0], 3)); set("y", fmt(m.p[1], 3)); set("yaw", fmt(yaw * 180 / Math.PI, 1));
  set("vx", fmt(m.v[0], 3)); set("vy", fmt(m.v[1], 3)); set("wz", fmt(m.w[2], 3));
  set("frames", `${m.frame_id} → ${m.child_frame_id}`);
  set("age", fmt((m.t_recv_ns - m.t_source_ns) / 1e6, 1));
  const now = wallNow();
  if (!traj.length || now - traj[traj.length - 1].t > 100) traj.push({ t: now, x: m.p[0], y: m.p[1], yaw });
  while (traj.length && now - traj[0].t > HISTORY_S * 1000) traj.shift();
  drawTraj();
}

function fitCanvas(c) {
  const dpr = window.devicePixelRatio || 1, r = c.getBoundingClientRect();
  if (c.width !== Math.round(r.width * dpr) || c.height !== Math.round(r.height * dpr)) {
    c.width = Math.round(r.width * dpr); c.height = Math.round(r.height * dpr);
  }
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: r.width, h: r.height };
}

function drawTraj() {
  const { ctx, w, h } = fitCanvas($("#traj"));
  ctx.clearRect(0, 0, w, h);
  if (!traj.length) return;
  const cur = traj[traj.length - 1];
  let span = 2;  // metres visible across the short side, at least
  for (const p of traj) span = Math.max(span, 2.4 * Math.abs(p.x - cur.x), 2.4 * Math.abs(p.y - cur.y));
  const s = Math.min(w, h) / span;
  // ROS x forward drawn up, y left drawn left.
  const X = (p) => w / 2 - (p.y - cur.y) * s, Y = (p) => h / 2 - (p.x - cur.x) * s;
  ctx.strokeStyle = css("--grid"); ctx.lineWidth = 1;
  const g0x = Math.floor(cur.y - w / s), g1x = Math.ceil(cur.y + w / s);
  const g0y = Math.floor(cur.x - h / s), g1y = Math.ceil(cur.x + h / s);
  ctx.beginPath();
  for (let gy = g0x; gy <= g1x; gy++) { const px = X({ y: gy }); ctx.moveTo(px, 0); ctx.lineTo(px, h); }
  for (let gx = g0y; gx <= g1y; gx++) { const py = Y({ x: gx }); ctx.moveTo(0, py); ctx.lineTo(w, py); }
  ctx.stroke();
  ctx.strokeStyle = css("--series-1"); ctx.lineWidth = 2; ctx.lineJoin = "round";
  ctx.beginPath();
  traj.forEach((p, i) => (i ? ctx.lineTo(X(p), Y(p)) : ctx.moveTo(X(p), Y(p))));
  ctx.stroke();
  ctx.save();
  ctx.translate(X(cur), Y(cur)); ctx.rotate(-cur.yaw);
  ctx.fillStyle = css("--series-1"); ctx.strokeStyle = css("--surface"); ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(0, -11); ctx.lineTo(7, 7); ctx.lineTo(-7, 7); ctx.closePath(); ctx.fill(); ctx.stroke();
  ctx.restore();
}

function renderSources() {
  for (const name of CAMS) {
    const src = sources[name], st = S[name];
    if (!st.el) continue;
    $(".mini", st.el).hidden = !src?.info?.depth;
    const nd = $(".nodata", st.el);
    if (!src) { nd.textContent = "꺼짐"; nd.hidden = false; }
    else if (src.state === "error") { nd.textContent = `오류: ${src.error}`; nd.hidden = false; }
  }
  const armSrc = sources.arms;
  if (armSrc && !["running", "starting"].includes(armSrc.state) && ARM_SENSORS.some((n) => latest[n])) {
    for (const n of ARM_SENSORS) { delete latest[n]; dirty.delete(n); }  // arms disconnected: no old positions
    renderArms();
  }
  for (const el of document.querySelectorAll(".arm")) {
    $('[data-k="err"]', el).textContent = armSrc?.state === "error" ? `오류: ${armSrc.error}` : !armSrc ? "팔 소스 꺼짐" : "";
  }
}

// ---------- control & recording ----------
let recState = null;
const busy = { control: false, record: false };
const ARM_UI = { idle: ["off", "–", "정지"], stopped: ["off", "–", "정지"], starting: ["warning", "…", "연결 중"],
  running: ["good", "✓", "제어 중"], stopping: ["warning", "…", "정지 중"], error: ["critical", "!", "오류"] };
const REC_UI = { idle: ["off", "–", "대기"], starting: ["warning", "…", "준비 중"], recording: ["rec", "●", "녹화 중"],
  saving: ["warning", "…", "저장 중"], validating: ["warning", "…", "검증 중"], finalizing: ["warning", "…", "마무리 중"],
  transferring: ["warning", "…", "전송 중"], converting: ["warning", "…", "변환 중"] };
const gb = (b) => (b / 1e9).toFixed(b < 1e9 ? 2 : 1);

function setStatus(el, [s, icon, label]) { el.dataset.s = s; $("i", el).textContent = icon; $("span", el).textContent = label; }
const mmss = (sec) => `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;

function showMsg(text) { const m = $("#ctl-msg"); m.textContent = text || ""; m.hidden = !text; }

function lastResultText(r) {
  if (!r) return "";
  const head = `${r.status === "completed" ? "마지막 녹화" : "마지막 녹화 실패"}: ${r.session} · ${r.frames} 프레임 · ${fmt(r.elapsed_s)}초`;
  if (r.status !== "completed") return `${head} · ${r.error || r.stop_reason}`;
  const q = r.camera_quality;
  const cam = q ? ` · 카메라 최대 지연 ${Math.max(...q.camera_max_age_ms).toFixed(0)} ms, 반복 프레임 ${q.camera_repeated_frames.join("/")}` : "";
  const g = r.capture_gaps, lost = g ? g.missing_cycles + g.skipped_cycles : 0;
  const gap = lost ? ` · 누락 ${lost}주기 (${fmt(100 * (1 - g.recorded_fraction), 1)}%)` : "";
  if (!r.validation) return head + gap + cam;
  return r.validation.passed ? `${head}${gap}${cam} · 검증 통과` : `${head}${gap}${cam} · 검증 실패: ${r.validation.error}`;
}

function renderControl() {
  renderBase();  // the joystick follows robot control
  renderLabelButton();
  const arms = sources.arms, st = arms?.state ?? "off";
  const btn = $("#btn-control");
  setStatus($("#arm-state"), arms ? (ARM_UI[st] || ["off", "–", st]) : ["off", "–", "팔 소스 꺼짐"]);
  const active = st === "running" || st === "starting";
  btn.disabled = !arms || busy.control || st === "starting" || st === "stopping";
  btn.textContent = st === "starting" ? "연결 중…" : st === "stopping" ? "정지 중…" : active ? "제어 중지" : "제어 시작";
  btn.className = `btn ${active ? "danger" : "primary"}`;

  const rec = recState, rbtn = $("#btn-record");
  const rs = rec?.state ?? "idle";
  if (!rec || !rec.available) {
    setStatus($("#rec-state"), ["off", "–", "사용 불가"]);
    rbtn.disabled = true;
    $("#rec-meta").textContent = "";
    $("#rec-last").textContent = rec ? "odometry가 꺼져 있어 녹화할 수 없습니다." : "녹화하려면 카메라 3대와 팔 소스가 필요합니다.";
    return;
  }
  setStatus($("#rec-state"), REC_UI[rs] || ["off", "–", rs]);
  const recording = rs === "recording";
  rbtn.disabled = busy.record || !(recording || (rs === "idle" && st === "running"));
  rbtn.classList.toggle("on", recording);
  $(".lbl", rbtn).textContent = recording ? "녹화 중지" : rs === "idle" ? "녹화 시작" : `${REC_UI[rs]?.[2] ?? rs}…`;
  const lost = (rec.missing_cycles || 0) + (rec.skipped_cycles || 0);
  const lostPct = lost ? ` · 누락 ${lost}주기 (${fmt(100 * lost / (lost + (rec.frames || 0)), 1)}%)` : "";
  let meta = rec.session ? `${mmss(rec.elapsed_s || 0)} · ${rec.frames ?? 0} 프레임${lostPct}` : "";
  if (rs === "transferring" && rec.transfer) {
    const t = rec.transfer, pct = t.total ? 100 * t.sent / t.total : 0;
    meta = `Jetson → DGX ${fmt(pct, 0)}% (${gb(t.sent)} / ${gb(t.total)} GB)` + (t.error ? ` · 재시도 중: ${t.error}` : "");
  } else if ((rs === "converting" || rs === "validating") && rec.total_frames) {
    meta = `${rec.frames ?? 0} / ${rec.total_frames} 프레임 변환`;
  }
  $("#rec-meta").textContent = meta;
  const parts = [];
  if (rec.synthetic) parts.push("합성 센서 사용 중: 녹화는 학습용 폴더가 아닌 곳에 _SYNTHETIC으로 저장됩니다");
  if (rec.message) parts.push(rec.message);
  if (rec.last_result) {
    const x = sessions.find((s) => s.session === rec.last_result.session);
    parts.push(lastResultText(rec.last_result) + (x ? (x.labels ? " · 라벨 저장됨" : " · 라벨 미입력") : ""));
  }
  $("#rec-last").textContent = parts.join(" · ");
}

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }));
  if (!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function bindControls() {
  $("#btn-control").addEventListener("click", async () => {
    const st = sources.arms?.state;
    const start = !(st === "running" || st === "starting");
    const limits = baseState ? ` (최대 ${baseState.max_linear} m/s, ${baseState.max_angular} rad/s)` : "";
    const ask = start
      ? "팔 4대가 준비 자세로 이동한 뒤 팔로워가 리더를 따라갑니다.\n" +
        `모바일 베이스도 가상 컨트롤러로 움직일 수 있게 됩니다${limits}.\n` +
        "주변에 사람, 물체, 케이블이 없는지 확인하세요.\n로봇 제어를 시작할까요?"
      : "녹화 중이면 먼저 저장합니다.\n베이스는 바로 멈추고, 팔 4대는 준비 자세를 거쳐 수면 자세로 돌아갑니다.\n로봇 제어를 중지할까요?";
    if (!confirm(ask)) return;
    const recording = !start && recState?.state === "recording" ? recState : null;
    busy.control = true; showMsg(""); renderControl();
    // State comes only from the websocket stream; a POST reply can be older than it.
    try {
      await post("/api/control", { action: start ? "start" : "stop" });
      if (recording) openLabelDialog(recording.session, recording);  // control stop also ends the recording
    } catch (e) { showMsg(`제어 ${start ? "시작" : "중지"} 실패: ${e.message}`); }
    busy.control = false; renderControl();
  });
  $("#btn-record").addEventListener("click", async () => {
    const stop = recState?.state === "recording";
    const recording = stop ? recState : null;
    busy.record = true; showMsg(""); renderControl();
    try {
      // The task text comes from the label (작업명); start with the last one this browser used.
      await post("/api/record", stop ? { action: "stop" } : { action: "start", task: store.get("teleop.task") || undefined });
      if (recording) openLabelDialog(recording.session, recording);
    } catch (e) { showMsg(`녹화 ${stop ? "중지" : "시작"} 실패: ${e.message}`); }
    busy.record = false; renderControl();
  });
}

// ---------- camera layout (position and size, per browser) ----------
const LAYOUT_KEY = "teleop.camLayout.v1";
const NARROW_PX = 900, MIN_W = 0.15, SNAP = 0.005;
let camLayout = null, layEditing = false, zTop = 3;
const snap = (v) => Math.round(v / SNAP) * SNAP;
const narrow = () => window.innerWidth <= NARROW_PX;

function defaultLayout() {
  const W = $("#cams").clientWidth || 1200, g = 12 / W, w = (1 - 2 * g) / 3;
  return Object.fromEntries(CAMS.map((c, i) => [c, { x: i * (w + g), y: 0, w, z: 1 }]));
}

function validLayout(L) {
  return L && CAMS.every((c) => L[c] && [L[c].x, L[c].y, L[c].w].every((v) => Number.isFinite(v)) && L[c].w >= MIN_W);
}

function loadLayout() {
  try { const L = JSON.parse(localStorage.getItem(LAYOUT_KEY)); if (validLayout(L)) return L; } catch { /* default */ }
  return defaultLayout();
}
function saveLayout() { try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(camLayout)); } catch { /* private mode */ } }

// Tile height depends on its width (4:3 video + info bar that may wrap): measure it in the DOM.
function tileHeight(name, wFrac, W) {
  const el = S[name].el, old = el.style.width;
  el.style.width = `${wFrac * W}px`;
  const h = el.offsetHeight;
  el.style.width = old;
  return h;
}

function applyLayout() {
  const stage = $("#cams");
  const custom = !narrow();
  stage.classList.toggle("custom", custom);
  $("#lay-edit").disabled = $("#lay-preset").disabled = $("#lay-reset").disabled = !custom;
  if (!custom) {
    stage.style.height = "";
    for (const c of CAMS) Object.assign(S[c].el.style, { left: "", top: "", width: "", zIndex: "" });
    setEditing(false);
    return;
  }
  const W = stage.clientWidth;
  let bottom = 0;
  for (const c of CAMS) {
    const L = camLayout[c], el = S[c].el;
    Object.assign(el.style, { left: `${L.x * W}px`, top: `${L.y * W}px`, width: `${L.w * W}px`, zIndex: String(L.z || 1) });
    bottom = Math.max(bottom, L.y * W + el.offsetHeight);
  }
  stage.style.height = `${Math.ceil(bottom)}px`;
}

function presetLayout(kind) {
  const W = $("#cams").clientWidth, g = 12 / W;
  if (kind === "row") return defaultLayout();
  if (kind === "stack") {
    const w = 0.62, x = (1 - w) / 2; let y = 0;
    return Object.fromEntries(CAMS.map((c) => { const r = [c, { x, y, w, z: 1 }]; y += tileHeight(c, w, W) / W + g; return r; }));
  }
  const main = `camera${kind.slice(3)}`, others = CAMS.filter((c) => c !== main);
  const mw = 0.66, sw = 1 - mw - g;
  const L = { [main]: { x: 0, y: 0, w: mw, z: 1 } };
  let y = 0;
  for (const c of others) { L[c] = { x: mw + g, y, w: sw, z: 1 }; y += tileHeight(c, sw, W) / W + g; }
  return L;
}

function setEditing(on) {
  layEditing = on;
  $("#cams").classList.toggle("editing", on);
  $("#lay-edit").setAttribute("aria-pressed", String(on));
  for (const c of CAMS) { if (on) S[c].el.tabIndex = 0; else S[c].el.removeAttribute("tabindex"); }
}

function bindLayout() {
  camLayout = loadLayout();
  const stage = $("#cams");
  $("#lay-edit").addEventListener("click", () => setEditing(!layEditing));
  $("#lay-reset").addEventListener("click", () => { camLayout = defaultLayout(); saveLayout(); applyLayout(); });
  $("#lay-preset").addEventListener("change", (e) => {
    if (!e.target.value) return;
    camLayout = presetLayout(e.target.value); e.target.value = ""; saveLayout(); applyLayout();
  });
  for (const c of CAMS) {
    const el = S[c].el, handle = $(".rs-handle", el);
    let drag = null;
    const start = (e, mode) => {
      if (!layEditing || narrow()) return;
      if (mode === "move" && (e.target.closest(".mini") || e.target === handle)) return;
      e.preventDefault(); e.stopPropagation();
      const L = camLayout[c];
      L.z = ++zTop;
      drag = { mode, x0: e.clientX, y0: e.clientY, L0: { ...L }, W: stage.clientWidth, id: e.pointerId };
      (mode === "move" ? el : handle).setPointerCapture(e.pointerId);
      el.classList.add("dragging");
      applyLayout();
    };
    const move = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const L = camLayout[c], dx = (e.clientX - drag.x0) / drag.W, dy = (e.clientY - drag.y0) / drag.W;
      if (drag.mode === "move") {
        L.x = Math.min(Math.max(0, snap(drag.L0.x + dx)), 1 - L.w);
        L.y = Math.max(0, snap(drag.L0.y + dy));
      } else {
        L.w = Math.min(Math.max(MIN_W, snap(drag.L0.w + dx)), 1 - L.x);
      }
      applyLayout();
    };
    const end = (e) => {
      if (!drag || (e.pointerId !== undefined && e.pointerId !== drag.id)) return;
      drag = null; el.classList.remove("dragging"); saveLayout();
    };
    el.addEventListener("pointerdown", (e) => start(e, "move"));
    handle.addEventListener("pointerdown", (e) => start(e, "resize"));
    for (const t of [el, handle]) {
      t.addEventListener("pointermove", move);
      t.addEventListener("pointerup", end);
      t.addEventListener("pointercancel", end);
    }
    el.addEventListener("keydown", (e) => {
      if (!layEditing || e.target !== el) return;
      const L = camLayout[c], step = e.shiftKey ? 0.05 : 0.01;
      const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
      if (moves[e.key]) {
        L.x = Math.min(Math.max(0, snap(L.x + moves[e.key][0])), 1 - L.w);
        L.y = Math.max(0, snap(L.y + moves[e.key][1]));
      } else if (e.key === "+" || e.key === "=") {
        L.w = Math.min(1 - L.x, snap(L.w + 0.02));
      } else if (e.key === "-") {
        L.w = Math.max(MIN_W, snap(L.w - 0.02));
      } else return;
      e.preventDefault(); e.stopPropagation();
      saveLayout(); applyLayout();
    });
  }
  let lastW = 0;  // re-layout on width changes only (applyLayout itself changes the height)
  new ResizeObserver((entries) => {
    const w = Math.round(entries[0].contentRect.width);
    if (w !== lastW) { lastW = w; applyLayout(); }
  }).observe(stage);
  window.addEventListener("resize", applyLayout);
  applyLayout();
}

// ---------- virtual base controller ----------
let baseState = null, myId = null;
const joy = { pointer: false, keys: new Set(), vx: 0, wz: 0, timer: null, busy: false };
const KEYMAP = { w: [1, 0], arrowup: [1, 0], s: [-1, 0], arrowdown: [-1, 0], a: [0, 1], arrowleft: [0, 1], d: [0, -1], arrowright: [0, -1] };

function wsSend(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
function speedScale() { return Number($("#joy-speed").value) / 100; }

function renderBase() {
  const b = baseState, pad = $("#joy-pad");
  const float = $("#joy-float"), wasHidden = float.hidden;
  float.hidden = !b;  // this server has no base controller
  if (!b) { pad.classList.add("disabled"); releaseInputs(); return; }
  if (wasHidden) window.dispatchEvent(new Event("resize"));  // place it now that it has a size
  const armsRunning = sources.arms?.state === "running";
  const mine = b.owner === myId, other = !!b.owner && !mine, usable = b.enabled && armsRunning && !other;
  setStatus($("#base-state"), !b.enabled ? ["off", "–", "꺼짐"] : !armsRunning ? ["warning", "…", "팔 준비 중"]
    : other ? ["warning", "!", "다른 사용자 조작 중"] : b.active && mine ? ["good", "✓", "조작 중"] : ["good", "✓", "켜짐"]);
  pad.classList.toggle("disabled", !usable);
  $("#joy-read").textContent = b.enabled
    ? `명령 vx ${fmt(b.cmd[0], 2)} m/s · wz ${fmt(b.cmd[1], 2)} rad/s · 최대 ${b.max_linear} m/s, ${b.max_angular} rad/s`
    : "로봇 제어를 시작하면 사용할 수 있습니다.";
  if (!usable || $("#joy-body").hidden) releaseInputs();
}

function setKnob(nx, ny) { $("#joy-knob").style.transform = `translate(calc(-50% + ${nx * 115}%), calc(-50% + ${ny * 115}%))`; }

function updateFromKeys() {
  let f = 0, t = 0;
  for (const k of joy.keys) { f += KEYMAP[k][0]; t += KEYMAP[k][1]; }
  f = Math.max(-1, Math.min(1, f)); t = Math.max(-1, Math.min(1, t));
  joy.vx = f * baseState.max_linear * speedScale(); joy.wz = t * baseState.max_angular * speedScale();
  setKnob(-t, -f);
}

function holding() { return joy.pointer || joy.keys.size > 0; }
function joyTick() { if (holding() && baseState?.enabled) wsSend({ type: "base_cmd", vx: joy.vx, wz: joy.wz }); }
function startHold() { $("#joy-pad").classList.add("held"); joyTick(); if (!joy.timer) joy.timer = setInterval(joyTick, 50); }
function endHold() {
  if (holding()) return;
  clearInterval(joy.timer); joy.timer = null; joy.vx = joy.wz = 0;
  $("#joy-pad").classList.remove("held"); setKnob(0, 0);
  wsSend({ type: "base_release" });
}
function releaseInputs() { const was = holding() || joy.timer; joy.pointer = false; joy.keys.clear(); if (was) endHold(); }

function bindBase() {
  const pad = $("#joy-pad");
  const move = (e) => {
    const r = pad.getBoundingClientRect(), rad = r.width / 2;
    let nx = (e.clientX - r.left - rad) / rad, ny = (e.clientY - r.top - rad) / rad;
    const len = Math.hypot(nx, ny); if (len > 1) { nx /= len; ny /= len; }
    joy.vx = -ny * baseState.max_linear * speedScale(); joy.wz = -nx * baseState.max_angular * speedScale();
    setKnob(nx, ny);
  };
  pad.addEventListener("pointerdown", (e) => {
    if (!baseState?.enabled || pad.classList.contains("disabled")) return;
    e.preventDefault();
    pad.setPointerCapture(e.pointerId); joy.pointer = true; move(e); startHold();
  });
  pad.addEventListener("pointermove", (e) => { if (joy.pointer) move(e); });
  for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) {
    pad.addEventListener(ev, () => { if (joy.pointer) { joy.pointer = false; endHold(); } });
  }
  const typing = (e) => e.target.closest?.("input, textarea, select") || e.ctrlKey || e.altKey || e.metaKey;
  window.addEventListener("keydown", (e) => {
    const k = e.key.toLowerCase();
    if (!(k in KEYMAP) || !baseState?.enabled || typing(e) || pad.classList.contains("disabled") || $("#joy-body").hidden) return;
    e.preventDefault();
    if (!joy.keys.has(k)) { joy.keys.add(k); updateFromKeys(); startHold(); }
  });
  window.addEventListener("keyup", (e) => {
    const k = e.key.toLowerCase();
    if (!joy.keys.delete(k)) return;
    if (joy.keys.size) updateFromKeys(); else endHold();
  });
  // Dead-man: losing focus or hiding the tab stops the base.
  window.addEventListener("blur", releaseInputs);
  document.addEventListener("visibilitychange", () => { if (document.hidden) releaseInputs(); });
  $("#joy-speed").addEventListener("input", (e) => { $("#joy-speed-val").textContent = `${e.target.value}%`; if (joy.keys.size) updateFromKeys(); });
}

// Floating controller: drag by its header, fold, transparency. Kept per browser.
const FLOAT_KEY = "teleop.joyFloat.v2";
function bindJoyFloat() {
  const box = $("#joy-float"), head = $("#joy-drag"), body = $("#joy-body"), fold = $("#joy-min"), alpha = $("#joy-alpha");
  let st = {};
  try { st = JSON.parse(localStorage.getItem(FLOAT_KEY)) || {}; } catch { /* default */ }
  const save = () => { try { localStorage.setItem(FLOAT_KEY, JSON.stringify(st)); } catch { /* private mode */ } };
  const M = 8;
  // Top-left corner in px (folding keeps the header in place); kept inside the window. Default: bottom right.
  const view = document.documentElement;  // client size excludes the scrollbars
  const clampX = (x) => Math.min(Math.max(M, x), Math.max(M, view.clientWidth - box.offsetWidth - M));
  const clampY = (y) => Math.min(Math.max(M, y), Math.max(M, view.clientHeight - box.offsetHeight - M));
  const place = () => {
    box.style.left = `${clampX(Number.isFinite(st.x) ? st.x : Infinity)}px`;
    box.style.top = `${clampY(Number.isFinite(st.y) ? st.y : Infinity)}px`;
  };
  const setAlpha = (t) => {
    st.alpha = Math.min(80, Math.max(0, Number(t) || 0));
    alpha.value = String(st.alpha); $("#joy-alpha-val").textContent = `${st.alpha}%`;
    box.style.opacity = String(1 - st.alpha / 100);
  };
  const setFold = (folded) => {
    st.min = folded; body.hidden = folded;
    fold.textContent = folded ? "펼치기" : "접기"; fold.setAttribute("aria-expanded", String(!folded));
    if (folded) releaseInputs();
    place();
  };
  let drag = null;
  head.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button")) return;
    e.preventDefault();
    drag = { id: e.pointerId, dx: e.clientX - box.offsetLeft, dy: e.clientY - box.offsetTop };
    head.setPointerCapture(e.pointerId); box.classList.add("dragging");
  });
  head.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    st.x = clampX(e.clientX - drag.dx); st.y = clampY(e.clientY - drag.dy);
    place();
  });
  for (const ev of ["pointerup", "pointercancel"]) {
    head.addEventListener(ev, () => { if (drag) { drag = null; box.classList.remove("dragging"); save(); } });
  }
  fold.addEventListener("click", () => { setFold(!st.min); save(); });
  alpha.addEventListener("input", () => { setAlpha(alpha.value); save(); });
  window.addEventListener("resize", place);
  setAlpha(st.alpha ?? 15);
  setFold(!!st.min);
}

// ---------- labels ----------
// Labels are entered only in the pop-up: it opens when this browser stops a recording, or from the
// "라벨" button next to the last recording (with a list of recent sessions).
let sessions = [];
let lastSeenResult = null;
const store = { get(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
                set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } } };

function sessionLabel(x) {
  const lab = x.labels ? `라벨: ${x.labels.outcome === "success" ? "성공" : "실패"}` : "라벨 미입력";
  const st = x.status === "completed" ? "" : ` · 녹화 ${x.status === "failed" ? "실패" : x.status}`;
  return `${x.session} · ${x.frames ?? 0} 프레임${st} · ${lab}`;
}

async function loadSessions() {
  try {
    const r = await fetch("/api/sessions?limit=30");
    sessions = (await r.json()).sessions || [];
  } catch { return; }
  if (dialogMode === "edit") fillSessionSelect($("#ld-session").value);
  renderControl();
}

function renderLabelButton() {
  const btn = $("#btn-labels"), r = recState?.last_result;
  const last = r && sessions.find((x) => x.session === r.session);
  btn.hidden = sessions.length === 0;
  btn.textContent = last && !last.labels ? "라벨 입력" : "라벨 보기·수정";
}

function onRecordState(m) {
  if (dialogPending && sessionFinal(dialogPending.session)) submitDialogLabel();
  if (dialogMode === "after-stop" && $("#label-dialog").open && sessionFinal(dialogSession)) {
    const r = m.last_result, g = r.capture_gaps, lost = g ? g.missing_cycles + g.skipped_cycles : 0;
    $("#ld-summary").textContent = `${r.session} · ${fmt(r.elapsed_s)}초 · ${r.frames} 프레임` +
      (lost ? ` · 누락 ${lost}주기` : "") + (r.validation ? (r.validation.passed ? " · 검증 통과" : " · 검증 실패") : "");
    if (!dialogPending) ldMsg("녹화 저장이 끝났습니다.");
  }
  const r = m?.last_result;
  if (!r || r.session === lastSeenResult) return;
  lastSeenResult = r.session;
  loadSessions();
}

// Label pop-up after the operator stops a recording. The session is still being saved at that
// moment; a label submitted before saving ends is kept and sent as soon as the session is final.
let dialogSession = null, dialogPending = null, dialogMode = null, ldDirty = false;  // mode: after-stop | edit

function ldMsg(text, err = false) { const m = $("#ld-msg"); m.textContent = text; m.classList.toggle("err", err); }

function openLabelDialog(session, rec) {
  if (!session) return;
  dialogSession = session; dialogPending = null; dialogMode = "after-stop"; ldDirty = false;
  $("#ld-pick").hidden = true;
  for (const r of document.querySelectorAll('input[name="ld-outcome"]')) r.checked = false;
  $("#ld-task").value = store.get("teleop.task") || rec?.task || "";
  $("#ld-env").value = store.get("teleop.env");
  $("#ld-op").value = store.get("teleop.operator");
  $("#ld-notes").value = "";
  $("#ld-save").disabled = false;
  $("#ld-summary").textContent = `${session} · ${mmss(rec?.elapsed_s || 0)} · ${rec?.frames ?? 0} 프레임`;
  ldMsg("녹화를 저장하고 DGX로 옮기는 중입니다. 지금 라벨을 저장하면 끝나는 대로 반영됩니다.");
  const d = $("#label-dialog");
  if (!d.open) d.showModal();
  $("#ld-task").focus();
}

// Edit mode: any finished session, with its saved labels.
function fillSessionSelect(keep) {
  const sel = $("#ld-session");
  sel.innerHTML = sessions.map((x) => `<option value="${x.session}">${sessionLabel(x)}</option>`).join("");
  if (keep && sessions.some((x) => x.session === keep)) sel.value = keep;
}

function fillDialog(name) {
  const x = sessions.find((s) => s.session === name), L = x?.labels || {};
  for (const r of document.querySelectorAll('input[name="ld-outcome"]')) r.checked = r.value === L.outcome;
  $("#ld-task").value = L.task_name ?? x?.task ?? "";
  $("#ld-env").value = L.environment ?? store.get("teleop.env");
  $("#ld-op").value = L.operator ?? store.get("teleop.operator");
  $("#ld-notes").value = L.notes ?? "";
  $("#ld-summary").textContent = !x ? "" : x.labels
    ? `저장됨 ${x.labels.labeled_at.slice(0, 19).replace("T", " ")} · 수정 ${x.labels.revision}회` : "라벨 미입력";
  ldDirty = false; ldMsg("");
}

function openLabelEditor(name) {
  if (!sessions.length) return;
  dialogMode = "edit"; dialogSession = null; dialogPending = null;
  fillSessionSelect(name || sessions[0].session);
  $("#ld-pick").hidden = false;
  fillDialog($("#ld-session").value);
  $("#ld-save").disabled = false;
  const d = $("#label-dialog");
  if (!d.open) d.showModal();
  $("#ld-session").focus();
}

// The label pop-up knows the Jetson session name; the DGX result may carry a different final name.
function finalName(session) {
  const r = recState?.last_result;
  return recState?.state === "idle" && r && (r.session === session || r.source_session === session) ? r.session : null;
}
function sessionFinal(session) { return finalName(session) !== null; }

async function submitDialogLabel() {
  const body = dialogPending;
  if (!body) return;
  if (dialogMode !== "edit") {  // a session from the editor list is already final
    if (!sessionFinal(body.session)) { ldMsg("녹화 저장 중입니다. 끝나면 자동으로 라벨을 저장합니다."); return; }
    body.session = finalName(body.session);
  }
  dialogPending = null;
  try {
    await post("/api/label", body);
    store.set("teleop.task", body.task_name); store.set("teleop.env", body.environment);
    store.set("teleop.operator", body.operator);
    $("#label-dialog").close();
    showMsg("");
    await loadSessions();
  } catch (err) {
    $("#ld-save").disabled = false;
    ldMsg(`라벨 저장 실패: ${err.message}`, true);
  }
}

function bindLabelDialog() {
  $("#ld-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const outcome = document.querySelector('input[name="ld-outcome"]:checked')?.value;
    if (!outcome) { ldMsg("결과(성공/실패)를 선택하세요.", true); return; }
    if (!$("#ld-task").value.trim()) { ldMsg("작업명을 입력하세요.", true); return; }
    const session = dialogMode === "edit" ? $("#ld-session").value : dialogSession;
    dialogPending = { session, outcome, task_name: $("#ld-task").value.trim(),
                      environment: $("#ld-env").value, operator: $("#ld-op").value, notes: $("#ld-notes").value };
    $("#ld-save").disabled = true;
    submitDialogLabel();
  });
  $("#ld-form").addEventListener("input", (e) => { if (e.target.id !== "ld-session") ldDirty = true; });
  $("#ld-session").addEventListener("change", (e) => {
    if (ldDirty && !confirm("저장하지 않은 라벨 입력이 있습니다. 버리고 다른 세션을 열까요?")) {
      e.target.value = e.target.dataset.shown; return;
    }
    fillDialog(e.target.value);
  });
  $("#ld-session").addEventListener("focus", (e) => { e.target.dataset.shown = e.target.value; });
  $("#btn-labels").addEventListener("click", () => openLabelEditor(recState?.last_result?.session));
  $("#ld-later").addEventListener("click", () => { dialogPending = null; $("#label-dialog").close(); });
  $("#label-dialog").addEventListener("close", () => { dialogPending = null; dialogMode = null; ldDirty = false; });
}

// ---------- latency table + sparklines ----------
function sourceOf(name) { return sources[name] || (ARM_SENSORS.includes(name) ? sources.arms : null); }
function statusOf(name, e2e) {
  const st = S[name], src = sourceOf(name);
  if (!src) return ["off", "–", "꺼짐"];
  if (src.state === "error") return ["critical", "!", "오류"];
  if (src.state === "idle" || src.state === "stopped") return ["off", "–", "정지"];
  if (src.state === "starting") return ["warning", "…", "연결 중"];
  if (src.state === "stopping") return ["warning", "…", "정지 중"];
  if (!st.last || wallNow() - st.last > STALE_MS) return ["critical", "!", "수신 없음"];
  if (!e2e) return ["warning", "?", "측정 중"];
  if (e2e.p95 < THR.good) return ["good", "✓", "정상"];
  if (e2e.p95 < THR.warn) return ["warning", "!", "주의"];
  return ["critical", "!", "위험"];
}

function tick() {
  const report = {};
  for (const name of SENSORS) {
    const st = S[name], row = st.row;
    const e2e = st.e2e.stats(), net = st.network.stats(), ren = st.render.stats();
    const hz = st.arrivals.rate();
    const sv = st.server?.stages || {};
    const [s, icon, label] = statusOf(name, e2e);
    const badge = $(".status", row);
    badge.dataset.s = s; $("i", badge).textContent = icon; $("span", badge).textContent = label;
    const set = (k, v) => { $(`[data-k="${k}"]`, row).textContent = v; };
    set("hz", fmt(hz)); set("sensor", fmt(sv.sensor_ms?.p50)); set("server", fmt(sv.server_ms?.p50));
    set("network", fmt(net?.p50)); set("render", fmt(ren?.p50));
    set("p50", fmt(e2e?.p50)); set("p95", fmt(e2e?.p95)); set("max", fmt(e2e?.max));
    set("drop", st.server ? String(st.server.dropped) : "–");
    st.history.push(e2e && st.last && wallNow() - st.last < STALE_MS ? e2e.p50 : null);
    if (st.history.length > HISTORY_S) st.history.shift();
    drawSpark(name);
    if (CAMS.includes(name) && st.el) {
      const up = sources[name]?.info?.unpaired_recent_pct;
      $(".fps", st.el).textContent = `${fmt(hz)} fps` + (up > 2 ? ` · ⚠ depth 짝 불일치 ${fmt(up, 0)}%` : "");
      $(".lat", st.el).textContent = fmt(e2e?.p50);
    }
    report[name] = { rate_hz: hz, drawn: st.drawn, network_ms: net, render_ms: ren, e2e_ms: e2e, status: label };
  }
  const L = (side) => fmt(S[`leader_${side}`].arrivals.rate(), 0), F = (side) => fmt(S[`follower_${side}`].arrivals.rate(), 0);
  for (const el of document.querySelectorAll(".arm")) {
    $('[data-k="rate"]', el).textContent = `리더 ${L(el.dataset.side)} Hz · 팔로워 ${F(el.dataset.side)} Hz`;
  }
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({ type: "client_report", t_ns: serverNow() * 1e6, offset_ms: clock.offset, rtt_ms: clock.rtt, sensors: report }));
  }
}
setInterval(tick, 1000);

function drawSpark(name) {
  const st = S[name], c = $("canvas", st.row);
  const { ctx, w, h } = fitCanvas(c);
  ctx.clearRect(0, 0, w, h);
  const vals = st.history;
  const top = Math.max(THR.good * 1.25, ...vals.filter((v) => v != null).map((v) => v * 1.15));
  const x = (i) => 2 + (w - 4) * (i + HISTORY_S - vals.length) / (HISTORY_S - 1);
  const y = (v) => h - 2 - (h - 4) * Math.min(v, top) / top;
  ctx.strokeStyle = css("--axis"); ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(0, y(THR.good) + 0.5); ctx.lineTo(w, y(THR.good) + 0.5); ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = css("--series-1"); ctx.lineWidth = 2; ctx.lineJoin = "round";
  ctx.beginPath();
  let pen = false;
  vals.forEach((v, i) => { if (v == null) { pen = false; return; } pen ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)); pen = true; });
  ctx.stroke();
  if (st.hover != null && vals[st.hover] != null) {
    ctx.strokeStyle = css("--muted"); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x(st.hover), 0); ctx.lineTo(x(st.hover), h); ctx.stroke();
    ctx.fillStyle = css("--series-1"); ctx.strokeStyle = css("--surface"); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(x(st.hover), y(vals[st.hover]), 4, 0, 7); ctx.fill(); ctx.stroke();
  }
}

function sparkHover(name, c, e) {
  const st = S[name], r = c.getBoundingClientRect(), vals = st.history;
  const slot = Math.round((e.clientX - r.left - 2) / (r.width - 4) * (HISTORY_S - 1));
  const i = slot - (HISTORY_S - vals.length);
  const tip = $("#tooltip");
  if (i < 0 || i >= vals.length) { tip.hidden = true; st.hover = null; drawSpark(name); return; }
  st.hover = i;
  drawSpark(name);
  const ago = vals.length - 1 - i;
  tip.textContent = `${LABELS[name]} · ${ago ? `${ago}초 전` : "현재"} · ${vals[i] == null ? "데이터 없음" : `종단 p50 ${fmt(vals[i])} ms`}`;
  tip.hidden = false;
  tip.style.left = `${Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8)}px`;
  tip.style.top = `${e.clientY - 34}px`;
}

buildCams(); buildArms(); buildOdom(); buildLatency(); bindControls(); bindLabelDialog(); bindBase(); bindJoyFloat(); bindLayout();
renderControl(); renderBase(); loadSessions();
connect();
