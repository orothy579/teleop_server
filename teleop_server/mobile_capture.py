"""Four arms + ROS odometry + three RGB cameras. Base is subscription-only."""
import argparse
import json
import math
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

JOINTS = [f"{arm}_{joint}.pos" for arm in ("left", "right")
          for joint in [*(f"joint_{i}" for i in range(6)), "left_carriage_joint"]]
BASE_NAMES = ["x", "y", "z", "qx", "qy", "qz", "qw", "vx", "vy", "vz", "wx", "wy", "wz"]


class BaseSubscriber:
    """Keep full raw ROS messages and latest samples using host monotonic time."""
    def __init__(self, odom_topic, cmd_topic, path):
        import rclpy
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry
        from rclpy.qos import qos_profile_sensor_data
        from rosidl_runtime_py.convert import message_to_ordereddict
        self.rclpy = rclpy
        self.lock = threading.Lock()
        self.latest = None
        self.command = None
        self.error = None
        self.stop = threading.Event()
        self.log = path.open("x")
        rclpy.init()
        self.node = rclpy.create_node("lerobot_mobile_capture")

        def callback(msg, kind):
            try:
                mono = time.monotonic_ns()
                raw = {"kind": kind, "receive_monotonic_ns": mono,
                       "receive_unix_ns": time.time_ns(), "message": message_to_ordereddict(msg)}
                self.log.write(json.dumps(raw) + "\n")
                self.log.flush()
                if kind == "odom":
                    p, q = msg.pose.pose.position, msg.pose.pose.orientation
                    v, w = msg.twist.twist.linear, msg.twist.twist.angular
                    values = [p.x, p.y, p.z, q.x, q.y, q.z, q.w, v.x, v.y, v.z, w.x, w.y, w.z]
                    if not all(math.isfinite(x) for x in values):
                        raise ValueError("Nonfinite odometry")
                    if not 0.9 < sum(x*x for x in values[3:7]) < 1.1:
                        raise ValueError("Invalid odometry quaternion")
                    sample = {"values": values, "receive_ns": mono,
                              "stamp_ns": msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec,
                              "frame_id": msg.header.frame_id, "child_frame_id": msg.child_frame_id}
                    with self.lock:
                        self.latest = sample
                else:
                    v, w = msg.linear, msg.angular
                    values = [v.x, v.y, v.z, w.x, w.y, w.z]
                    if not all(math.isfinite(x) for x in values):
                        raise ValueError("Nonfinite cmd_vel")
                    with self.lock:
                        self.command = {"values": values, "receive_ns": mono}
            except Exception as exc:
                self.error = exc

        self.node.create_subscription(Odometry, odom_topic, lambda m: callback(m, "odom"), qos_profile_sensor_data)
        self.node.create_subscription(Twist, cmd_topic, lambda m: callback(m, "cmd_vel"), qos_profile_sensor_data)
        def spin():
            try:
                while not self.stop.is_set():
                    rclpy.spin_once(self.node, timeout_sec=0.05)
            except Exception as exc:
                self.error = exc
        self.thread = threading.Thread(target=spin, daemon=True)
        self.thread.start()

    def sample(self, max_age_s):
        if self.error:
            raise RuntimeError("ROS capture failed") from self.error
        with self.lock:
            odom, cmd = self.latest, self.command
        now = time.monotonic_ns()
        if odom is None or (now - odom["receive_ns"]) / 1e9 > max_age_s:
            raise RuntimeError("No fresh odometry; check base driver, ROS_DOMAIN_ID and odom topic")
        return odom, cmd, now

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        self.node.destroy_node()
        self.rclpy.shutdown()
        self.log.close()


def features():
    def ft(names, dtype="float32"):
        return {"dtype": dtype, "shape": (len(names),), "names": names}
    result = {"observation.state": ft(JOINTS), "action": ft(JOINTS),
              "observation.leader_state": ft(JOINTS), "observation.base_state": ft(BASE_NAMES),
              "observation.base_command": ft(["vx", "vy", "vz", "wx", "wy", "wz"]),
              "observation.base_command_valid": ft(["fresh"], "int64"),
              "observation.base_age_ms": ft(["odom", "command"]),
              "observation.base_stamp_ns": ft(["ros_header"], "int64"),
              "observation.sample_monotonic_ns": ft(["capture_start", "capture_end"], "int64"),
              "observation.sample_unix_ns": ft(["capture_start"], "int64"),
              "observation.camera_age_ms": ft(["camera1", "camera2", "camera3"])}
    for cam in ("camera1", "camera2", "camera3"):
        result[f"observation.images.{cam}"] = {"dtype": "video", "shape": (480, 640, 3),
                                               "names": ["height", "width", "channels"]}
    return result


