"""Jetson-side recording (all timestamps on the Jetson clock).

Everything is written to the Jetson disk at capture time, so a recording does not depend on Wi-Fi.
After the operator stops, the session is uploaded to the DGX and deleted here only once the DGX has
verified every file (size + SHA-256). Layout of ``<record_dir>/<YYYYMMDD_HHMMSS>/``:

  manifest.json          format, task, cameras (calibration), counters, file list with SHA-256
  rgb_<camera>.mp4       H.264, 640x480, 30 fps; video frame i = recorded cycle i
  depth_<camera>.bin     "TWDEPTH1" + records (<u32 index, u32 length, u8 codec> + lossless depth blob)
  cycles.jsonl           one line per recorded cycle: arms, base sample, camera frame metadata
  base_raw.jsonl         every raw Odometry / Twist message received while recording
  READY                  marker: finalized, waiting for upload
"""
import hashlib
import json
import logging
import queue
import shutil
import struct
import threading
import time
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

from .protocol import encode_depth

log = logging.getLogger(__name__)
FORMAT = "teleop-jetson-session/1"
DEPTH_MAGIC = b"TWDEPTH1"
DEPTH_RECORD = struct.Struct("<IIB")  # frame index, blob length, codec id
DEPTH_CODECS = {"u16-delta-lz4": 1, "u16-delta-zlib": 2, "f32-zlib": 3}
MANIFEST, READY = "manifest.json", "READY"
CAMERA_MAX_AGE_NS = 500_000_000  # same limits as the DGX recorder
MAX_DEPTH_DT_MS = 2.0
MAX_BASE_AGE_NS = 250_000_000
START_MAX_AGE_NS = 1_000_000_000  # start check: is the input arriving at all?
STALL_S = 1.0  # recording but nothing stored for this long: the status says why
# Why a cycle was not stored, for the operator (also used for an empty session's error on the DGX).
SKIP_HINTS = {
    "odometry too old": "베이스(SLATE) odometry가 들어오지 않습니다. SLATE 전원과 드라이버를 확인하세요.",
    "odometry frame IDs changed": "베이스 odometry 좌표계 이름이 바뀌었습니다.",
    "camera frame too old": "카메라 프레임이 들어오지 않거나 늦습니다.",
    "missing or unpaired RGB/depth": "RGB와 depth 짝이 맞는 카메라 프레임이 없습니다.",
    "writer behind": "Jetson 저장이 밀리고 있습니다.",
    "arm control not running": "팔 제어가 꺼져 있습니다.",
}


def skip_hint(reason):
    return SKIP_HINTS.get(reason, reason)
RESYNC_CYCLES = 15  # buffered sampling: one frame behind for this long -> jump to the newest
SAMPLING = {
    "buffered": "per camera the oldest paired frame not yet recorded (at most 1 frame behind the newest; "
                f"after {RESYNC_CYCLES} cycles behind, the newest): arm-loop jitter does not repeat or skip frames",
    "latest": "newest paired frame at each arm cycle",
}
KST = ZoneInfo("Asia/Seoul")


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


class CameraWriter:
    """One camera: RGB -> H.264 mp4 and depth -> lossless blobs, in its own thread."""

    def __init__(self, root, name, fps, scale, crf=18, preset="superfast", depth_compressor="lz4"):
        import av
        self.name, self.fps, self.scale, self.compressor = name, fps, scale, depth_compressor
        self.rgb_path, self.depth_path = root / f"rgb_{name}.mp4", root / f"depth_{name}.bin"
        self.container = av.open(str(self.rgb_path), "w")
        self.stream = self.container.add_stream("libx264", rate=fps)
        self.stream.width, self.stream.height, self.stream.pix_fmt = 640, 480, "yuv420p"
        self.stream.options = {"preset": preset, "crf": str(crf)}
        self.depth = self.depth_path.open("wb")
        self.depth.write(DEPTH_MAGIC)
        self.queue = queue.Queue(maxsize=90)  # 3 s
        self.frames = 0
        self.error = None
        self.codecs = set()
        self.thread = threading.Thread(target=self._run, name=f"rec-{name}", daemon=True)
        self.thread.start()

    def room(self):
        return self.queue.qsize() < self.queue.maxsize - 1

    def put(self, index, frame):
        self.queue.put_nowait((index, frame))

    def close(self):
        self.queue.put(None)
        self.thread.join()

    def _run(self):
        import av
        import cv2
        try:
            while (item := self.queue.get()) is not None:
                if self.error:
                    continue
                index, frame = item
                # OpenCV's I420 uses the same BT.601 limited-range matrix as swscale, ~1 ms cheaper.
                video = av.VideoFrame.from_ndarray(cv2.cvtColor(frame.rgb, cv2.COLOR_RGB2YUV_I420),
                                                   format="yuv420p")
                video.pts, video.time_base = index, Fraction(1, self.fps)
                for packet in self.stream.encode(video):
                    self.container.mux(packet)
                codec, blob = encode_depth(frame.depth, self.scale, self.compressor, frame.depth_units)
                self.codecs.add(codec)
                self.depth.write(DEPTH_RECORD.pack(index, len(blob), DEPTH_CODECS[codec]))
                self.depth.write(blob)
                self.frames += 1
        except Exception as exc:
            log.exception("%s writer failed", self.name)
            self.error = repr(exc)
            while self.queue.get() is not None:  # drain so close() can finish
                pass
        finally:
            try:
                for packet in self.stream.encode():
                    self.container.mux(packet)
                self.container.close()
            except Exception as exc:
                self.error = self.error or repr(exc)
            self.depth.close()


