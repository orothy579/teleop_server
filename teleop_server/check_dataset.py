"""Check local 14-state/14-action datasets, including complete video decoding."""
import argparse
import json
from pathlib import Path

import av
import numpy as np
import pyarrow.parquet as pq


def check(root, expected_cameras=None):
    root = Path(root)
    info = json.loads((root / "meta/info.json").read_text())
    files = sorted((root / "data").rglob("*.parquet"))
    assert files, "No data parquet files"
    import pyarrow as pa
    df = pa.concat_tables([pq.read_table(f) for f in files]).to_pandas()
    assert len(df) == info["total_frames"] > 0
    assert df.episode_index.nunique() == info["total_episodes"]
    for key in ("observation.state", "action"):
        values = np.stack(df[key])
        assert values.shape == (len(df), 14), (key, values.shape)
        assert np.isfinite(values).all(), f"Nonfinite {key}"
        names = info["features"][key]["names"]
        assert sum(n.startswith("left_") for n in names) == 7, names
        assert sum(n.startswith("right_") for n in names) == 7, names
    for _, episode in df.groupby("episode_index"):
        assert np.array_equal(episode.frame_index, np.arange(len(episode)))
        assert np.allclose(np.diff(episode.timestamp), 1 / info["fps"], atol=1e-4)
    counts = {}
    image_keys = sorted(key for key in info["features"] if key.startswith("observation.images."))
    is_depth = lambda key: bool((info["features"][key].get("info") or {}).get("is_depth_map"))
    camera_keys = [key for key in image_keys if not is_depth(key)]  # RGB
    depth_keys = [key for key in image_keys if is_depth(key)]      # e.g. teleop_server recordings
    assert camera_keys, "No camera features"
    if expected_cameras is not None:
        assert len(camera_keys) == expected_cameras, (camera_keys, expected_cameras)
    for key in camera_keys:
        assert info["features"][key]["dtype"] == "video"
        videos = sorted((root / "videos" / key).rglob("*.mp4"))
        assert videos, f"Missing videos: {key}"
        count = 0
        for path in videos:
            with av.open(str(path)) as container:
                for frame in container.decode(video=0):
                    assert [frame.height, frame.width, 3] == info["features"][key]["shape"]
                    count += 1
        assert count == len(df), (key, count, len(df))
        counts[key] = count
    depth_counts = {}
    for key in depth_keys:  # 1-channel depth videos: full decode, same frame count as the data
        assert info["features"][key]["dtype"] == "video"
        videos = sorted((root / "videos" / key).rglob("*.mp4"))
        assert videos, f"Missing videos: {key}"
        count = 0
        for path in videos:
            with av.open(str(path)) as container:
                for frame in container.decode(video=0):
                    assert [frame.height, frame.width, 1] == info["features"][key]["shape"]
                    count += 1
        assert count == len(df), (key, count, len(df))
        depth_counts[key] = count
    result = {"root": str(root), "frames": len(df), "episodes": info["total_episodes"],
              "video_frames": counts, **({"depth_frames": depth_counts} if depth_counts else {}),
              "storage_checks": "PASS",
              "limits": "Does not prove physical timing synchronization, scene quality, or robot tracking."}
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--expected-cameras", type=int)
    args = parser.parse_args()
    check(args.root, args.expected_cameras)
