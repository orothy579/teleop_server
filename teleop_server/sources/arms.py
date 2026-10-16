"""Leader/follower proprioception (and, in teleop mode, leader -> follower mirroring).

Real mode uses the same lerobot Trossen classes as the existing collectors, so connecting MOVES
every arm to its staged pose, followers then track the leaders, and a normal stop performs the
staged -> sleep disconnect motion. After a fault only ``driver.cleanup()`` is attempted.

After a policy (AI) run the followers hold their pose; ``return_to_teleop`` moves all four arms back
to the staged pose together and resumes teleop, the same state as right after connecting.
"""
import logging
import math
import threading
import time

from teleop_server.latency import MinOffsetClock
from teleop_server.sources.base import Source
from teleop_server.policy_control import PolicyControl

log = logging.getLogger(__name__)
SIDES = ("left", "right")
JOINT_NAMES = [*(f"joint_{i}" for i in range(6)), "left_carriage_joint"]
STAGED = [0.0, math.pi / 3, math.pi / 6, math.pi / 5, 0.0, 0.0, 0.0]  # lerobot Trossen default (rad, gripper m)


class Reading:
    __slots__ = ("pos", "vel", "eff", "ts_us")

    def __init__(self, pos, vel, eff, ts_us):
        self.pos, self.vel, self.eff, self.ts_us = pos, vel, eff, ts_us


class RealArm:
    def __init__(self, role, side, ip, loop_rate):
        self.role, self.side, self.ip = role, side, ip
        if role == "follower":
            from lerobot_robot_trossen.config_widowxai_follower import WidowXAIFollowerConfig
            from lerobot_robot_trossen.widowxai_follower import WidowXAIFollower
            self.dev = WidowXAIFollower(WidowXAIFollowerConfig(id=f"follower_{side}", ip_address=ip, loop_rate=loop_rate))
        else:
            from lerobot_teleoperator_trossen.config_widowxai_leader import WidowXAILeaderTeleopConfig
            from lerobot_teleoperator_trossen.widowxai_leader import WidowXAILeaderTeleop
            self.dev = WidowXAILeaderTeleop(WidowXAILeaderTeleopConfig(id=f"leader_{side}", ip_address=ip))

    def connect(self):
        self.dev.connect()  # lerobot connect: staged-pose motion (leader then gravity-compensated)

    def read(self):
        out = self.dev.driver.get_robot_output()
        j = out.joint.all
        return Reading(list(j.positions), list(j.velocities), list(j.efforts), int(out.header.timestamp))

    def command(self, positions):
        sent = self.dev.send_action({f"{n}.pos": p for n, p in zip(JOINT_NAMES, positions)})
        return [sent[f"{n}.pos"] for n in JOINT_NAMES]

    @property
    def staged(self):
        return list(self.dev.config.staged_positions)

    # Leaders are gravity-compensated (moved by hand); to move one, switch it to position control.
    def position_mode(self, hold):
        import trossen_arm
        self.dev.driver.set_all_modes(trossen_arm.Mode.position)
        self.move(hold)

    def move(self, positions):
        self.dev.driver.set_all_positions(list(positions), goal_time=0.1, blocking=False)
        return list(positions)

    def gravity_mode(self):  # as lerobot's leader configure(): external effort 0 = gravity compensation
        import trossen_arm
        self.dev.driver.set_all_modes(trossen_arm.Mode.external_effort)
        self.dev.driver.set_all_external_efforts([0.0] * len(JOINT_NAMES), goal_time=0.0, blocking=True)

    def close(self, fault):
        if fault:
            self.dev.driver.cleanup()  # no motion after a fault
        elif self.dev.is_connected:
            self.dev.disconnect()  # lerobot disconnect: staged -> sleep motion


