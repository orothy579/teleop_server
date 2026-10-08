"""RGB(+depth) camera sources. Frames are JPEG-encoded in the capture thread and published as binary.

Depth is captured in the same frameset as the colour image, aligned to the colour pixel grid
(``rs.align``) and converted to metres, so every recorded RGB frame has its depth pair.
"""
import threading
import time
from collections import deque

import cv2
import numpy as np

from teleop_server.latency import MinOffsetClock
from teleop_server.sources.base import Source

DEPTH_VIEW_MIN_M = 0.05
PAIR_MAX_DT_MS = 2.0  # RGB/depth of one exposure differ by ~0.3 ms on the D405; one frame off is ~33 ms
PAIR_WINDOW = 150         # framesets used for the recent unpaired percentage
PAIR_RESTART_PCT = 20.0   # sustained above this: reopen the RealSense pipeline (observed once to fix it)


class CameraFrame:
    """One capture: RGB and (optionally) its paired depth in metres. ``depth_units`` is the same
    depth as the camera's uint16 units when known (lets encoders skip the float round trip)."""
    __slots__ = ("rgb", "depth", "t_recv_ns", "seq", "frame_number", "depth_dt_ms", "depth_units")

    def __init__(self, rgb, depth, t_recv_ns, seq, frame_number, depth_dt_ms, depth_units=None):
        self.rgb, self.depth, self.t_recv_ns = rgb, depth, t_recv_ns
        self.seq, self.frame_number, self.depth_dt_ms = seq, frame_number, depth_dt_ms
        self.depth_units = depth_units


def depth_preview(depth_m, max_m, size=(320, 240)):
    """Single-hue preview: near = bright, far = dark, no measurement = black."""
    n = np.clip((depth_m - DEPTH_VIEW_MIN_M) / (max_m - DEPTH_VIEW_MIN_M), 0.0, 1.0)
    img = (255 - 195 * n).astype(np.uint8)
    img[depth_m <= 0] = 0
    return cv2.resize(img, size, interpolation=cv2.INTER_NEAREST)


