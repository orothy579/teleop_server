"""Dataset browser for the web UI: list recorded sessions, show one (labels, quality, videos, depth,
joint and base time series) and move one to ``<record_dir>/.trash`` ("delete").

Every entry point takes the record directory and a session name; names are validated with the
labels module's pattern, so no path outside the record directory is ever touched.
"""
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from teleop_server.labels import SESSION_RE, LabelError

CAMERA_RE = re.compile(r"^camera[1-3]$")
TRASH = ".trash"
MAX_POINTS = 600  # time-series samples sent to the browser per session
# Depth preview videos for the browser; outside the dataset folder so transfers never carry them.
PREVIEW_CACHE = Path(os.environ.get("TELEOP_PREVIEW_CACHE", "~/.cache/teleop_server/depth_preview")).expanduser()
PREVIEW_VERSION = 1


def _order(name):  # start time (to the second), then _n
    parts = name.removesuffix("_SYNTHETIC").split("_")
    return parts[0], parts[1], int(parts[2]) if len(parts) > 2 else 1


def _root(record_dir, session):
    if not isinstance(session, str) or not SESSION_RE.match(session):
        raise LabelError("잘못된 세션 이름입니다.")
    root = Path(record_dir) / session
    if not (root / "session.json").is_file():
        raise LabelError("세션을 찾을 수 없습니다.", 404)
    return root


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def summary(root):
    m = _read_json(root / "session.json") or {}
    v = _read_json(root / "validation.json") or m.get("validation") or {}
    fps = m.get("fps") or 30
    frames = m.get("frames") or 0
    elapsed = m.get("recording_elapsed_s") or m.get("elapsed_s") or (frames / fps if frames else None)
    passed = v.get("passed") if "passed" in v else (v.get("storage_checks") == "PASS" if v else None)
    return {"session": root.name, "status": m.get("status"), "frames": frames, "fps": fps, "elapsed_s": elapsed,
            "start_unix_ns": m.get("start_unix_ns"), "task": m.get("task"), "labels": m.get("labels"),
            "synthetic": m.get("synthetic", root.name.endswith("_SYNTHETIC")), "error": m.get("error"),
            "stop_reason": m.get("stop_reason"), "validation_passed": passed,
            "source": "jetson" if m.get("source_session") else "dgx"}


def list_all(record_dir, limit=1000):
    record_dir = Path(record_dir)
    if not record_dir.is_dir():
        return []
    names = sorted((p.name for p in record_dir.iterdir() if p.is_dir() and SESSION_RE.match(p.name)),
                   key=_order, reverse=True)[:limit]
    out = [summary(record_dir / n) for n in names if (record_dir / n / "session.json").is_file()]
    out.sort(key=lambda x: (x["start_unix_ns"] or 0, _order(x["session"])), reverse=True)
    return out


def _dir_size(path):
    total = 0
    if path.is_file():
        return path.stat().st_size
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def video_path(record_dir, session, camera):
    """The Jetson's original H.264 file if present, else the LeRobot dataset video (one episode)."""
    root = _root(record_dir, session)
    if not CAMERA_RE.match(camera or ""):
        raise LabelError("잘못된 카메라 이름입니다.")
    for p in (root / "rgb_mp4" / f"{camera}.mp4",
              root / "dataset" / "videos" / f"observation.images.{camera}" / "chunk-000" / "file-000.mp4"):
        if p.is_file():
            return p
    raise LabelError("영상 파일이 없습니다.", 404)


def _series(root, fps):
    """Downsampled time series from the LeRobot parquet: follower/leader joints and the base."""
    import numpy as np
    import pyarrow.parquet as pq
    files = sorted((root / "dataset" / "data").rglob("*.parquet")) if (root / "dataset" / "data").is_dir() else []
    if not files:
        return None
    info = _read_json(root / "dataset" / "meta" / "info.json") or {}
    feats = info.get("features", {})
    cols = [c for c in ("frame_index", "observation.state", "observation.leader_state", "action",
                        "observation.base_state") if c in pq.read_schema(files[0]).names]
    table = pq.read_table(files, columns=cols)
    n = table.num_rows
    if not n:
        return None
    idx = np.unique(np.linspace(0, n - 1, min(n, MAX_POINTS)).round().astype(int))

    def arr(name):
        if name not in cols:
            return None
        return np.asarray(table.column(name).to_pylist(), dtype=float)[idx]
    frame = arr("frame_index")
    follower = arr("observation.state")
    leader = arr("observation.leader_state")
    if leader is None:
        leader = arr("action")
    names = feats.get("observation.state", {}).get("names") or [f"joint {i}" for i in range(14)]
    out = {"frame": frame.astype(int).tolist(), "t": (frame / fps).round(3).tolist(), "joint_names": names,
           "follower": None if follower is None else np.round(follower, 4).tolist(),
           "leader": None if leader is None else np.round(leader, 4).tolist()}
    base = arr("observation.base_state")
    bnames = feats.get("observation.base_state", {}).get("names") or []
    if base is not None and all(k in bnames for k in ("x", "y", "qx", "qy", "qz", "qw")):
        col = {k: base[:, bnames.index(k)] for k in bnames}
        yaw = np.arctan2(2 * (col["qw"] * col["qz"] + col["qx"] * col["qy"]), 1 - 2 * (col["qy"] ** 2 + col["qz"] ** 2))
        out["base"] = {"x": np.round(col["x"], 4).tolist(), "y": np.round(col["y"], 4).tolist(),
                       "yaw": np.round(yaw, 4).tolist(),
                       "vx": np.round(col.get("vx", np.zeros(len(idx))), 4).tolist(),
                       "wz": np.round(col.get("wz", np.zeros(len(idx))), 4).tolist()}
    return out


