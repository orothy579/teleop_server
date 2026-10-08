"""Build a local-only, two/three-camera recording command; --execute starts robot motion."""
import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--top-serial", required=True)
    p.add_argument("--wrist-serial", required=True)
    p.add_argument("--extra-serial", help="Optional third RGB camera, saved as camera3")
    p.add_argument("--episodes", type=int, default=1)
    p.add_argument("--seconds", type=int, default=10)
    p.add_argument("--execute", action="store_true")
    a = p.parse_args()
    camera_pairs = [("camera1", a.top_serial), ("camera2", a.wrist_serial)]
    if a.extra_serial:
        camera_pairs.append(("camera3", a.extra_serial))
    requested_serials = {serial for _, serial in camera_pairs}
    if len(requested_serials) != len(camera_pairs) or a.episodes < 1 or a.seconds < 1:
        p.error("Use distinct camera serials and positive episodes/seconds")
    project = Path(__file__).resolve().parent.parent
    tag = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S")
    root = project / "capture_tools" / "outputs" / f"record_{tag}"
    cameras = {
        name: {"type": "intelrealsense", "serial_number_or_name": serial,
               "width": 640, "height": 480, "fps": 30, "use_depth": False}
        for name, serial in camera_pairs
    }
    cmd = [str(Path(sys.executable).with_name("lerobot-record")),
           "--robot.type=bi_widowxai_follower_robot",
           "--robot.left_arm_ip_address=192.168.1.5",
           "--robot.right_arm_ip_address=192.168.1.4",
           "--robot.id=bimanual_follower", "--robot.loop_rate=30",
           "--robot.cameras=" + json.dumps(cameras),
           "--teleop.type=bi_widowxai_leader_teleop",
           "--teleop.left_arm_ip_address=192.168.1.3",
           "--teleop.right_arm_ip_address=192.168.1.2",
           "--teleop.id=bimanual_leader",
           "--display_data=false", "--play_sounds=false",
           "--dataset.repo_id=local/widowx_pick_place",
           f"--dataset.root={root}", "--dataset.push_to_hub=false",
           "--dataset.fps=30", "--dataset.rgb_encoder.vcodec=h264",
           f"--dataset.num_episodes={a.episodes}",
           f"--dataset.episode_time_s={a.seconds}", "--dataset.reset_time_s=15",
           "--dataset.single_task=Pick up the cube with the right arm and place it in the bowl."]
    print(shlex.join(cmd), flush=True)
    if not a.execute:
        print("Dry run. --execute starts both arms, including their startup/staging motion.")
        return
    import pyrealsense2 as rs
    context = rs.context()
    serials = {d.get_info(rs.camera_info.serial_number) for d in context.query_devices()}
    if not requested_serials <= serials:
        p.error(f"Requested cameras are not connected. Detected: {sorted(serials)}")
    # Keep a wall-clock session anchor outside the dataset. LeRobot frame timestamps
    # are nominal episode-relative times; this is not hardware synchronization.
    root.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"command": cmd, "start_wall_time": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
                "cameras": cameras, "root": str(root), "synthetic": False}
    manifest_path = root.with_suffix(".session.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    env = {**os.environ, "HF_HOME": str(project / "capture_tools" / "outputs" / "hf_cache")}
    result = subprocess.run(cmd, cwd=project, env=env)
    manifest.update(exit_code=result.returncode, end_wall_time=datetime.now(ZoneInfo("Asia/Seoul")).isoformat())
    manifest_path.write_text(json.dumps(manifest, indent=2))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
