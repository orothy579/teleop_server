import asyncio
import json
import queue
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
from aiohttp import ClientSession, web

from teleop_server.server import parse_args
from teleop_server.sources.camera import CameraFrame
from teleop_server.distributed.protocol import encode_packet, decode_packet
from teleop_server.distributed.edge import Edge
from teleop_server.distributed.server import create_app, BRIDGE, Bridge, prepare_packet
from teleop_server.distributed.protocol import LINK_TIMEOUT_S


def setUpModule():
    # Importing LeRobot/torch holds the GIL for seconds. Edge and DGX share this test process, so do
    # it before any stream runs (in production they are separate machines).
    try:
        from teleop_server.recorder import collector_modules
        collector_modules()
        import lerobot.datasets.lerobot_dataset  # noqa: F401
    except ImportError:
        pass  # Jetson: no recording dependencies; only the codec tests apply there


class CodecTests(unittest.TestCase):
    def test_live_jpeg_is_forwarded_without_dgx_recompression(self):
        rgb = np.arange(480*640*3, dtype=np.uint32).reshape(480, 640, 3).astype(np.uint8)
        frame = CameraFrame(rgb, None, 123, 1, 1, None)
        for codec in ("jpeg", "png"):
            packet = encode_packet({"sequence": 1}, [("camera1", frame, {})], codec)
            _, decoded = decode_packet(packet, with_encoded_rgb=True)
            _, prepared = prepare_packet(packet, False)
            jpeg = prepared["camera1"][3]
            self.assertTrue(jpeg.startswith(b"\xff\xd8"))
            if codec == "jpeg":
                self.assertEqual(jpeg, decoded["camera1"][3])
            else:
                self.assertNotEqual(jpeg, decoded["camera1"][3])

    def test_lossless_rgb_and_metric_depth(self):
        rng = np.random.default_rng(4)
        rgb = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
        depth = rng.integers(0, 65535, (480, 640), dtype=np.uint16).astype(np.float32)*np.float32(.0001)
        frame = CameraFrame(rgb, depth, 123, 7, 19, .305)
        for info in ({}, {"depth_scale_m": .0001}):
            for compressor in ("lz4", "zlib"):
                data = encode_packet({"sequence": 7}, [("camera1", frame, info)], "png", compressor)
                meta, frames = decode_packet(data)
                out_rgb, out_depth, desc = frames["camera1"]
                np.testing.assert_array_equal(rgb, out_rgb)
                np.testing.assert_array_equal(depth, out_depth)  # bit-exact with either compressor
                self.assertEqual(desc["frame_number"], 19)
                self.assertEqual(desc["depth_dt_ms"], .305)
                expected = ("u16-delta-" + compressor) if info else "f32-zlib"
                self.assertEqual(desc["depth_codec"], expected)

    def test_fast_depth_path_keeps_the_wire_format(self):
        """Raw uint16 units (Jetson fast path) give byte-identical blobs to the original float path."""
        import lz4.frame
        from teleop_server.distributed.protocol import encode_depth, decode_depth

        def original(units):  # the format as first defined (int32 zigzag, stacked planes)
            previous = np.zeros_like(units)
            previous[:, 1:] = units[:, :-1]
            delta = (units-previous).view("<i2").astype(np.int32)
            zigzag = ((delta << 1) ^ (delta >> 31)).astype(np.uint16)
            return np.stack((zigzag & 255, zigzag >> 8)).astype(np.uint8).tobytes()
        rng = np.random.default_rng(7)
        smooth = (3000 + np.cumsum(rng.integers(-40, 41, (480, 640)), axis=1)).clip(0, 65535).astype(np.uint16)
        for units in (rng.integers(0, 65536, (480, 640), dtype=np.uint16), smooth,
                      np.array([[0, 65535, 0, 1, 65534, 32768, 32767]], np.uint16)):
            metres = units.astype(np.float32)*np.float32(.0001)
            for compressor in ("lz4", "zlib"):
                fast = encode_depth(None, .0001, compressor, units)
                self.assertEqual(fast, encode_depth(metres, .0001, compressor))
                planes = lz4.frame.decompress(fast[1]) if compressor == "lz4" else __import__("zlib").decompress(fast[1])
                self.assertEqual(planes, original(units))
                np.testing.assert_array_equal(decode_depth(fast[1], fast[0], units.shape, .0001), metres)

    def test_reject_corrupt_and_trailing_payloads(self):
        frame = CameraFrame(np.zeros((4, 4, 3), np.uint8), np.ones((4, 4), np.float32), 1, 1, 1, .3)
        data = encode_packet({}, [("camera1", frame, {})])
        for bad in (b"", b"\xff"*16, data[:-1], data+b"x"):
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                decode_packet(bad)
        # lz4 depth: corrupted bytes and a wrong declared size are rejected.
        lz = encode_packet({}, [("camera1", frame, {"depth_scale_m": .0001})])
        meta_len = int.from_bytes(lz[:4], "little")
        for i in (len(lz) - 3, len(lz) - 12):
            bad = bytearray(lz); bad[i] ^= 0xFF
            with self.assertRaises(ValueError):
                decode_packet(bytes(bad))
        self.assertGreater(meta_len, 0)