class FakeArm:
    """Simulated arm: leaders move sinusoidally, followers track commands with a first-order lag."""

    def __init__(self, role, side, read_delay_ms=0.4, source_delay_ms=(0.5, 2.0)):
        self.role, self.side = role, side
        self.read_delay_ms, self.source_delay_ms = read_delay_ms, source_delay_ms
        self.pos = [0.0] * 7
        self.target = [0.0] * 7
        self.t0 = self.last = None
        self.connected = False
        self.held = False  # leader under position control (not moved by hand)
        self.offset = [0.0] * 7  # a released leader continues its motion from where it was held
        self.staged = list(STAGED)

    def connect(self):
        self.t0 = self.last = time.monotonic()
        self.connected = True

    def read(self):
        import random
        time.sleep(self.read_delay_ms / 1e3)
        now = time.monotonic()
        t = now - self.t0
        phase = 0.0 if self.side == "left" else 1.0
        if self.role == "leader" and not self.held:
            self.pos = [o + v for o, v in zip(self.offset, self._hand(t, phase))]
        else:
            a = min(1.0, (now - self.last) / 0.05)
            self.pos = [p + a * (g - p) for p, g in zip(self.pos, self.target)]
        self.last = now
        lag = random.uniform(*self.source_delay_ms) / 1e3
        ts_us = int((now - self.t0 - lag) * 1e6)
        return Reading(list(self.pos), [0.0] * 7, [0.0] * 7, ts_us)

    @staticmethod
    def _hand(t, phase):  # the operator's simulated hand motion
        return [0.4 * math.sin(0.5 * t + phase + i) for i in range(6)] + [0.02 + 0.015 * math.sin(0.8 * t + phase)]

    def command(self, positions):
        self.target = list(positions)
        return list(positions)

    def position_mode(self, hold):
        self.held = True
        self.move(hold)

    def move(self, positions):
        self.target = list(positions)
        return list(positions)

    def gravity_mode(self):
        t = time.monotonic() - self.t0
        self.offset = [p - v for p, v in zip(self.pos, self._hand(t, 0.0 if self.side == "left" else 1.0))]
        self.held = False

    def close(self, fault):
        self.connected = False


class ReturnMotion:
    """All arms from where they are to their staged pose along one smooth (smoothstep) path.

    The slowest joint sets the duration: on average at most ``speed`` rad/s (peak 1.5x), never
    shorter than lerobot's own 2 s staged move. ``settle`` keeps commanding the staged pose briefly so
    the arms are there before the leaders are released.
    """

    def __init__(self, starts, goals, now, speed=0.5, min_s=2.0, max_s=8.0, settle=0.4):
        self.starts, self.goals, self.t0 = starts, goals, now
        far = max((abs(g - s) for k in starts for i, (s, g) in enumerate(zip(starts[k], goals[k])) if i != 6), default=0.0)
        self.duration, self.settle = min(max_s, max(min_s, far / speed)), settle

    def progress(self, now):
        return min(1.0, max(0.0, (now - self.t0) / self.duration))

    def at(self, key, now):
        x = self.progress(now)
        x = x * x * (3 - 2 * x)
        return [s + x * (g - s) for s, g in zip(self.starts[key], self.goals[key])]

    def finished(self, now):
        return now - self.t0 >= self.duration + self.settle


class Cycle:
    """One teleop cycle, in the same order and with the same clocks as teleop_server/mobile_capture.py."""
    __slots__ = ("begin_ns", "end_ns", "unix_ns", "follower", "leader", "action")

    def __init__(self, begin_ns, end_ns, unix_ns, follower, leader, action):
        self.begin_ns, self.end_ns, self.unix_ns = begin_ns, end_ns, unix_ns
        self.follower, self.leader, self.action = follower, leader, action


