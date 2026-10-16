"""Policy ownership, failure handling and complete command transport with simulated arms."""
import asyncio
import json
import time
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from aiohttp import WSMsgType, WSServerHandshakeError

from teleop_server.policy_control import PolicyControl
from teleop_server.sources.arms import STAGED, ReturnMotion
from teleop_server.distributed.server import BRIDGE
from teleop_server.distributed.test_distributed import IntegrationTests


class PolicyLimitTests(unittest.TestCase):
    def controller(self):
        policy = PolicyControl()
        policy.observe([0.] * 14, now=1.)
        policy.begin("one", now=1.)
        return policy

    def test_velocity_limits_preserve_joint_and_gripper_units(self):
        policy = self.controller()
        target = [0.2]*14
        target[6] = target[13] = .01
        policy.accept("one", 0, target, now=1.01)
        sent = policy.command(1/30, now=1.02)
        self.assertAlmostEqual(sent[0], .02)
        self.assertAlmostEqual(sent[7], .02)
        self.assertAlmostEqual(sent[6], .02/30)
        self.assertAlmostEqual(sent[13], .02/30)
        for step in range(1, 15):
            policy.observe(sent, now=1.02+step/30)
            new = policy.command(1/30, now=1.02+step/30)
            for i in range(14):
                self.assertLessEqual(abs(new[i]-sent[i]), (.02 if i in (6, 13) else .6)/30+1e-9)
            sent = new

    def test_deadline_holds_measured_pose_and_rejects_resumption(self):
        policy = self.controller()
        policy.accept("one", 0, [.1]*6+[.01]+[.1]*6+[.01], now=1.1)
        policy.observe([.02]*6+[.001]+[.02]*6+[.001], now=1.6)
        held = policy.command(1/30, now=1.6)
        self.assertEqual(held, policy.measured)
        self.assertEqual(policy.mode, "hold")
        with self.assertRaisesRegex(RuntimeError, "no longer owns"):
            policy.accept("one", 1, held, now=1.61)
        self.assertIsNotNone(policy.command(1/30, now=1.7))

    def test_foreign_owner_cannot_send_or_stop(self):
        policy = self.controller()
        with self.assertRaises(RuntimeError):
            policy.begin("two", now=1.01)
        with self.assertRaises(RuntimeError):
            policy.accept("two", 0, [0.]*14, now=1.01)
        self.assertFalse(policy.hold("foreign", "two"))
        self.assertEqual(policy.mode, "policy")

    def test_invalid_targets_and_replay_enter_hold(self):
        for target in ([float("nan")]*14, [0.]*13, [True]*14, [100.]*14):
            policy = self.controller()
            with self.assertRaises(ValueError):
                policy.accept("one", 0, target, now=1.1)
            self.assertEqual(policy.mode, "hold")
        policy = self.controller()
        policy.accept("one", 3, [0.]*14, now=1.1)
        with self.assertRaisesRegex(ValueError, "out-of-order"):
            policy.accept("one", 3, [0.]*14, now=1.2)
        self.assertEqual(policy.mode, "hold")

    def test_return_runs_from_hold_only_and_is_not_interrupted(self):
        policy = self.controller()
        with self.assertRaisesRegex(RuntimeError, "stop it first"):
            policy.start_return()  # never while a policy owns the arms
        policy.hold("policy stopped", "one")
        self.assertTrue(policy.start_return())
        self.assertIsNone(policy.command(1/30, now=1.1), "the arm source drives the return")
        with self.assertRaisesRegex(RuntimeError, "returning"):
            policy.begin("two", now=1.1)
        self.assertFalse(policy.hold("late stop"))
        self.assertEqual(policy.mode, "returning")
        policy.finish_return()
        self.assertEqual((policy.mode, policy.owner), ("teleop", None))
        self.assertFalse(policy.start_return(), "teleop already: nothing to do")

    def test_return_motion_is_smooth_and_paced_by_the_farthest_joint(self):
        near = ReturnMotion({"a": [v + .3 for v in STAGED], "b": list(STAGED)}, {"a": list(STAGED), "b": list(STAGED)}, now=0.0)
        self.assertEqual(near.duration, 2.0)  # lerobot's own 2 s staged move is the minimum
        far = ReturnMotion({"a": [2.5]+[0.0]*6}, {"a": list(STAGED)}, now=0.0)
        self.assertAlmostEqual(far.duration, 5.0)  # 2.5 rad at 0.5 rad/s on average
        path = [far.at("a", t/30)[0] for t in range(0, 151)]
        steps = np.abs(np.diff(path))
        self.assertLess(steps.max(), 0.75/30 + 1e-9, "peak speed 1.5x the average")
        self.assertLess(steps[0], 1e-3, "starts gently")
        self.assertEqual(far.at("a", 5.0), list(STAGED))
        self.assertFalse(far.finished(5.0))
        self.assertTrue(far.finished(5.5))


class PolicyTransportTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the real edge/server fixture without inheriting unrelated recording tests.
    FPS = 30
    asyncSetUp = IntegrationTests.asyncSetUp
    asyncTearDown = IntegrationTests.asyncTearDown
    until = IntegrationTests.until
    _control_running = IntegrationTests._control_running

    async def start_policy(self):
        bridge = await self._control_running()
        ws = await self.client.ws_connect(self.url+"/policy/ws", headers={"Authorization": "Bearer test-secret"})
        await ws.send_json({"type": "start"})
        reply = await ws.receive_json(timeout=3)
        self.assertEqual(reply["type"], "started", reply)
        self.assertEqual(self.edge.arms.policy.mode, "policy")
        self.assertFalse(self.edge.base.enabled)
        return bridge, ws

    async def send_target(self, ws, sequence=0, issued=None, positions=None):
        if positions is None:
            positions = list(self.edge.arms.policy.measured)
        await ws.send_json(dict(type="target", sequence=sequence, positions=positions,
                               issued_ns=time.time_ns() if issued is None else issued))
        return await ws.receive_json(timeout=3)

    async def test_targets_reach_followers_without_leader_mirroring(self):
        bridge, ws = await self.start_policy()
        initial = list(self.edge.arms.policy.measured)
        goal = [v-.1 if i not in (6, 13) else v for i, v in enumerate(initial)]
        for sequence in range(6):
            reply = await self.send_target(ws, sequence, positions=goal)
            self.assertEqual(reply["type"], "accepted", reply)
            await asyncio.sleep(1/30)
        for side, offset in (("left", 0), ("right", 7)):
            follower = self.edge.arms.arms[("follower", side)]
            leader = self.edge.arms.arms[("leader", side)]
            self.assertAlmostEqual(follower.target[0], goal[offset], delta=.02)
            self.assertNotAlmostEqual(follower.target[0], leader.pos[0], delta=.005)
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            await bridge.call("base_enable")
        await ws.close()
        await self.until(lambda: self.edge.arms.policy.mode == "hold")
        await asyncio.sleep(.1)
        self.assertIsNotNone(self.edge.arms.policy.command(1/30))
        self.assertTrue(self.edge.arms.thread.is_alive())

    async def test_stale_client_command_stops_and_leaves_no_pending_future(self):
        bridge, ws = await self.start_policy()
        reply = await self.send_target(ws, issued=time.time_ns()-1_000_000_000)
        self.assertEqual(reply["type"], "error", reply)
        await self.until(lambda: self.edge.arms.policy.mode == "hold" and not bridge.pending)
        await ws.close()

    async def test_dead_client_watchdog_acts_without_websocket_disconnect(self):
        bridge, ws = await self.start_policy()
        self.assertEqual((await self.send_target(ws))["type"], "accepted")
        await asyncio.sleep(.65)
        self.assertEqual(self.edge.arms.policy.mode, "hold")
        self.assertIn("timeout", self.edge.arms.policy.reason)
        self.assertTrue(bridge.connected)
        reply = await self.send_target(ws, sequence=1)
        self.assertEqual(reply["type"], "error", reply)
        await ws.close()

    async def test_stop_api_revokes_ownership_without_sleep_motion(self):
        bridge, ws = await self.start_policy()
        async with self.client.post(self.url+"/api/policy", json={"action": "stop"}) as response:
            self.assertEqual(response.status, 200, await response.text())
        await self.until(lambda: self.edge.arms.policy.mode == "hold" and bridge.policy_owner is None)
        self.assertTrue(self.edge.arms.thread.is_alive())
        self.assertFalse(self.edge.base.enabled)
        await ws.close()

    async def test_return_brings_all_arms_to_the_staged_pose_then_teleop(self):
        bridge, ws = await self.start_policy()
        goal = [v-.3 if i not in (6, 13) else v for i, v in enumerate(self.edge.arms.policy.measured)]
        for sequence in range(20):
            await self.send_target(ws, sequence, positions=goal)
            await asyncio.sleep(1/30)
        await ws.close()
        await self.until(lambda: self.edge.arms.policy.mode == "hold")
        async with self.client.post(self.url+"/api/control", json={"action": "return"}) as r:
            self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(self.edge.arms.policy.mode, "returning")
        await self.until(lambda: "return" in self.edge.arms.info)
        leaders = [self.edge.arms.arms[("leader", side)] for side in ("left", "right")]
        self.assertTrue(all(a.held for a in leaders), "leaders under position control while they move")
        with self.assertRaisesRegex(RuntimeError, "returning"):
            await bridge.call("policy_start", owner="x")  # no AI run during the return
        await bridge.call("policy_stop")  # a late stop does not leave the arms half way
        self.assertEqual(self.edge.arms.policy.mode, "returning")
        self.assertFalse(self.edge.base.enabled)
        await self.until(lambda: self.edge.arms.policy.mode == "teleop", timeout=6)
        for key, arm in self.edge.arms.arms.items():
            self.assertTrue(np.allclose(arm.pos[:6], STAGED[:6], atol=.02), (key, arm.pos))
        self.assertFalse(any(a.held for a in leaders), "leaders gravity-compensated again")
        self.assertTrue(self.edge.base.enabled, "as after control start")
        self.assertNotIn("return", self.edge.arms.info)
        await asyncio.sleep(.2)  # followers mirror the leaders again
        follower = self.edge.arms.arms[("follower", "left")]
        self.assertAlmostEqual(follower.target[0], self.edge.arms.arms[("leader", "left")].pos[0], delta=.05)
        async with self.client.post(self.url+"/api/control", json={"action": "return"}) as r:
            self.assertEqual(r.status, 200, "teleop already: nothing to do")
        self.assertTrue(self.edge.arms.thread.is_alive())

    async def test_authentication_and_single_owner(self):
        bridge = await self._control_running()
        with self.assertRaises(WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.url+"/policy/ws")
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.edge.arms.policy.mode, "teleop")
        ws = await self.client.ws_connect(self.url+"/policy/ws", headers={"Authorization": "Bearer test-secret"})
        await ws.send_json({"type": "start"})
        self.assertEqual((await ws.receive_json())["type"], "started")
        with self.assertRaises(WSServerHandshakeError) as caught:
            await self.client.ws_connect(self.url+"/policy/ws", headers={"Authorization": "Bearer test-secret"})
        self.assertEqual(caught.exception.status, 409)
        await ws.close()

    async def test_robot_cli_reads_rgb_executes_and_releases(self):
        # Load torch outside patch.dict: removing its modules cannot undo native registrations.
        import torch  # noqa: F401
        await self._control_running()
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]/"inference"/"pi05"))
        import robot

        class Runner:
            device = "cpu"
            def __init__(self, *args, **kwargs):
                pass
            def predict(self, state, images, task):
                self_images = set(images)
                if self_images != {"camera1", "camera2", "camera3"}:
                    raise AssertionError(self_images)
                actions = np.tile(np.array(state, dtype=np.float32), (20, 1))
                actions[:, 0] -= .01
                actions[:, 7] -= .01
                return actions

        token_file = Path(self.tmp.name)/"edge.env"
        token_file.write_text("TELEOP_EDGE_TOKEN=test-secret\n")
        (Path(self.tmp.name)/"config.json").write_text(json.dumps({"chunk_size": 50}))
        config_file = Path(self.tmp.name)/"config.yaml"
        config_file.write_text("model:\n  action_horizon: 20\ncontrol:\n  joint_speed_rad_s: 0.3\n" +
                               "  max_joint_gap_rad: 1.2\nruntime:\n  log_dir: outputs\n")
        args = robot.parse_args(["--config", str(config_file), "--server-url", self.url,
            "--token-file", str(token_file), "--model", self.tmp.name, "--device", "cpu",
            "--task", "pick test", "--seconds", ".2", "--action-steps", "20", "--execute"])
        with patch.dict(sys.modules, {"infer": SimpleNamespace(PI05Runner=Runner)}):
            await robot.run(args)
        self.assertEqual(self.edge.arms.policy.mode, "hold")
        self.assertGreaterEqual(self.edge.arms.policy.sequence, 2)
        self.assertTrue(self.edge.arms.thread.is_alive())
        self.assertEqual(self.edge.arms.policy.settings["joint_speed_rad_s"], .3)
        self.assertEqual(self.edge.arms.policy.settings["max_joint_gap_rad"], 1.2)

    async def test_repeat_of_last_target_holds_through_a_short_data_stall(self):
        """While sensor data is stale only the last pose may be repeated (keep-alive); new motion waits."""
        bridge, ws = await self.start_policy()
        positions = list(self.edge.arms.policy.measured)
        reply = await self.send_target(ws, 0, positions=positions)
        self.assertEqual(reply["type"], "accepted", reply)
        bridge.receive = lambda meta, prepared: None  # the DGX stops hearing sensor data (Wi-Fi stall)
        bridge.last_data = time.monotonic() - 1.0
        reply = await self.send_target(ws, 1, positions=positions)
        self.assertEqual(reply["type"], "accepted", reply)
        self.assertEqual(self.edge.arms.policy.mode, "policy")
        moved = list(positions)
        moved[0] -= 0.01
        reply = await self.send_target(ws, 2, positions=moved)
        self.assertEqual(reply["type"], "error", reply)
        self.assertIn("stale Jetson sensor stream", reply["error"])
        await ws.close()

    async def test_invalid_settings_leave_teleop_control_active(self):
        await self._control_running()
        ws = await self.client.ws_connect(self.url+"/policy/ws", headers={"Authorization": "Bearer test-secret"})
        await ws.send_json({"type": "start", "settings": {"joint_speed_rad_s": float("nan")}})
        reply = await ws.receive_json(timeout=3)
        self.assertEqual(reply["type"], "error", reply)
        self.assertEqual(self.edge.arms.policy.mode, "teleop")
        await ws.close()


if __name__ == "__main__":
    unittest.main()
