"""DGX: turn an uploaded Jetson session into the teleop_server session layout.

The recorder's own writer (``Recorder._write`` / ``_finish``) builds the LeRobot dataset, the raw
depth PNGs, the quality counters, session.json and the validation, so a Jetson-recorded session has
exactly the same format as before. Timestamps stay on the Jetson clock.
"""
import json
import queue
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from teleop_server.recorder import (DEFAULT_TASK, RAW_DEPTH_DIR, REPO_ID, ROBOT_TYPE, Recorder, Session,
                                 collector_modules)
from teleop_server.sources.arms import Cycle
from teleop_server.sources.camera import CameraFrame
from .local_recorder import DEPTH_CODECS, DEPTH_MAGIC, DEPTH_RECORD, MANIFEST, skip_hint
from .protocol import decode_depth

CODEC_NAMES = {v: k for k, v in DEPTH_CODECS.items()}


class _NoBase:
    def close(self):
        pass


class _DepthReader:
    def __init__(self, path):
        self.f = open(path, "rb")
        if self.f.read(len(DEPTH_MAGIC)) != DEPTH_MAGIC:
            raise ValueError(f"{path.name}: not a depth container")

    def read(self, index, shape, scale):
        head = self.f.read(DEPTH_RECORD.size)
        if len(head) != DEPTH_RECORD.size:
            raise ValueError(f"depth container ended before frame {index}")
        got, length, codec = DEPTH_RECORD.unpack(head)
        if got != index:
            raise ValueError(f"depth frame {got} where {index} was expected")
        return decode_depth(self.f.read(length), CODEC_NAMES[codec], shape, scale)

    def at_end(self):
        return self.f.read(1) == b""

    def close(self):
        self.f.close()