class LivePreviewTests(unittest.TestCase):
    def test_rgb_stays_full_rate_and_depth_preview_does_not_change_recording_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            sensors = parse_args(["--cameras", "fake", "--arms", "fake", "--odom", "fake",
                                  "--fps", "30", "--arm-rate", "30"])
            edge = Edge(Namespace(dgx="http://127.0.0.1:1", token="test", rgb_codec="jpeg", depth_codec="lz4",
                                  encode_workers=3, window=2, queue=4, depth_mode="raw", record_dir=tmp,
                                  rgb_crf=18, rgb_preset="superfast", preview_hz_recording=30,
                                  preview_depth_hz=10, upload_mbps=1000), sensors)
            edge.connected = True
            original = CameraFrame(np.zeros((4, 4, 3), np.uint8), np.ones((4, 4), np.float32),
                                   time.time_ns(), 1, 1, .3)
            for camera in edge.cameras:
                camera._latest = camera._latest_paired = original
            try:
                for state in ("idle", "recording"):
                    edge.recorder.state = state
                    depth_packets = 0
                    for _ in range(30):
                        edge.stream_cycle()
                        meta, frames = edge.pending.get_nowait()
                        self.assertEqual(len(frames), 3)
                        expected_shape = (4, 4, 3) if state == "idle" else (2, 2, 3)
                        self.assertTrue(all(frame.rgb.shape == expected_shape for _, frame, _ in frames))
                        has_depth = all(frame.depth is original.depth for _, frame, _ in frames)
                        depth_packets += has_depth
                        self.assertTrue(has_depth or all(frame.depth is None for _, frame, _ in frames))
                    self.assertEqual(depth_packets, 10)
                    self.assertEqual(edge.sequence, 30 if state == "idle" else 60)
                    self.assertTrue(all(camera.latest_frame(paired=True).depth is original.depth
                                        for camera in edge.cameras))
                    self.assertTrue(all(camera.latest_frame(paired=True).rgb is original.rgb
                                        for camera in edge.cameras))

                writer_queue = queue.Queue()
                for _ in range(16):
                    writer_queue.put(object())
                edge.recorder.current = SimpleNamespace(writers={"camera1": SimpleNamespace(queue=writer_queue)})
                start = edge.sequence
                for _ in range(30):
                    edge.stream_cycle()
                    while not edge.pending.empty():
                        edge.pending.get_nowait()
                self.assertTrue(edge.writer_backoff)
                self.assertEqual(edge.sequence-start, 10)
                for _ in range(12):
                    writer_queue.get_nowait()
                self.assertEqual(edge.preview_stride(), 1)
            finally:
                edge.encoder.shutdown(wait=False, cancel_futures=True)


