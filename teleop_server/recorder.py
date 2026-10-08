"""Browser-controlled episode recorder.

Writes exactly what ``teleop_server/mobile_capture.py`` writes: one LeRobot episode per session
directory with the same features (imported from that script), ``base_raw.jsonl`` from the same
``BaseSubscriber``, and ``session.json``; then runs the same ``check_mobile.validate``.

Frames are produced by the arm teleop loop (one frame per cycle, like mobile_capture.py) and
written by a separate thread so disk and video work never slows teleoperation.
"""
import json
import logging
import math
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np

log = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROBOT_TYPE = "bi_widowxai_follower_robot"  # BiWidowXAIFollowerRobot.name, as in existing datasets
REPO_ID = "local/mobile_paper_bag"
DEFAULT_TASK = "Pick up the paper bag and place it in the marked target area."
CAMERA_MAX_AGE_NS = 500_000_000  # lerobot read_latest() default used by mobile_capture.py
MAX_DEPTH_DT_MS = 2.0  # RGB/depth of one frameset differ by ~0.3 ms on the D405
# D405 reports up to 65535 x 0.0001 m; storing that full range avoids clipping far pixels. Log
# quantization to 12 bits then costs <= 0.06 % of the distance (0.18 mm at 0.3 m).
DEPTH_MIN_M, DEPTH_MAX_M = 0.05, 6.5535  # values <= DEPTH_MIN_M mean "no measurement"
DEPTH_PRESET = "ultrafast"  # x265 lossless: ~4x faster saving than the default preset, ~40 % larger
RAW_DEPTH_DIR = "depth_raw"  # depth_storage="png16": <session>/depth_raw/<camera>/<frame_index:06d>.png
STATE_KO = {"idle": "대기", "starting": "녹화 준비", "recording": "녹화", "saving": "저장", "validating": "검증"}


class RecorderError(RuntimeError):
    pass


def write_depth_png(path, depth_m, scale):
    """Store depth as the camera's own uint16 units, losslessly. Refuses values off the unit grid."""
    units = np.rint(depth_m / np.float32(scale))
    if units.min() < 0 or units.max() > 65535:
        raise ValueError(f"{path.name}: depth outside the uint16 range")
    units = units.astype(np.uint16)
    if not np.array_equal(units.astype(np.float32) * np.float32(scale), depth_m):
        raise ValueError(f"{path.name}: depth is not on the camera's {scale} m grid")
    if not cv2.imwrite(str(path), units, [cv2.IMWRITE_PNG_COMPRESSION, 1]):
        raise OSError(f"could not write {path}")


def collector_modules():
    """The existing collector's schema helpers (single source of truth for the data format)."""
    from teleop_server import check_mobile, mobile_capture
    return mobile_capture, check_mobile


class FakeBase:
    """Synthetic stand-in for ``mobile_capture.BaseSubscriber`` (same sample/close/raw-log contract)."""

    def __init__(self, path, rate=20.0):
        self.log = path.open("x")
        self.lock = threading.Lock()
        self.latest = self.command = self.error = None
        self.stop = threading.Event()
        self.rate = rate
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        t0 = time.monotonic()
        while not self.stop.wait(1 / self.rate):
            t = time.monotonic() - t0
            yaw = 0.3 * t
            values = [math.cos(0.3 * t) - 1, math.sin(0.3 * t), 0.0, 0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2),
                      0.3, 0.0, 0.0, 0.0, 0.0, 0.3]
            mono, unix = time.monotonic_ns(), time.time_ns()
            self.log.write(json.dumps({"kind": "odom", "receive_monotonic_ns": mono, "receive_unix_ns": unix,
                                       "message": {"synthetic": True, "values": values}}) + "\n")
            self.log.flush()
            with self.lock:
                self.latest = {"values": values, "receive_ns": mono, "stamp_ns": unix - 3_000_000,
                               "frame_id": "odom", "child_frame_id": "base_link"}

    def sample(self, max_age_s):
        if self.error:
            raise RuntimeError("fake base failed") from self.error
        with self.lock:
            odom, cmd = self.latest, self.command
        now = time.monotonic_ns()
        if odom is None or (now - odom["receive_ns"]) / 1e9 > max_age_s:
            raise RuntimeError("No fresh odometry")
        return odom, cmd, now

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        self.log.close()


