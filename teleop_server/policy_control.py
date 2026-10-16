"""Exclusive policy ownership and bounded position targets, enforced on the robot host.

Modes: ``teleop`` (followers mirror the leaders), ``policy`` (one owner sends targets), ``hold``
(followers keep their measured pose) and ``returning`` (all arms move back to the staged pose; the
arm source then resumes teleop). A return can only start from hold and is not interrupted by policy
stops; ending arm control is the way to abort it.
"""
import math
import threading
import time


class PolicyControl:
    TIMEOUT = 0.5
    JOINT_SPEED = 0.6  # radians / second
    GRIPPER_SPEED = 0.02  # metres / second
    MAX_JOINT_GAP = 1.0
    MAX_GRIPPER_GAP = 0.025

    @classmethod
    def defaults(cls):
        return dict(command_timeout_s=cls.TIMEOUT, joint_speed_rad_s=cls.JOINT_SPEED,
                    gripper_speed_m_s=cls.GRIPPER_SPEED, max_joint_gap_rad=cls.MAX_JOINT_GAP,
                    max_gripper_gap_m=cls.MAX_GRIPPER_GAP, joint_tracking_gap_rad=.08,
                    gripper_tracking_gap_m=.003, command_hz=30.)

    @classmethod
    def validated_settings(cls, values):
        settings = cls.defaults()
        if values is None:
            return settings
        if not isinstance(values, dict) or values.keys()-settings.keys():
            raise ValueError("unknown policy control settings")
        settings.update(values)
        for name, value in settings.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        if settings["command_hz"] > 30 or settings["command_timeout_s"] < 2/settings["command_hz"]:
            raise ValueError("command_hz must be <=30 and timeout must allow two command periods")
        if settings["joint_tracking_gap_rad"] > settings["max_joint_gap_rad"] or \
                settings["gripper_tracking_gap_m"] > settings["max_gripper_gap_m"]:
            raise ValueError("tracking gaps must not exceed target rejection gaps")
        return settings

    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.mode, self.owner, self.reason = "teleop", None, None
            self.settings = self.defaults()
            self.measured = self.target = self.last_command = None
            self.measured_at = self.deadline = 0.0
            self.sequence = -1
            self.clipped = 0

    def observe(self, positions, now=None):
        with self.lock:
            self.measured = self.validate(positions)
            self.measured_at = time.monotonic() if now is None else now

    @staticmethod
    def validate(positions):
        if not isinstance(positions, (list, tuple)) or len(positions) != 14:
            raise ValueError("positions must contain left 7 then right 7")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in positions):
            raise ValueError("positions must be finite numbers")
        return list(positions)

    def begin(self, owner, now=None, settings=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.mode == "policy":
                raise RuntimeError("another policy owns the arms")
            if self.mode == "returning":
                raise RuntimeError("arms are returning to the staged pose")
            if self.measured is None or now - self.measured_at > 0.1:
                raise RuntimeError("fresh follower positions are required")
            configured = self.validated_settings(settings)
            self.settings = configured
            self.mode, self.owner, self.reason = "policy", owner, None
            self.target = self.last_command = list(self.measured)
            self.sequence = -1
            self.deadline = now + self.settings["command_timeout_s"]

    def hold(self, reason, owner=None):
        with self.lock:
            if owner is not None and owner != self.owner:
                return False
            if self.mode in ("teleop", "returning"):
                return False
            self.mode, self.owner, self.reason = "hold", None, reason
            self.target = list(self.measured) if self.measured is not None else None
            self.last_command = list(self.target) if self.target is not None else None
            return True

    def start_return(self):
        """hold -> returning. Returns False when already in teleop (nothing to do)."""
        with self.lock:
            if self.mode == "teleop":
                return False
            if self.mode == "policy":
                raise RuntimeError("a policy owns the arms; stop it first")
            if self.mode == "returning":
                raise RuntimeError("arms are already returning to the staged pose")
            self.mode, self.owner, self.reason = "returning", None, "returning to the staged pose"
            return True

    def finish_return(self):
        with self.lock:
            if self.mode == "returning":
                self.mode, self.reason = "teleop", None
                self.target = self.last_command = None

    def accept(self, owner, sequence, positions, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.mode != "policy" or owner != self.owner:
                raise RuntimeError("policy no longer owns the arms")
            if now >= self.deadline:
                self.hold("policy command timeout")
                raise RuntimeError("policy command timeout")
            try:
                target = self.validate(positions)
                if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= self.sequence:
                    raise ValueError("out-of-order policy command")
                for i, (goal, present) in enumerate(zip(target, self.measured)):
                    limit = self.settings["max_gripper_gap_m" if i in (6, 13) else "max_joint_gap_rad"]
                    if abs(goal - present) > limit:
                        raise ValueError(f"target too far from measured position at index {i}: "
                                         f"measured={present:.6f}, target={goal:.6f}, "
                                         f"gap={abs(goal-present):.6f}, limit={limit:.6f}")
            except (ValueError, TypeError):
                self.hold("invalid policy target")
                raise
            self.target, self.sequence, self.deadline = target, sequence, now + self.settings["command_timeout_s"]

    def command(self, period, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.mode == "policy" and now >= self.deadline:
                self.hold("policy command timeout")
            if self.mode in ("teleop", "returning"):  # the arm source drives both
                return None
            if self.target is None:
                raise RuntimeError("no measured hold position")
            if self.mode == "hold":
                return list(self.target)
            result = []
            for i, (goal, last, present) in enumerate(zip(self.target, self.last_command, self.measured)):
                gripper = i in (6, 13)
                step = self.settings["gripper_speed_m_s" if gripper else "joint_speed_rad_s"] * min(period, 1 / 30)
                # Bound both command velocity and commanded displacement from actual feedback.
                gap = self.settings["gripper_tracking_gap_m" if gripper else "joint_tracking_gap_rad"]
                limited = max(last - step, min(last + step, goal))
                limited = max(present - gap, min(present + gap, limited))
                self.clipped += int(abs(limited - goal) > 1e-9)
                result.append(limited)
            self.last_command = result
            return list(result)

    def status(self):
        with self.lock:
            return dict(mode=self.mode, owner=self.owner, reason=self.reason,
                        sequence=self.sequence, clipped=self.clipped,
                        timeout_s=self.settings["command_timeout_s"],
                        joint_speed_rad_s=self.settings["joint_speed_rad_s"],
                        gripper_speed_m_s=self.settings["gripper_speed_m_s"], settings=dict(self.settings))
