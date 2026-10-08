"""Jetson-owned sensor/control loop. Connects outward to the DGX receiver."""
import argparse
import asyncio
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections import deque

import numpy as np
from concurrent.futures import ThreadPoolExecutor

from aiohttp import ClientSession, ClientTimeout, WSMsgType

from teleop_server.base_control import BaseController, FakeTwistPublisher, RosTwistPublisher
from teleop_server.latency import StatsRegistry
from teleop_server.server import build_sources, parse_args as sensor_args
from teleop_server.sources.odom import OdomSourceBase
from teleop_server.sources.camera import FakeCamera, RealSenseCamera
from .local_recorder import MANIFEST, LocalRecorder
from .protocol import GAP_LOG_S, LINK_TIMEOUT_S, MAX_PACKET, encode_camera, pack_packet

log = logging.getLogger(__name__)
# Commands that start motion are dropped when they arrive late (never replayed after a stall);
# commands that stop something are always executed, however late.
EXPIRING_COMMANDS = {"control_start", "base_enable", "base_cmd", "record_start"}


class PresentRealSenseCamera(RealSenseCamera):
    encode_previews = False  # CaptureHub keeps headers only; previews are rebuilt on the DGX
    def _stream(self, stall_restart_s=5.0):
        import pyrealsense2 as rs
        # pipeline.start() for an absent serial can hold Python's GIL for seconds.
        # Check USB enumeration first so a missing camera cannot starve control heartbeats.
        serials = [d.get_info(rs.camera_info.serial_number) for d in rs.context().query_devices()]
        if self.serial not in serials:
            raise RuntimeError(f"Camera {self.serial} is not connected to USB")
        return super()._stream(stall_restart_s)


class RawDepthFakeCamera(FakeCamera):
    """Synthetic camera whose depth is on the D405 0.1 mm grid, like real raw z16 depth."""
    SCALE = 0.0001
    encode_previews = False

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        if self.depth_enabled:
            intr = dict(width=self.width, height=self.height, fx=400., fy=400., ppx=self.width/2, ppy=self.height/2,
                        model="distortion.brown_conrady", coeffs=[0.]*5)
            self.info.update(depth_scale_m=self.SCALE, depth_aligned_to="none", depth_intrinsics=intr,
                             color_intrinsics=dict(intr, model="distortion.inverse_brown_conrady"),
                             depth_to_color_extrinsics=dict(rotation=[1., 0., 0., 0., 1., 0., 0., 0., 1.],
                                                            translation=[0., 0., 0.]))

    def emit(self, bgr, t_capture_ns, t_recv_ns, capture_clock, extra=None, depth_m=None, depth_dt_ms=None,
             depth_units=None):
        if depth_m is not None:
            scale = np.float32(self.SCALE)
            depth_units = np.rint(depth_m / scale).astype(np.uint16)
            depth_m = depth_units.astype(np.float32) * scale
        super().emit(bgr, t_capture_ns, t_recv_ns, capture_clock, extra, depth_m, depth_dt_ms, depth_units)


class CaptureHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.messages = {}

    def publish(self, key, header, payload=None):
        # JPEG previews are reconstructed on DGX from the transmitted frame pair.
        if key.startswith("depth:"):
            return
        with self.lock:
            self.messages[key] = dict(header)

    def snapshot(self):
        with self.lock:
            return [dict(v) for k, v in self.messages.items() if not k.startswith("cam:")]


