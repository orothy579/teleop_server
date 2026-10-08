"""Synthetic storage checks; no robot connections or ROS publications."""
import json
import tempfile
from pathlib import Path

import numpy as np
from lerobot.configs.video import RGBEncoderConfig
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from teleop_server.mobile_capture import base_frame, features
from teleop_server.check_mobile import validate


def main():
    now = 10**15
    odom = {"values": [0,0,0,0,0,0,1,0,0,0,0,0,0], "stamp_ns": 10**18,
            "receive_ns": now-10**7}
    assert base_frame(odom, None, now, .25)["observation.base_command_valid"][0] == 0
    cmd = {"values": [1,0,0,0,0,.2], "receive_ns": now-10**7}
    assert base_frame(odom, cmd, now, .25)["observation.base_command_valid"][0] == 1
    assert base_frame(odom, cmd, now+10**9, .25)["observation.base_command_valid"][0] == 0
    output_dir = Path(__file__).parent / "outputs"
    output_dir.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="SYNTHETIC_mobile_", dir=output_dir))
    ds = LeRobotDataset.create("local/SYNTHETIC_mobile",30,features(),root=root/"dataset",
        robot_type="synthetic_not_hardware",video_backend="pyav",rgb_encoder=RGBEncoderConfig(vcodec="h264"),
        image_writer_threads=3)
    for i in range(6):
        frame = {key: np.zeros(spec["shape"], dtype=spec["dtype"]) for key,spec in features().items()
                 if spec["dtype"] != "video"}
        frame.update(base_frame(odom, None, now, .25))
        frame["observation.sample_monotonic_ns"] = np.array([now+i*33333333,now+i*33333333+1000],dtype=np.int64)
        frame["observation.sample_unix_ns"] = np.array([10**18+i*33333333],dtype=np.int64)
        for cam in ("camera1","camera2","camera3"):
            frame[f"observation.images.{cam}"] = np.full((480,640,3), i*20, dtype=np.uint8)
        frame["task"] = "SYNTHETIC TEST; NOT FOR TRAINING"
        ds.add_frame(frame)
    ds.save_episode()
    ds.finalize()
    (root/"session.json").write_text(json.dumps({"status":"completed","frames":6,"synthetic":True}))
    (root/"base_raw.jsonl").write_text(json.dumps({"kind":"odom","synthetic":True})+"\n")
    validate(root)
    loaded = LeRobotDataset("local/SYNTHETIC_mobile",root=root/"dataset",video_backend="pyav")
    sample = loaded[3]
    assert sample["observation.leader_state"].shape == (14,)
    assert sample["observation.base_state"].shape == (13,)
    assert sample["observation.base_stamp_ns"].item() == 10**18
    assert sample["observation.sample_monotonic_ns"][0].item() == now+3*33333333
    assert sample["observation.images.camera3"].shape == (3,480,640)
    print("Synthetic mobile storage, int64 timestamps, command validity and reader: PASS")


if __name__ == "__main__":
    main()