class CameraSource(Source):
    kind = "camera"
    encode_previews = True  # JPEG + depth preview for browsers; the Jetson edge has none

    def __init__(self, name, hub, stats, width=640, height=480, fps=30, jpeg_quality=80, scale=1.0,
                 depth=False, depth_preview_hz=5.0, depth_view_max_m=1.0):
        super().__init__(name, hub, stats)
        self.width, self.height, self.fps = width, height, fps
        self.jpeg_quality, self.scale = jpeg_quality, scale
        self.depth_enabled = depth
        self.depth_preview_hz, self.depth_view_max_m = depth_preview_hz, depth_view_max_m
        self._last_preview = 0.0
        self.seq = 0
        self._latest = None         # latest CameraFrame (full resolution, not the lossy JPEG)
        self._latest_paired = None  # latest CameraFrame whose depth is from the same exposure
        self.unpaired = 0
        self._pair_hist = deque(maxlen=PAIR_WINDOW)
        self._recent_paired = deque(maxlen=4)  # for recorders that consume frames in order
        self._recent_lock = threading.Lock()

    def unpaired_recent_pct(self):
        """Percentage of recent framesets whose depth was not from the same exposure (None: no data)."""
        if len(self._pair_hist) < 30:
            return None
        return 100.0 * (1.0 - sum(self._pair_hist) / len(self._pair_hist))

    def latest_frame(self, paired=False):
        """Latest capture as a CameraFrame (RGB uint8 + depth float32 metres or None), or None.
        With ``paired`` only frames whose RGB and depth were verified to be one exposure."""
        return self._latest_paired if paired else self._latest

    def paired_after(self, seq):
        """Paired frames newer than ``seq``, oldest first (the last few only)."""
        with self._recent_lock:
            return [f for f in self._recent_paired if f.seq > seq]

    def emit(self, bgr, t_capture_ns, t_recv_ns, capture_clock, extra=None, depth_m=None, depth_dt_ms=None,
             depth_units=None):
        """Encode and publish. ``t_capture_ns`` is the exposure timestamp mapped to host unix ns."""
        extra = extra or {}
        if self.depth_enabled:
            paired = depth_m is not None and depth_dt_ms is not None and abs(depth_dt_ms) <= PAIR_MAX_DT_MS
            self._pair_hist.append(paired)
            pct = self.unpaired_recent_pct()
            self.info["unpaired_recent_pct"] = None if pct is None else round(pct, 1)
        if depth_m is not None and (depth_dt_ms is None or abs(depth_dt_ms) > PAIR_MAX_DT_MS):
            # The syncer occasionally hands over depth from the neighbouring exposure (seen at
            # start-up and rarely later): never offer that as a pair.
            self.unpaired += 1
            self.info["unpaired_framesets"] = self.unpaired
            depth_m = depth_units = None
        # Keep an RGB copy (the RealSense buffer is reused; LeRobot datasets store RGB).
        frame = CameraFrame(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), depth_m, t_recv_ns, self.seq + 1,
                            extra.get("frame_number", self.seq + 1), depth_dt_ms,
                            None if depth_m is None else depth_units)
        self._latest = frame
        if depth_m is not None:
            self._latest_paired = frame
            with self._recent_lock:
                self._recent_paired.append(frame)
        if self.scale != 1.0 and self.encode_previews:
            bgr = cv2.resize(bgr, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA)
        if self.encode_previews:
            ok, jpg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
            if not ok:
                raise RuntimeError("JPEG encode failed")
        else:
            jpg = np.empty(0, np.uint8)  # header only: timing metadata, no image
        t_enc = time.time_ns()
        self.seq += 1
        stages = {"sensor_ms": (t_recv_ns - t_capture_ns) / 1e6 if t_capture_ns else None,
                  "exposure_ms": extra.get("exposure_to_frame_end_ms"),
                  "transport_ms": (t_recv_ns - extra["t_frame_end_ns"]) / 1e6 if "t_frame_end_ns" in extra else None,
                  "encode_ms": (t_enc - t_recv_ns) / 1e6}
        self.stats.record(stages, t_recv_ns)
        header = {"type": "camera", "sensor": self.name, "seq": self.seq, "t_capture_ns": t_capture_ns,
                  "t_recv_ns": t_recv_ns, "t_encoded_ns": t_enc, "capture_clock": capture_clock,
                  "width": bgr.shape[1], "height": bgr.shape[0], "bytes": int(jpg.size),
                  "depth_dt_ms": depth_dt_ms, **extra}
        self.hub.publish(f"cam:{self.name}", header, jpg.tobytes())
        if (self.encode_previews and depth_m is not None
                and time.monotonic() - self._last_preview >= 1.0 / self.depth_preview_hz):
            self._last_preview = time.monotonic()
            ok, dj = cv2.imencode(".jpg", depth_preview(depth_m, self.depth_view_max_m), [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                valid = float((depth_m > 0).mean())
                self.hub.publish(f"depth:{self.name}", {"type": "depth_preview", "camera": self.name,
                                                       "t_recv_ns": t_recv_ns, "valid_fraction": round(valid, 3),
                                                       "range_m": [DEPTH_VIEW_MIN_M, self.depth_view_max_m]},
                                 dj.tobytes())


def _intrinsics(profile):
    intr = profile.as_video_stream_profile().get_intrinsics()
    return {"width": intr.width, "height": intr.height, "fx": intr.fx, "fy": intr.fy,
            "ppx": intr.ppx, "ppy": intr.ppy, "model": str(intr.model), "coeffs": list(intr.coeffs)}


class RealSenseCamera(CameraSource):
    """RealSense colour (+ depth). Uses librealsense global time so timestamps are host-clock.

    Depth is aligned to the colour grid by default; with ``align_depth=False`` it is the sensor's
    own z16 image (depth optical frame), sent with its intrinsics and depth->colour extrinsics."""

    def __init__(self, name, hub, stats, serial, align_depth=True, **kw):
        super().__init__(name, hub, stats, **kw)
        self.serial = serial
        self.align_depth = align_depth
        self.info = {"serial": serial, "depth": self.depth_enabled}

    def run(self):
        # Like lerobot's RealSenseCamera, never give up on a stalled or lost stream: a stall is
        # reported (UI shows "no data"), a lost device is reopened once it reappears on USB.
        while not self.stop_event.is_set():
            try:
                self._stream()
            except Exception as exc:
                self.info["restarts"] = self.info.get("restarts", 0) + 1
                self._set_state("error", repr(exc))
                self.stop_event.wait(2.0)

    def _stream(self, stall_restart_s=5.0):
        import pyrealsense2 as rs
        pipe, cfg = rs.pipeline(), rs.config()
        cfg.enable_device(self.serial)
        cfg.enable_stream(rs.stream.color, self.width, self.height, rs.format.bgr8, self.fps)
        if self.depth_enabled:
            cfg.enable_stream(rs.stream.depth, self.width, self.height, rs.format.z16, self.fps)
        profile = pipe.start(cfg)
        try:
            device = profile.get_device()
            for sensor in device.query_sensors():
                if sensor.supports(rs.option.global_time_enabled):
                    sensor.set_option(rs.option.global_time_enabled, 1)
            self.info["color_intrinsics"] = _intrinsics(profile.get_stream(rs.stream.color))
            align = scale = None
            if self.depth_enabled:
                scale = device.first_depth_sensor().get_depth_scale()  # D405: 0.0001 m per unit
                self.info.update(depth_scale_m=scale, depth_unit="m")
                if self.align_depth:
                    # Depth comes from a different optical frame (D405: ~0.4 deg rotation, different
                    # intrinsics); reproject it onto the colour pixel grid so pixels correspond 1:1.
                    align = rs.align(rs.stream.color)
                    self.info["depth_aligned_to"] = "color"
                else:
                    depth_stream, color_stream = profile.get_stream(rs.stream.depth), profile.get_stream(rs.stream.color)
                    e = depth_stream.get_extrinsics_to(color_stream)
                    self.info.update(depth_aligned_to="none", depth_intrinsics=_intrinsics(depth_stream),
                                     depth_to_color_extrinsics={
                                         "rotation": list(e.rotation), "translation": list(e.translation),
                                         "convention": "librealsense column-major rotation, metres"})
            relative = MinOffsetClock()
            last_frame = time.monotonic()
            while not self.stop_event.is_set():
                ok, frames = pipe.try_wait_for_frames(timeout_ms=1000)
                if not ok:
                    self.info["frame_timeouts"] = self.info.get("frame_timeouts", 0) + 1
                    if time.monotonic() - last_frame > stall_restart_s:
                        raise RuntimeError(f"no frames for {stall_restart_s:.0f} s")
                    continue
                last_frame = time.monotonic()
                t_recv = time.time_ns()
                depth_m = depth_dt = depth_units = None
                if align is not None:
                    frames = align.process(frames)
                if self.depth_enabled:
                    depth = frames.get_depth_frame()
                color = frames.get_color_frame()
                if not color:
                    continue
                if self.depth_enabled:
                    if depth:
                        depth_units = np.array(depth.get_data(), dtype=np.uint16, copy=True)  # raw z16
                        depth_m = depth_units.astype(np.float32) * np.float32(scale)
                        depth_dt = color.get_timestamp() - depth.get_timestamp()  # same frameset: ~0.3 ms
                    else:
                        self.info["missing_depth"] = self.info.get("missing_depth", 0) + 1
                # get_timestamp() is the frame (readout end) time; in global_time domain librealsense
                # maps it to the host clock by regression, which jitters by a few ms.
                domain = color.get_frame_timestamp_domain()
                ts_ns = int(color.get_timestamp() * 1e6)
                if domain == rs.timestamp_domain.global_time:
                    t_frame_end, clock = ts_ns, "global"
                else:  # device clock: offset is only known relative to fastest delivery
                    t_frame_end, clock = relative.to_host_ns(ts_ns, t_recv), "relative"
                # sensor_timestamp marks mid-exposure on the device clock; shift back to it so
                # latency is measured from when the image content was actually captured.
                md = rs.frame_metadata_value
                mid_to_end_ms = None
                if color.supports_frame_metadata(md.frame_timestamp) and color.supports_frame_metadata(md.sensor_timestamp):
                    mid_to_end_ms = (color.get_frame_metadata(md.frame_timestamp)
                                     - color.get_frame_metadata(md.sensor_timestamp)) / 1000.0
                t_capture = t_frame_end - int((mid_to_end_ms or 0.0) * 1e6)
                extra = {"frame_number": color.get_frame_number(), "t_frame_end_ns": t_frame_end,
                         "exposure_to_frame_end_ms": mid_to_end_ms}
                if color.supports_frame_metadata(md.actual_exposure):
                    extra["exposure_us"] = color.get_frame_metadata(md.actual_exposure)
                self.emit(np.asanyarray(color.get_data()), t_capture, t_recv, clock, extra, depth_m, depth_dt,
                          depth_units)
                if self.state != "running":
                    self._set_state("running")  # only after the first real frame
                pct = self.unpaired_recent_pct()
                if self.depth_enabled and len(self._pair_hist) == PAIR_WINDOW and pct > PAIR_RESTART_PCT:
                    self.info["pairing_restarts"] = self.info.get("pairing_restarts", 0) + 1
                    self._pair_hist.clear()
                    raise RuntimeError(f"RGB/depth pairing lost ({pct:.0f}% of recent framesets); reopening camera")
        finally:
            pipe.stop()


class FakeCamera(CameraSource):
    """Synthetic frames (+ synthetic paired depth) with a simulated exposure-to-host delay."""

    def __init__(self, name, hub, stats, sensor_delay_ms=(8, 16), mismatch_every=0, **kw):
        super().__init__(name, hub, stats, **kw)
        self.sensor_delay_ms = sensor_delay_ms
        self.mismatch_every = mismatch_every  # >0: every Nth frameset carries the previous depth
        self.info = {"fake": True, "depth": self.depth_enabled}
        if self.depth_enabled:
            self.info.update(depth_aligned_to="color", depth_unit="m")

    def run(self):
        rng = np.random.default_rng(abs(hash(self.name)) % 2**32)
        base = np.zeros((self.height, self.width, 3), np.uint8)
        base[..., 0] = np.linspace(40, 200, self.width, dtype=np.uint8)[None, :]
        base[..., 1] = np.linspace(30, 120, self.height, dtype=np.uint8)[:, None]
        # Depth scene that matches the image: a tilted floor, the red patch is a near object.
        floor = (0.5 + 0.3 * np.arange(self.height, dtype=np.float32)[:, None] / self.height
                 + 0.1 * np.arange(self.width, dtype=np.float32)[None, :] / self.width)
        next_t = time.monotonic()
        while not self.stop_event.is_set():
            next_t += 1 / self.fps
            self.sleep_until(next_t)
            t_recv = time.time_ns()
            t_capture = t_recv - int(rng.uniform(*self.sensor_delay_ms) * 1e6)
            img = base.copy()
            x = int((self.seq * 7) % self.width)
            img[:, max(0, x - 6):x + 6] = 255
            cv2.putText(img, f"{self.name} #{self.seq + 1}", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
            img[380:460, 20:100] = (0, 0, 255)  # pure red in BGR: lets tests check stored channel order
            depth, dt = None, None
            if self.depth_enabled:
                mismatch = self.mismatch_every and (self.seq + 1) % self.mismatch_every == 0
                xd = int(((self.seq - 1) * 7) % self.width) if mismatch else x  # previous exposure
                depth = np.broadcast_to(floor, (self.height, self.width)).copy()
                depth[:, max(0, xd - 6):xd + 6] = 0.4  # the moving bar is nearer than the floor
                depth[380:460, 20:100] = 0.25          # the red patch: pairs RGB and depth regions
                depth[:16, :16] = 0.0                  # no measurement
                dt = -33.3 if mismatch else 0.3
            self.emit(img, t_capture, t_recv, "simulated", {"frame_number": self.seq + 1}, depth, dt)
            if self.state != "running":
                self._set_state("running")  # like RealSenseCamera: only after the first frame