class BaseCapture(OdomSourceBase):
    """Capture every raw Odometry/Twist plus coherent latest samples on Jetson."""
    def __init__(self, hub, stats, odom_topic, cmd_topic):
        super().__init__(hub, stats)
        self.odom_topic, self.cmd_topic = odom_topic, cmd_topic
        self.lock = threading.Lock()
        self.latest = self.command = None
        self.raw = deque(maxlen=2000)
        self.raw_seq = self.raw_dropped = 0
        self.raw_tap = None  # Jetson recorder: receives every raw message while recording
        self.info = {"topic": odom_topic, "cmd_topic": cmd_topic}

    def ingest(self, kind, message, values, stamp=None, frame="odom", child="base_link"):
        mono, unix = time.monotonic_ns(), time.time_ns()
        with self.lock:
            self.raw_seq += 1
            if len(self.raw) == self.raw.maxlen:
                self.raw_dropped += 1
            item = dict(kind=kind, sequence=self.raw_seq, receive_monotonic_ns=mono,
                        receive_unix_ns=unix, message=message)
            self.raw.append(item)
            tap = self.raw_tap
            if tap is not None:
                tap(item)
            sample = dict(values=values, receive_ns=mono)
            if kind == "odom":
                sample.update(stamp_ns=stamp or unix, frame_id=frame, child_frame_id=child)
                self.latest = sample
            else:
                self.command = sample
        if kind == "odom":
            self.emit(values[:3], values[3:7], values[7:10], values[10:], stamp or unix, unix, frame, child)

    def snapshot(self, drain=True):
        with self.lock:
            result = dict(odom=self.latest, command=self.command, raw=list(self.raw) if drain else [],
                          raw_dropped=self.raw_dropped, now_ns=time.monotonic_ns())
            if drain:
                self.raw.clear()
            return result

    def run(self):
        import rclpy
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry
        from rclpy.qos import qos_profile_sensor_data
        from rclpy.executors import SingleThreadedExecutor
        from rosidl_runtime_py.convert import message_to_ordereddict
        ctx = rclpy.Context()
        rclpy.init(context=ctx)
        node = rclpy.create_node("teleop_jetson_capture", context=ctx)
        executor = SingleThreadedExecutor(context=ctx)
        executor.add_node(node)
        def odom(msg):
            p, q, v, w = msg.pose.pose.position, msg.pose.pose.orientation, msg.twist.twist.linear, msg.twist.twist.angular
            self.ingest("odom", message_to_ordereddict(msg),
                        [p.x, p.y, p.z, q.x, q.y, q.z, q.w, v.x, v.y, v.z, w.x, w.y, w.z],
                        msg.header.stamp.sec*10**9+msg.header.stamp.nanosec, msg.header.frame_id, msg.child_frame_id)
        def cmd(msg):
            v, w = msg.linear, msg.angular
            self.ingest("cmd_vel", message_to_ordereddict(msg), [v.x, v.y, v.z, w.x, w.y, w.z])
        node.create_subscription(Odometry, self.odom_topic, odom, qos_profile_sensor_data)
        node.create_subscription(Twist, self.cmd_topic, cmd, qos_profile_sensor_data)
        self._set_state("running")
        try:
            while not self.stop_event.is_set():
                executor.spin_once(timeout_sec=0.05)
        finally:
            executor.shutdown()
            node.destroy_node()
            rclpy.shutdown(context=ctx)


class FakeBaseCapture(BaseCapture):
    def __init__(self, hub, stats):
        super().__init__(hub, stats, "fake", "fake")
        self.vx = self.wz = 0.0
        self.info["fake"] = True

    def command_input(self, vx, wz):
        self.vx, self.wz = vx, wz
        values = [vx, 0., 0., 0., 0., wz]
        self.ingest("cmd_vel", {"synthetic": True, "values": values}, values)

    def run(self):
        self._set_state("running")
        while not self.stop_event.wait(0.05):
            values = [0., 0., 0., 0., 0., 0., 1., self.vx, 0., 0., 0., 0., self.wz]
            self.ingest("odom", {"synthetic": True, "values": values}, values)


