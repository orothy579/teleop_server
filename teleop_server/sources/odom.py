"""Mobile base odometry. Subscription only: never publishes velocity or enables the base."""
import math
import time

from teleop_server.sources.base import Source


class OdomSourceBase(Source):
    kind = "odom"

    def __init__(self, hub, stats):
        super().__init__("odom", hub, stats)

    def emit(self, p, q, v, w, t_source_ns, t_recv_ns, frame_id, child_frame_id, extra=None):
        values = [*p, *q, *v, *w]
        if not all(math.isfinite(x) for x in values):
            self.stats.dropped += 1
            return
        self.stats.record({"sensor_ms": (t_recv_ns - t_source_ns) / 1e6}, t_recv_ns)
        self.hub.publish("odom", {"type": "odom", "sensor": "odom", "p": list(p), "q": list(q), "v": list(v),
                                  "w": list(w), "frame_id": frame_id, "child_frame_id": child_frame_id,
                                  "t_source_ns": t_source_ns, "t_recv_ns": t_recv_ns, **(extra or {})})


class RosOdom(OdomSourceBase):
    """nav_msgs/Odometry. The header stamp is the source time (same host clock for the SLATE driver)."""

    def __init__(self, hub, stats, topic="/odom"):
        super().__init__(hub, stats)
        self.topic = topic
        self.info = {"topic": topic}

    def run(self):
        import rclpy
        from nav_msgs.msg import Odometry
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        ctx = rclpy.Context()
        rclpy.init(context=ctx)
        node = rclpy.create_node("teleop_server_odom", context=ctx)
        executor = SingleThreadedExecutor(context=ctx)
        executor.add_node(node)

        def callback(msg):
            t_recv = time.time_ns()
            pp, qq = msg.pose.pose.position, msg.pose.pose.orientation
            vv, ww = msg.twist.twist.linear, msg.twist.twist.angular
            self.emit((pp.x, pp.y, pp.z), (qq.x, qq.y, qq.z, qq.w), (vv.x, vv.y, vv.z), (ww.x, ww.y, ww.z),
                      msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec, t_recv,
                      msg.header.frame_id, msg.child_frame_id)

        node.create_subscription(Odometry, self.topic, callback, qos_profile_sensor_data)
        self._set_state("running")
        try:
            while not self.stop_event.is_set():
                executor.spin_once(timeout_sec=0.1)
        finally:
            executor.shutdown()
            node.destroy_node()
            rclpy.shutdown(context=ctx)


class FakeOdom(OdomSourceBase):
    """20 Hz synthetic odometry. Default: circular motion. ``driven``: integrates velocity commands
    (``command(vx, wz)``) like a differential base, stopping 0.3 s after the last command."""

    def __init__(self, hub, stats, rate=20.0, delay_ms=3.0, driven=False):
        super().__init__(hub, stats)
        self.rate, self.delay_ms, self.driven = rate, delay_ms, driven
        self.info = {"fake": True, "driven": driven}
        self._cmd, self._t_cmd = (0.0, 0.0), 0.0

    def command(self, vx, wz):
        self._cmd, self._t_cmd = (vx, wz), time.monotonic()

    def run(self):
        self._set_state("running")
        t0 = next_t = time.monotonic()
        r, w = 1.0, 0.3
        x = y = yaw = 0.0
        while not self.stop_event.is_set():
            next_t += 1 / self.rate
            self.sleep_until(next_t)
            t_recv = time.time_ns()
            if self.driven:
                vx, wz = self._cmd if time.monotonic() - self._t_cmd <= 0.3 else (0.0, 0.0)
                yaw += wz / self.rate
                x += vx * math.cos(yaw) / self.rate
                y += vx * math.sin(yaw) / self.rate
                self.emit((x, y, 0.0), (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)), (vx, 0.0, 0.0),
                          (0.0, 0.0, wz), t_recv - int(self.delay_ms * 1e6), t_recv, "odom", "base_link")
                continue
            t = time.monotonic() - t0
            yaw_c = w * t + math.pi / 2
            self.emit((r * math.cos(w * t) - r, r * math.sin(w * t), 0.0),
                      (0.0, 0.0, math.sin(yaw_c / 2), math.cos(yaw_c / 2)), (r * w, 0.0, 0.0), (0.0, 0.0, w),
                      t_recv - int(self.delay_ms * 1e6), t_recv, "odom", "base_link")


def build_odom_source(args, hub, stats):
    if args.odom == "ros":
        return RosOdom(hub, stats, args.odom_topic)
    return FakeOdom(hub, stats, driven=getattr(args, "base_control", "off") in ("auto", "fake"))