class CameraSamplingTests(unittest.TestCase):
    """Jetson recording picks one camera frame per arm cycle; arm-loop jitter must not repeat frames."""

    class Camera:
        name = "camera1"

        def __init__(self):
            self.frames = []

        def arrive(self, seq):
            self.frames = (self.frames + [SimpleNamespace(seq=seq)])[-4:]

        def paired_after(self, seq):
            return [f for f in self.frames if f.seq > seq]

        def latest_frame(self, paired=True):
            return self.frames[-1] if self.frames else None

    def run_cycles(self, sampling, cycle_ms, first_frame_ms=5.0):
        from teleop_server.distributed.local_recorder import LocalRecorder, Recording
        with tempfile.TemporaryDirectory() as tmp:
            recorder = LocalRecorder(tmp, [], None, 30, "boot", sampling=sampling)
            rec, cam = Recording("x", Path(tmp), ""), self.Camera()
            used, t, seq = [], 0.0, 0
            for dt in cycle_ms:
                t += dt
                while first_frame_ms + seq*1000/30 <= t:  # a steady 30 fps camera
                    seq += 1
                    cam.arrive(seq)
                frame, behind = recorder._pick(rec, cam)
                used.append(frame.seq)
                rec.last_seq[cam.name], rec.behind[cam.name] = frame.seq, behind  # as on_cycle commits
            return np.diff(np.asarray(used))

    def test_buffered_sampling_absorbs_arm_loop_jitter(self):
        rng = np.random.default_rng(3)
        # Like the Jetson arm loop: mostly 33 ms, frequent 50 ms overruns followed by ~17 ms catch-ups.
        cycles = [33.3]*5 + [x for _ in range(150) for x in ((50.0, 16.6) if rng.random() < .4 else (33.3,))]
        latest, buffered = self.run_cycles("latest", cycles), self.run_cycles("buffered", cycles)
        self.assertGreater(int((latest == 0).sum()), 20)        # the old behaviour: many repeats
        self.assertLessEqual(int((buffered == 0).sum()), 1)
        self.assertLessEqual(int((buffered > 1).sum()), 2)

    def test_buffered_sampling_does_not_stay_a_frame_behind(self):
        # Cycles exactly in step with the camera but starting two frames late: resync to the newest.
        diffs = self.run_cycles("buffered", [1000/30]*60, first_frame_ms=-40.0)
        self.assertEqual(int((diffs == 0).sum()), 0)
        self.assertEqual(int((diffs == 2).sum()), 1)  # one deliberate jump, then in step


class ClockEstimatorTests(unittest.TestCase):
    def test_recent_samples_replace_pre_ntp_clock_offset(self):
        bridge = Bridge("secret")
        # Initial samples say DGX is 350 ms behind Jetson.
        for i in range(7):
            t0 = 1_000_000_000+i*10_000_000
            bridge.observe_clock(t0, t0+350_000_000, t0+2_000_000)
        self.assertAlmostEqual(bridge.offset_ns/1e6, -349, delta=.01)
        # After an NTP step, the rolling window must follow the new +12 ms offset.
        for i in range(7):
            t0 = 2_000_000_000+i*10_000_000
            bridge.observe_clock(t0, t0-12_000_000, t0+2_000_000)
        self.assertAlmostEqual(bridge.offset_ns/1e6, 13, delta=.01)

    def test_arm_transition_temporarily_relaxes_control_deadline(self):
        bridge = Bridge("secret")
        bridge.ws = SimpleNamespace(closed=False)
        bridge.last_control = time.monotonic()-LINK_TIMEOUT_S-1
        self.assertFalse(bridge.connected)
        bridge.control_grace_until = time.monotonic()+10.0
        self.assertTrue(bridge.connected)
        bridge.disconnected("test")
        self.assertFalse(bridge.in_control_transition)

    def test_stop_grace_remains_until_arms_finish_disconnect(self):
        bridge = Bridge("secret")
        bridge.control_grace_until = time.monotonic()+10.0
        bridge.control_transition_edge_ns = 100
        bridge.control_transition_action = "control_stop"
        sources = [source.status() for source in bridge.sources.values()]
        next(item for item in sources if item["name"] == "arms")["state"] = "running"
        bridge.update_sources(sources, 101)
        self.assertTrue(bridge.in_control_transition)
        next(item for item in sources if item["name"] == "arms")["state"] = "stopped"
        bridge.update_sources(sources, 102)
        self.assertFalse(bridge.in_control_transition)