def base_frame(odom, cmd, now, max_age):
    valid = cmd is not None and (now - cmd["receive_ns"]) / 1e9 <= max_age
    return {"observation.base_state": np.array(odom["values"], dtype=np.float32),
            "observation.base_stamp_ns": np.array([odom["stamp_ns"]], dtype=np.int64),
            "observation.base_command": np.array(cmd["values"] if valid else [0]*6, dtype=np.float32),
            "observation.base_command_valid": np.array([int(valid)], dtype=np.int64),
            "observation.base_age_ms": np.array([(now-odom["receive_ns"])/1e6,
                (now-cmd["receive_ns"])/1e6 if cmd else -1], dtype=np.float32)}


def run(args):
    from lerobot.configs.video import RGBEncoderConfig
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    print(f"Session: {root}", flush=True)
    manifest = {"task": args.task, "status": "preflight", "base_control": "subscribe_only",
                "odom_topic": args.odom_topic, "cmd_topic": args.cmd_topic,
                "serials": args.serials, "fps": args.fps, "requested_seconds": args.seconds,
                "ips": {"follower_left": "192.168.1.5", "follower_right": "192.168.1.4",
                        "leader_left": "192.168.1.3", "leader_right": "192.168.1.2"},
                "start_unix_ns": time.time_ns(), "frames": 0}
    manifest_path = root / "session.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    base = robot = leader = dataset = None
    arms = []
    frames = 0
    fault = False
    try:
        base = BaseSubscriber(args.odom_topic, args.cmd_topic, root / "base_raw.jsonl")
        deadline = time.monotonic() + args.base_timeout
        while base.latest is None and time.monotonic() < deadline and base.error is None:
            time.sleep(0.05)
        odom, _, _ = base.sample(args.max_base_age)
        manifest["base_frames"] = {k: odom[k] for k in ("frame_id", "child_frame_id")}
        if not args.execute:
            manifest["status"] = "base_probe_pass"
            print("Fresh odometry received. No arm connection performed.")
            return
        import pyrealsense2 as rs
        from lerobot.cameras.realsense.configuration_realsense import RealSenseCameraConfig
        from lerobot_robot_trossen.bi_widowxai_follower import BiWidowXAIFollowerRobot
        from lerobot_robot_trossen.config_bi_widowxai_follower import BiWidowXAIFollowerRobotConfig
        from lerobot_teleoperator_trossen.bi_widowxai_leader import BiWidowXAILeaderRobot
        from lerobot_teleoperator_trossen.config_bi_widowxai_leader import BiWidowXAILeaderRobotConfig
        context = rs.context()
        detected = {d.get_info(rs.camera_info.serial_number) for d in context.query_devices()}
        if not set(args.serials) <= detected:
            raise RuntimeError(f"Required cameras missing, detected {sorted(detected)}")
        camera_configs = {f"camera{i+1}": RealSenseCameraConfig(serial_number_or_name=s,
            width=640, height=480, fps=args.fps, use_depth=False) for i, s in enumerate(args.serials)}
        robot = BiWidowXAIFollowerRobot(BiWidowXAIFollowerRobotConfig(
            id="bimanual_follower", left_arm_ip_address="192.168.1.5", right_arm_ip_address="192.168.1.4",
            cameras=camera_configs, loop_rate=args.fps))
        leader = BiWidowXAILeaderRobot(BiWidowXAILeaderRobotConfig(
            id="bimanual_leader", left_arm_ip_address="192.168.1.3", right_arm_ip_address="192.168.1.2"))
        # Validate camera streams BEFORE any driver.connect() startup movement.
        for cam in robot.cameras.values():
            cam.connect()
            cam.read_latest()
        arms = [robot.left_arm, robot.right_arm, leader.left_arm, leader.right_arm]
        for arm in arms:
            base.sample(args.max_base_age)
            arm.connect()
        dataset = LeRobotDataset.create("local/mobile_paper_bag", args.fps, features(),
            root=root/"dataset", robot_type=robot.name, video_backend="pyav",
            rgb_encoder=RGBEncoderConfig(vcodec="h264"), image_writer_threads=6)
        manifest["status"] = "recording"
        print(f"RECORDING {args.seconds}s: four arms, base odometry, three cameras", flush=True)
        started = time.monotonic()
        while time.monotonic() - started < args.seconds:
            tick = time.monotonic()
            begin_ns, unix_ns = time.monotonic_ns(), time.time_ns()
            obs = robot.get_observation()
            command = leader.get_action()  # Actual leader positions, same driver ownership.
            odom, base_cmd, now = base.sample(args.max_base_age)
            if any(odom[k] != manifest["base_frames"][k] for k in ("frame_id", "child_frame_id")):
                raise RuntimeError("Odometry frame IDs changed during recording")
            sent = robot.send_action(command)
            frame = {"observation.state": np.array([obs[k] for k in JOINTS], dtype=np.float32),
                     "observation.leader_state": np.array([command[k] for k in JOINTS], dtype=np.float32),
                     "action": np.array([sent[k] for k in JOINTS], dtype=np.float32),
                     "observation.sample_monotonic_ns": np.array([begin_ns, time.monotonic_ns()], dtype=np.int64),
                     "observation.sample_unix_ns": np.array([unix_ns], dtype=np.int64),
                     "observation.camera_age_ms": np.array([(time.perf_counter()-c.latest_timestamp)*1000
                         for c in robot.cameras.values()], dtype=np.float32),
                     "task": args.task, **base_frame(odom, base_cmd, now, args.max_base_age)}
            for cam in robot.cameras:
                frame[f"observation.images.{cam}"] = obs[cam]
            dataset.add_frame(frame)
            frames += 1
            time.sleep(max(0, 1/args.fps - (time.monotonic()-tick)))
        manifest["recording_elapsed_s"] = time.monotonic()-started
        manifest["status"] = "completed"
    except BaseException as exc:
        fault = True
        manifest.update(status="failed", error=repr(exc))
        raise
    finally:
        # Release each device independently; never stage an arm after a capture fault.
        errors = []
        for arm in arms:
            try:
                if fault:
                    arm.driver.cleanup()
                elif arm.is_connected:
                    arm.disconnect()
            except Exception as exc:
                errors.append(repr(exc))
                try:
                    arm.driver.cleanup()
                except Exception as cleanup_exc:
                    errors.append(repr(cleanup_exc))
        if robot:
            for cam in robot.cameras.values():
                try:
                    if cam.is_connected:
                        cam.disconnect()
                except Exception as exc:
                    errors.append(repr(exc))
        if base:
            try:
                base.close()
            except Exception as exc:
                errors.append(repr(exc))
        try:
            if dataset is not None and frames:
                dataset.save_episode()
            if dataset is not None:
                dataset.finalize()
        except Exception as exc:
            errors.append(repr(exc))
        manifest.update(frames=frames, end_unix_ns=time.time_ns(), cleanup_errors=errors)
        if errors:
            manifest["status"] = "failed"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        if errors and not fault:
            raise RuntimeError(f"Session incomplete: {errors}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Connect/move arms and record; otherwise probe base only")
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--odom-topic", default="/odom")
    parser.add_argument("--cmd-topic", default="/cmd_vel", help="geometry_msgs/Twist, optional")
    parser.add_argument("--base-timeout", type=float, default=5)
    parser.add_argument("--max-base-age", type=float, default=0.25)
    parser.add_argument("--serials", nargs=3, default=["260322273312", "260522278645", "260322272677"])
    parser.add_argument("--task", default="Pick up the paper bag and place it in the marked target area.")
    tag = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S_%f")
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1]/"capture_tools"/"outputs"/f"mobile_{tag}")
    args = parser.parse_args()
    if args.seconds <= 0 or args.fps <= 0 or args.base_timeout <= 0 or args.max_base_age <= 0 or len(set(args.serials)) != 3:
        parser.error("Positive durations/rate and three distinct camera serials required")
    os.environ.setdefault("ROS_LOG_DIR", "/tmp/lerobot-ros-logs")
    run(args)


if __name__ == "__main__":
    main()
