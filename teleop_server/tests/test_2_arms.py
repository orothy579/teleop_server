"""Feature 2: leader/follower proprioception streaming (four arms)."""
import os
import unittest

import numpy as np

from teleop_server.sources.arms import SIDES, ArmSource, FakeArm, RealArm
from teleop_server.tests.util import AppHarness, browse

SENSORS = ["leader_left", "follower_left", "leader_right", "follower_right"]
KEYS = [("follower", "left"), ("follower", "right"), ("leader", "left"), ("leader", "right")]


class RecordingArm(FakeArm):
    log = []

    def __init__(self, role, side, fail_after=None):
        super().__init__(role, side)
        self.reads, self.fail_after = 0, fail_after

    def connect(self):
        super().connect()
        self.log.append(("connect", self.role, self.side))

    def read(self):
        self.reads += 1
        if self.fail_after and self.reads > self.fail_after:
            raise RuntimeError("simulated controller error")
        return super().read()

    def close(self, fault):
        self.log.append(("close", self.role, self.side, fault))


def fake_factory(arm_cls=FakeArm, **kw):
    def factory(hub, stats):
        return [ArmSource(hub, stats, {k: arm_cls(*k) for k in KEYS}, autostart=True, **kw)]
    return factory


class FakeArmStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_four_arm_streams(self):
        async with AppHarness(fake_factory(rate=30.0)) as h:
            _, texts = await h.collect(3.0)
            stats = await h.stats()
        msgs = {n: [t for t in texts if t.get("type") == "arm" and t["sensor"] == n] for n in SENSORS}
        for n, ms in msgs.items():
            self.assertGreaterEqual(len(ms), 75, n)
            for m in ms:
                self.assertEqual(len(m["pos"]), 7)
                self.assertTrue(np.isfinite(m["pos"]).all())
                chain = [m["t_recv_ns"], m["t_hub_ns"], m["t_sent_ns"], m["t_client_ns"]]
                self.assertEqual(chain, sorted(chain))
                self.assertLessEqual(m["t_source_ns"], m["t_recv_ns"])
            s = stats["sensors"][n]
            self.assertAlmostEqual(s["rate_hz"], 30, delta=3, msg=n)
            self.assertIn("sensor_ms", s["stages"])
            self.assertIn("read_ms", s["stages"])
        for side in SIDES:
            lead = msgs[f"leader_{side}"]
            fol = msgs[f"follower_{side}"]
            # Follower command equals the leader reading of the same cycle.
            self.assertTrue(all("cmd" in m for m in fol))
            err = np.abs(np.array(lead[-1]["pos"]) - np.array(fol[-1]["pos"])).max()
            self.assertLess(err, 0.1, f"{side} follower does not track leader")
        print("arm messages:", {n: len(m) for n, m in msgs.items()},
              "sensor_ms p50:", {n: round(stats["sensors"][n]["stages"]["sensor_ms"]["p50"], 2) for n in SENSORS})


class ArmLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_normal_stop_disconnects_without_fault(self):
        RecordingArm.log = []
        async with AppHarness(fake_factory(RecordingArm)) as h:
            await h.collect(0.5)
        closes = [e for e in RecordingArm.log if e[0] == "close"]
        self.assertEqual(len(closes), 4)
        self.assertTrue(all(e[3] is False for e in closes))
        connects = [e[1:] for e in RecordingArm.log if e[0] == "connect"]
        self.assertEqual(connects, KEYS)  # followers first, then leaders
        self.assertEqual([e[1:3] for e in closes], KEYS[::-1])

    async def test_read_fault_stops_and_cleans_up(self):
        RecordingArm.log = []

        class Failing(RecordingArm):
            def __init__(self, role, side):
                super().__init__(role, side, fail_after=10 if (role, side) == ("leader", "right") else None)

        async with AppHarness(fake_factory(Failing)) as h:
            _, texts = await h.collect(1.5)
            stats = await h.stats()
        src = next(s for s in stats["sources"] if s["name"] == "arms")
        self.assertEqual(src["state"], "error")
        self.assertIn("simulated controller error", src["error"])
        closes = [e for e in RecordingArm.log if e[0] == "close"]
        self.assertEqual(len(closes), 4)
        self.assertTrue(all(e[3] is True for e in closes), "fault path must not run staged moves")
        self.assertTrue(any(t.get("type") == "status" and t.get("state") == "error" for t in texts))


class ArmBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def test_page_shows_arm_values(self):
        async with AppHarness(fake_factory()) as h:
            page = await browse(h, 4.0, "2_arms_fake.png")
        self.assertEqual(page["exceptions"], [])
        for n in SENSORS:
            s = page["sensors"][n]
            self.assertGreater(s["drawn"], 50, n)
            self.assertEqual(s["status"], "정상", n)
            self.assertLess(s["e2e"]["p95"], 100, n)
        print("browser arms:", {n: (round(page["sensors"][n]["hz"], 1), round(page["sensors"][n]["e2e"]["p50"], 1)) for n in SENSORS})


@unittest.skipUnless(os.environ.get("TELEOP_ARMS") == "1",
                     "set TELEOP_ARMS=1 to connect the real arms (THEY WILL MOVE to staged pose and back to sleep)")
class RealArmTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_four_arms(self):
        ips = {("follower", "left"): "192.168.1.5", ("follower", "right"): "192.168.1.4",
               ("leader", "left"): "192.168.1.3", ("leader", "right"): "192.168.1.2"}
        factory = lambda hub, stats: [ArmSource(hub, stats, {k: RealArm(*k, ip, 30.0) for k, ip in ips.items()},
                                                autostart=True)]
        async with AppHarness(factory) as h:
            _, texts = await h.collect(20.0)  # connect (~8 s staged moves) + streaming
            stats = await h.stats()
        src = next(s for s in stats["sources"] if s["name"] == "arms")
        self.assertEqual(src["state"], "running", src)
        for n in SENSORS:
            self.assertGreater(stats["sensors"][n]["rate_hz"], 25, n)
        print("real arms:", {n: stats["sensors"][n]["stages"] for n in SENSORS})


if __name__ == "__main__":
    unittest.main()
