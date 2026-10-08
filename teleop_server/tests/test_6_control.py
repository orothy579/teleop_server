"""Feature 5: control button API. Arms connect (and move) only on request."""
import time
import unittest

from teleop_server.sources.arms import ArmSource, FakeArm
from teleop_server.sources.camera import FakeCamera
from teleop_server.tests.util import AppHarness

KEYS = [("follower", "left"), ("follower", "right"), ("leader", "left"), ("leader", "right")]


def arms_state(stats):
    return next(s["state"] for s in stats["sources"] if s["name"] == "arms")


class LoggedArm(FakeArm):
    log = []

    def connect(self):
        super().connect()
        self.log.append(("connect", self.role, self.side))

    def close(self, fault):
        self.log.append(("close", self.role, self.side, fault))


class ControlApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_stop_restart(self):
        LoggedArm.log = []
        sessions = []

        def make():
            arms = {k: LoggedArm(*k) for k in KEYS}
            sessions.append(arms)
            return arms

        async with AppHarness(lambda hub, stats: [ArmSource(hub, stats, make)]) as h:
            stats = await h.wait_for(lambda s: arms_state(s) == "idle", 3)
            self.assertEqual(arms_state(stats), "idle")
            self.assertEqual(LoggedArm.log, [], "server start must not connect (move) any arm")
            _, texts = await h.collect(0.5)
            self.assertFalse([t for t in texts if t.get("type") == "arm"], "no arm data while idle")

            code, body = await h.post("/api/control", action="start")
            self.assertEqual((code, body["ok"]), (200, True), body)
            stats = await h.wait_for(lambda s: arms_state(s) == "running", 5)
            self.assertEqual(arms_state(stats), "running")
            code, body = await h.post("/api/control", action="start")
            self.assertEqual(code, 409, body)
            _, texts = await h.collect(1.0)
            self.assertGreater(len([t for t in texts if t.get("type") == "arm"]), 60)

            code, body = await h.post("/api/control", action="stop")
            self.assertEqual(code, 200, body)
            stats = await h.wait_for(lambda s: arms_state(s) == "stopped", 5)
            self.assertEqual(arms_state(stats), "stopped")
            code, body = await h.post("/api/control", action="stop")
            self.assertEqual(code, 409, body)

            code, _ = await h.post("/api/control", action="start")  # restart uses fresh arm objects
            self.assertEqual(code, 200)
            await h.wait_for(lambda s: arms_state(s) == "running", 5)
            await h.post("/api/control", action="stop")
            await h.wait_for(lambda s: arms_state(s) == "stopped", 5)

        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0].keys(), sessions[1].keys())
        self.assertIsNot(sessions[0][KEYS[0]], sessions[1][KEYS[0]])
        connects = [e[1:] for e in LoggedArm.log if e[0] == "connect"]
        self.assertEqual(connects, KEYS + KEYS)  # followers first, then leaders, each session
        closes = [e for e in LoggedArm.log if e[0] == "close"]
        self.assertEqual(len(closes), 8)
        self.assertTrue(all(e[3] is False for e in closes), "normal stop must use the normal disconnect")

    async def test_bad_requests(self):
        async with AppHarness(lambda hub, stats: [FakeCamera("camera1", hub, stats)]) as h:
            code, body = await h.post("/api/control", action="start")
            self.assertEqual(code, 400, body)  # no arm source
            code, body = await h.post("/api/record", action="start")
            self.assertEqual(code, 400, body)  # no recorder
        async with AppHarness(lambda hub, stats: [ArmSource(hub, stats, {k: FakeArm(*k) for k in KEYS})]) as h:
            code, body = await h.post("/api/control", action="jump")
            self.assertEqual(code, 400, body)

    async def test_refused_while_cameras_start(self):
        class SlowCamera(FakeCamera):
            def run(self):
                time.sleep(1.5)
                super().run()

        factory = lambda hub, stats: [SlowCamera("camera1", hub, stats),
                                      ArmSource(hub, stats, {k: FakeArm(*k) for k in KEYS})]
        async with AppHarness(factory) as h:
            code, body = await h.post("/api/control", action="start")
            self.assertEqual(code, 409, body)
            self.assertIn("카메라", body["error"])
            await h.wait_for(lambda s: s["sources"][0]["state"] == "running", 5)
            code, body = await h.post("/api/control", action="start")
            self.assertEqual(code, 200, body)


if __name__ == "__main__":
    unittest.main()
