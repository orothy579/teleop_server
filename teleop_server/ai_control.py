"""Web "AI 제어": list the models under the inference folder, keep one loaded and start/stop runs.

Every model is a folder ``<INFERENCE_ROOT>/<id>/`` with ``inference.yaml``::

    name: pi05                          # optional (default: the folder name)
    serve: [bash, serve.sh]             # keep-loaded worker, run in the model folder
    config: config.yaml                 # optional: execution.task / execution.seconds defaults
    checkpoints: "models/*"             # optional: folders offered as checkpoints (--model <path>)
    logs: outputs                       # optional: run logs (robot_run_*.jsonl) listed and downloadable

Every error (a run that ends with an error, a failed model load, a worker that exits abnormally) is
saved as one self-contained report in ``<logs>/errors/ai_error_<time>_<kind>.json``: message,
traceback, the run and its last log events, the worker's own log and the robot/sensor state when the
error was seen. The browser lists them and offers each for download.

The worker runs as a transient systemd user unit ``teleop-inference-<id>`` (it survives web-server
restarts, so the model stays loaded; logs: ``journalctl --user -u teleop-inference-<id>``) and serves
the worker API on a Unix socket given in $TELEOP_INFERENCE_SOCKET: GET /status, POST /run {task,
seconds, execute}, POST /stop, POST /shutdown. See <INFERENCE_ROOT>/README.md.
"""
import asyncio
import io
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)
INFERENCE_ROOT = Path(os.environ.get("TELEOP_INFERENCE_ROOT", "/home/midbar/ws/inference"))
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
SOCKET_DIR = RUNTIME_DIR / "teleop-inference"
MANIFEST = "inference.yaml"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ACTIVE = ("loading", "ready", "running", "stopping")
LOG_GLOB = "robot_run_*.jsonl"
RUNS_LISTED = 100  # run logs shown (and selectable) in the browser
ERRORS_LISTED = 50  # error reports likewise (sent with every status update)
BUNDLE_MAX = 300  # files in one downloaded zip
LOG_RE = re.compile(r"^robot_run_\d{8}_\d{6}(_\d+)?\.jsonl$")
ERROR_RE = re.compile(r"^ai_error_\d{8}_\d{6}_(run|load|crash)(_\d+)?\.json$")
ERROR_TITLES = {"run": "AI 제어가 오류로 끝났습니다", "load": "모델을 불러오지 못했습니다", "crash": "AI 워커가 비정상 종료됨"}


class AIError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def _yaml(path):
    import yaml
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else {}


def checkpoint_info(folder, rel):
    """How a checkpoint is shown: training run name (train_config.json job_name), step (folder name)
    and when training saved the weights (file time of model.safetensors, kept when copied)."""
    path = folder / rel
    job = None
    try:
        job = json.loads((path / "train_config.json").read_text()).get("job_name")
    except (OSError, ValueError):
        pass
    weights = path / "model.safetensors"
    files = [weights] if weights.is_file() else [p for p in path.iterdir() if p.is_file()]
    saved = max((p.stat().st_mtime for p in files), default=None)
    stamp = time.strftime("%m-%d %H:%M", time.localtime(saved)) if saved else None
    label = " · ".join(x for x in (job, path.name, stamp) if x)
    return {"path": rel, "job": job, "step": path.name, "saved_unix": saved, "label": label}


def discover(root=None):
    """Every folder with an inference.yaml, as the browser shows it (invalid ones with an error)."""
    root = Path(root or INFERENCE_ROOT)
    models = []
    if not root.is_dir():
        return models
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        manifest = folder / MANIFEST
        if not manifest.is_file():
            continue
        entry = {"id": folder.name, "path": str(folder), "name": folder.name, "error": None, "checkpoints": [],
                 "checkpoint_info": [], "default_checkpoint": None, "defaults": {}, "log_dir": str(folder / "outputs")}
        models.append(entry)
        try:
            if not ID_RE.match(folder.name):
                raise ValueError("폴더 이름은 영문·숫자·_.-만 쓸 수 있습니다.")
            m = _yaml(manifest)
            serve = m.get("serve")
            if not isinstance(serve, list) or not serve or not all(isinstance(x, str) and x for x in serve):
                raise ValueError("serve: 실행 명령 목록이 필요합니다 (예: [bash, serve.sh]).")
            entry.update(name=str(m.get("name") or folder.name), serve=serve,
                         log_dir=str(folder / str(m.get("logs") or "outputs")))
            pattern = m.get("checkpoints")
            if pattern:
                entry["checkpoints"] = sorted(str(p.relative_to(folder)) for p in folder.glob(str(pattern)) if p.is_dir())
                entry["checkpoint_info"] = [checkpoint_info(folder, c) for c in entry["checkpoints"]]
            config = folder / str(m.get("config") or "")
            if m.get("config") and config.is_file():
                c = _yaml(config)
                execution, model = c.get("execution") or {}, c.get("model") or {}
                entry["defaults"] = {k: execution[k] for k in ("task", "seconds") if k in execution}
                configured = str(model.get("path") or "")
                if configured in entry["checkpoints"]:
                    entry["default_checkpoint"] = configured
            if entry["checkpoints"] and not entry["default_checkpoint"]:
                entry["default_checkpoint"] = entry["checkpoints"][-1]
        except Exception as exc:
            entry["error"] = f"{MANIFEST}: {exc}"
    return models