class StallProxy:
    """TCP proxy between Jetson and DGX that can freeze all traffic, like a Wi-Fi stall."""

    def __init__(self, target_port):
        self.target_port = target_port
        self.flowing = asyncio.Event()
        self.flowing.set()
        self.tasks = set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def handle(self, reader, writer):
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.target_port)

        async def pump(src, dst):
            try:
                while data := await src.read(1 << 16):
                    await self.flowing.wait()
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                dst.close()
        pair = {asyncio.create_task(pump(reader, up_writer)), asyncio.create_task(pump(up_reader, writer))}
        self.tasks |= pair
        await asyncio.gather(*pair, return_exceptions=True)

    async def stall(self, seconds):
        self.flowing.clear()
        await asyncio.sleep(seconds)
        self.flowing.set()

    async def close(self):
        self.flowing.set()
        self.server.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    FPS = 10

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jetson_tmp = tempfile.TemporaryDirectory()  # the Jetson's own disk
        self.preload = patch("teleop_server.server._preload_recording", lambda: None)
        self.preload.start()
        self.app = create_app("test-secret", Path(self.tmp.name), fps=self.FPS, validate=False)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.proxy = StallProxy(port)  # the Jetson reaches the DGX through it ("Wi-Fi")
        await self.proxy.start()
        sensors = parse_args(["--cameras", "fake", "--arms", "fake", "--odom", "fake",
                              "--fps", str(self.FPS), "--arm-rate", str(self.FPS)])
        self.edge = Edge(Namespace(dgx=f"http://127.0.0.1:{self.proxy.port}", token="test-secret", rgb_codec="jpeg", depth_codec="lz4",
                                   encode_workers=3, window=2, queue=4, depth_mode="raw",
                                   record_dir=self.jetson_tmp.name, rgb_crf=18,
                                   rgb_preset="superfast", preview_hz_recording=self.FPS,
                                   preview_depth_hz=10, upload_mbps=1000), sensors)
        self.edge.recorder.min_free_bytes = 0
        self.task = asyncio.create_task(self.edge.run())
        self.client = ClientSession()

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.client.close()
        await self.proxy.close()
        await self.runner.cleanup()
        self.preload.stop()
        self.tmp.cleanup()
        self.jetson_tmp.cleanup()

    async def until(self, predicate, timeout=8):
        end = asyncio.get_running_loop().time()+timeout
        while not predicate():
            if asyncio.get_running_loop().time() > end:
                self.fail("Timed out waiting for distributed state")
            await asyncio.sleep(.05)

    async def test_stream_control_watchdog_and_reconnect(self):
        bridge = self.app[BRIDGE]
        await self.until(lambda: bridge.received >= 3)
        self.assertTrue(all(bridge.sources[f"camera{i}"].latest_frame(True) is not None for i in range(1, 4)))
        self.assertIsNotNone(bridge.base_sample["odom"])
        response = await self.client.get(self.url+"/edge/control")
        self.assertEqual(response.status, 401)
        await bridge.call("control_start")
        await self.until(lambda: bridge.sources["arms"].state == "running")
        self.assertTrue(self.edge.base.enabled, "robot control start enables the base too")
        await self.until(lambda: (bridge.base_status or {}).get("enabled"))
        await bridge.call("base_cmd", owner="test", vx=.1, wz=.2)
        await self.until(lambda: self.edge.base.active)
        await asyncio.sleep(.4)
        self.assertFalse(self.edge.base.active)
        self.assertEqual(self.edge.base.publisher.sent[-1][1:], (0., 0.))
        await bridge.ws.close()
        await self.until(lambda: self.edge.arms.thread is not None and not self.edge.arms.thread.is_alive())
        self.assertTrue(self.edge.link_fault.is_set())
        self.assertFalse(self.edge.base.enabled)
        prior = bridge.received
        await self.until(lambda: bridge.received > prior+3)
        self.assertNotEqual(self.edge.arms.state, "running")

    async def test_robot_control_stop_disables_base(self):
        bridge = self.app[BRIDGE]
        await self.until(lambda: bridge.received >= 3)
        await bridge.call("control_start")
        await self.until(lambda: bridge.sources["arms"].state == "running")
        self.assertTrue(self.edge.base.enabled)
        await bridge.call("control_stop")
        await self.until(lambda: bridge.sources["arms"].state in ("stopped", "idle"), timeout=20)
        self.assertFalse(self.edge.base.enabled)
        self.assertEqual(self.edge.base.publisher.sent[-1][1:], (0., 0.))

    async def _control_running(self):
        bridge = self.app[BRIDGE]
        await self.until(lambda: bridge.received >= 3)
        await bridge.call("control_start")
        await self.until(lambda: bridge.sources["arms"].state == "running" and not bridge.in_control_transition)
        return bridge

    async def test_short_wifi_stall_keeps_robot_control(self):
        if self.FPS != 10:
            self.skipTest("link timing does not depend on the frame rate")
        bridge = await self._control_running()
        thread = self.edge.arms.thread
        await self.proxy.stall(LINK_TIMEOUT_S/2)  # 1.5 s: the old 0.75 s / 1 s limits stopped the arms here
        await asyncio.sleep(1.0)
        self.assertIs(self.edge.arms.thread, thread)
        self.assertTrue(thread.is_alive() and self.edge.arms.state == "running")
        self.assertFalse(self.edge.link_fault.is_set())
        self.assertTrue(self.edge.base.enabled and bridge.connected)
        # Both sides logged and counted the gap.
        self.assertGreater(bridge.link["max_gap_s"], 1.0)
        self.assertGreater(self.edge.link["max_gap_s"], 1.0)
        await self.until(lambda: (bridge.edge_link or {}).get("gaps", 0) >= 1)
        self.assertIsNone(bridge.link["closed"])
        prior = bridge.received
        await self.until(lambda: bridge.received > prior+self.FPS)  # stream resumed

    async def test_long_wifi_stall_stops_arm_control(self):
        if self.FPS != 10:
            self.skipTest("link timing does not depend on the frame rate")
        bridge = await self._control_running()
        stall = asyncio.create_task(self.proxy.stall(LINK_TIMEOUT_S+3))  # starts right after the arm transition
        started = asyncio.get_running_loop().time()
        # The Jetson's own watchdog must act while nothing gets through (no help from the DGX).
        await self.until(lambda: self.edge.link_fault.is_set(), timeout=LINK_TIMEOUT_S+2.5)
        self.assertFalse(self.proxy.flowing.is_set(), "stopped only after traffic resumed")
        self.assertLess(asyncio.get_running_loop().time()-started, LINK_TIMEOUT_S+1)
        await self.until(lambda: self.edge.arms.thread is None or not self.edge.arms.thread.is_alive(), timeout=10)
        await stall
        self.assertFalse(self.edge.base.enabled)
        self.assertGreaterEqual(bridge.link["closed"]["silent_s"], LINK_TIMEOUT_S)
        await self.until(lambda: bridge.connected and bridge.received > 0, timeout=15)  # reconnects by itself

    async def test_late_stop_command_runs_but_late_motion_command_does_not(self):
        if self.FPS != 10:
            self.skipTest("link timing does not depend on the frame rate")
        bridge = await self._control_running()
        stall = asyncio.create_task(self.proxy.stall(1.0))
        await asyncio.sleep(0.05)
        with self.assertRaisesRegex(RuntimeError, "expired"):
            await bridge.call("base_cmd", owner="test", vx=.1, wz=0.)  # would move the base 1 s late
        await stall
        self.assertFalse(self.edge.base.active)
        stall = asyncio.create_task(self.proxy.stall(1.0))
        await asyncio.sleep(0.05)
        await bridge.call("control_stop")  # delivered 1 s late: still executed
        await stall
        await self.until(lambda: self.edge.arms.thread is None or not self.edge.arms.thread.is_alive(), timeout=20)
        self.assertFalse(self.edge.base.enabled)

    async def test_every_capture_cycle_is_delivered(self):
        bridge = self.app[BRIDGE]
        await self.until(lambda: bridge.received >= 3)
        start, t0 = bridge.received, asyncio.get_running_loop().time()
        await asyncio.sleep(2)
        rate = (bridge.received-start)/(asyncio.get_running_loop().time()-t0)
        self.assertGreater(rate, .9*self.FPS)
        self.assertEqual(self.edge.dropped, 0)

    async def test_data_link_survives_gil_blocking_arm_connect(self):
        """Native Trossen connect holds the Jetson GIL for seconds; the data link must survive it."""
        import teleop_server.sources.arms as arms_mod
        original = arms_mod.FakeArm.connect

        def blocking_connect(arm):
            sum(range(250_000_000))  # C loop: holds the GIL ~2.5 s, like the native driver configure
            original(arm)
        import teleop_server.distributed.server as server_mod
        slow_prepare = server_mod.prepare_packet

        def wifi_like(*args, **kwargs):  # slower acks keep the edge's in-flight window full, as on Wi-Fi
            time.sleep(0.04)
            return slow_prepare(*args, **kwargs)
        bridge = self.app[BRIDGE]
        await self.until(lambda: bridge.received >= 5)
        with patch.object(arms_mod.FakeArm, "connect", blocking_connect), \
                patch.object(server_mod, "prepare_packet", wifi_like):
            started = asyncio.get_running_loop().time()
            await bridge.call("control_start")
            await self.until(lambda: bridge.sources["arms"].state in ("running", "error"), timeout=60)
            blocked_s = asyncio.get_running_loop().time()-started
        self.assertEqual(bridge.sources["arms"].state, "running")
        self.assertTrue(bridge.connected)
        prior = bridge.received
        await self.until(lambda: bridge.received > prior+self.FPS)  # still streaming
        print(f"arm connect blocked the edge loop for about {blocked_s:.1f} s; link kept")

    async def _record(self, seconds, during=None, timeout=180):
        """Start on the DGX UI API, record on the Jetson, stop, wait for transfer + conversion."""
        bridge = self.app[BRIDGE]
        await self.until(lambda: bridge.received >= 5)
        await bridge.call("control_start")
        await self.until(lambda: bridge.sources["arms"].state == "running")
        async with self.client.post(self.url+"/api/record", json={"action": "start", "task": "jetson local test"}) as r:
            body = await r.json()
            self.assertEqual(r.status, 200, body)
        name = body["record"]["session"]
        self.assertEqual(self.edge.recorder.state, "recording")
        if during:
            await during(bridge, name)
        await asyncio.sleep(seconds)
        self.assertEqual(self.edge.recorder.state, "recording", "only the stop button may end a recording")
        async with self.client.post(self.url+"/api/record", json={"action": "stop"}) as r:
            self.assertEqual(r.status, 200, await r.text())
        await self.until(lambda: bridge.recorder.state == "idle" and (bridge.recorder.last_result or {})
                         .get("source_session") == name, timeout=timeout)
        return name, bridge.recorder.last_result

    async def test_jetson_local_recording_moves_to_dgx(self):
        name, result = await self._record(2.0)
        self.assertEqual(result["status"], "completed", result)
        self.assertGreater(result["frames"], 1.2*self.FPS)
        root = Path(result["path"])
        self.assertEqual(root.parent, Path(self.tmp.name))
        # The Jetson deleted its copy only after the DGX verified it; the DGX upload area is empty.
        self.assertFalse((self.edge.recorder.root/name).exists())
        self.assertEqual(self.edge.recorder.pending(), [])
        self.assertEqual(list((Path(self.tmp.name)/".incoming").iterdir()), [])
        manifest = json.loads((root/"session.json").read_text())
        self.assertEqual(manifest["source_session"], name)
        self.assertEqual(manifest["task"], "jetson local test")
        self.assertIn("Jetson", manifest["clock"])
        self.assertTrue(manifest["synthetic"])
        self.assertEqual(manifest["depth"]["storage"], "png16")
        self.assertEqual(manifest["depth"]["cameras"]["camera1"]["depth_intrinsics"]["width"], 640)
        import av
        import cv2
        from teleop_server.recorder import RAW_DEPTH_DIR
        for camera in ("camera1", "camera2", "camera3"):
            files = sorted((root/RAW_DEPTH_DIR/camera).glob("*.png"))
            self.assertEqual(len(files), result["frames"])
            depth = cv2.imread(str(files[-1]), cv2.IMREAD_UNCHANGED)
            self.assertEqual((depth.dtype, depth.shape), (np.uint16, (480, 640)))
            self.assertEqual(depth[400, 50], 2500)  # synthetic red patch at 0.25 m: raw units unchanged
            with av.open(str(root/"rgb_mp4"/f"{camera}.mp4")) as c:  # Jetson's own H.264, one frame per cycle
                stream = c.streams.video[0]
                self.assertEqual((stream.codec_context.name, stream.width, stream.height), ("h264", 640, 480))
                self.assertEqual(sum(1 for _ in c.decode(video=0)), result["frames"])
        self.assertEqual(len((root/"capture_cycles.jsonl").read_text().splitlines()), result["frames"])
        self.assertTrue((root/"base_raw.jsonl").stat().st_size)
        import pyarrow.parquet as pq
        data = pq.read_table(next((root/"dataset"/"data").rglob("*.parquet"))).to_pandas()
        self.assertEqual(len(data), result["frames"])
        seq = np.asarray(data["observation.capture_sequence"].tolist()).reshape(-1)
        self.assertTrue((np.diff(seq) >= 1).all())
        # RGB/depth pairing survives mp4 + container: the white bar's column must match the near bar.
        last = result["frames"]-1
        with av.open(str(root/"rgb_mp4"/"camera1.mp4")) as c:
            rgb = list(c.decode(video=0))[last].to_ndarray(format="rgb24")
        depth = cv2.imread(str(root/RAW_DEPTH_DIR/"camera1"/f"{last:06d}.png"), cv2.IMREAD_UNCHANGED)
        red = rgb[400:440, 40:80].reshape(-1, 3).mean(axis=0)  # synthetic red patch: channel order + matrix
        self.assertTrue(red[0] > 230 and red[1] < 25 and red[2] < 25, red)
        white = np.where(rgb[100:300].min(axis=(0, 2)) > 200)[0]
        # Background gradient (G by row, B by column) must decode to the source values.
        rows, cols = np.mgrid[100:360, 0:640]
        expected = np.stack((np.zeros_like(rows), np.linspace(30, 120, 480).astype(np.uint8)[rows],
                             np.linspace(40, 200, 640).astype(np.uint8)[cols]), axis=-1)
        keep = np.abs(cols-np.median(white)) > 14
        error = np.abs(rgb[100:360].astype(int)-expected)[keep].mean(axis=0)
        self.assertTrue((error < 3).all(), error)
        near = np.where((depth[100:300] == 4000).mean(axis=0) > 0.9)[0]
        self.assertTrue(len(white) and len(near), "synthetic bar not found")
        self.assertLessEqual(abs(float(np.median(white))-float(np.median(near))), 3.0, (white, near))
        from teleop_server.validate import validate
        report = await asyncio.to_thread(validate, root)
        self.assertEqual(report["depth_frames"], {f"camera{i}": result["frames"] for i in range(1, 4)})

    async def test_record_start_refused_without_base_odometry(self):
        """SLATE off: no odometry at all, so every cycle would be skipped. Refuse with the reason."""
        if self.FPS != 10:
            self.skipTest("does not depend on the frame rate")
        bridge = self.app[BRIDGE]
        await self.until(lambda: bridge.received >= 5)
        await bridge.call("control_start")
        await self.until(lambda: bridge.sources["arms"].state == "running")
        cap, original = self.edge.base_capture, self.edge.base_capture.snapshot
        cap.snapshot = lambda drain=True: dict(original(drain), odom=None)
        try:
            async with self.client.post(self.url+"/api/record", json={"action": "start"}) as r:
                body = await r.json()
            self.assertEqual(r.status, 409, body)
            self.assertIn("SLATE", body["error"])
            self.assertEqual(self.edge.recorder.state, "idle")
            self.assertEqual(list(self.edge.recorder.root.iterdir()), [])
        finally:
            del cap.snapshot

    async def test_recording_without_stored_frames_is_reported_not_crashed(self):
        """Odometry stops after start: the UI status says why nothing is stored, and the empty
        session ends as a clear failure (no mp4 exists) instead of a conversion crash."""
        if self.FPS != 10:
            self.skipTest("does not depend on the frame rate")
        import teleop_server.distributed.local_recorder as lr
        bridge = self.app[BRIDGE]

        async def stalled_is_shown(bridge, name):
            await self.until(lambda: (bridge.recorder.status().get("stalled") or {}).get("reason") == "odometry too old",
                             timeout=5)
            self.assertIn("SLATE", bridge.recorder.status()["stalled"]["hint"])
        with patch.object(lr, "MAX_BASE_AGE_NS", -1):  # every cycle: "odometry too old"
            name, result = await self._record(0.5, stalled_is_shown)
        self.assertEqual((result["status"], result["frames"]), ("failed", 0), result)
        self.assertIn("녹화된 프레임 없음", result["error"])
        self.assertIn("SLATE", result["error"])
        self.assertNotIn("FileNotFoundError", result["error"])
        root = Path(result["path"])
        manifest = json.loads((root/"session.json").read_text())
        self.assertEqual((manifest["status"], manifest["source_session"]), ("failed", name))
        self.assertGreater(manifest["capture_gaps"]["skipped"]["odometry too old"], 0)
        self.assertTrue((root/"jetson_upload"/"manifest.json").is_file())
        self.assertEqual(list((Path(self.tmp.name)/".incoming").iterdir()), [], "nothing left to re-convert")
        self.assertFalse((self.edge.recorder.root/name).exists())
        async with self.client.get(self.url+"/api/sessions") as r:  # listed, so it can still be labelled
            self.assertIn(root.name, [x["session"] for x in (await r.json())["sessions"]])

    async def test_slate_driver_restart_button(self):
        """Web button -> DGX -> Jetson: restarts the SLATE driver's systemd user service."""
        if self.FPS != 10:
            self.skipTest("does not depend on the frame rate")
        bridge = self.app[BRIDGE]
        await self.until(lambda: bridge.received >= 3 and bridge.base_status.get("driver_restart"))
        calls = []

        class Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return Proc()
        with patch("asyncio.create_subprocess_exec", fake_exec):
            async with self.client.post(self.url+"/api/base", json={"action": "restart_driver"}) as r:
                body = await r.json()
            self.assertEqual((r.status, body.get("slate")), (200, "simulated"), body)
            self.assertEqual(calls, [], "fake sensors must never touch systemd")
            self.edge.fake_base = False  # the real Jetson path
            async with self.client.post(self.url+"/api/base", json={"action": "restart_driver"}) as r:
                body = await r.json()
            self.assertEqual((r.status, body.get("slate")), (200, "restarting"), body)
            self.assertEqual(calls, [("systemctl", "--user", "restart", "--no-block", "teleop-jetson-slate.service")])
            Proc.returncode = 1
            async with self.client.post(self.url+"/api/base", json={"action": "restart_driver"}) as r:
                body = await r.json()
            self.assertEqual(r.status, 409)
            self.assertIn("재시작 실패", body["error"])

    async def test_upload_failure_keeps_jetson_copy_and_retries(self):
        bridge = self.app[BRIDGE]
        recordings, calls, kept = bridge.recorder, [0], []
        original = type(recordings).upload_file

        async def flaky(request):
            calls[0] += 1
            if calls[0] == 2:  # second file of the first attempt: the DGX fails mid-transfer
                await request.read()
                kept.append(any(self.edge.recorder.root.glob("*/READY")))
                raise web.HTTPInternalServerError(text="simulated DGX failure")
            return await original(recordings, request)
        for route in self.app.router.routes():
            if route.method == "PUT":
                route._handler = flaky
        name, result = await self._record(1.0)
        self.assertEqual(result["status"], "completed", result)
        self.assertGreater(calls[0], 2)
        self.assertEqual(kept, [True], "the Jetson must keep the session until the DGX verified it")
        self.assertFalse((self.edge.recorder.root/name).exists())

    async def test_recording_survives_jetson_link_drop(self):
        async def drop_link(bridge, name):
            await asyncio.sleep(0.5)
            await bridge.ws.close()  # Wi-Fi drop: Jetson arm control stops, the local recording does not
            await self.until(lambda: self.edge.arms.thread is None or not self.edge.arms.thread.is_alive())
            await self.until(lambda: bridge.connected and bridge.received > 0, timeout=10)
            self.assertEqual(self.edge.recorder.state, "recording")
            await bridge.call("control_start")
            await self.until(lambda: bridge.sources["arms"].state == "running", timeout=10)
        name, result = await self._record(1.0, drop_link)
        self.assertEqual(result["status"], "completed", result)
        self.assertGreater(result["frames"], self.FPS)
        gaps = result["capture_gaps"]
        self.assertGreater(gaps["skipped"].get("arm control not running", 0), 0, gaps)
        events = json.loads((Path(result["path"])/"session.json").read_text())["capture_gaps"]["events"]
        self.assertTrue(any("link lost" in e["event"] for e in events), events)
        import pyarrow.parquet as pq
        data = pq.read_table(next((Path(result["path"])/"dataset"/"data").rglob("*.parquet"))).to_pandas()
        seq = np.asarray(data["observation.capture_sequence"].tolist()).reshape(-1)
        self.assertGreater(int(np.diff(seq).max()), 1)  # the pause is a visible gap in cycle numbers

    async def test_live_view_rate_is_reduced_while_recording(self):
        bridge = self.app[BRIDGE]
        await self.until(lambda: bridge.received >= 5)
        await bridge.call("control_start")
        await self.until(lambda: bridge.sources["arms"].state == "running")
        self.edge.reduced_every = 3  # explicit lower-rate preview option

        async def rate():
            start, t0 = bridge.received, asyncio.get_running_loop().time()
            await asyncio.sleep(1.5)
            return (bridge.received-start)/(asyncio.get_running_loop().time()-t0)
        full = await rate()
        async with self.client.post(self.url+"/api/record", json={"action": "start"}) as r:
            self.assertEqual(r.status, 200, await r.text())
        reduced = await rate()
        async with self.client.post(self.url+"/api/record", json={"action": "stop"}) as r:
            self.assertEqual(r.status, 200, await r.text())
        await self.until(lambda: bridge.recorder.state == "idle", timeout=180)
        expected = self.FPS/self.edge.reduced_every
        self.assertGreater(full, .8*self.FPS)
        self.assertLess(reduced, 1.3*expected+1)
        self.assertGreater(reduced, .6*expected)


class Integration30FpsTests(IntegrationTests):
    FPS = 30


if __name__ == "__main__":
    unittest.main()