class Edge:
    def __init__(self, args, sensors):
        self.args, self.sensor_args = args, sensors
        self.boot = str(uuid.uuid4())
        self.hub, self.stats = CaptureHub(), StatsRegistry()
        odom_mode = sensors.odom
        sensors.odom = "off"
        self.sources = build_sources(sensors, self.hub, self.stats)
        if sensors.cameras == "realsense":
            self.sources = [s for s in self.sources if s.kind != "camera"] + [
                PresentRealSenseCamera(f"camera{i+1}", self.hub, self.stats, serial,
                                       fps=sensors.fps, jpeg_quality=sensors.jpeg_quality,
                                       scale=sensors.scale, depth=sensors.depth,
                                       align_depth=args.depth_mode == "aligned")
                for i, serial in enumerate(sensors.serials)]
        elif sensors.cameras == "fake":
            self.sources = [s for s in self.sources if s.kind != "camera"] + [
                RawDepthFakeCamera(f"camera{i+1}", self.hub, self.stats, fps=sensors.fps,
                                   jpeg_quality=sensors.jpeg_quality, scale=sensors.scale, depth=sensors.depth)
                for i in range(3)]
        sensors.odom = odom_mode
        self.cameras = [s for s in self.sources if s.kind == "camera"]
        self.arms = next(s for s in self.sources if s.kind == "arms")
        self.base_capture = (FakeBaseCapture(self.hub, self.stats) if odom_mode == "fake" else
                             BaseCapture(self.hub, self.stats, sensors.odom_topic, sensors.cmd_topic))
        self.sources.append(self.base_capture)
        publisher = (FakeTwistPublisher(self.base_capture.command_input) if odom_mode == "fake"
                     else RosTwistPublisher(sensors.cmd_topic))
        self.base = BaseController(self.hub, publisher, sensors.base_max_linear, sensors.base_max_angular)
        # Robot control is one unit: control_start enables the base, any end of arm control disables it.
        self.arms.exit_hooks.append(lambda error: self.base.disable("robot control stopped"))
        self.link_fault = threading.Event()
        original_make = self.arms.make_arms
        def make_arms():
            arms = original_make()
            for arm in arms.values():
                original_close = arm.close
                arm.close = lambda fault, close=original_close: close(fault or self.link_fault.is_set())
            return arms
        self.arms.make_arms = make_arms
        self.arms.cycle_sink = self.on_cycle
        # Recording happens here on the Jetson; the DGX receives the finished session afterwards.
        self.recorder = LocalRecorder(args.record_dir, self.cameras, self.base_capture, sensors.fps, self.boot,
                                      synthetic=sensors.cameras == "fake", crf=args.rgb_crf, preset=args.rgb_preset,
                                      depth_compressor=args.depth_codec,
                                      sampling=getattr(args, "camera_sampling", "buffered"))
        self.cycle_count = 0
        self.stream_tick = 0
        self.uploading = False
        # While recording or uploading, the live view gets every Nth cycle (CPU and Wi-Fi go to the data).
        self.reduced_every = max(1, round(sensors.fps / args.preview_hz_recording))
        # Keep raw capture references in a small latest-value queue. Submitting compression jobs
        # here would move the backlog into ThreadPoolExecutor's unbounded work queue, where old
        # frames could no longer be cancelled and live latency would grow by hundreds of ms.
        self.encoder = ThreadPoolExecutor(max_workers=args.encode_workers, thread_name_prefix="encode")
        self.pending = queue.Queue(maxsize=args.queue)
        self.sequence = self.dropped = 0
        self.connected = False
        self.last_control = 0.
        self.control_grace_until = 0.0
        self.control_transition_started = 0.0
        self.control_transition_action = None
        self.transition_done_at = 0.0   # when the arms reached their final state (watchdog baseline)
        self.done = threading.Event()
        self.control_session = None
        self.control_ws = None
        self.link = {"timeout_s": LINK_TIMEOUT_S, "gaps": 0, "max_gap_s": 0.0, "last_gap": None}

    def snapshot(self, cycle=None):
        frames = []
        for camera in self.cameras:
            frame = camera.latest_frame(paired=camera.depth_enabled)
            if frame is not None:
                info = dict(camera.info)
                with self.hub.lock:
                    capture = self.hub.messages.get("cam:"+camera.name)
                    if capture and capture.get("seq") == frame.seq:
                        info["capture"] = dict(capture)
                frames.append((camera.name, frame, info))
        meta = dict(boot=self.boot, sequence=0, sample_unix_ns=time.time_ns(),
                    sample_monotonic_ns=time.monotonic_ns(), sources=[s.status() for s in self.sources],
                    messages=self.hub.snapshot(), cycle=None, base=self.base_capture.snapshot(drain=False))
        if cycle is not None:
            meta["cycle"] = {k: getattr(cycle, k) for k in cycle.__slots__}
        return meta, frames

    def enqueue(self, cycle=None):
        if not self.connected:
            return
        meta, frames = self.snapshot(cycle)
        self.sequence += 1
        meta["sequence"] = self.sequence
        meta["queued_monotonic_ns"] = time.monotonic_ns()
        if self.pending.full():
            try:
                self.pending.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
        self.pending.put_nowait((meta, frames))

    def reduced(self):
        return self.recorder.state != "idle" or self.uploading

    def stream_cycle(self, cycle=None):
        self.stream_tick += 1
        if not self.reduced() or self.stream_tick % self.reduced_every == 0:
            self.enqueue(cycle)

    def on_cycle(self, cycle):
        self.cycle_count += 1
        self.recorder.on_cycle(cycle, self.cycle_count)
        self.stream_cycle(cycle)

    def idle_capture(self):
        period = 1/self.sensor_args.fps
        deadline = time.monotonic()
        while not self.done.wait(max(0., deadline-time.monotonic())):
            deadline = max(deadline+period, time.monotonic()-period)
            if self.arms.state != "running":
                self.cycle_count += 1  # cycle numbers keep counting, so a control pause is a visible gap
                self.recorder.note_idle()
                self.stream_cycle()

    def link_lost(self):
        if self.connected:
            self.recorder.note_event("DGX link lost: arm control stopped (recording continues)")
        self.connected = False
        self.control_grace_until = 0.0
        self.control_transition_started = 0.0
        self.control_transition_action = None
        self.transition_done_at = 0.0
        self.link_fault.set()
        self.base.disable("DGX link lost")
        self.arms.stop_control()

    async def control(self, session):
        url = self.args.dgx.rstrip("/") + "/edge/control"
        async with session.ws_connect(url, heartbeat=120, max_msg_size=1<<20) as ws:
            self.control_ws = ws
            self.control_session = str(uuid.uuid4())
            await ws.send_json(dict(type="hello", boot=self.boot, session=self.control_session,
                                    synthetic=self.sensor_args.cameras == "fake", rgb_codec=self.args.rgb_codec,
                                    fps=self.sensor_args.fps, sources=[s.status() for s in self.sources]))
            self.connected = True
            self.last_control = time.monotonic()
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                now = time.monotonic()
                if now-self.last_control > GAP_LOG_S:
                    self.note_gap(now-self.last_control)
                self.last_control = now
                if data.get("type") == "ping":
                    await ws.send_json(dict(type="pong", t0=data["t0"], edge_ns=time.time_ns(),
                                            sources=[s.status() for s in self.sources], base=self.base.status(),
                                            dropped=self.dropped, recorder=self.recorder.status(), link=self.link))
                    continue
                if data.get("type") != "command":
                    continue
                action = data.get("action")
                reply = {}
                try:
                    # Late commands that start motion are never replayed after network stalls or
                    # reconnects; late stop commands still run.
                    age = (time.time_ns() - data["issued_ns"]) / 1e9
                    if action in EXPIRING_COMMANDS and not -0.15 <= age <= 0.3:
                        raise RuntimeError("expired command or unsynchronized clocks")
                    if age > 0.3:
                        log.warning("%s arrived %.2f s late; executed", action, age)
                    if action == "control_start":
                        if len(self.cameras) != 3 or any(c.state != "running" or c.latest_frame(True) is None or
                                time.time_ns()-c.latest_frame(True).t_recv_ns > 500_000_000 for c in self.cameras):
                            raise RuntimeError("three fresh RGB/depth camera pairs required")
                        if self.arms.thread is not None and self.arms.thread.is_alive():
                            raise RuntimeError("arms are already active")
                        self.control_transition_started = time.monotonic()
                        self.control_grace_until = self.control_transition_started+60.0
                        self.control_transition_action = action
                        self.transition_done_at = 0.0
                        self.link_fault.clear()
                        self.arms.start_control()
                        self.base.enable()
                        self.recorder.note_event("arm control started")
                    elif action == "control_stop":
                        self.control_transition_started = time.monotonic()
                        self.control_grace_until = self.control_transition_started+60.0
                        self.control_transition_action = action
                        self.transition_done_at = 0.0
                        self.base.disable("arm control stopped")
                        self.arms.stop_control()
                        self.recorder.note_event("arm control stopped")
                    elif action == "base_enable":
                        self.base.enable()
                    elif action == "base_disable":
                        self.base.disable("user")
                    elif action == "base_cmd":
                        error = self.base.command(data["owner"], data.get("vx"), data.get("wz"))
                        if error:
                            raise RuntimeError(error)
                    elif action == "base_release":
                        self.base.release(data["owner"])
                    elif action == "record_start":
                        if self.arms.state != "running":
                            raise RuntimeError("팔 제어가 실행 중일 때만 녹화할 수 있습니다.")
                        reply["session"] = self.recorder.start(
                            data.get("task") or "", extra={"dgx_issued_ns": data["issued_ns"]})
                    elif action == "record_stop":
                        reply["session"] = self.recorder.request_stop(data.get("reason") or "user")
                    else:
                        raise ValueError("unknown action")
                    await ws.send_json(dict(type="result", id=data["id"], ok=True, **reply))
                except Exception as exc:
                    if action in ("control_start", "control_stop"):
                        self.control_grace_until = 0.0
                        self.control_transition_started = 0.0
                        self.control_transition_action = None
                    await ws.send_json(dict(type="result", id=data["id"], ok=False, error=str(exc)))

    async def transmit(self, session):
        while not self.done.is_set():
            if not self.connected:
                await asyncio.sleep(0.1)
                continue
            try:
                async with session.ws_connect(self.args.dgx.rstrip("/")+"/edge/data", heartbeat=120,
                                               max_msg_size=MAX_PACKET) as ws:
                    await ws.send_json(dict(boot=self.boot, session=self.control_session))
                    await self.stream(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Only the camera/telemetry stream is affected: reconnect it. Arm control ends only
                # when the control link itself is silent (watchdog).
                log.warning("data link lost (%r); reconnecting it, arm control continues", exc)
                await asyncio.sleep(1)

    async def stream(self, ws):
        """Send packets in order with up to ``window`` unacknowledged; DGX acks every sequence."""
        inflight = deque()
        acked = asyncio.Event()
        async def read_acks():
            try:
                async for message in ws:
                    if message.type != WSMsgType.TEXT:
                        continue
                    ack = json.loads(message.data)
                    if not inflight or ack.get("sequence") != inflight.popleft():
                        raise RuntimeError("invalid data acknowledgement")
                    acked.set()
                raise RuntimeError("data link closed")
            finally:
                acked.set()
        reader = asyncio.create_task(read_acks())

        def take():
            """Oldest queued cycle, or None. Discard only a stale backlog (>100 ms): short scheduling
            bursts keep every capture cycle; sustained overload jumps to the newest snapshot (the
            DGX records the sequence gap and keeps recording)."""
            try:
                meta, frames = self.pending.get_nowait()
            except queue.Empty:
                return None
            while time.monotonic_ns()-meta["queued_monotonic_ns"] > 100_000_000:
                try:
                    meta, frames = self.pending.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    break
            jobs = [self.encoder.submit(encode_camera, name, frame, info, self.args.rgb_codec,
                                        self.args.depth_codec) for name, frame, info in frames]
            return meta, jobs

        current = ahead = None
        try:
            while self.connected:
                if reader.done():
                    reader.result()
                if current is None:
                    current = take()
                    if current is None:
                        await asyncio.sleep(0.002)
                        continue
                # Pipeline: compress the next cycle while this one is finished and sent.
                if ahead is None:
                    ahead = take()
                meta, jobs = current
                encoded = [await asyncio.wrap_future(job) for job in jobs]
                queued_ns = meta.pop("queued_monotonic_ns")
                meta["encode_ms"] = (time.monotonic_ns()-queued_ns)/1e6
                # The allowed wait is re-evaluated every 0.25 s instead of being fixed up front: an
                # arm connect/disconnect can block this event loop for seconds (native Trossen calls
                # hold the GIL), and a deadline computed before that must not expire during it.
                waited_from = time.monotonic()
                while len(inflight) >= self.args.window:
                    acked.clear()
                    if time.monotonic()-waited_from > self.transport_limit():
                        raise TimeoutError("data acknowledgement timeout")
                    try:
                        await asyncio.wait_for(acked.wait(), 0.25)
                    except asyncio.TimeoutError:
                        pass
                    if reader.done():
                        reader.result()
                base_raw = self.base_capture.snapshot()
                meta["base"]["raw"] = base_raw["raw"]
                meta["base"]["raw_dropped"] = base_raw["raw_dropped"]
                meta["dropped"] = self.dropped
                meta["queue"] = self.pending.qsize()
                meta["inflight"] = len(inflight)
                meta["sent_unix_ns"] = time.time_ns()
                packet = pack_packet(meta, encoded)
                inflight.append(meta["sequence"])
                send = asyncio.ensure_future(ws.send_bytes(packet))
                sent_from = time.monotonic()
                while not send.done():
                    await asyncio.wait({send}, timeout=0.25)
                    if not send.done() and time.monotonic()-sent_from > self.transport_limit():
                        send.cancel()
                        raise TimeoutError("data send timeout")
                send.result()
                current, ahead = ahead, None
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    def transport_limit(self):
        """Seconds a data-link wait may take: generous only during a deliberate arm transition."""
        if time.monotonic() < self.control_grace_until or self.arms.state in ("starting", "stopping"):
            return 60.0
        return LINK_TIMEOUT_S

    def note_gap(self, gap):
        transition = time.monotonic() < self.control_grace_until or self.arms.state in ("starting", "stopping")
        self.link["gaps"] += 1
        self.link["max_gap_s"] = round(max(self.link["max_gap_s"], gap), 3)
        self.link["last_gap"] = {"gap_s": round(gap, 3), "unix_ns": time.time_ns(), "arm_transition": transition}
        log.warning("DGX -> Jetson control gap %.2f s (limit %.1f s)%s", gap, LINK_TIMEOUT_S,
                    " during an arm transition" if transition else "")

    async def uploader(self, session):
        """Move finished sessions to the DGX; delete here only after the DGX verified every file."""
        base = self.args.dgx.rstrip("/")
        limit = self.args.upload_mbps * 1e6 / 8  # bytes/s: leave Wi-Fi room for the control link
        while not self.done.is_set():
            await asyncio.sleep(1)
            pending = self.recorder.pending()
            self.uploading = bool(pending)
            if not pending or not self.connected:
                continue
            name = pending[0]
            root = self.recorder.root / name
            try:
                manifest = json.loads((root / MANIFEST).read_text())
                files = manifest["files"]
                progress = dict(session=name, sent=0, total=sum(f["size"] for f in files.values()))
                self.recorder.upload = progress
                started = time.monotonic()

                async def chunks(path):
                    with open(path, "rb") as f:
                        while block := f.read(1 << 20):
                            progress["sent"] += len(block)
                            yield block
                            ahead = progress["sent"]/limit - (time.monotonic()-started)
                            if ahead > 0:
                                await asyncio.sleep(ahead)

                for fname, meta in files.items():
                    async with session.put(f"{base}/edge/upload/{name}/{fname}", data=chunks(root / fname),
                                           headers={"X-Sha256": meta["sha256"], "X-Size": str(meta["size"])},
                                           timeout=ClientTimeout(total=None, sock_read=120)) as response:
                        if response.status != 200:
                            raise RuntimeError(f"{fname}: HTTP {response.status} {await response.text()}")
                async with session.post(f"{base}/edge/upload/{name}/commit", json=manifest,
                                        timeout=ClientTimeout(total=120)) as response:
                    body = await response.json()
                    if response.status != 200 or not body.get("ok"):
                        raise RuntimeError(f"commit refused: {body}")
                await asyncio.to_thread(self.recorder.delete, name)
                log.info("session %s moved to the DGX (%.0f MB) and deleted here", name, progress["total"]/1e6)
            except Exception as exc:
                log.exception("upload of %s failed; files kept, retrying", name)
                if self.recorder.upload:
                    self.recorder.upload["error"] = str(exc)
                await asyncio.sleep(5)
            finally:
                if self.recorder.upload and "error" not in self.recorder.upload:
                    self.recorder.upload = None

    async def watchdog(self):
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            transitioning = self.arms.state in ("starting", "stopping")
            finished_states = (("running", "error", "stopped")
                               if self.control_transition_action == "control_start" else ("error", "stopped"))
            # End grace after a post-transition control message arrives. Otherwise the watchdog
            # could run first when a GIL-holding native call returns and reject a healthy socket
            # based on the deliberately stale pre-call timestamp. Silence is then measured from the
            # moment the arms finished, so a stall right at that moment still ends after the
            # normal limit instead of the 60 s grace.
            if self.control_grace_until and self.arms.state in finished_states:
                if not self.transition_done_at:
                    self.transition_done_at = now
                if (self.last_control >= self.control_transition_started or
                        now-self.transition_done_at > LINK_TIMEOUT_S):
                    self.control_grace_until = 0.0
                    self.control_transition_started = 0.0
                    self.control_transition_action = None
            timeout = 60.0 if transitioning or now < self.control_grace_until else LINK_TIMEOUT_S
            silent = now-max(self.last_control, self.transition_done_at)
            if self.connected and silent > timeout:
                log.error("control link silent for %.2f s (limit %.1f s): stopping arm control and base",
                          silent, timeout)
                self.link_lost()
                if self.control_ws:
                    await self.control_ws.close()

    async def run(self):
        for source in self.sources:
            source.start()
        self.base.start()
        await asyncio.gather(*(asyncio.to_thread(s.ready.wait, 20.) for s in self.sources if s.kind != "arms"))
        producer = threading.Thread(target=self.idle_capture, daemon=True)
        producer.start()
        headers = {"Authorization": "Bearer "+self.args.token}
        async with ClientSession(headers=headers, timeout=ClientTimeout(total=None, sock_connect=5)) as session:
            tasks = [asyncio.create_task(self.transmit(session)), asyncio.create_task(self.watchdog()),
                     asyncio.create_task(self.uploader(session))]
            try:
                while True:
                    try:
                        await self.control(session)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("control link disconnected")
                    finally:
                        self.link_lost()
                    await asyncio.sleep(1)
            finally:
                self.done.set()
                self.link_lost()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.to_thread(self.base.stop)
                self.encoder.shutdown(wait=False, cancel_futures=True)
                await asyncio.gather(*(asyncio.to_thread(s.stop) for s in self.sources))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dgx", required=True)
    parser.add_argument("--token", default=os.environ.get("TELEOP_EDGE_TOKEN"))
    parser.add_argument("--rgb-codec", choices=["png", "jpeg"], default="jpeg")
    parser.add_argument("--depth-mode", choices=["raw", "aligned"], default="raw",
                        help="raw: the depth sensor's own z16 image (default); aligned: reprojected to colour")
    parser.add_argument("--encode-workers", type=int, default=3, help="Parallel camera encoder threads")
    parser.add_argument("--record-dir", default="~/ws/teleop_jetson/recordings",
                        help="Sessions are recorded here, then moved to the DGX")
    parser.add_argument("--rgb-crf", type=int, default=18, help="H.264 quality of the recorded RGB mp4")
    parser.add_argument("--rgb-preset", default="superfast", help="x264 preset (Jetson CPU vs size)")
    parser.add_argument("--preview-hz-recording", type=float, default=10.0,
                        help="Live view rate while recording/uploading")
    parser.add_argument("--camera-sampling", choices=["buffered", "latest"], default="buffered",
                        help="Recording: oldest unrecorded frame (absorbs arm-loop jitter) or newest frame")
    parser.add_argument("--upload-mbps", type=float, default=200.0, help="Session upload rate limit (Mbit/s)")
    parser.add_argument("--depth-codec", choices=["lz4", "zlib"], default="lz4",
                        help="Lossless depth compressor (lz4: ~7x less Jetson CPU, ~30 %% more bytes)")
    parser.add_argument("--window", type=int, default=2, help="Unacknowledged packets in flight")
    parser.add_argument("--queue", type=int, default=4, help="Raw cycles buffered; stale backlog is dropped")
    args, rest = parser.parse_known_args()
    if not args.token:
        parser.error("TELEOP_EDGE_TOKEN is required")
    sensors = sensor_args(rest)
    if sensors.arms == "off" or sensors.odom == "off" or not sensors.depth:
        parser.error("arms, odometry and depth are required")
    if sensors.fps != sensors.arm_rate:
        parser.error("fps and arm-rate must match")
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(Edge(args, sensors).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