def _tail_lines(path, size=65536):
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - size))
        return f.read().decode(errors="replace").splitlines()


def run_summary(path):
    """One run log -> what the browser lists (the "end" event, or the last error for older logs)."""
    out = {"file": path.name, "bytes": path.stat().st_size, "modified_unix": path.stat().st_mtime,
           "status": "unknown", "error": None, "task": None, "execute": None}
    try:
        with open(path) as f:
            first = json.loads(f.readline() or "{}")
        out.update(task=first.get("task") or (first.get("settings") or {}).get("execution", {}).get("task"),
                   execute=first.get("execute"))
    except (OSError, ValueError):
        pass
    events = []
    for line in _tail_lines(path):
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    end = next((e for e in reversed(events) if e.get("event") == "end"), None)
    if end:
        out.update({k: end.get(k) for k in ("status", "error", "chunks", "elapsed_s", "stalls", "stall_s", "execute", "task")})
    else:  # logs written before the "end" event existed
        err = next((e for e in reversed(events) if e.get("event") == "error"), None)
        chunks = max((e.get("chunk", 0) for e in events if e.get("event") == "execution"), default=0)
        out.update(status="error" if err else "unknown", error=err and err.get("error"), chunks=chunks)
    return out


def run_log_tail(path, events=30):
    """The settings and the last events of a run log, without the predicted action arrays."""
    try:
        with open(path) as f:
            first = json.loads(f.readline() or "{}")
        tail = []
        for line in _tail_lines(path, 262144)[1:]:
            try:
                tail.append(json.loads(line))
            except ValueError:
                continue
    except (OSError, ValueError) as exc:
        return {"file": Path(path).name, "error": str(exc)}
    tail = [{k: v for k, v in e.items() if k != "actions"} for e in tail[-events:]]
    return {"file": Path(path).name, "config": first if first.get("event") == "config" else None, "last_events": tail}