class SessionConverter(Recorder):
    def __init__(self, raw_dir, output_dir, publish=None, validate=True):
        self.raw = Path(raw_dir)
        self.jm = json.loads((self.raw / MANIFEST).read_text())
        cameras = [SimpleNamespace(name=n, info=dict(info), depth_enabled=True, unpaired=0, state="running")
                   for n, info in self.jm["cameras"].items()]
        arms = SimpleNamespace(cycle_sink=None, exit_hooks=[], state="running", rate=self.jm["fps"])
        extra = {"architecture": "Jetson records locally -> DGX after stop", "clock": self.jm["clock"],
                 "source_session": self.jm["session"], "jetson_boot": self.jm["boot"],
                 "rgb_recorded": self.jm["rgb"], "depth_recorded": self.jm["depth"]}
        super().__init__(SimpleNamespace(publish=lambda *a, **k: None), arms, cameras, lambda path: _NoBase(),
                         fps=self.jm["fps"], synthetic=self.jm["synthetic"], output_dir=output_dir,
                         default_task=self.jm.get("task") or DEFAULT_TASK, manifest_extra=extra, validate=validate,
                         depth=True, depth_storage="png16")
        self._publish = publish or (lambda: None)
        self.total = self.jm["frames"]

    def publish(self):
        self._publish()

    def features(self, mc):
        feats = super().features(mc)
        # Jetson arm-cycle number of every stored frame; gaps are cycles skipped at capture time.
        feats["observation.capture_sequence"] = {"dtype": "int64", "shape": (1,), "names": ["jetson_cycle"]}
        return feats

    def _target_dir(self):
        """Final folder named like the Jetson session. A leftover from an interrupted conversion
        (status "converting") is replaced; any other existing folder gets a _n suffix."""
        name = self.jm["session"]
        for n in range(1, 100):
            root = self.output_dir / (name if n == 1 else f"{name}_{n}")
            if root.exists():
                try:
                    if json.loads((root / "session.json").read_text()).get("status") == "converting":
                        shutil.rmtree(root)
                    else:
                        continue
                except (OSError, ValueError):
                    continue
            root.mkdir(parents=True)
            return root
        raise RuntimeError("could not create the session folder")

    def convert(self):
        """Blocking. Returns the result dict (same shape as a live recording's last_result)."""
        from lerobot.configs.video import RGBEncoderConfig
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        jm = self.jm
        if not jm.get("frames"):
            return self._no_frames()
        mc, check = collector_modules()
        root = self._target_dir()
        task = jm.get("task") or self.default_task
        manifest = {"task": task, "status": "converting", "base_control": "subscribe_only", **self.manifest_extra,
                    "fps": self.fps, "requested_seconds": None, "recorder": "teleop_server (Jetson-local)",
                    "synthetic": self.synthetic, "start_unix_ns": jm["start_unix_ns"], "frames": 0,
                    "cameras": {c.name: dict(c.info) for c in self.cameras}, "depth": self._depth_manifest(),
                    "base_frames": jm.get("base_frames")}
        self._write_manifest(root, manifest)
        shutil.copy2(self.raw / "base_raw.jsonl", root / "base_raw.jsonl")
        dataset = LeRobotDataset.create(REPO_ID, self.fps, self.features(mc), root=root / "dataset",
                                        robot_type=ROBOT_TYPE, video_backend="pyav",
                                        rgb_encoder=RGBEncoderConfig(vcodec="h264"), image_writer_threads=8)
        s = Session(root, manifest, task, _NoBase(), dataset, mc, check)
        s.unpaired_start, s.unpaired_stop = [0] * len(self.cameras), list(jm.get("camera_unpaired") or [0] * 3)
        s.queue = queue.Queue(maxsize=60)
        for cam in self.cameras:
            (root / RAW_DEPTH_DIR / cam.name).mkdir(parents=True)
        s.depth_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="depth-png")
        s.skipped = dict(jm.get("skipped") or {})
        s.events = [{"frame": e.get("frame"), "event": f"Jetson: {e['event']}"} for e in jm.get("events") or []]
        s.events += [{"frame": None, "event": f"Jetson error: {e}"} for e in jm.get("errors") or []]
        if jm.get("raw_messages_lost"):
            s.events.append({"frame": None, "event": f"base_raw messages lost on Jetson: {jm['raw_messages_lost']}"})
        s.t0, s.t_stop = 0.0, float(jm["elapsed_s"])  # elapsed = Jetson recording time, not conversion time
        s.stop_reason = jm.get("stop_reason")
        if jm.get("errors"):
            s.error = "; ".join(jm["errors"])
        s.writer = threading.Thread(target=self._write, args=(s,), name="convert-writer", daemon=True)
        with self.lock:
            self.session, self.state = s, "saving"
        s.writer.start()
        try:
            self._feed(s)
        except Exception as exc:
            s.error = s.error or f"conversion: {exc!r}"
        s.stop_event.set()
        s.writer.join()
        result = dict(self.last_result, source_session=jm["session"])
        if result["status"] == "completed":
            self._keep_originals(root)
        return result

    def _no_frames(self):
        """Every cycle was skipped on the Jetson (no video files exist). Keep a failed session that
        says why, with the uploaded files, instead of failing on a missing mp4."""
        jm = self.jm
        skipped = jm.get("skipped") or {}
        top = max(skipped, key=skipped.get) if skipped else None
        error = ("녹화된 프레임 없음: " + (f"{skip_hint(top)} (건너뛴 주기 {skipped[top]}개)" if top else "기록된 주기가 없습니다.")
                 + (f" · Jetson 오류: {'; '.join(jm['errors'])}" if jm.get("errors") else ""))
        root = self._target_dir()
        manifest = {"task": jm.get("task") or self.default_task, "status": "failed", **self.manifest_extra,
                    "fps": self.fps, "recorder": "teleop_server (Jetson-local)", "synthetic": self.synthetic,
                    "start_unix_ns": jm["start_unix_ns"], "frames": 0, "stop_reason": jm.get("stop_reason"),
                    "error": error, "elapsed_s": jm.get("elapsed_s"),
                    "capture_gaps": {"recorded_frames": 0, "skipped": skipped, "events": jm.get("events") or []},
                    "jetson_upload": "jetson_upload/ (files as received; no frames)"}
        self._write_manifest(root, manifest)
        shutil.move(str(self.raw), root / "jetson_upload")
        return {"session": root.name, "path": str(root), "frames": 0, "status": "failed", "error": error,
                "stop_reason": jm.get("stop_reason"), "elapsed_s": jm.get("elapsed_s"), "task": manifest["task"],
                "capture_gaps": {"missing_cycles": 0, "skipped_cycles": sum(skipped.values()),
                                 "recorded_fraction": 0.0, "skipped": skipped},
                "source_session": jm["session"]}

    def _put(self, s, item):
        while True:
            if not s.writer.is_alive():
                raise RuntimeError("dataset writer stopped")
            try:
                return s.queue.put(item, timeout=0.5)
            except queue.Full:
                continue

    def _feed(self, s):
        import av
        names = [c.name for c in self.cameras]
        scales = {c.name: c.info["depth_scale_m"] for c in self.cameras}
        containers = {n: av.open(str(self.raw / f"rgb_{n}.mp4")) for n in names}
        videos = {n: containers[n].decode(video=0) for n in names}
        depths = {n: _DepthReader(self.raw / f"depth_{n}.bin") for n in names}
        try:
            with open(self.raw / "cycles.jsonl") as lines:
                for i, line in enumerate(lines):
                    rec = json.loads(line)
                    if rec["index"] != i:
                        raise ValueError(f"cycles.jsonl line {i} has index {rec['index']}")
                    frames = []
                    for cam in rec["cameras"]:
                        n = cam["name"]
                        rgb = next(videos[n]).to_ndarray(format="rgb24")
                        depth = depths[n].read(i, tuple(cam["shape"]), scales[n])
                        frames.append(CameraFrame(rgb, depth, cam["t_recv_ns"], cam["seq"], cam["frame_number"],
                                                  cam["depth_dt_ms"]))
                    base = rec["base"]
                    self._put(s, (Cycle(**rec["cycle"]), frames, rec["sample_unix_ns"], base["odom"], base["command"],
                                  base["now_ns"],
                                  {"observation.capture_sequence": np.array([rec["cycle_number"]], dtype=np.int64)}))
            for n in names:
                if next(videos[n], None) is not None or not depths[n].at_end():
                    raise ValueError(f"{n}: more frames than cycles")
        finally:
            for c in containers.values():
                c.close()
            for d in depths.values():
                d.close()

    def _keep_originals(self, root):
        """Keep the Jetson's RGB mp4 (better than the re-encoded dataset video) and cycle log."""
        (root / "rgb_mp4").mkdir()
        for cam in self.cameras:
            shutil.move(str(self.raw / f"rgb_{cam.name}.mp4"), root / "rgb_mp4" / f"{cam.name}.mp4")
        shutil.copy2(self.raw / "cycles.jsonl", root / "capture_cycles.jsonl")
        manifest = json.loads((root / "session.json").read_text())
        manifest["rgb_original"] = {"path": "rgb_mp4/<camera>.mp4", **self.jm["rgb"],
                                    "note": "as recorded on the Jetson; dataset videos are re-encoded by LeRobot"}
        manifest["capture_log"] = "capture_cycles.jsonl (Jetson clock, one line per dataset frame)"
        self._write_manifest(root, manifest)
