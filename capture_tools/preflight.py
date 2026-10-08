"""Read-only hardware inventory: never connects a robot driver or sends actions."""
import json
import platform
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

import av
import pyrealsense2 as rs
import torch


def main():
    report = {
        "checked_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "platform": platform.machine(),
        "packages": {p: version(p) for p in ("lerobot", "torch", "torchvision", "pyrealsense2", "transformers")},
        "torch_cuda": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
        "realsense_devices": [],
        "robot_motion": "not attempted",
    }
    context = rs.context()
    for dev in context.query_devices():
        report["realsense_devices"].append({
            "name": dev.get_info(rs.camera_info.name),
            "serial": dev.get_info(rs.camera_info.serial_number),
        })
    if report["cuda_available"]:
        report["gpu"] = torch.cuda.get_device_name(0)
        x = torch.ones((64, 64), device="cuda")
        report["cuda_matmul_pass"] = bool(torch.all(x @ x == 64).item())
    report["h264_encoder"] = av.codec.Codec("libx264", "w").name
    text = json.dumps(report, indent=2)
    print(text)
    out = Path(__file__).parent / "outputs"
    out.mkdir(exist_ok=True)
    (out / "preflight.json").write_text(text + "\n")


if __name__ == "__main__":
    main()