class ErrorLog:
    """Error reports of one model, one JSON file each in <logs>/errors/ (newest last in ``entries``)."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.entries = None  # key -> summary; read from the folder once, then kept up to date

    @staticmethod
    def summary(name, report):
        return {"file": name, "key": report.get("key"), "kind": report.get("kind"),
                "time_unix": report.get("time_unix"), "message": report.get("message")}

    def _entries(self):
        if self.entries is None:
            self.entries = {}
            for path in sorted(self.folder.glob("ai_error_*.json")) if self.folder.is_dir() else []:
                if not ERROR_RE.match(path.name):
                    continue
                try:
                    report = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                self.entries[report.get("key") or path.name] = self.summary(path.name, report)
        return self.entries

    def has(self, key):
        return key in self._entries()

    def write(self, report):
        entries = self._entries()
        self.folder.mkdir(parents=True, exist_ok=True)
        base = f"ai_error_{time.strftime('%Y%m%d_%H%M%S', time.localtime(report['time_unix']))}_{report['kind']}"
        for n in itertools.count():
            path = self.folder / (base + (f"_{n}" if n else "") + ".json")
            try:
                with open(path, "x") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2, default=str)
                break
            except FileExistsError:
                continue
        entries[report["key"]] = self.summary(path.name, report)
        return path

    def recent(self, limit=20):
        return list(self._entries().values())[-limit:][::-1]

    def path(self, name):
        if not isinstance(name, str) or not ERROR_RE.match(name):
            raise AIError("잘못된 오류 기록 이름입니다.", 400)
        path = self.folder / name
        if not path.is_file():
            raise AIError("오류 기록을 찾을 수 없습니다.", 404)
        return path


def current_errors(model):
    """What is wrong with a model right now, each with a key that names this one occurrence."""
    out, w = [], model.get("worker") or {}
    if model.get("state") == "error" and w:
        out.append({"kind": "load", "key": f"load:{model['id']}:{w.get('load_started_unix')}",
                    "message": w.get("error") or "모델 오류"})
    if model.get("state") == "failed":
        unit = model.get("unit_info") or {}
        out.append({"kind": "crash", "key": f"crash:{model['id']}:{unit.get('invocation') or unit.get('exit_time')}",
                    "message": unit.get("summary") or "워커가 비정상 종료했습니다."})
    run = w.get("last_run") or {}
    if run.get("status") == "error" or (not run.get("status") and run.get("error")):
        out.append({"kind": "run", "key": f"run:{model['id']}:{run.get('finished_unix')}",
                    "message": run.get("error") or "오류"})
    return out


def run_log_path(model, name):
    """A run log of the model, by its listed name only (no other files)."""
    if not isinstance(name, str) or not LOG_RE.match(name):
        raise AIError("잘못된 로그 이름입니다.", 400)
    path = Path(model["log_dir"]) / name
    if not path.is_file():
        raise AIError(f"로그를 찾을 수 없습니다: {name}", 404)
    return path


def list_runs(model, limit=RUNS_LISTED):
    folder = Path(model["log_dir"])
    if not folder.is_dir():
        return []
    files = sorted(folder.glob(LOG_GLOB), key=lambda p: p.name, reverse=True)[:limit]
    return [run_summary(p) for p in files if LOG_RE.match(p.name)]


class SystemdLauncher:
    """Workers as transient systemd user units: independent of the web server's lifetime."""

    def __init__(self):
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(RUNTIME_DIR))

    @staticmethod
    def unit(model_id):
        return f"teleop-inference-{model_id}"

    def _run(self, *args, timeout=30):
        return subprocess.run(list(args), env=self.env, capture_output=True, text=True, timeout=timeout)

    def start(self, model, checkpoint, socket):
        argv = list(model["serve"])
        argv[0] = shutil.which(argv[0]) or argv[0]  # systemd needs an absolute executable path
        if checkpoint:
            argv += ["--model", str(Path(model["path"]) / checkpoint)]
        unit = self.unit(model["id"])
        self._run("systemctl", "--user", "reset-failed", unit)
        # No --collect: a worker that exits abnormally stays "failed" (with its exit status) until the
        # next load or unload, so the browser can report it. Clean exits are unloaded as usual.
        r = self._run("systemd-run", "--user", f"--unit={unit}", "--quiet",
                      f"--description=teleop AI worker {model['id']}", "-p", f"WorkingDirectory={model['path']}",
                      "-p", "KillSignal=SIGTERM", "-p", "TimeoutStopSec=30",
                      f"--setenv=TELEOP_INFERENCE_SOCKET={socket}", *argv)
        if r.returncode != 0:
            raise AIError(f"워커를 시작하지 못했습니다: {(r.stderr or r.stdout).strip()[-300:]}")

    def stop(self, model_id):
        self._run("systemctl", "--user", "stop", self.unit(model_id), timeout=60)
        self._run("systemctl", "--user", "reset-failed", self.unit(model_id))

    def state(self, model_id):
        return self._run("systemctl", "--user", "is-active", self.unit(model_id)).stdout.strip() or "inactive"

    def unit_info(self, model_id):
        """How a failed worker ended (systemd's record of this run of the unit)."""
        r = self._run("systemctl", "--user", "show", self.unit(model_id), "-p", "InvocationID", "-p", "Result",
                      "-p", "ExecMainCode", "-p", "ExecMainStatus", "-p", "ExecMainExitTimestamp")
        props = dict(line.partition("=")[::2] for line in r.stdout.splitlines() if "=" in line)
        code = {"1": "exited", "2": "killed", "3": "dumped"}.get(props.get("ExecMainCode"), props.get("ExecMainCode"))
        return {"invocation": props.get("InvocationID") or None, "result": props.get("Result"),
                "exit": f"{code} {props.get('ExecMainStatus')}", "exit_time": props.get("ExecMainExitTimestamp") or None,
                "summary": f"워커가 비정상 종료했습니다 ({props.get('Result')}, {code} {props.get('ExecMainStatus')})"}

    def logs(self, model_id, lines=40):
        r = self._run("journalctl", "--user", "-u", self.unit(model_id), "-n", str(lines), "--no-pager", "-o", "cat")
        return r.stdout.splitlines()[-lines:]


