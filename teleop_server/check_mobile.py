"""Validate a mobile_capture session, including all four arms and real timing."""
import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from teleop_server.check_dataset import check


def validate(root):
    root = Path(root)
    manifest = json.loads((root / "session.json").read_text())
    assert manifest["status"] == "completed", manifest
    result = check(root / "dataset", expected_cameras=3)
    data = pa.concat_tables([pq.read_table(p) for p in sorted((root/"dataset/data").rglob("*.parquet"))]).to_pandas()
    assert len(data) == manifest["frames"]
    for key, width in [("observation.leader_state", 14), ("observation.base_state", 13),
                       ("observation.base_command", 6), ("observation.base_age_ms", 2),
                       ("observation.camera_age_ms", 3)]:
        values = np.stack(data[key])
        assert values.shape == (len(data), width) and np.isfinite(values).all(), key
    times = np.stack(data["observation.sample_monotonic_ns"])
    assert (times[:, 1] >= times[:, 0]).all()
    assert (np.diff(times[:, 0]) > 0).all()
    ages = np.stack(data["observation.base_age_ms"])[:, 0]
    assert (ages >= 0).all()
    raw_counts = {"odom": 0, "cmd_vel": 0}
    with (root/"base_raw.jsonl").open() as file:
        for line in file:
            raw_counts[json.loads(line)["kind"]] += 1
    assert raw_counts["odom"] > 0
    result.update(raw_message_counts=raw_counts, max_base_receive_age_ms=float(ages.max()),
                  cmd_valid_frames=int(np.stack(data["observation.base_command_valid"]).sum()),
                  measured_sample_hz=float((len(data)-1)*1e9/(times[-1,0]-times[0,0])) if len(data)>1 else None)
    (root/"validation.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    validate(parser.parse_args().root)
