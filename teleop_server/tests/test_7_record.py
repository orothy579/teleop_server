"""Feature 6: browser-controlled recording in the teleop_server/mobile_capture.py format."""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import av
import numpy as np

from teleop_server.recorder import ROBOT_TYPE, FakeBase, Recorder, collector_modules
from teleop_server.sources.arms import ArmSource, FakeArm
from teleop_server.sources.camera import FakeCamera
from teleop_server.sources.odom import FakeOdom
from teleop_server.tests.util import AppHarness

KEYS = [("follower", "left"), ("follower", "right"), ("leader", "left"), ("leader", "right")]


def factories(out_dir, base_cls=FakeBase):
    def sources(hub, stats):
        return ([FakeCamera(f"camera{i}", hub, stats) for i in (1, 2, 3)]
                + [ArmSource(hub, stats, lambda: {k: FakeArm(*k) for k in KEYS}), FakeOdom(hub, stats)])

    def recorder(hub, srcs):
        arms = next(s for s in srcs if s.kind == "arms")
        cams = [s for s in srcs if s.kind == "camera"]
        return Recorder(hub, arms, cams, base_cls, fps=30, synthetic=True, output_dir=out_dir,
                        manifest_extra={"odom_topic": "synthetic"})
    return sources, recorder


def arms_state(stats):
    return next(s for s in stats["sources"] if s["name"] == "arms")["state"]


async def start_control(test, h):
    await h.wait_for(lambda s: all(x["state"] == "running" for x in s["sources"] if x["kind"] == "camera"), 5)
    code, body = await h.post("/api/control", action="start")
    test.assertEqual(code, 200, body)
    stats = await h.wait_for(lambda s: arms_state(s) == "running", 5)
    test.assertEqual(arms_state(stats), "running")


async def wait_saved(h, timeout=90):
    stats = await h.wait_for(lambda s: s["record"]["state"] == "idle" and s["record"]["last_result"], timeout, 0.25)
    return stats["record"]["last_result"], stats


class RecordTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="teleop_rec_"))

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    async def test_episode_matches_collector_format(self):
        async with AppHarness(*factories(self.out)) as h:
            code, body = await h.post("/api/record", action="start")
            self.assertEqual(code, 409, body)  # control not running
            self.assertIn("팔 제어", body["error"])
            await start_control(self, h)
            code, body = await h.post("/api/record", action="start", task="test task: pick")
            self.assertEqual(code, 200, body)
            self.assertEqual(body["record"]["state"], "recording")
            code, body = await h.post("/api/record", action="start")
            self.assertEqual(code, 409, body)
            time.sleep(0)  # keep the loop responsive
            await h.collect(3.0)
            code, body = await h.post("/api/record", action="stop")
            self.assertEqual(code, 200, body)
            result, stats = await wait_saved(h)
            self.assertEqual(arms_state(stats), "running", "teleop keeps running after a recording")
            arm_info = next(s for s in stats["sources"] if s["name"] == "arms")["info"]

        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["validation"]["passed"], result)
        root = Path(result["path"])
        self.assertRegex(root.name, r"^\d{8}_\d{6}(_\d+)?_SYNTHETIC$")  # start time first = time order
        manifest = json.loads((root / "session.json").read_text())
        self.assertEqual((manifest["status"], manifest["task"], manifest["stop_reason"]),
                         ("completed", "test task: pick", "user"))
        self.assertEqual(manifest["frames"], result["frames"])
        self.assertAlmostEqual(result["frames"], manifest["recording_elapsed_s"] * 30, delta=6)
        self.assertTrue((root / "base_raw.jsonl").stat().st_size > 0)
        self.assertTrue(json.loads((root / "validation.json").read_text())["storage_checks"] == "PASS")
        quality = manifest["camera_quality"]
        self.assertEqual(result["camera_quality"], quality)
        self.assertLess(max(quality["camera_max_age_ms"]), 200, quality)
        self.assertLess(max(quality["camera_repeated_frames"]), 0.2 * result["frames"], quality)

        info = json.loads((root / "dataset/meta/info.json").read_text())
        mc, _ = collector_modules()
        self.assertEqual(info["robot_type"], ROBOT_TYPE)
        self.assertEqual(info["fps"], 30)
        for key, ft in mc.features().items():
            self.assertIn(key, info["features"])
            self.assertEqual(list(info["features"][key]["shape"]), list(ft["shape"]), key)
            self.assertEqual(info["features"][key]["dtype"], ft["dtype"], key)
        self.assertEqual(info["features"]["observation.images.camera1"]["info"]["video.codec"], "h264")

        # Stored images are RGB: the fake camera's pure-red (BGR 0,0,255) patch must decode as red.
        video = next((root / "dataset/videos/observation.images.camera1").rglob("*.mp4"))
        with av.open(str(video)) as container:
            rgb = next(container.decode(video=0)).to_ndarray(format="rgb24")
        r, g, b = rgb[390:450, 30:90].reshape(-1, 3).mean(axis=0)
        self.assertGreater(r, 180)
        self.assertLess(max(g, b), 80, (r, g, b))

        overruns = arm_info.get("loop_overruns", 0)
        print(f"recorded {result['frames']} frames in {manifest['recording_elapsed_s']:.2f} s; quality {quality}; "
              f"validation {result['validation']}; arm loop overruns {overruns}/{arm_info['cycles']}")
        self.assertLess(overruns, 0.05 * arm_info["cycles"], "recording slowed the teleop loop")

    async def test_stop_control_while_recording(self):
        async with AppHarness(*factories(self.out)) as h:
            await start_control(self, h)
            code, _ = await h.post("/api/record", action="start")
            self.assertEqual(code, 200)
            await h.collect(1.5)
            code, body = await h.post("/api/control", action="stop")
            self.assertEqual(code, 200, body)
            result, stats = await wait_saved(h)
            self.assertEqual(arms_state(stats), "stopped")
        self.assertEqual((result["status"], result["stop_reason"]), ("completed", "arm control stopped"), result)
        self.assertTrue(result["validation"]["passed"], result)

    async def test_base_dropout_skips_cycles_but_recording_continues(self):
        class DroppingBase(FakeBase):
            """Publishes normally, then goes silent after 30 samples (~1 s of recording)."""

            def __init__(self, path):
                self.calls = 0
                super().__init__(path)

            def sample(self, max_age_s):
                self.calls += 1
                if self.calls == 31:
                    self.stop.set()  # publisher thread exits; samples go stale after 0.25 s
                return super().sample(max_age_s)

        async with AppHarness(*factories(self.out, DroppingBase)) as h:
            await start_control(self, h)
            code, _ = await h.post("/api/record", action="start")
            self.assertEqual(code, 200)
            await h.collect(2.5)  # odometry goes silent after ~1 s
            stats = await h.stats()
            self.assertEqual(stats["record"]["state"], "recording", "only the stop button may end a recording")
            self.assertGreater(stats["record"]["skipped_cycles"], 10)
            code, _ = await h.post("/api/record", action="stop")
            self.assertEqual(code, 200)
            result, stats = await wait_saved(h, 30)
            self.assertEqual(arms_state(stats), "running")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["stop_reason"], "user")
        self.assertGreater(result["frames"], 10)  # cycles with fresh odometry
        gaps = result["capture_gaps"]
        self.assertGreater(gaps["skipped"].get("No fresh odometry", 0), 10, gaps)
        self.assertLess(gaps["recorded_fraction"], 0.8)
        self.assertTrue(result["validation"]["passed"], result)
        manifest = json.loads((Path(result["path"]) / "session.json").read_text())
        self.assertEqual(manifest["capture_gaps"]["skipped_cycles"], gaps["skipped_cycles"])

    async def test_server_shutdown_marks_recording_failed(self):
        async with AppHarness(*factories(self.out)) as h:
            await start_control(self, h)
            code, _ = await h.post("/api/record", action="start")
            self.assertEqual(code, 200)
            await h.collect(1.0)
        roots = list(self.out.iterdir())
        self.assertEqual(len(roots), 1)
        manifest = json.loads((roots[0] / "session.json").read_text())
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error"], "server shutdown during recording")
        self.assertGreater(manifest["frames"], 10)
        self.assertTrue((roots[0] / "dataset/meta/info.json").exists())


if __name__ == "__main__":
    unittest.main()


class FolderNameTest(unittest.TestCase):
    def test_time_ordered_unique_names(self):
        import re
        import types
        from teleop_server.labels import list_sessions
        from teleop_server.server import DATASET_DIR, REPO
        self.assertEqual(DATASET_DIR, REPO / "dataset")
        out = Path(tempfile.mkdtemp(prefix="teleop_names_")) / "dataset"
        try:
            rec = Recorder(None, types.SimpleNamespace(cycle_sink=None, exit_hooks=[]), [], None, fps=30,
                           synthetic=False, output_dir=out, depth=False)
            a, b = rec._new_session_dir(), rec._new_session_dir()
            self.assertRegex(a.name, r"^\d{8}_\d{6}$")
            self.assertTrue(b.name == a.name + "_2" or re.match(r"^\d{8}_\d{6}$", b.name), b.name)
            rec.synthetic = True
            c = rec._new_session_dir()
            self.assertTrue(c.name.endswith("_SYNTHETIC"))
            for i, root in enumerate((a, b, c)):
                (root / "session.json").write_text(json.dumps({"status": "completed", "frames": i,
                                                               "start_unix_ns": 1_000 + i}))
            (out / "notes.txt").write_text("not a session")
            listed = [x["session"] for x in list_sessions(out)]
            self.assertEqual(len(listed), 3)
            self.assertEqual(listed[-1], a.name)  # newest first, oldest last
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)