class LineWriter:
    """cycles.jsonl and base_raw.jsonl, written by one thread (the arm loop only enqueues)."""

    def __init__(self, root):
        self.files = {"cycle": (root / "cycles.jsonl").open("x"), "raw": (root / "base_raw.jsonl").open("x")}
        self.queue = queue.Queue(maxsize=3000)
        self.lost_raw = 0
        self.error = None
        self.thread = threading.Thread(target=self._run, name="rec-lines", daemon=True)
        self.thread.start()

    def room(self):
        return self.queue.qsize() < self.queue.maxsize - 100

    def cycle(self, record):
        self.queue.put_nowait(("cycle", record))

    def raw(self, item):
        try:
            self.queue.put_nowait(("raw", item))
        except queue.Full:
            self.lost_raw += 1

    def close(self):
        self.queue.put(None)
        self.thread.join()

    def _run(self):
        last_flush = time.monotonic()
        try:
            while (item := self.queue.get()) is not None:
                kind, record = item
                self.files[kind].write(json.dumps(record, allow_nan=False) + "\n")
                if time.monotonic() - last_flush > 0.5:
                    last_flush = time.monotonic()
                    for f in self.files.values():
                        f.flush()
        except Exception as exc:
            log.exception("line writer failed")
            self.error = repr(exc)
            while self.queue.get() is not None:
                pass
        finally:
            for f in self.files.values():
                f.close()


class Recording:
    def __init__(self, name, root, task):
        self.name, self.root, self.task = name, root, task
        self.frames = 0
        self.skipped = {}
        self.events = []
        self.last_seq = {}   # camera -> seq of the last recorded frame
        self.behind = {}     # camera -> consecutive cycles with a newer frame left over
        self.writers = {}
        self.lines = None
        self.base_frames = None
        self.t0 = time.monotonic()
        self.t_stop = None
        self.start_unix_ns, self.start_monotonic_ns = time.time_ns(), time.monotonic_ns()
        self.manifest = {}
        self.last_stored = self.t0   # monotonic time of the last stored frame (or the start)
        self.last_skip = None        # reason of the latest skipped cycle

    def skip(self, reason):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1
        self.last_skip = reason

    def event(self, text):
        if len(self.events) < 1000 and (not self.events or self.events[-1]["event"] != text):
            self.events.append({"frame": self.frames, "t_s": round(self.elapsed, 3), "event": text})

    @property
    def elapsed(self):
        return (self.t_stop or time.monotonic()) - self.t0