def detail(record_dir, session):
    root = _root(record_dir, session)
    m = _read_json(root / "session.json") or {}
    v = _read_json(root / "validation.json") or m.get("validation") or {}
    s = summary(root)
    cams = sorted((m.get("cameras") or {}).keys()) or ["camera1", "camera2", "camera3"]
    cameras = []
    for cam in cams:
        try:
            video = video_path(record_dir, session, cam).relative_to(root).as_posix()
        except LabelError:
            video = None
        depth_dir = root / "depth_raw" / cam
        cameras.append({"name": cam, "video": video,
                        "depth_frames": sum(1 for _ in depth_dir.glob("*.png")) if depth_dir.is_dir() else 0})
    parts = {"rgb_mp4": root / "rgb_mp4", "depth_raw": root / "depth_raw", "dataset": root / "dataset"}
    sizes = {k: _dir_size(p) if p.exists() else 0 for k, p in parts.items()}
    sizes["total"] = _dir_size(root)
    sizes["other"] = sizes["total"] - sum(sizes[k] for k in parts)
    gaps = m.get("capture_gaps") or {}
    s.update(
        labels=m.get("labels"), cameras=cameras, sizes=sizes,
        recorded_task=m.get("recorded_task"), end_unix_ns=m.get("end_unix_ns"),
        camera_quality=m.get("camera_quality"),
        capture_gaps={k: gaps.get(k) for k in ("missing_cycles", "skipped_cycles", "recorded_fraction", "skipped")},
        validation={k: v.get(k) for k in ("passed", "storage_checks", "measured_sample_hz", "max_rgb_depth_dt_ms",
                                          "max_base_receive_age_ms", "camera_frames_skipped", "error") if k in v},
        depth_scale_m={c: (m.get("depth") or {}).get("cameras", {}).get(c, {}).get("depth_scale_m") for c in cams},
        series=_series(root, s["fps"]) if s["frames"] else None)
    return s


