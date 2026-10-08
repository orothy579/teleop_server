"""Capture RGB from connected RealSense cameras without robot connections."""
import argparse
import json
import time
from pathlib import Path

import pyrealsense2 as rs
from PIL import Image
from lerobot.cameras.realsense.camera_realsense import RealSenseCamera
from lerobot.cameras.realsense.configuration_realsense import RealSenseCameraConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-count", type=int, default=2)
    args = parser.parse_args()
    context = rs.context()
    serials = sorted(d.get_info(rs.camera_info.serial_number) for d in context.query_devices())
    if len(serials) != args.expected_count:
        raise RuntimeError(f"Expected {args.expected_count} cameras, found {serials}")
    output = Path(__file__).parent / "outputs" / "cameras"
    output.mkdir(parents=True, exist_ok=True)
    cameras = []
    report = {s: {"samples": 0, "capture_times": []} for s in serials}
    try:
        for serial in serials:
            camera = RealSenseCamera(RealSenseCameraConfig(
                serial_number_or_name=serial, fps=30, width=640, height=480, use_depth=False))
            cameras.append(camera)
            camera.connect()
        start = time.perf_counter()
        for i in range(90):
            for serial, camera in zip(serials, cameras):
                frame = camera.read_latest()
                assert frame.shape == (480, 640, 3), frame.shape
                report[serial]["samples"] += 1
                report[serial]["capture_times"].append(camera.latest_timestamp)
                if i == 89:
                    Image.fromarray(frame).save(output / f"{serial}.png")
            time.sleep(max(0, start + (i + 1) / 30 - time.perf_counter()))
        for serial, data in report.items():
            timestamps = sorted(set(data.pop("capture_times")))
            data["distinct_host_capture_timestamps"] = len(timestamps)
            data["observed_update_hz"] = (len(timestamps) - 1) / (timestamps[-1] - timestamps[0])
            data["preview"] = str(output / f"{serial}.png")
            assert len(timestamps) >= 60, f"Too few fresh frames: {serial}"
        (output / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        for camera in cameras:
            if camera.is_connected:
                camera.disconnect()


if __name__ == "__main__":
    main()