class Session:
    def __init__(self, root, manifest, task, base, dataset, mc, check):
        self.root, self.manifest, self.task = root, manifest, task
        self.base, self.dataset, self.mc, self.check = base, dataset, mc, check
        self.queue = None
        self.stop_event = threading.Event()
        self.stop_reason = self.error = None
        self.frames = 0
        self.t0 = self.t_stop = None
        self.writer = None
        self.cam_last_seq = [None, None, None]
        self.cam_repeats = [0, 0, 0]
        self.cam_max_age_ms = [0.0, 0.0, 0.0]
        self.cam_max_depth_dt_ms = [0.0, 0.0, 0.0]
        self.depth_pool = None
        self.depth_jobs = []
        # Recording never stops on its own for a data problem (user requirement): unusable cycles
        # are skipped and every gap is accounted for here and in session.json["capture_gaps"].
        self.enqueued = 0
        self.missing_cycles = 0       # cycles the transport never delivered (distributed mode)
        self.gap_events = []          # [frame index where the gap is, cycles missing]
        self.skipped = {}             # reason -> cycles received but not recorded
        self.events = []              # disconnects, arm faults, stream restarts

    @property
    def elapsed(self):
        if self.t0 is None:
            return 0.0
        return (self.t_stop or time.monotonic()) - self.t0