def depth_jpeg(record_dir, session, camera, index, max_m=1.0):
    """One stored raw depth frame as the live view's single-hue preview (near = bright)."""
    import cv2
    import numpy as np
    from teleop_server.sources.camera import depth_preview
    root = _root(record_dir, session)
    if not CAMERA_RE.match(camera or ""):
        raise LabelError("잘못된 카메라 이름입니다.")
    if not isinstance(index, int) or index < 0:
        raise LabelError("잘못된 프레임 번호입니다.")
    path = root / "depth_raw" / camera / f"{index:06d}.png"
    raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED) if path.is_file() else None
    if raw is None:
        raise LabelError("depth 프레임이 없습니다.", 404)
    m = _read_json(root / "session.json") or {}
    scale = (m.get("depth") or {}).get("cameras", {}).get(camera, {}).get("depth_scale_m") or 1e-4
    depth = raw.astype(np.float32) * np.float32(scale)
    ok, jpg = cv2.imencode(".jpg", depth_preview(depth, max_m, size=(raw.shape[1], raw.shape[0])),
                           [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise LabelError("depth 이미지를 만들 수 없습니다.", 500)
    valid = float((raw > 0).mean())
    return jpg.tobytes(), {"valid_fraction": round(valid, 3),
                           "median_m": round(float(np.median(depth[raw > 0])), 3) if valid else None}


class DepthPreviews:
    """Stored raw depth as 30 fps preview videos (the live view's single-hue mapping: near = bright,
    0-1 m, black = no measurement), one per camera, so the browser can play depth in step with RGB.
    Video frame i = depth_raw/<camera>/<i>.png. Made once in the background, then cached."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}  # (session, camera) -> {"done", "total", "error", "thread"}

    @staticmethod
    def _files(session, camera):
        d = PREVIEW_CACHE / session
        return d / f"{camera}.mp4", d / f"{camera}.json"

    @staticmethod
    def _stamp(root, camera):
        m = _read_json(root / "session.json") or {}
        return {"version": PREVIEW_VERSION, "frames": m.get("frames") or 0, "start_unix_ns": m.get("start_unix_ns"),
                "pngs": sum(1 for _ in (root / "depth_raw" / camera).glob("*.png"))}

    def _ready(self, root, camera):
        mp4, meta = self._files(root.name, camera)
        return mp4.is_file() and _read_json(meta) == self._stamp(root, camera)

    def status(self, record_dir, session):
        root = _root(record_dir, session)
        out = {}
        for cam in sorted(p.name for p in (root / "depth_raw").iterdir() if p.is_dir() and CAMERA_RE.match(p.name)) \
                if (root / "depth_raw").is_dir() else []:
            job = self.jobs.get((session, cam))
            if self._ready(root, cam):
                out[cam] = {"ready": True}
            elif job:
                out[cam] = {"ready": False, "done": job["done"], "total": job["total"], "error": job["error"]}
            else:
                out[cam] = {"ready": False, "done": 0, "total": 0, "error": None}
        return out

    def ensure(self, record_dir, session):
        """Start making whatever is missing; returns the status."""
        root = _root(record_dir, session)
        for cam in self.status(record_dir, session):
            key = (session, cam)
            with self.lock:
                job = self.jobs.get(key)
                if self._ready(root, cam) or (job and job["thread"].is_alive()):
                    continue
                job = {"done": 0, "total": 0, "error": None}
                job["thread"] = threading.Thread(target=self._make, args=(root, cam, job), daemon=True,
                                                 name=f"depth-preview-{cam}")
                self.jobs[key] = job
                job["thread"].start()
        return self.status(record_dir, session)

    def video(self, record_dir, session, camera):
        root = _root(record_dir, session)
        if not CAMERA_RE.match(camera or ""):
            raise LabelError("잘못된 카메라 이름입니다.")
        if not self._ready(root, camera):
            raise LabelError("depth 영상을 아직 만드는 중입니다.", 409)
        return self._files(session, camera)[0]

    def _make(self, root, camera, job):
        import av
        import cv2
        import numpy as np
        from teleop_server.sources.camera import depth_preview
        mp4, meta = self._files(root.name, camera)
        tmp = mp4.with_suffix(".tmp.mp4")
        try:
            stamp = self._stamp(root, camera)
            m = _read_json(root / "session.json") or {}
            fps = int(m.get("fps") or 30)
            scale = (m.get("depth") or {}).get("cameras", {}).get(camera, {}).get("depth_scale_m") or 1e-4
            frames = stamp["frames"] or stamp["pngs"]
            job["total"] = frames
            mp4.parent.mkdir(parents=True, exist_ok=True)
            out = av.open(str(tmp), "w", format="mp4", options={"movflags": "+faststart"})
            stream, img = None, None
            for i in range(frames):
                raw = cv2.imread(str(root / "depth_raw" / camera / f"{i:06d}.png"), cv2.IMREAD_UNCHANGED)
                if raw is not None:  # a missing frame repeats the previous one, keeping frame i at time i/fps
                    img = depth_preview(raw.astype(np.float32) * np.float32(scale), 1.0, size=(raw.shape[1], raw.shape[0]))
                if img is None:
                    continue
                if stream is None:
                    stream = out.add_stream("libx264", rate=fps)
                    stream.width, stream.height, stream.pix_fmt = img.shape[1], img.shape[0], "yuv420p"
                    stream.options = {"preset": "veryfast", "crf": "23"}
                    stream.codec_context.thread_count = 2
                frame = av.VideoFrame.from_ndarray(img, format="gray")
                frame.pts = i
                for packet in stream.encode(frame):
                    out.mux(packet)
                job["done"] = i + 1
            if stream is not None:
                for packet in stream.encode():
                    out.mux(packet)
            out.close()
            os.replace(tmp, mp4)
            meta.write_text(json.dumps(stamp))
        except Exception as exc:  # shown in the browser; the per-frame images still work
            job["error"] = repr(exc)
            tmp.unlink(missing_ok=True)

    @staticmethod
    def drop(session):
        if SESSION_RE.match(session or ""):
            shutil.rmtree(PREVIEW_CACHE / session, ignore_errors=True)


def trash(record_dir, session):
    """"Delete" = move to <record_dir>/.trash/ (recoverable until that folder is emptied)."""
    root = _root(record_dir, session)
    bin_ = Path(record_dir) / TRASH
    bin_.mkdir(exist_ok=True)
    target = bin_ / session
    if target.exists():
        target = bin_ / f"{session}.{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.move(str(root), target)
    DepthPreviews.drop(session)
    return target
