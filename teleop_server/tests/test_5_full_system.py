"""All real sensors in one server, driven like the browser buttons: control -> record -> stop.

Cameras must keep streaming while the four arms connect/move, and the recording must pass the
existing teleop_server/check_mobile.py validation.
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from teleop_server.server import build_recorder, build_sources, parse_args
from teleop_server.tests.util import HW, AppHarness

FULL = HW and os.environ.get("TELEOP_ARMS") == "1" and os.environ.get("TELEOP_ROS") == "1"
CAMS = ["camera1", "camera2", "camera3"]


def source(stats, name):
    return next(s for s in stats["sources"] if s["name"] == name)


@unittest.skipUnless(FULL, "set TELEOP_HW=1 TELEOP_ARMS=1 TELEOP_ROS=1 (arms WILL MOVE)")
class FullSystemTest(unittest.IsolatedAsyncioTestCase):
    async def test_control_record_stop_on_real_hardware(self):
        out = Path(tempfile.mkdtemp(prefix="teleop_hw_rec_"))
        args = parse_args(["--cameras", "realsense", "--arms", "teleop", "--odom", "ros", "--record-dir", str(out)])
        harness = AppHarness(lambda hub, stats: build_sources(args, hub, stats),
                             lambda hub, sources: build_recorder(args, hub, sources))
        report = {}
        try:
            async with harness as h:
                stats = await h.wait_for(lambda s: all(source(s, c)["state"] == "running" for c in CAMS), 20)
                self.assertTrue(all(source(stats, c)["state"] == "running" for c in CAMS), stats["sources"])
                self.assertEqual(source(stats, "arms")["state"], "idle", "server start must not move the arms")
                collector = asyncio.create_task(h.collect(60.0))

                code, body = await h.post("/api/control", action="start")
                self.assertEqual(code, 200, body)
                stats = await h.wait_for(lambda s: source(s, "arms")["state"] in ("running", "error"), 40)
                self.assertEqual(source(stats, "arms")["state"], "running", source(stats, "arms"))

                code, body = await h.post("/api/record", action="start", task="hardware test recording")
                self.assertEqual(code, 200, body)
                self.assertFalse(body["record"]["synthetic"])
                await asyncio.sleep(10.0)
                code, body = await h.post("/api/record", action="stop")
                self.assertEqual(code, 200, body)
                stats = await h.wait_for(lambda s: s["record"]["state"] == "idle" and s["record"]["last_result"], 180, 0.5)
                result = stats["record"]["last_result"]
                arm_info = source(stats, "arms")["info"]

                code, body = await h.post("/api/control", action="stop")
                self.assertEqual(code, 200, body)
                stats = await h.wait_for(lambda s: source(s, "arms")["state"] in ("stopped", "error"), 60, 0.5)
                self.assertEqual(source(stats, "arms")["state"], "stopped", source(stats, "arms"))
                frames, texts = await collector

            arm_states = [(t["state"], t["t_client_ns"]) for t in texts
                          if t.get("type") == "status" and t.get("name") == "arms"]
            rec_states = [(t["state"], t["t_client_ns"]) for t in texts if t.get("type") == "record"]
            t_conn = next(t for st, t in arm_states if st == "starting")
            t_run = next(t for st, t in arm_states if st == "running")
            t_rec = next(t for st, t in rec_states if st == "recording")
            t_save = next(t for st, t in rec_states if st in ("saving", "validating"))
            t_idle = next(t for st, t in rec_states if st == "idle" and t > t_save)
            t_stop = next((t for st, t in arm_states if st == "stopping"), None) or t_idle
            phases = {"arm_connect": (t_conn, t_run), "recording": (t_rec, t_save),
                      "save_and_validate": (t_save, t_idle), "arm_disconnect": (t_stop, t_stop + 30 * 10**9)}
            report["arm_connect_s"] = round((t_run - t_conn) / 1e9, 1)
            for cam in CAMS:
                t_recv = [hdr["t_recv_ns"] for hdr, _ in frames if hdr["sensor"] == cam]
                pairs = list(zip(t_recv, t_recv[1:]))
                report[cam] = {name: round(max([(b - a) / 1e6 for a, b in pairs if lo <= b <= hi] or [0]), 1)
                               for name, (lo, hi) in phases.items()}
                report[cam]["overall"] = round(max((b - a) / 1e6 for a, b in pairs), 1)
                self.assertLess(report[cam]["recording"], 200, f"{cam} stalled while recording: {report[cam]}")
                self.assertLess(report[cam]["overall"], 500, f"{cam} stalled: {report[cam]}")

            self.assertEqual(result["status"], "completed", result)
            self.assertTrue(result["validation"]["passed"], result)
            self.assertAlmostEqual(result["frames"], result["elapsed_s"] * 30, delta=10)
            self.assertRegex(result["session"], r"^\d{8}_\d{6}(_\d+)?$")
            report["recording"] = {k: result[k] for k in ("session", "frames", "elapsed_s", "camera_quality")}
            self.assertLess(max(result["camera_quality"]["camera_max_age_ms"]), 200, result["camera_quality"])
            report["validation"] = result["validation"]
            # Depth: one colour-aligned depth frame per RGB frame, from the same exposure.
            v = result["validation"]
            for cam in CAMS:
                self.assertEqual(v["depth_frames"][f"observation.images.{cam}_depth"], result["frames"])
            self.assertLess(max(v["max_rgb_depth_dt_ms"]), 1.0, v)
            self.assertLess(max(result["camera_quality"]["max_rgb_depth_dt_ms"]), 1.0)
            report["arm_loop_overruns"] = f"{arm_info.get('loop_overruns', 0)}/{arm_info['cycles']}"
            self.assertLess(arm_info.get("loop_overruns", 0), 0.05 * arm_info["cycles"])
        finally:
            shutil.rmtree(out, ignore_errors=True)
            for k, v in report.items():
                print(f"{k}: {v}")


if __name__ == "__main__":
    unittest.main()
