"""Synthetic storage smoke test. This is NOT robot demonstration data."""
import tempfile
from pathlib import Path

import numpy as np
from lerobot.configs.video import RGBEncoderConfig
from lerobot.datasets.lerobot_dataset import LeRobotDataset

from teleop_server.check_dataset import check


def main():
    parent = Path(__file__).parent / "outputs"
    parent.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="SYNTHETIC_", dir=parent)) / "dataset"
    names = [f"{arm}_{joint}.pos" for arm in ("left", "right")
             for joint in [*(f"joint_{i}" for i in range(6)), "left_carriage_joint"]]
    features = {key: {"dtype": "float32", "shape": (14,), "names": names}
                for key in ("observation.state", "action")}
    for camera in ("camera1", "camera2"):
        features[f"observation.images.{camera}"] = {
            "dtype": "video", "shape": (480, 640, 3), "names": ["height", "width", "channels"]}
    ds = LeRobotDataset.create("local/SYNTHETIC_storage_test", 30, features, root=root,
                              robot_type="synthetic_not_hardware", video_backend="pyav",
                              rgb_encoder=RGBEncoderConfig(vcodec="h264"), image_writer_threads=2)
    for i in range(60):
        frame = {"observation.state": np.full(14, i / 100, dtype=np.float32),
                 "action": np.full(14, (i + 1) / 100, dtype=np.float32),
                 "task": "SYNTHETIC STORAGE TEST - NOT FOR TRAINING"}
        for c, camera in enumerate(("camera1", "camera2")):
            frame[f"observation.images.{camera}"] = np.full((480, 640, 3), (i * 3 + c * 20) % 255, dtype=np.uint8)
        ds.add_frame(frame)
    ds.save_episode()
    ds.finalize()
    check(root)
    reloaded = LeRobotDataset("local/SYNTHETIC_storage_test", root=root, video_backend="pyav")
    assert len(reloaded) == 60
    for index in (0, 29, 59):
        sample = reloaded[index]
        assert tuple(sample["observation.images.camera1"].shape) == (3, 480, 640)
        assert tuple(sample["observation.state"].shape) == (14,)
    print("LeRobot dataset reader: PASS (synthetic only)")


if __name__ == "__main__":
    main()
