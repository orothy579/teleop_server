"""Feature 1 (robustness): a stalled or lost RealSense stream is reported and recovered."""
import sys
import time
import types
import unittest
from unittest import mock

import numpy as np

from teleop_server.hub import Hub
from teleop_server.latency import StatsRegistry
from teleop_server.sources.camera import RealSenseCamera


def fake_rs(script):
    """pyrealsense2 stand-in. ``script`` is a list of per-pipeline behaviours: 'frames', 'stall', 'fail'."""
    rs = types.SimpleNamespace()
    rs.stream = types.SimpleNamespace(color="color", depth="depth")
    rs.format = types.SimpleNamespace(bgr8="bgr8", z16="z16")
    rs.option = types.SimpleNamespace(global_time_enabled="gte")
    rs.timestamp_domain = types.SimpleNamespace(global_time="global")
    rs.frame_metadata_value = types.SimpleNamespace(frame_timestamp="ft", sensor_timestamp="st", actual_exposure="ae")
    rs.starts = []

    class DepthFrame:
        def __init__(self, t):
            self.t = t
        def __bool__(self): return True
        def get_timestamp(self): return self.t
        def get_data(self): return np.full((48, 64), 5000, np.uint16)

    class Frame:
        def __init__(self, n, depth_offset_ms=0.3):
            self.n, self.t = n, time.time() * 1000 - 5
            self.depth = DepthFrame(self.t - depth_offset_ms)

        def get_depth_frame(self): return self.depth

        def get_color_frame(self): return self
        def get_frame_timestamp_domain(self): return "global"
        def get_timestamp(self): return self.t
        def supports_frame_metadata(self, k): return True
        def get_frame_metadata(self, k): return {"ft": 20000, "st": 4000, "ae": 30000}[k]
        def get_frame_number(self): return self.n
        def get_data(self): return np.zeros((48, 64, 3), np.uint8)

    intr = types.SimpleNamespace(width=64, height=48, fx=40.0, fy=40.0, ppx=32.0, ppy=24.0, model="none", coeffs=[0] * 5)

    class Profile:
        def get_device(self):
            return types.SimpleNamespace(query_sensors=lambda: [],
                                         first_depth_sensor=lambda: types.SimpleNamespace(get_depth_scale=lambda: 0.0001))
        def get_stream(self, stream):
            return types.SimpleNamespace(as_video_stream_profile=lambda: types.SimpleNamespace(get_intrinsics=lambda: intr))

    class Pipeline:
        def start(self, cfg):
            behaviour = script[min(len(rs.starts), len(script) - 1)]
            rs.starts.append(behaviour)
            if behaviour == "fail":
                raise RuntimeError("No device connected")
            self.behaviour, self.n = behaviour, 0
            return Profile()

        def try_wait_for_frames(self, timeout_ms):
            if self.behaviour == "stall":
                time.sleep(timeout_ms / 1000)
                return False, None
            time.sleep(1 / 30)
            self.n += 1
            # "unsynced": depth always from the previous exposure (the syncer state seen once on hardware)
            return True, Frame(self.n, 33.3 if self.behaviour == "unsynced" else 0.3)

        def stop(self): pass

    rs.pipeline = Pipeline
    rs.align = lambda stream: types.SimpleNamespace(process=lambda frames: frames)
    rs.config = lambda: types.SimpleNamespace(enable_device=lambda s: None, enable_stream=lambda *a: None)
    return rs


class RecoveryTest(unittest.TestCase):
    def run_camera(self, script, seconds, stall_restart_s=0.5, depth=False):
        rs = fake_rs(script)
        hub, stats = Hub(StatsRegistry()), StatsRegistry()
        cam = RealSenseCamera("camera1", hub, stats, "SERIAL", depth=depth)
        states = []
        orig = cam._set_state
        cam._set_state = lambda st, err=None: (states.append((st, err)), orig(st, err))
        stream = cam._stream
        cam._stream = lambda: stream(stall_restart_s=stall_restart_s)
        with mock.patch.dict(sys.modules, {"pyrealsense2": rs}), mock.patch.object(Hub, "publish"):
            cam.start()
            time.sleep(seconds)
            cam.stop()
        return cam, rs, states, stats

    def test_stall_then_recovers(self):
        cam, rs, states, stats = self.run_camera(["stall", "frames"], 4.0)
        self.assertEqual(rs.starts[:2], ["stall", "frames"])
        names = [s for s, _ in states]
        self.assertIn("error", names)
        self.assertEqual(names[-2:], ["running", "stopped"])
        self.assertIn("no frames", next(e for s, e in states if s == "error"))
        self.assertGreater(stats.get("camera1").count, 20, "frames resumed after restart")
        self.assertGreaterEqual(cam.info["frame_timeouts"], 1)

    def test_lost_rgb_depth_pairing_reopens_camera(self):
        cam, rs, states, stats = self.run_camera(["unsynced", "frames"], 9.0, stall_restart_s=30, depth=True)
        self.assertEqual(rs.starts[:2], ["unsynced", "frames"])
        self.assertEqual(cam.info["pairing_restarts"], 1)
        self.assertIn("pairing lost", next(e for s, e in states if s == "error"))
        self.assertEqual(states[-2][0], "running")
        latest = cam.latest_frame(paired=True)
        self.assertIsNotNone(latest)
        self.assertAlmostEqual(float(latest.depth.max()), 0.5, places=3)  # 5000 x 0.0001 m
        self.assertLess(cam.unpaired_recent_pct(), 1.0)

    def test_lost_device_reconnects(self):
        cam, rs, states, stats = self.run_camera(["fail", "fail", "frames"], 6.0)
        self.assertEqual(rs.starts[:3], ["fail", "fail", "frames"])
        self.assertEqual(cam.info["restarts"], 2)
        self.assertIn("No device connected", next(e for s, e in states if s == "error"))
        self.assertEqual(states[-2][0], "running")
        self.assertGreater(stats.get("camera1").count, 20)


if __name__ == "__main__":
    unittest.main()
