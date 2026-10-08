"""Record passive base motion only; never publish commands or connect arms."""
import argparse
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from teleop_server.mobile_capture import BaseSubscriber, BASE_NAMES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--odom-topic", default="/odom")
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("seconds must be positive")
    os.environ.setdefault("ROS_LOG_DIR", "/tmp/lerobot-ros-logs")
    tag = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S_%f")
    root = Path(__file__).parent/"outputs"/f"push_{tag}"
    root.mkdir(parents=True)
    base = BaseSubscriber(args.odom_topic, "/cmd_vel", root/"base_raw.jsonl")
    rows = []
    report = {"root": str(root), "status": "failed", "commands_sent": 0}
    try:
        deadline = time.monotonic()+5
        while base.latest is None and time.monotonic() < deadline and base.error is None:
            time.sleep(.05)
        base.sample(.25)
        print(f"RECORDING {args.seconds}s — push test; {root}", flush=True)
        start = time.monotonic()
        last = None
        while time.monotonic()-start < args.seconds:
            odom, _, _ = base.sample(.25)
            if odom['receive_ns'] != last:
                rows.append({**dict(zip(BASE_NAMES, odom['values'])),
                             "receive_monotonic_ns": odom['receive_ns'],
                             "ros_stamp_ns": odom['stamp_ns'],
                             "frame_id": odom['frame_id'], "child_frame_id": odom['child_frame_id']})
                last = odom['receive_ns']
            time.sleep(.01)
        report['status'] = 'completed'
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        base.close()
        if rows:
            pq.write_table(pa.Table.from_pylist(rows), root/'base.parquet')
            a = np.array([[r[k] for k in BASE_NAMES] for r in rows])
            yaw = np.unwrap([math.atan2(2*(r['qw']*r['qz']+r['qx']*r['qy']),
                           1-2*(r['qy']**2+r['qz']**2)) for r in rows])
            report.update(samples=len(rows), x_range_m=float(np.ptp(a[:,0])),
                          y_range_m=float(np.ptp(a[:,1])), yaw_range_rad=float(np.ptp(yaw)),
                          max_linear_speed_m_s=float(np.linalg.norm(a[:,7:10],axis=1).max()),
                          max_angular_speed_rad_s=float(np.linalg.norm(a[:,10:13],axis=1).max()),
                          observed_hz=(len(rows)-1)*1e9/(rows[-1]['receive_monotonic_ns']-rows[0]['receive_monotonic_ns']) if len(rows)>1 else None)
        (root/'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