class AIControl:
    """Discovery + worker processes + status pushed to browsers (hub message "ai")."""

    def __init__(self, publish=lambda status: None, launcher=None, root=None, fallback_stop=None, context=None,
                 return_arms=None):
        self.publish_cb, self.root = publish, root
        self.launcher = launcher or SystemdLauncher()
        self.fallback_stop = fallback_stop  # async: release the arms if no worker answers
        self.return_arms = return_arms      # async: all arms to the staged pose, then teleop again
        self.return_after = {}              # model id -> requested_unix of a run that returns when it ends
        self.auto_return = None             # the last automatic return: {time_unix, ok, error}
        self.context = context  # robot/sensor state saved with each error report
        self.error_logs = {}    # log dir -> ErrorLog
        self.noting = set()     # error keys being written, or that could not be written (not retried)
        self.checkpoints = {}  # model id -> checkpoint the worker was started with
        self.loading = {}      # model id -> monotonic time the load was requested
        self.snapshot = {"models": []}
        self.task = None

    def socket(self, model_id):
        return SOCKET_DIR / f"{model_id}.sock"

    async def _worker(self, model_id, method, path, body=None, timeout=3.0):
        sock = self.socket(model_id)
        if not sock.exists():
            return None
        try:
            connector = aiohttp.UnixConnector(path=str(sock))
            async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                async with s.request(method, "http://worker" + path, json=body) as r:
                    return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return None

    def _model(self, model_id):
        model = next((m for m in discover(self.root) if m["id"] == model_id), None)
        if model is None:
            raise AIError("모델을 찾을 수 없습니다.", 404)
        if model["error"]:
            raise AIError(model["error"])
        return model

    # ---------- status ----------
    async def refresh(self):
        models = await asyncio.to_thread(discover, self.root)
        states = await asyncio.gather(*(asyncio.to_thread(self.launcher.state, m["id"]) for m in models))
        workers = await asyncio.gather(*(self._worker(m["id"], "GET", "/status", timeout=1.5)
                                         if s in ("active", "activating") else asyncio.sleep(0, None)
                                         for m, s in zip(models, states)))
        for m, unit, w in zip(models, states, workers):
            m["unit"], m["worker"] = unit, w
            m["checkpoint"] = self.checkpoints.get(m["id"])
            if w and w.get("model_path"):  # the worker knows what it loaded, also after a web-server restart
                try:
                    m["checkpoint"] = Path(w["model_path"]).relative_to(m["path"]).as_posix()
                except ValueError:
                    m["checkpoint"] = w["model_path"]
                self.checkpoints[m["id"]] = m["checkpoint"]
            if w:
                m["state"] = w["state"]
                self.loading.pop(m["id"], None)
            elif unit in ("active", "activating"):
                m["state"] = "starting"
            elif unit == "failed":
                m["state"] = "failed"
                m["unit_info"] = await asyncio.to_thread(self.launcher.unit_info, m["id"])
            else:
                m["state"] = "off"
            if m["state"] in ("starting", "loading") and m["id"] in self.loading:
                m["waiting_s"] = round(time.monotonic() - self.loading[m["id"]], 1)
            await self._note_errors(m)
            self._check_return(m)
        active = next(({"model": m["id"], "name": m["name"], "checkpoint": m.get("checkpoint"),
                        "checkpoint_label": next((c["label"] for c in m["checkpoint_info"] if c["path"] == m.get("checkpoint")),
                                                 m.get("checkpoint")), **m["worker"]["run"]}
                       for m in models if m.get("worker") and m["worker"].get("run")), None)
        self.snapshot = {"models": models, "active": active, "root": str(self.root or INFERENCE_ROOT),
                         "auto_return": self.auto_return}
        return self.snapshot

    def _check_return(self, m):
        """A run started with return_after ended on its own (time up or error): send the arms back.
        After 'AI 정지' the arms keep holding; a stop is often a reaction to something going wrong."""
        marker = self.return_after.get(m["id"])
        w = m.get("worker") or {}
        last = w.get("last_run") or {}
        if marker is None or w.get("run") or last.get("requested_unix") != marker:
            if marker is not None and m.get("unit") not in ("active", "activating"):  # worker gone: no end to wait for
                self.return_after.pop(m["id"], None)
            return
        self.return_after.pop(m["id"], None)
        if last.get("execute") and last.get("status") in ("done", "error") and not last.get("stopped_by_user"):
            asyncio.get_running_loop().create_task(self._auto_return(m["id"], last.get("status")))

    async def _auto_return(self, model_id, status):
        try:
            await self.return_arms()
            self.auto_return = {"time_unix": time.time(), "ok": True, "error": None, "model": model_id}
            log.info("AI run of %s ended (%s): arms return to the staged pose", model_id, status)
        except Exception as exc:
            self.auto_return = {"time_unix": time.time(), "ok": False, "error": str(exc) or repr(exc), "model": model_id}
            log.warning("automatic return after the AI run failed: %s", self.auto_return["error"])
        await self.refresh()
        self.publish_cb(dict(self.snapshot, type="ai"))

    def error_log(self, model):
        folder = str(Path(model["log_dir"]) / "errors")
        if folder not in self.error_logs:
            self.error_logs[folder] = ErrorLog(folder)
        return self.error_logs[folder]

    async def _note_errors(self, m):
        """Save a report the first time each error is seen, then list the reports with the model."""
        errors, elog = current_errors(m), self.error_log(m)
        for e in errors:
            if e["key"] in self.noting:
                continue
            self.noting.add(e["key"])  # before any await: concurrent refreshes write each report once
            if await asyncio.to_thread(elog.has, e["key"]):
                self.noting.discard(e["key"])
                continue
            try:
                context = None
                if self.context:
                    try:
                        context = self.context()
                    except Exception as exc:
                        context = {"error": repr(exc)}
                path = await asyncio.to_thread(self._write_report, m, e, context)
                log.warning("AI error (%s) saved to %s: %s", e["kind"], path, e["message"])
                self.noting.discard(e["key"])
            except Exception:
                log.exception("could not save the AI error report")
        reports = {r["key"]: r["file"] for r in await asyncio.to_thread(elog.recent, 200)}
        m["current_errors"] = [dict(e, file=reports.get(e["key"])) for e in errors]
        m["errors"] = elog.recent(ERRORS_LISTED)

    def _write_report(self, m, error, context):
        w = m.get("worker") or {}
        run = dict(w.get("last_run") or {}) if error["kind"] == "run" else None
        trace = run.pop("traceback", None) if run is not None else w.get("error_traceback") if error["kind"] == "load" else None
        try:
            worker_log = self.launcher.logs(m["id"], 200)
        except Exception as exc:
            worker_log = [f"(worker log unavailable: {exc!r})"]
        now = time.time()
        when = (run or {}).get("finished_unix") or now  # a run error: when the run ended
        report = {
            "kind": error["kind"], "key": error["key"], "title": ERROR_TITLES[error["kind"]], "message": error["message"],
            "time_unix": when, "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when)), "detected_unix": now,
            "model": {"id": m["id"], "name": m["name"], "path": m["path"], "checkpoint": m.get("checkpoint"),
                      "checkpoint_label": next((c["label"] for c in m.get("checkpoint_info", [])
                                                if c["path"] == m.get("checkpoint")), m.get("checkpoint"))},
            "traceback": trace.rstrip().splitlines() if trace else None,
            "run": run,
            "run_log": run_log_tail(run["log"]) if run and run.get("log") else None,
            "worker": {k: w.get(k) for k in ("state", "error", "pid", "device", "model_path", "config", "load_s",
                                             "gpu_max_gib")} if w else None,
            "unit": m.get("unit_info"),
            "worker_log": worker_log,
            "robot_when_detected": context,
        }
        return self.error_log(m).write(report)

    async def poll(self):
        last = None
        while True:
            try:
                snap = await self.refresh()
                text = json.dumps(snap, sort_keys=True, default=str)
                busy = snap["active"] or any(m["state"] in ("starting", "loading", "stopping") for m in snap["models"])
                if text != last or busy:
                    last = text
                    self.publish_cb(dict(snap, type="ai"))
                await asyncio.sleep(0.5 if busy else 2.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("AI status poll failed")
                await asyncio.sleep(2.0)

    def start_polling(self):
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self.poll())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    # ---------- actions ----------
    async def load(self, model_id, checkpoint=None):
        model = self._model(model_id)
        if checkpoint is not None and checkpoint not in model["checkpoints"]:
            raise AIError("없는 체크포인트입니다.", 400)
        checkpoint = checkpoint or model["default_checkpoint"]
        state = await asyncio.to_thread(self.launcher.state, model_id)
        if state in ("active", "activating"):
            if self.checkpoints.get(model_id) == checkpoint:
                return await self.refresh()
            await self.unload(model_id)  # a different checkpoint: reload
        self.socket(model_id).parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket(model_id).parent, 0o700)
        await asyncio.to_thread(self.launcher.start, model, checkpoint, str(self.socket(model_id)))
        self.checkpoints[model_id] = checkpoint
        self.loading[model_id] = time.monotonic()
        log.info("AI worker %s started (checkpoint %s)", model_id, checkpoint)
        return await self.refresh()

    async def unload(self, model_id):
        self._model(model_id)
        await self._worker(model_id, "POST", "/shutdown", timeout=15)
        await asyncio.to_thread(self.launcher.stop, model_id)
        self.checkpoints.pop(model_id, None)
        self.loading.pop(model_id, None)
        return await self.refresh()

    async def run(self, model_id, task, seconds, execute, return_after=False):
        self._model(model_id)
        reply = await self._worker(model_id, "POST", "/run", dict(task=task, seconds=seconds, execute=execute), timeout=10)
        if reply is None:
            raise AIError("모델이 로드되어 있지 않습니다. 먼저 '모델 로드'를 누르세요.")
        if not reply.get("ok"):
            raise AIError(reply.get("error") or "AI 제어를 시작하지 못했습니다.")
        self.return_after.pop(model_id, None)
        if return_after and execute and self.return_arms and (reply.get("run") or {}).get("requested_unix"):
            self.return_after[model_id] = reply["run"]["requested_unix"]
        log.info("AI run started: %s execute=%s seconds=%s return_after=%s", model_id, execute, seconds, return_after)
        return await self.refresh()

    async def stop(self):
        """Stop every running worker; if none answers, release the arms through the Jetson directly."""
        snap = await self.refresh()
        running = [m for m in snap["models"] if (m.get("worker") or {}).get("state") in ("running", "stopping")]
        answered = await asyncio.gather(*(self._worker(m["id"], "POST", "/stop", timeout=15) for m in running))
        if self.fallback_stop and (not running or any(a is None for a in answered)):
            try:
                await self.fallback_stop()
            except Exception:
                log.exception("fallback policy stop failed")
        return await self.refresh()

    async def logs(self, model_id):
        self._model(model_id)
        return await asyncio.to_thread(self.launcher.logs, model_id)

    async def runs(self, model_id):
        return await asyncio.to_thread(list_runs, self._model(model_id))

    def error_report(self, model_id, name):
        """Path of one saved error report, for download."""
        return self.error_log(self._model(model_id)).path(name)

    def run_log(self, model_id, name):
        """Path of one run log, for download (only files listed by ``runs``)."""
        return run_log_path(self._model(model_id), name)

    def bundle(self, model_id, runs=(), errors=()):
        """Chosen run logs and error reports of one model as one zip: (file name, bytes).

        Inside one folder: runs/<run log>, errors/<report> and index.json (each run's result and
        each error's message, so the zip explains itself)."""
        model = self._model(model_id)
        runs, errors = list(dict.fromkeys(runs or ())), list(dict.fromkeys(errors or ()))
        if not runs and not errors:
            raise AIError("받을 로그를 고르세요.", 400)
        if len(runs) + len(errors) > BUNDLE_MAX:
            raise AIError(f"한 번에 {BUNDLE_MAX}개까지 받을 수 있습니다.", 400)
        run_paths = [run_log_path(model, name) for name in runs]
        error_paths = [self.error_log(model).path(name) for name in errors]
        stamp = time.strftime("%Y%m%d_%H%M%S")
        folder = f"{model_id}_ai_logs_{stamp}"
        index = {"model": model_id, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "runs": [run_summary(p) for p in run_paths], "errors": []}
        for path in error_paths:
            try:
                index["errors"].append(ErrorLog.summary(path.name, json.loads(path.read_text())))
            except (OSError, ValueError) as exc:
                index["errors"].append({"file": path.name, "error": str(exc)})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for sub, paths in (("runs", run_paths), ("errors", error_paths)):
                for path in paths:
                    z.write(path, f"{folder}/{sub}/{path.name}")
            z.writestr(f"{folder}/index.json", json.dumps(index, ensure_ascii=False, indent=2, default=str))
        return f"{folder}.zip", buf.getvalue()
