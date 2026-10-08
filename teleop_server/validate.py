"""Validate a teleop_server recording session.

Runs teleop_server/check_mobile.py (same checks as mobile_capture.py sessions), then checks what
teleop_server adds: depth paired with every RGB frame (LeRobot depth videos, or raw uint16 PNGs in
depth_raw/ for Jetson recordings), RGB/depth timing and depth value ranges.
Usage:  python -m teleop_server.validate dataset/YYYYMMDD_HHMMSS
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from teleop_server.recorder import MAX_DEPTH_DT_MS, RAW_DEPTH_DIR, collector_modules


def validate(root):
    root = Path(root)
    _, check_mobile = collector_modules()
    result = check_mobile.validate(root)  # asserts; includes full RGB (and depth) video decoding
    manifest = json.loads((root / "session.json").read_text())
    info = json.loads((root / "dataset/meta/info.json").read_text())
    data = pa.concat_tables([pq.read_table(p) for p in sorted((root / "dataset/data").rglob("*.parquet"))]).to_pandas()
    n = len(data)
    cams = sorted(k.removeprefix("observation.images.") for k, f in info["features"].items()
                  if k.startswith("observation.images.") and not (f.get("info") or {}).get("is_depth_map"))
    numbers = np.stack(data["observation.camera_frame_number"])
    assert numbers.shape == (n, len(cams)), numbers.shape
    assert (np.diff(numbers, axis=0) >= 0).all(), "camera frame numbers went backwards"
    result["camera_frames_skipped"] = [int(x) for x in (np.diff(numbers, axis=0) - 1).clip(min=0).sum(axis=0)]

    depth = manifest.get("depth", {})
    if depth.get("enabled") and depth.get("storage") == "png16":
        validate_raw_depth(root, depth, cams, data, result)
    elif depth.get("enabled"):
        depth_keys = [f"observation.images.{c}_depth" for c in cams]
        for key in depth_keys:
            ft = info["features"][key]
            assert (ft.get("info") or {}).get("is_depth_map"), key
            assert result["depth_frames"][key] == n, (key, result["depth_frames"][key], n)
        dt = np.stack(data["observation.camera_depth_dt_ms"])
        assert dt.shape == (n, len(cams)) and np.isfinite(dt).all()
        result["max_rgb_depth_dt_ms"] = [round(float(x), 3) for x in np.abs(dt).max(axis=0)]
        assert max(result["max_rgb_depth_dt_ms"]) < MAX_DEPTH_DT_MS, f"RGB/depth not paired: {result['max_rgb_depth_dt_ms']}"
        # Decode depth back to metres through LeRobot itself (first / middle / last frame).
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        ds = LeRobotDataset(info.get("repo_id", "local/mobile_paper_bag"), root=root / "dataset",
                            video_backend="pyav", depth_output_unit="m")
        lo, hi = depth["depth_min_m"], depth["depth_max_m"]
        valid = {}
        for i in sorted({0, n // 2, n - 1}):
            item = ds[i]
            for c, key in zip(cams, depth_keys):
                d = item[key].numpy().squeeze()
                assert d.shape == (480, 640) and np.isfinite(d).all(), (key, d.shape)
                assert d.min() >= lo * 0.999 and d.max() <= hi * 1.001, (key, float(d.min()), float(d.max()))
                assert item[f"observation.images.{c}"].shape[-2:] == d.shape, "RGB/depth size differ"
                valid.setdefault(c, []).append(round(float((d > lo * 1.001).mean()), 3))
        result["depth_valid_fraction"] = valid
    (root / "validation.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return result


def validate_raw_depth(root, depth, cams, data, result):
    import cv2
    n = len(data)
    dt = np.stack(data["observation.camera_depth_dt_ms"])
    assert dt.shape == (n, len(cams)) and np.isfinite(dt).all()
    result["max_rgb_depth_dt_ms"] = [round(float(x), 3) for x in np.abs(dt).max(axis=0)]
    assert max(result["max_rgb_depth_dt_ms"]) < MAX_DEPTH_DT_MS, f"RGB/depth not paired: {result['max_rgb_depth_dt_ms']}"
    expected = [f"{i:06d}.png" for i in range(n)]
    result["depth_frames"], result["depth_valid_fraction"] = {}, {}
    for c in cams:
        files = sorted(p.name for p in (root / RAW_DEPTH_DIR / c).iterdir())
        assert files == expected, (c, len(files), n)
        result["depth_frames"][c] = len(files)
        calib = depth["cameras"][c]
        assert 0 < calib["depth_scale_m"] < 1, calib
        intr = calib.get("depth_intrinsics")
        valid = []
        for i in sorted({0, n // 2, n - 1}):
            d = cv2.imread(str(root / RAW_DEPTH_DIR / c / expected[i]), cv2.IMREAD_UNCHANGED)
            assert d is not None and d.dtype == np.uint16 and d.ndim == 2, (c, i)
            if intr:
                assert d.shape == (intr["height"], intr["width"]), (c, d.shape, intr)
            valid.append(round(float((d > 0).mean()), 3))
        result["depth_valid_fraction"][c] = valid


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path)
    validate(parser.parse_args().root)