class ArmSource(Source):
    """Four arms, connected only on request (``start_control``) unless ``autostart``.

    Each cycle reads both followers (observation), then both leaders, then commands the followers
    with the leader positions, like mobile_capture.py. ``cycle_sink`` (if set) receives every
    ``Cycle`` on the arm thread and must return quickly; ``exit_hooks`` are called with the error
    (or None) before any arm is disconnected.
    """
    kind = "arms"

    def __init__(self, hub, registry, arms, rate=30.0, telemetry_hz=50.0, teleop=True, autostart=False):
        super().__init__("arms", hub, registry)
        self.registry = registry
        self.make_arms = arms if callable(arms) else (lambda: arms)  # {(role, side): arm}
        self.arms = {}
        self.rate, self.telemetry_hz, self.teleop, self.autostart = rate, telemetry_hz, teleop, autostart
        self.control_lock = threading.Lock()
        self.policy = PolicyControl()
        self.cycle_sink = None
        self.exit_hooks = []
        self.teleop_hooks = []  # called on the arm thread when teleop resumes after a return
        self.motion = None
        self.loop_overruns = 0
        self.cycles = 0
        self.info = {"rate_hz": rate, "teleop": teleop}

    def start(self):
        """Server start-up: arms stay disconnected (no motion) unless autostart."""
        if self.autostart:
            self.start_control()
        else:
            self._set_state("idle")

    def start_control(self):
        with self.control_lock:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError(f"arm control is already {self.state}")
            self.stop_event = threading.Event()
            self.policy.reset()
            self.motion = None
            self.error = None
            self.arms = self.make_arms()
            self.clocks = {k: MinOffsetClock() for k in self.arms}
            self.last_pub = {k: 0.0 for k in self.arms}
            self.info["arms"] = {f"{r}_{s}": getattr(a, "ip", "fake") for (r, s), a in self.arms.items()}
            self.thread = threading.Thread(target=self._main, name=self.name, daemon=True)
            self.thread.start()

    def stop_control(self):
        """Request a normal stop (staged -> sleep motion); returns immediately."""
        self.stop_event.set()

    def start_policy(self, owner, settings=None):
        with self.policy.lock:
            if self.state != "running" or self.stop_event.is_set() or not self.teleop:
                raise RuntimeError("connect the arms and wait for running state first")
            configured = self.policy.validated_settings(settings)
            if configured["command_hz"] > self.rate:
                raise ValueError("policy command frequency exceeds the arm loop rate")
            self.policy.begin(owner, settings=configured)

    def return_to_teleop(self):
        """After a policy run: move every arm back to the staged pose, then resume teleop."""
        with self.policy.lock:
            if self.state != "running" or self.stop_event.is_set() or not self.teleop:
                raise RuntimeError("arm control is not running")
            self.policy.start_return()
            return self.policy.status()

    def _return_step(self, fol, lead, now):
        """One cycle of the return motion (policy lock held); returns the follower commands."""
        if self.motion is None:  # first cycle: leaders hold where they are, then all four move together
            starts = {("follower", side): fol[side][0].pos for side in SIDES}
            starts.update({("leader", side): lead[side][0].pos for side in SIDES})
            for side in SIDES:
                self.arms[("leader", side)].position_mode(starts[("leader", side)])
            self.motion = ReturnMotion(starts, {k: self.arms[k].staged for k in starts}, now)
            log.info("arms returning to the staged pose (%.1f s)", self.motion.duration)
        sent = {}
        for side in SIDES:
            sent[side] = self.arms[("follower", side)].command(self.motion.at(("follower", side), now))
            self.arms[("leader", side)].move(self.motion.at(("leader", side), now))
        self.info["return"] = dict(progress=round(self.motion.progress(now), 3), duration_s=round(self.motion.duration, 2))
        if self.motion.finished(now):
            for side in SIDES:
                self.arms[("leader", side)].gravity_mode()
            self.motion = None
            self.info.pop("return", None)
            self.policy.finish_return()
            log.info("arms at the staged pose; teleop resumed")
            for hook in self.teleop_hooks:
                try:
                    hook()
                except Exception:
                    log.exception("teleop hook failed")
        return sent

    def stop(self, timeout=60.0):  # server shutdown; staged/sleep disconnect of four arms takes ~16 s
        super().stop(timeout)

    def _read(self, key):
        t0 = time.time_ns()
        r = self.arms[key].read()
        t1 = time.time_ns()
        t_source = self.clocks[key].to_host_ns(r.ts_us * 1000, t1)
        name = f"{key[0]}_{key[1]}"
        self.registry.get(name).record({"sensor_ms": (t1 - t_source) / 1e6, "read_ms": (t1 - t0) / 1e6}, t1)
        return r, t0, t1, t_source

    def _publish(self, key, sample, cmd=None):
        r, t0, t1, t_source = sample
        if time.monotonic() - self.last_pub[key] < 1.0 / self.telemetry_hz:
            return
        self.last_pub[key] = time.monotonic()
        name = f"{key[0]}_{key[1]}"
        msg = {"type": "arm", "sensor": name, "role": key[0], "side": key[1], "pos": r.pos, "vel": r.vel,
               "eff": r.eff, "controller_ts_us": r.ts_us, "t_source_ns": t_source, "t_recv_ns": t1,
               "read_ms": (t1 - t0) / 1e6}
        if cmd is not None:
            msg["cmd"] = cmd
        self.hub.publish(f"arm:{name}", msg)

    def run(self):
        opened, error = [], None
        order = [("follower", s) for s in SIDES] + [("leader", s) for s in SIDES]
        try:
            for key in order:
                if self.stop_event.is_set():
                    return
                self.arms[key].connect()
                opened.append(key)
            self._set_state("running")
            period, next_t = 1.0 / self.rate, time.monotonic()
            while not self.stop_event.is_set():
                begin_ns, unix_ns = time.monotonic_ns(), time.time_ns()
                fol = {side: self._read(("follower", side)) for side in SIDES}
                lead = {side: self._read(("leader", side)) for side in SIDES}
                sent = {}
                with self.policy.lock:
                    self.policy.observe([*fol["left"][0].pos, *fol["right"][0].pos])
                    if self.teleop and self.policy.mode == "returning":
                        sent = self._return_step(fol, lead, time.monotonic())
                    else:
                        targets = self.policy.command(period)
                        if self.teleop:
                            for index, side in enumerate(SIDES):
                                positions = lead[side][0].pos if targets is None else targets[index*7:(index+1)*7]
                                sent[side] = self.arms[("follower", side)].command(positions)
                    self.info["policy"] = self.policy.status()
                end_ns = time.monotonic_ns()
                for side in SIDES:
                    self._publish(("leader", side), lead[side])
                    self._publish(("follower", side), fol[side], sent.get(side))
                self.cycles += 1
                self.info["cycles"] = self.cycles
                sink = self.cycle_sink
                if sink is not None and self.teleop:
                    try:  # a recorder problem must never stop teleoperation
                        sink(Cycle(begin_ns, end_ns, unix_ns,
                                   [*fol["left"][0].pos, *fol["right"][0].pos],
                                   [*lead["left"][0].pos, *lead["right"][0].pos],
                                   [*sent["left"], *sent["right"]]))
                    except Exception:
                        log.exception("cycle sink failed")
                next_t += period
                if time.monotonic() > next_t:
                    self.loop_overruns += 1
                    self.info["loop_overruns"] = self.loop_overruns
                    next_t = time.monotonic()
                self.sleep_until(next_t)
        except BaseException as exc:
            error = exc
            raise
        finally:
            for hook in self.exit_hooks:  # e.g. stop recording BEFORE any disconnect motion
                try:
                    hook(error)
                except Exception:
                    pass
            if error is None and opened:
                self._set_state("stopping")
            errors = []
            for key in reversed(opened):
                try:
                    self.arms[key].close(error is not None)
                except Exception as exc:
                    errors.append(f"{key}: {exc!r}")
                    try:
                        if hasattr(self.arms[key], "dev"):
                            self.arms[key].dev.driver.cleanup()
                    except Exception:
                        pass
            if errors:
                self.error = "; ".join(errors)


def build_arm_source(args, hub, stats):
    ips = {("follower", "left"): args.follower_left, ("follower", "right"): args.follower_right,
           ("leader", "left"): args.leader_left, ("leader", "right"): args.leader_right}
    if args.arms == "teleop":
        make = lambda: {k: RealArm(*k, ip, args.arm_rate) for k, ip in ips.items()}  # fresh drivers per session
    else:
        make = lambda: {k: FakeArm(*k) for k in ips}
    return ArmSource(hub, stats, make, rate=args.arm_rate, telemetry_hz=args.telemetry_hz)
