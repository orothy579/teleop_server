"""DGX side of Jetson-local recording.

Start/stop go to the Jetson over the control link; the Jetson records locally and, after stop,
uploads the session (size + SHA-256 checked here per file). Only after every file is verified does
the commit reply let the Jetson delete its copy. The session is then converted to the usual layout
(``convert.SessionConverter``) and validated. ``status()``, ``session``, ``output_dir`` and
``last_result`` keep the interface the shared web UI and label API already use.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from teleop_server.recorder import DEFAULT_TASK, RecorderError
from .local_recorder import MANIFEST

log = logging.getLogger(__name__)
SESSION_RE = re.compile(r"^\d{8}_\d{6}(_\d{1,2})?(_SYNTHETIC)?$")
FILE_RE = re.compile(r"^(rgb_camera[1-3]\.mp4|depth_camera[1-3]\.bin|cycles\.jsonl|base_raw\.jsonl)$")


class JetsonRecordings:
    def __init__(self, bridge, output_dir, validate=True):
        self.bridge, self.validate = bridge, validate
        self.output_dir = Path(output_dir)
        self.incoming = self.output_dir / ".incoming"
        self.incoming.mkdir(parents=True, exist_ok=True)
        self.default_task = DEFAULT_TASK
        self.state = "idle"
        self.message = None
        self.last_result = None
        self.jetson = {}
        self.current = None        # session name the UI follows
        self.converter = None
        self.queue = deque()       # verified sessions waiting for conversion
        self.lock = threading.Lock()
        self.worker = None
        self._last_pub = 0.0
        for d in sorted(self.incoming.iterdir()):  # verified before a DGX restart: convert now
            if (d / MANIFEST).is_file():
                self.queue.append(d.name)
        if self.queue:
            self._kick()

    # ---------- shared-UI interface ----------
    @property
    def synthetic(self):
        return self.bridge.synthetic

    @property
    def session(self):
        """Label API: a session that is still being produced cannot be labelled yet."""
        if self.state == "idle" or not self.current:
            return None
        return SimpleNamespace(root=self.output_dir / self.current)

    def status(self):
        d = {"state": self.state, "default_task": self.default_task, "synthetic": self.synthetic,
             "message": self.message, "last_result": self.last_result, "available": True,
             "output_dir": str(self.output_dir), "where": "jetson"}
        j = self.jetson
        if self.state in ("recording", "finalizing"):
            d.update(session=j.get("session") or self.current, task=j.get("task"), frames=j.get("frames", 0),
                     elapsed_s=j.get("elapsed_s", 0), missing_cycles=0,
                     skipped_cycles=sum((j.get("skipped") or {}).values()))
        elif self.state == "transferring":
            up = j.get("upload") or {}
            d.update(session=up.get("session") or self.current,
                     transfer={"sent": up.get("sent", 0), "total": up.get("total", 0), "error": up.get("error")},
                     pending=j.get("pending", []))
        elif self.converter is not None:
            c = self.converter
            d.update(session=self.current, frames=c.session.frames if c.session else 0, total_frames=c.total)
        return d

    def publish(self):
        self._last_pub = time.monotonic()
        self.bridge.hub.publish("record", {"type": "record", **self.status()})

    def wait(self, timeout=None):
        if self.worker is not None:
            self.worker.join(timeout)

    # ---------- state ----------
    def _derive(self):
        c = self.converter
        if c is not None:
            return "validating" if c.state == "validating" else "converting"
        j = self.jetson
        if j.get("state") == "recording":
            return "recording"
        if j.get("state") == "finalizing":
            return "finalizing"
        if j.get("upload") or j.get("pending") or self.queue:
            return "transferring"
        return "idle"

    def refresh(self):
        new = self._derive()
        changed = new != self.state
        self.state = new
        if changed or (new != "idle" and time.monotonic() - self._last_pub > 0.5):
            self.publish()

    def on_jetson(self, status):
        """Every control pong (5 Hz) carries the Jetson recorder status."""
        self.jetson = status or {}
        if self.jetson.get("state") == "recording" and self.jetson.get("session"):
            self.current = self.jetson["session"]  # also adopts a recording that outlived a DGX restart
        self.refresh()

    # ---------- commands ----------
    async def start(self, task=None):
        if self.state != "idle":
            raise RecorderError(f"지금은 이전 녹화를 처리 중입니다 ({self.state}).")
        if self.bridge.sources["arms"].state != "running":
            raise RecorderError("팔 제어가 실행 중일 때만 녹화할 수 있습니다.")
        try:
            result = await self.bridge.call("record_start", task=(task or self.default_task).strip())
        except (RuntimeError, asyncio.TimeoutError) as exc:
            raise RecorderError(str(exc) or "Jetson 응답 없음") from exc
        self.current, self.message = result["session"], None
        self.jetson = dict(self.jetson, state="recording", session=result["session"], frames=0, elapsed_s=0)
        self.refresh()
        return self.status()

    async def stop(self, reason="user"):
        if self.state != "recording":
            return False
        try:
            await self.bridge.call("record_stop", reason=reason)
        except (RuntimeError, asyncio.TimeoutError) as exc:
            raise RecorderError(str(exc) or "Jetson 응답 없음") from exc
        self.jetson = dict(self.jetson, state="finalizing")
        self.refresh()
        return True

    # ---------- upload (Jetson -> DGX) ----------
    async def upload_file(self, request):
        self.bridge.authorize(request)
        name, fname = request.match_info["session"], request.match_info["file"]
        if not SESSION_RE.match(name) or not FILE_RE.match(fname):
            raise web.HTTPBadRequest(text="invalid session or file name")
        expected, size = request.headers.get("X-Sha256", ""), int(request.headers.get("X-Size", "-1"))
        folder = self.incoming / name
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / (fname + ".part")
        digest, received = hashlib.sha256(), 0
        with open(part, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                f.write(chunk)
                digest.update(chunk)
                received += len(chunk)
        if received != size or digest.hexdigest() != expected:
            part.unlink(missing_ok=True)
            raise web.HTTPBadRequest(text=f"{fname}: size or SHA-256 mismatch")
        os.replace(part, folder / fname)
        (folder / (fname + ".sha256")).write_text(expected)
        return web.json_response({"ok": True})

    async def upload_commit(self, request):
        """Reply ok only when every listed file arrived intact: the Jetson then deletes its copy."""
        self.bridge.authorize(request)
        name = request.match_info["session"]
        if not SESSION_RE.match(name):
            raise web.HTTPBadRequest(text="invalid session name")
        manifest = await request.json()
        folder = self.incoming / name
        missing = [f for f, meta in manifest.get("files", {}).items()
                   if not FILE_RE.match(f) or not (folder / f).is_file() or (folder / f).stat().st_size != meta["size"]
                   or (folder / (f + ".sha256")).read_text() != meta["sha256"]]
        if missing or manifest.get("session") != name:
            return web.json_response({"ok": False, "missing": missing}, status=409)
        (folder / MANIFEST).write_text(json.dumps(manifest, indent=2))
        if name not in self.queue and (self.converter is None or self.current != name):
            self.queue.append(name)
        self._kick()
        self.refresh()
        return web.json_response({"ok": True})

    # ---------- conversion ----------
    def _kick(self):
        with self.lock:
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._convert_all, name="convert", daemon=True)
                self.worker.start()

    def _convert_all(self):
        from .convert import SessionConverter
        while True:
            with self.lock:
                if not self.queue:
                    self.worker = None
                    return
                name = self.queue.popleft()
            folder = self.incoming / name
            self.current = name
            try:
                self.converter = SessionConverter(folder, self.output_dir, publish=self.refresh, validate=self.validate)
                self.refresh()
                result = self.converter.convert()
                if result["status"] == "completed":
                    shutil.rmtree(folder)  # originals kept in the session (rgb_mp4/, capture_cycles.jsonl)
                self.last_result = result
                self.current = result["session"]
                log.info("converted %s: %s", name, {k: result.get(k) for k in ("status", "frames", "elapsed_s")})
            except Exception as exc:
                log.exception("conversion of %s failed; raw files kept in %s", name, folder)
                self.last_result = {"session": name, "path": str(folder), "frames": 0, "status": "failed",
                                    "error": f"conversion failed: {exc!r}", "elapsed_s": 0, "source_session": name}
            finally:
                self.converter = None
                self.refresh()
