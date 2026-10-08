"""Virtual base controller: browser joystick/keyboard -> geometry_msgs/Twist on /cmd_vel.

Safety model (the SLATE driver moves as soon as it receives a non-zero Twist):
- commands are accepted only after an explicit enable, and only from one client at a time;
- velocities are clamped to ``max_linear`` / ``max_angular`` (driver hard limit is 1.0);
- the browser sends commands only while the joystick/keys are held (dead-man);
- if no command arrives for ``timeout_s`` the server publishes zeros and stops publishing
  (the SLATE driver additionally zeroes after 300 ms without a command);
- disable, owner disconnect, arm-control stop and server shutdown publish zeros immediately.
Nothing is published while idle, so other /cmd_vel publishers are not overridden.
"""
import logging
import math
import threading
import time

log = logging.getLogger(__name__)


class RosTwistPublisher:
    """Publishes Twist (linear.x, angular.z) on its own rclpy context."""

    def __init__(self, topic="/cmd_vel"):
        import rclpy
        from geometry_msgs.msg import Twist
        self.rclpy, self.Twist, self.topic = rclpy, Twist, topic
        self.ctx = rclpy.Context()
        rclpy.init(context=self.ctx)
        self.node = rclpy.create_node("teleop_server_base_cmd", context=self.ctx)
        self.pub = self.node.create_publisher(Twist, topic, 10)  # reliable, like the driver subscription

    def publish(self, vx, wz):
        msg = self.Twist()
        msg.linear.x, msg.angular.z = float(vx), float(wz)
        self.pub.publish(msg)

    def close(self):
        try:
            self.node.destroy_node()
            self.rclpy.shutdown(context=self.ctx)
        except Exception:
            pass


class FakeTwistPublisher:
    """Records published commands; optionally drives a FakeOdom (``sink(vx, wz)``)."""

    def __init__(self, sink=None):
        self.sent = []
        self.sink = sink
        self.topic = "fake"

    def publish(self, vx, wz):
        self.sent.append((time.monotonic(), vx, wz))
        if self.sink:
            self.sink(vx, wz)

    def close(self):
        pass


class BaseController:
    def __init__(self, hub, publisher, max_linear=0.3, max_angular=0.6, rate=20.0, timeout_s=0.25, lease_s=1.0):
        self.hub, self.publisher = hub, publisher
        self.max_linear, self.max_angular = max_linear, max_angular
        self.rate, self.timeout_s, self.lease_s = rate, timeout_s, lease_s
        self.lock = threading.Lock()
        self.enabled = False
        self.owner = None
        self.cmd = (0.0, 0.0)
        self.t_cmd = 0.0
        self.active = False
        self.published = 0
        self.zero_pending = 0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="base-cmd", daemon=True)

    # ---------- lifecycle ----------
    def start(self):
        self.thread.start()
        self.publish_status()

    def stop(self):
        self.disable("server shutdown")
        self.stop_event.set()
        self.thread.join(timeout=2)
        self.publisher.close()

    # ---------- state ----------
    def status(self):
        with self.lock:
            return {"type": "base_ctl", "enabled": self.enabled, "owner": self.owner, "active": self.active,
                    "cmd": list(self.cmd), "max_linear": self.max_linear, "max_angular": self.max_angular,
                    "topic": self.publisher.topic, "published": self.published}

    def publish_status(self):
        self.hub.publish("base_ctl", self.status())

    def enable(self):
        with self.lock:
            self.enabled = True
        self.publish_status()

    def disable(self, reason="user"):
        with self.lock:
            was = self.enabled or self.active
            self.enabled, self.owner, self.cmd, self.active = False, None, (0.0, 0.0), False
            self._send_zero_now()
        if was:
            log.info("base control disabled (%s)", reason)
        self.publish_status()

    # ---------- commands (event loop thread) ----------
    def command(self, owner, vx, wz):
        """Returns None if accepted, else a reason string."""
        try:
            vx, wz = float(vx), float(wz)
        except (TypeError, ValueError):
            return "잘못된 명령입니다."
        if not (math.isfinite(vx) and math.isfinite(wz)):
            return "잘못된 명령입니다."
        now = time.monotonic()
        changed = False
        with self.lock:
            if not self.enabled:
                return "베이스 조작이 꺼져 있습니다."
            if self.owner not in (None, owner) and now - self.t_cmd < self.lease_s:
                return "다른 사용자가 조작 중입니다."
            changed = self.owner != owner
            self.owner = owner
            self.cmd = (max(-self.max_linear, min(self.max_linear, vx)),
                        max(-self.max_angular, min(self.max_angular, wz)))
            self.t_cmd = now
        if changed:
            self.publish_status()
        return None

    def release(self, owner):
        with self.lock:
            if self.owner != owner:
                return
            self.owner, self.cmd, self.active = None, (0.0, 0.0), False
            self._send_zero_now()
        self.publish_status()

    def _send_zero_now(self):  # call with lock held
        try:
            self.publisher.publish(0.0, 0.0)
            self.published += 1
        except Exception:
            log.exception("zero command publish failed")
        self.zero_pending = 2  # repeat on the next ticks in case one message is lost

    # ---------- publisher thread ----------
    def _loop(self):
        period = 1.0 / self.rate
        next_t = time.monotonic()
        last_status = 0.0
        while not self.stop_event.is_set():
            next_t += period
            self.stop_event.wait(max(0.0, next_t - time.monotonic()))
            notify = False
            with self.lock:
                fresh = self.enabled and self.owner is not None and time.monotonic() - self.t_cmd <= self.timeout_s
                try:
                    if fresh:
                        self.publisher.publish(*self.cmd)
                        self.published += 1
                        notify = not self.active
                        self.active = True
                    elif self.active or self.zero_pending:
                        if self.active:  # watchdog: commands stopped arriving
                            self.cmd, self.active, self.zero_pending, notify = (0.0, 0.0), False, 3, True
                        self.publisher.publish(0.0, 0.0)
                        self.published += 1
                        self.zero_pending -= 1
                except Exception:
                    log.exception("base command publish failed")
            if notify or (self.active and time.monotonic() - last_status > 0.2):
                last_status = time.monotonic()
                self.publish_status()