class Recorder:
    def __init__(self, hub, arms, cameras, base_factory, *, fps, synthetic, output_dir, default_task=DEFAULT_TASK,
                 manifest_extra=None, max_base_age=0.25, base_timeout=5.0, validate=True, queue_size=300,
                 depth=None, depth_min=DEPTH_MIN_M, depth_max=DEPTH_MAX_M, depth_preset=DEPTH_PRESET,
                 depth_storage="lerobot"):
        self.hub, self.arms, self.cameras, self.base_factory = hub, arms, cameras, base_factory
        self.fps, self.synthetic, self.output_dir = fps, synthetic, Path(output_dir)
        self.default_task, self.manifest_extra = default_task, manifest_extra or {}
        self.max_base_age, self.base_timeout, self.validate = max_base_age, base_timeout, validate
        self.queue_size = queue_size
        self.depth = all(c.depth_enabled for c in cameras) if depth is None else depth
        self.depth_min, self.depth_max, self.depth_preset = depth_min, depth_max, depth_preset
        # "lerobot": depth video features (12-bit log quantized). "png16": the camera's uint16 depth
        # unchanged, one PNG per frame next to the dataset (LeRobot cannot store 16-bit depth).
        if depth_storage not in ("lerobot", "png16"):
            raise ValueError(depth_storage)
        self.depth_storage = depth_storage
        self.lock = threading.Lock()
        self.state = "idle"
        self.session = None
        self.last_result = None
        self.message = None
        self._last_pub = 0.0
        arms.cycle_sink = self.on_cycle
        arms.exit_hooks.append(self.on_arms_exit)

    def features(self, mc):
        """mobile_capture.py's schema plus colour-aligned depth and per-frame pairing information."""
        feats = dict(mc.features())
        names = [c.name for c in self.cameras]
        feats["observation.camera_frame_number"] = {"dtype": "int64", "shape": (len(names),), "names": names}
        if self.depth:
            feats["observation.camera_depth_dt_ms"] = {"dtype": "float32", "shape": (len(names),), "names": names}
            for name in names if self.depth_storage == "lerobot" else []:
                feats[f"observation.images.{name}_depth"] = {
                    "dtype": "video", "shape": (480, 640, 1), "names": ["height", "width", "channels"],
                    "info": {"is_depth_map": True}}
        return feats

    # ---------- status ----------
    def status(self):
        s = self.session
        d = {"state": self.state, "default_task": self.default_task, "synthetic": self.synthetic,
             "message": self.message, "last_result": self.last_result,
             "available": self.base_factory is not None, "output_dir": str(self.output_dir)}
        if s is not None:
            d.update(session=s.root.name, task=s.task, frames=s.frames, elapsed_s=round(s.elapsed, 2),
                     missing_cycles=s.missing_cycles, skipped_cycles=sum(s.skipped.values()))
        return d

    def publish(self):
        self.hub.publish("record", {"type": "record", **self.status()})

    def _set(self, state, message=None):
        with self.lock:
            self.state = state
            if message is not None:
                self.message = message
        self.publish()

    # ---------- start ----------
    def check_ready(self):
        if self.base_factory is None:
            raise RecorderError("odometry가 꺼져 있어 기존 수집 형식으로 녹화할 수 없습니다 (--odom).")
        if self.arms.state != "running":
            raise RecorderError("팔 제어가 실행 중일 때만 녹화할 수 있습니다.")
        if abs(self.arms.rate - self.fps) > 1e-6:
            raise RecorderError(f"팔 루프 {self.arms.rate} Hz와 녹화 {self.fps} fps가 같아야 합니다.")
        now = time.time_ns()
        for cam in self.cameras:
            pct = cam.unpaired_recent_pct() if self.depth else None
            if pct is not None and pct > 10.0:
                raise RecorderError(f"{cam.name}의 RGB/depth 짝 불일치가 많습니다 ({pct:.0f}%). 잠시 후 다시 시도하세요.")
            latest = cam.latest_frame(paired=self.depth)
            age = None if latest is None else (now - latest.t_recv_ns) / 1e6
            if cam.state != "running" or age is None or age > CAMERA_MAX_AGE_NS / 1e6:
                detail = f"상태 {cam.state}" + ("" if age is None else f", 마지막 프레임 {age:.0f} ms 전")
                what = "RGB+depth 짝" if self.depth else "영상"
                raise RecorderError(f"{cam.name} {what}이 들어오지 않습니다 ({detail}).")

    def start(self, task=None):
        """Blocking (~1 s): call from a worker thread. Returns the status dict."""
        task = (task or self.default_task).strip()
        with self.lock:
            if self.state != "idle":
                raise RecorderError(f"지금은 {STATE_KO.get(self.state, self.state)} 중입니다.")
            self.check_ready()
            self.state, self.message = "starting", None
        self.publish()
        root = base = None
        manifest = {}
        try:
            mc, check = collector_modules()
            root = self._new_session_dir()
            manifest = {"task": task, "status": "preflight", "base_control": "subscribe_only", **self.manifest_extra,
                        "fps": self.fps, "requested_seconds": None, "recorder": "teleop_server",
                        "synthetic": self.synthetic, "start_unix_ns": time.time_ns(), "frames": 0,
                        "cameras": {c.name: dict(c.info) for c in self.cameras},
                        "depth": self._depth_manifest()}
            self._write_manifest(root, manifest)
            base = self.base_factory(root / "base_raw.jsonl")
            deadline = time.monotonic() + self.base_timeout
            while base.latest is None and time.monotonic() < deadline and base.error is None:
                time.sleep(0.05)
            odom, _, _ = base.sample(self.max_base_age)
            manifest["base_frames"] = {k: odom[k] for k in ("frame_id", "child_frame_id")}
            from lerobot.configs.video import DepthEncoderConfig, RGBEncoderConfig
            from lerobot.datasets.lerobot_dataset import LeRobotDataset
            depth_encoder = (DepthEncoderConfig(depth_min=self.depth_min, depth_max=self.depth_max, shift=0.0,
                                                use_log=True, preset=self.depth_preset)
                             if self.depth and self.depth_storage == "lerobot" else None)
            dataset = LeRobotDataset.create(REPO_ID, self.fps, self.features(mc), root=root / "dataset",
                                            robot_type=ROBOT_TYPE, video_backend="pyav",
                                            rgb_encoder=RGBEncoderConfig(vcodec="h264"), depth_encoder=depth_encoder,
                                            image_writer_threads=8)
            self.check_ready()  # still true after the slow setup?
            manifest["status"] = "recording"
            self._write_manifest(root, manifest)
            s = Session(root, manifest, task, base, dataset, mc, check)
            s.unpaired_start = [c.unpaired for c in self.cameras]
            s.queue = queue.Queue(maxsize=self.queue_size)
            if self.depth and self.depth_storage == "png16":
                for cam in self.cameras:
                    (root / RAW_DEPTH_DIR / cam.name).mkdir(parents=True)
                s.depth_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="depth-png")
            s.writer = threading.Thread(target=self._write, args=(s,), name="recorder", daemon=True)
            with self.lock:
                s.t0 = time.monotonic()
                self.session, self.state = s, "recording"
            s.writer.start()
        except Exception as exc:
            if base is not None:
                try:
                    base.close()
                except Exception:
                    pass
            if root is not None and root.exists():
                manifest.update(status="failed", error=repr(exc), end_unix_ns=time.time_ns())
                self._write_manifest(root, manifest)
            self._set("idle", f"녹화 시작 실패: {exc}")
            raise RecorderError(str(exc)) from exc
        log.info("recording %s", root)
        self.publish()
        return self.status()

    def _depth_manifest(self):
        if not self.depth:
            return {"enabled": False}
        pairing = "same RealSense frameset; observation.camera_depth_dt_ms"
        if self.depth_storage == "lerobot":
            return {"enabled": True, "unit": "m", "aligned_to": "color", "dtype_in": "float32",
                    "codec": f"hevc lossless ({self.depth_preset}), gray12le, 12-bit log quantized",
                    "depth_min_m": self.depth_min, "depth_max_m": self.depth_max,
                    "no_measurement": f"<= {self.depth_min} m", "pairing": pairing}
        calibration = {}
        for cam in self.cameras:
            scale = cam.info.get("depth_scale_m")
            if not isinstance(scale, (int, float)) or not 0 < scale < 1:
                raise RecorderError(f"{cam.name}: depth scale unknown, raw depth cannot be stored")
            calibration[cam.name] = {k: cam.info.get(k) for k in (
                "depth_scale_m", "depth_aligned_to", "depth_intrinsics", "color_intrinsics", "depth_to_color_extrinsics")}
        return {"enabled": True, "storage": "png16", "path": RAW_DEPTH_DIR + "/<camera>/<frame_index:06d>.png",
                "dtype": "uint16", "unit": "depth_scale_m per camera (metres = value * depth_scale_m)",
                "no_measurement": 0, "codec": "PNG (lossless)", "pairing": pairing, "cameras": calibration}

    def _new_session_dir(self):
        """<output_dir>/YYYYMMDD_HHMMSS (KST start time), so name order is time order. A second
        recording within the same second gets _2, _3 ...; fake-sensor data ends in _SYNTHETIC."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S")
        suffix = "_SYNTHETIC" if self.synthetic else ""
        for n in range(1, 100):
            root = (self.output_dir / f"{stamp}{'' if n == 1 else f'_{n}'}{suffix}").resolve()
            try:
                root.mkdir(exist_ok=False)
                return root
            except FileExistsError:
                continue
        raise RuntimeError("could not create a session folder")

    # ---------- per cycle (arm thread: must be fast and never raise) ----------
    MAX_EVENTS = 1000

    def note_skip(self, s, reason):
        """A cycle could not be recorded; keep recording and count why."""
        s.skipped[reason] = s.skipped.get(reason, 0) + 1

    def note_event(self, s, text):
        if len(s.events) < self.MAX_EVENTS:
            s.events.append({"frame": s.enqueued, "elapsed_s": round(s.elapsed, 3), "event": text})
        log.warning("recording %s: %s", s.root.name, text)

    def on_cycle(self, cycle):
        s = self.session
        if self.state != "recording" or s is None or s.stop_event.is_set():
            return
        try:
            sample_ns = time.time_ns()
            frames = []
            for cam in self.cameras:
                latest = cam.latest_frame(paired=self.depth)
                if latest is None or sample_ns - latest.t_recv_ns > CAMERA_MAX_AGE_NS:
                    raise RuntimeError(f"{cam.name}: no {'paired RGB+depth' if self.depth else 'camera'} frame within 0.5 s")
                frames.append(latest)
            odom, cmd, now = s.base.sample(self.max_base_age)
            if any(odom[k] != s.manifest["base_frames"][k] for k in ("frame_id", "child_frame_id")):
                raise RuntimeError("Odometry frame IDs changed during recording")
            s.queue.put_nowait((cycle, frames, sample_ns, odom, cmd, now))
            s.enqueued += 1
        except queue.Full:
            self.note_skip(s, "writer behind")
        except Exception as exc:
            self.note_skip(s, str(exc) or type(exc).__name__)

    def on_arms_exit(self, error):
        """Arm loop is ending: stop recording before any disconnect motion is sampled."""
        if error is None:
            self.request_stop("arm control stopped")  # the user pressed control stop
        else:  # keep the session open; cycles resume if control is started again
            s = self.session
            if s is not None and self.state == "recording":
                self.note_event(s, f"arm fault: {error!r}")

    # ---------- stop ----------
    def request_stop(self, reason, error=None):
        """Non-blocking from any thread; the writer drains, saves and validates."""
        with self.lock:
            s = self.session
            if self.state != "recording" or s is None or s.stop_event.is_set():
                return False
            s.t_stop = time.monotonic()
            s.unpaired_stop = [c.unpaired for c in self.cameras]
            s.stop_reason = reason
            if error and not s.error:
                s.error = error
            s.stop_event.set()
            self.state = "saving"
        log.info("recording stop requested: %s %s", reason, error or "")
        self.publish()
        return True

    def wait(self, timeout=None):
        s = self.session
        if s is not None and s.writer is not None:
            s.writer.join(timeout)

    # ---------- writer thread ----------
    def _write(self, s):
        try:
            while True:
                try:
                    item = s.queue.get(timeout=0.1)
                except queue.Empty:
                    if s.stop_event.is_set():
                        break
                    continue
                cycle, frames, sample_ns, odom, cmd, now = item[:6]
                extra = item[6] if len(item) > 6 else None
                frame = {"observation.state": np.array(cycle.follower, dtype=np.float32),
                         "observation.leader_state": np.array(cycle.leader, dtype=np.float32),
                         "action": np.array(cycle.action, dtype=np.float32),
                         "observation.sample_monotonic_ns": np.array([cycle.begin_ns, cycle.end_ns], dtype=np.int64),
                         "observation.sample_unix_ns": np.array([cycle.unix_ns], dtype=np.int64),
                         "observation.camera_age_ms": np.array([(sample_ns - f.t_recv_ns) / 1e6 for f in frames],
                                                               dtype=np.float32),
                         "observation.camera_frame_number": np.array([f.frame_number for f in frames], dtype=np.int64),
                         "task": s.task, **s.mc.base_frame(odom, cmd, now, self.max_base_age)}
                if extra:
                    frame.update(extra)
                if self.depth:
                    frame["observation.camera_depth_dt_ms"] = np.array([f.depth_dt_ms for f in frames], dtype=np.float32)
                for i, f in enumerate(frames):
                    name = self.cameras[i].name
                    frame[f"observation.images.{name}"] = f.rgb
                    if self.depth and self.depth_storage == "png16":
                        # Same CameraFrame as the RGB, stored under this frame's index.
                        scale = s.manifest["depth"]["cameras"][name]["depth_scale_m"]
                        path = s.root / RAW_DEPTH_DIR / name / f"{s.frames:06d}.png"
                        s.depth_jobs.append(s.depth_pool.submit(write_depth_png, path, f.depth, scale))
                        s.cam_max_depth_dt_ms[i] = max(s.cam_max_depth_dt_ms[i], abs(f.depth_dt_ms))
                    elif self.depth:
                        # RGB and depth come from the same CameraFrame: they are stored as a pair at
                        # this index. 0 (no measurement) -> depth_min, which decodes as "no data".
                        frame[f"observation.images.{name}_depth"] = np.maximum(f.depth, self.depth_min)[..., None]
                        s.cam_max_depth_dt_ms[i] = max(s.cam_max_depth_dt_ms[i], abs(f.depth_dt_ms))
                    # Same camera frame sampled again = a stalled/slow camera (some repeats are
                    # normal: the 30 Hz arm loop and the camera clock are not synchronized).
                    if f.seq == s.cam_last_seq[i]:
                        s.cam_repeats[i] += 1
                    s.cam_last_seq[i] = f.seq
                    s.cam_max_age_ms[i] = max(s.cam_max_age_ms[i], (sample_ns - f.t_recv_ns) / 1e6)
                s.dataset.add_frame(frame)
                s.frames += 1
                while s.depth_jobs and s.depth_jobs[0].done():
                    s.depth_jobs.pop(0).result()  # a failed PNG write stops the recording
                if time.monotonic() - self._last_pub > 0.5:
                    self._last_pub = time.monotonic()
                    self.publish()
        except Exception as exc:
            log.exception("recording write failed")
            if not self.request_stop("write error", error=repr(exc)):
                s.error = s.error or repr(exc)
        finally:
            self._finish(s)

    def _finish(self, s):
        errors = []
        if s.depth_pool is not None:
            s.depth_pool.shutdown(wait=True)
            for job in s.depth_jobs:
                try:
                    job.result()
                except Exception as exc:
                    errors.append(repr(exc))
                    break
        try:
            s.base.close()
        except Exception as exc:
            errors.append(repr(exc))
        try:
            if s.frames:
                s.dataset.save_episode()
            s.dataset.finalize()
        except Exception as exc:
            errors.append(repr(exc))
        if not s.frames and not s.error:
            s.error = "no frames recorded"
        failed = bool(s.error or errors)
        quality = {"camera_max_age_ms": [round(a, 1) for a in s.cam_max_age_ms],
                   "camera_repeated_frames": list(s.cam_repeats)}
        if self.depth:
            quality["max_rgb_depth_dt_ms"] = [round(a, 3) for a in s.cam_max_depth_dt_ms]
            # Framesets whose depth was from another exposure; the previous verified pair was used.
            quality["unpaired_framesets_skipped"] = [e - u for e, u in zip(s.unpaired_stop, s.unpaired_start)]
        s.manifest["cameras"] = {c.name: dict(c.info) for c in self.cameras}  # counters as of the end
        skipped = sum(s.skipped.values())
        expected = s.frames + s.missing_cycles + skipped
        gaps = {"recorded_frames": s.frames, "missing_cycles": s.missing_cycles, "skipped_cycles": skipped,
                "recorded_fraction": round(s.frames / expected, 4) if expected else None,
                "skipped": dict(s.skipped), "gap_events": s.gap_events[:self.MAX_EVENTS], "events": s.events}
        s.manifest["capture_gaps"] = gaps
        s.manifest.update(frames=s.frames, end_unix_ns=time.time_ns(), recording_elapsed_s=round(s.elapsed, 3),
                          stop_reason=s.stop_reason, cleanup_errors=errors, camera_quality=quality,
                          status="failed" if failed else "completed")
        if s.error:
            s.manifest["error"] = s.error
        self._write_manifest(s.root, s.manifest)
        result = {"session": s.root.name, "path": str(s.root), "frames": s.frames, "status": s.manifest["status"],
                  "stop_reason": s.stop_reason, "error": s.error or ("; ".join(errors) or None),
                  "elapsed_s": round(s.elapsed, 2), "task": s.task, "camera_quality": quality,
                  "capture_gaps": {k: gaps[k] for k in ("missing_cycles", "skipped_cycles", "recorded_fraction", "skipped")}}
        if not failed and self.validate:
            self._set("validating")
            result["validation"] = self._validate(s.root)
        with self.lock:
            self.last_result = result
            self.session = None
            self.state = "idle"
        log.info("recording finished: %s", result)
        self.publish()

    @staticmethod
    def _validate(root):
        """Run teleop_server/check_mobile.py in its own process: full video decoding is CPU/GIL heavy
        and must not stall the live streams or the teleop loop."""
        try:
            proc = subprocess.run([sys.executable, "-m", "teleop_server.validate", str(root)], cwd=PROJECT_ROOT,
                                  capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            return {"passed": False, "error": "validation timed out"}
        if proc.returncode != 0:
            lines = [line for line in proc.stderr.strip().splitlines() if line.strip()]
            return {"passed": False, "error": lines[-1] if lines else f"exit code {proc.returncode}"}
        v = json.loads((root / "validation.json").read_text())
        return {"passed": True, "measured_sample_hz": v.get("measured_sample_hz"), "video_frames": v.get("video_frames"),
                "depth_frames": v.get("depth_frames"), "max_rgb_depth_dt_ms": v.get("max_rgb_depth_dt_ms"),
                "max_base_receive_age_ms": v.get("max_base_receive_age_ms")}

    @staticmethod
    def _write_manifest(root, manifest):
        (root / "session.json").write_text(json.dumps(manifest, indent=2))