class LocalRecorder:
    def __init__(self, root, cameras, base_capture, fps, boot, synthetic=False, crf=18, preset="superfast",
                 depth_compressor="lz4", min_free_bytes=20e9, sampling="buffered"):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cameras, self.base_capture, self.fps, self.boot = cameras, base_capture, fps, boot
        self.synthetic, self.crf, self.preset, self.compressor = synthetic, crf, preset, depth_compressor
        self.min_free_bytes = min_free_bytes
        if sampling not in SAMPLING:
            raise ValueError(f"unknown camera sampling {sampling!r}")
        self.sampling = sampling
        self.lock = threading.Lock()
        self.state = "idle"  # idle | recording | finalizing
        self.current = None
        self.last_error = None
        self.upload = None   # {"session", "sent", "total"} while uploading

    # ---------- status ----------
    def pending(self):
        return sorted(p.parent.name for p in self.root.glob(f"*/{READY}"))

    def status(self):
        d = {"state": self.state, "error": self.last_error, "pending": self.pending(),
             "upload": dict(self.upload) if self.upload else None,
             "free_bytes": shutil.disk_usage(self.root).free}
        r = self.current
        if r is not None:
            d.update(session=r.name, task=r.task, frames=r.frames, skipped=dict(r.skipped),
                     elapsed_s=round(r.elapsed, 2))
            idle = time.monotonic()-r.last_stored
            if self.state == "recording" and idle > STALL_S and r.last_skip:
                d["stalled"] = {"for_s": round(idle, 1), "reason": r.last_skip, "hint": skip_hint(r.last_skip)}
        return d

    def input_problem(self):
        """Why a recording started now would store nothing (None: inputs are arriving)."""
        now = time.time_ns()
        for cam in self.cameras:
            f = cam.latest_frame(paired=True)
            if f is None or not 0 <= now-f.t_recv_ns <= START_MAX_AGE_NS:
                return f"{cam.name}: RGB/depth 프레임이 들어오지 않습니다."
        base = self.base_capture.snapshot(drain=False)
        odom = base["odom"]
        if odom is None or not 0 <= base["now_ns"]-odom["receive_ns"] <= START_MAX_AGE_NS:
            return SKIP_HINTS["odometry too old"]
        return None

    # ---------- start ----------
    def _new_dir(self):
        stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
        suffix = "_SYNTHETIC" if self.synthetic else ""
        for n in range(1, 100):
            root = self.root / f"{stamp}{'' if n == 1 else f'_{n}'}{suffix}"
            try:
                root.mkdir()
                return root
            except FileExistsError:
                continue
        raise RuntimeError("could not create a session folder")

    def start(self, task, extra=None):
        with self.lock:
            if self.state != "idle":
                raise RuntimeError(f"Jetson recorder is {self.state}")
            free = shutil.disk_usage(self.root).free
            if free < self.min_free_bytes:
                raise RuntimeError(f"Jetson disk almost full ({free / 1e9:.0f} GB free)")
            problem = self.input_problem()
            if problem:  # every cycle would be skipped: refuse instead of recording nothing
                raise RuntimeError(f"녹화를 시작하지 않았습니다: {problem}")
            calibration = {}
            for cam in self.cameras:
                scale = cam.info.get("depth_scale_m")
                if not isinstance(scale, (int, float)) or not 0 < scale < 1:
                    raise RuntimeError(f"{cam.name}: depth scale unknown")
                calibration[cam.name] = dict(cam.info)
            root = self._new_dir()
            rec = Recording(root.name, root, task)
            rec.unpaired_start = [c.unpaired for c in self.cameras]
            try:
                rec.writers = {cam.name: CameraWriter(root, cam.name, self.fps, cam.info["depth_scale_m"], self.crf,
                                                      self.preset, self.compressor) for cam in self.cameras}
                rec.lines = LineWriter(root)
            except Exception:
                for w in rec.writers.values():
                    w.close()
                shutil.rmtree(root, ignore_errors=True)
                raise
            rec.manifest = {"format": FORMAT, "session": rec.name, "task": task, "fps": self.fps, "boot": self.boot,
                            "synthetic": self.synthetic, "clock": "Jetson (unix and monotonic ns)",
                            "start_unix_ns": rec.start_unix_ns, "start_monotonic_ns": rec.start_monotonic_ns,
                            "cameras": calibration, "camera_sampling": {"mode": self.sampling,
                                                                         "rule": SAMPLING[self.sampling]},
                            "rgb": {"codec": "h264 (libx264)", "crf": self.crf, "preset": self.preset,
                                    "size": [640, 480], "frame_index": "video frame i = cycles.jsonl line i"},
                            "depth": {"container": "TWDEPTH1", "compressor": self.compressor, "unit": "camera units"},
                            **(extra or {})}
            (root / MANIFEST).write_text(json.dumps(rec.manifest, indent=2))
            self.current, self.state, self.last_error = rec, "recording", None
            self.base_capture.raw_tap = rec.lines.raw
        log.info("recording %s", root)
        return rec.name

    # ---------- per cycle (arm thread: fast, never raises) ----------
    def on_cycle(self, cycle, cycle_number):
        rec = self.current
        if self.state != "recording" or rec is None:
            return
        try:
            now_unix, now_mono = time.time_ns(), time.monotonic_ns()
            frames, behind = [], {}
            for cam in self.cameras:
                f, behind[cam.name] = self._pick(rec, cam)
                if f is None or f.depth is None or f.depth_dt_ms is None or abs(f.depth_dt_ms) > MAX_DEPTH_DT_MS:
                    return rec.skip("missing or unpaired RGB/depth")
                if not 0 <= now_unix - f.t_recv_ns <= CAMERA_MAX_AGE_NS:
                    return rec.skip("camera frame too old")
                frames.append(f)
            base = self.base_capture.snapshot(drain=False)
            odom = base["odom"]
            if odom is None or not 0 <= base["now_ns"] - odom["receive_ns"] <= MAX_BASE_AGE_NS:
                return rec.skip("odometry too old")
            ids = {k: odom[k] for k in ("frame_id", "child_frame_id")}
            if rec.base_frames is None:
                rec.base_frames = ids
            elif ids != rec.base_frames:
                return rec.skip("odometry frame IDs changed")
            if not rec.lines.room() or not all(w.room() for w in rec.writers.values()):
                return rec.skip("writer behind")  # skip the cycle for every camera: streams stay aligned
            index = rec.frames
            for cam, f in zip(self.cameras, frames):
                rec.writers[cam.name].put(index, f)
            rec.lines.cycle({"index": index, "cycle_number": cycle_number,
                             "cycle": {k: getattr(cycle, k) for k in cycle.__slots__},
                             "sample_unix_ns": now_unix, "sample_monotonic_ns": now_mono,
                             "cameras": [{"name": cam.name, "seq": f.seq, "frame_number": f.frame_number,
                                          "t_recv_ns": f.t_recv_ns, "depth_dt_ms": f.depth_dt_ms,
                                          "shape": list(f.depth.shape)}
                                         for cam, f in zip(self.cameras, frames)],
                             "base": {"odom": odom, "command": base["command"], "now_ns": base["now_ns"]}})
            rec.frames += 1
            rec.last_stored = time.monotonic()
            for cam, f in zip(self.cameras, frames):  # consumed only once the cycle is recorded
                rec.last_seq[cam.name] = f.seq
            rec.behind.update(behind)
        except Exception as exc:
            log.exception("recording cycle failed")
            rec.skip(f"error: {type(exc).__name__}")

    def note_idle(self):
        """A 30 Hz tick without arm control (link lost or arms stopped): counted, never recorded."""
        rec = self.current
        if self.state == "recording" and rec is not None:
            rec.skip("arm control not running")

    def note_event(self, text):
        rec = self.current
        if self.state == "recording" and rec is not None:
            rec.event(text)

    def _pick(self, rec, cam):
        """(frame, cycles behind) for this cycle; see SAMPLING."""
        if self.sampling == "latest":
            return cam.latest_frame(paired=True), 0
        new = cam.paired_after(rec.last_seq.get(cam.name, -1))
        if not new:
            return cam.latest_frame(paired=True), 0  # no new exposure since the last cycle: repeat
        behind = rec.behind.get(cam.name, 0) + 1 if len(new) >= 2 else 0
        if behind >= RESYNC_CYCLES:
            return new[-1], 0
        return (new[0] if len(new) <= 2 else new[-2]), behind

    # ---------- stop ----------
    def request_stop(self, reason):
        """Returns at once; finalizing (flush, SHA-256) continues in a thread."""
        with self.lock:
            if self.state != "recording":
                raise RuntimeError("not recording")
            self.state = "finalizing"
            rec = self.current
            rec.t_stop = time.monotonic()
            rec.unpaired_stop = [c.unpaired for c in self.cameras]
            self.base_capture.raw_tap = None
        threading.Thread(target=self._finalize, args=(rec, reason), name="rec-finalize", daemon=True).start()
        return rec.name

    def _finalize(self, rec, reason):
        errors = []
        try:
            for w in rec.writers.values():
                w.close()
                if w.error:
                    errors.append(f"{w.name}: {w.error}")
                if w.frames != rec.frames:
                    errors.append(f"{w.name}: {w.frames} frames written, {rec.frames} cycles recorded")
            rec.lines.close()
            if rec.lines.error:
                errors.append(f"lines: {rec.lines.error}")
            files = {}
            for path in sorted(rec.root.iterdir()):
                if path.is_file() and path.name not in (MANIFEST, READY):
                    files[path.name] = {"size": path.stat().st_size, "sha256": sha256_file(path)}
            rec.manifest.update(frames=rec.frames, skipped=dict(rec.skipped), stop_reason=reason,
                                elapsed_s=round(rec.elapsed, 3), end_unix_ns=time.time_ns(),
                                base_frames=rec.base_frames, raw_messages_lost=rec.lines.lost_raw,
                                camera_unpaired=[e - s for e, s in zip(rec.unpaired_stop, rec.unpaired_start)],
                                depth_codecs={n: sorted(w.codecs) for n, w in rec.writers.items()},
                                events=rec.events, errors=errors, files=files)
            (rec.root / MANIFEST).write_text(json.dumps(rec.manifest, indent=2))
            (rec.root / READY).touch()
            log.info("recording %s finalized: %d frames, %.1f MB", rec.name, rec.frames,
                     sum(f["size"] for f in files.values()) / 1e6)
        except Exception as exc:
            log.exception("finalize failed")
            self.last_error = f"{rec.name}: {exc!r}"
        finally:
            with self.lock:
                self.current, self.state = None, "idle"

    def delete(self, name):
        """Called only after the DGX confirmed every file."""
        path = self.root / name
        if path.parent != self.root or not (path / MANIFEST).is_file():
            raise ValueError(f"not a session: {name}")
        shutil.rmtree(path)
