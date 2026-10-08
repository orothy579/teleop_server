# Four arms + SLATE + three cameras

Verified on 2026-10-06: `outputs/mobile_20261006_181559_050637` contains a real
10-second session with 300 frames and three 300-frame videos. Full video decoding and
LeRobot first/middle/last reloading passed. Measured sample rate 29.92 Hz, maximum
base receive age 49.49 ms. Raw log contains 764 odometry messages over the broader
connect/capture/disconnect interval and zero cmd_vel messages. The base pose/twist
was constant during recording: mobile movement is not yet validated. The ROS base
driver was left running after verification; do not start another copy.

The earlier `mobile_20261006_181337_933929` attempt acquired samples but failed to
persist the LeRobot episode due to a truthiness bug, now fixed. That session is explicitly
marked failed and must not be used for training. Its raw base log is retained.

The collector `mobile_capture.py` records one local LeRobot episode per invocation.
Follower arms follow the leaders; the base is **subscription-only**. Drive the base through
your existing teleoperation system if movement is required. This program does not enable
base torque or publish velocity commands. Do not run the legacy direct-serial MobileAI
driver and the ROS SLATE driver against the same USB port simultaneously.

## Start the SLATE ROS driver (terminal A)

```bash
source /opt/ros/jazzy/setup.bash
source /home/midbar/interbotix_ws/install/setup.bash
export ROS_LOG_DIR=/tmp/lerobot-ros-logs
ros2 run interbotix_slate_driver slate_base_node --ros-args -p publish_tf:=true
```

Run only one base driver. The USB device detected on 2026-10-06 is
`/dev/serial/by-id/usb-1a86_usb2.0_serial_chassis-if00-port0` → `/dev/ttyUSB0`.
The driver reports SLATE v1.0.0. Its default odometry update rate is 20 Hz.

## Collect (terminal B)

```bash
cd /home/midbar/ws/teleop_server
source /opt/ros/jazzy/setup.bash
source /home/midbar/interbotix_ws/install/setup.bash
export ROS_LOG_DIR=/tmp/lerobot-ros-logs
export UV_CACHE_DIR=/tmp/lerobot-uv-cache
export HF_HOME=/home/midbar/ws/teleop_server/capture_tools/outputs/hf_cache

# Probe base only: no arm connection or movement.
/home/midbar/ws/lerobot_trossen/.venv/bin/python -m teleop_server.mobile_capture

# Actual capture: arms move on connect/disconnect; stop other arm controllers first.
/home/midbar/ws/lerobot_trossen/.venv/bin/python -m teleop_server.mobile_capture --seconds 20 --execute

# Replace SESSION with the printed session directory.
/home/midbar/ws/lerobot_trossen/.venv/bin/python -m teleop_server.check_mobile capture_tools/outputs/SESSION
```

Defaults: follower left/right 192.168.1.5 / .4; leader left/right .3 / .2.
Camera1 / camera2 / camera3: 260322273312 / 260522278645 / 260322272677.
RGB 640×480 at 30 FPS; no depth. Set `--serials SERIAL1 SERIAL2 SERIAL3` to override.
`--odom-topic` defaults to `/odom` (`nav_msgs/msg/Odometry`).
`--cmd-topic` defaults to `/cmd_vel` (`geometry_msgs/msg/Twist`; TwistStamped is not supported).
The default task is `Pick up the paper bag and place it in the marked target area.`
Override `--task` to describe the actual demonstration.

## Files and signals

| File/key | Meaning |
|---|---|
| `dataset/data/**/*.parquet`: `observation.state` | 14 measured follower positions, left 7 then right 7 |
| `observation.leader_state` | 14 measured leader positions used to form the command |
| `action` | 14 follower position targets returned by send_action |
| `observation.base_state` | x,y,z (m), quaternion qx,qy,qz,qw, linear vx,vy,vz (m/s), angular wx,wy,wz (rad/s) |
| `observation.base_command` | Latest observed cmd_vel: vx,vy,vz,wx,wy,wz; not a motor feedback measurement |
| `observation.base_command_valid` | 1 only if a command was received recently; 0 means the zero vector is a placeholder, NOT an observed stop command |
| `observation.base_age_ms` | Host receive ages for odom/command; -1 for a command never received |
| `observation.base_stamp_ns` | Original odometry ROS header timestamp as int64 |
| `observation.sample_monotonic_ns` | Host begin/end of arm observation→leader read→action send, int64 |
| `observation.sample_unix_ns` | Host wall-clock start, int64 |
| `observation.camera_age_ms` | Approximate age of latest camera buffers when sampled; not exact exposure timestamps |
| `dataset/videos/observation.images.camera{1,2,3}/...mp4` | Three RGB streams |
| `base_raw.jsonl` | All received odom/cmd messages, including covariance, frame IDs, source timestamp and host receipt clocks |
| `session.json` | Configuration, status, frame count, clocks and cleanup errors |
| `validation.json` | Full video decoding, numeric/timing checks and observed capture rate |

Arm data here means **positions**, not velocity/torque/external effort. Rotary joints are
radians and gripper carriage positions metres. Raw motor packet timestamps are not available
in this collector. ROS frame_id/child_frame_id are preserved and must remain constant within a session.

## Timing and failure handling

This is **host-timestamped latest-sample alignment**, not hardware synchronization.
20 Hz odometry is sampled into the nominal 30 Hz recording loop, so repeated base values
are expected. LeRobot `timestamp` is nominal episode time; use the host monotonic clocks
to measure actual sampling intervals. Camera buffer age is approximate because their
background capture threads run independently. Raw ROS messages permit later re-alignment.
The same ROS_DOMAIN_ID must be used by driver and collector. A fresh receipt does not prove
a remote publisher's underlying sensor data is fresh; source timestamps should also be reviewed.

No fresh odometry → abort BEFORE connecting arms. Camera streams are opened before the arms.
Odometry receipt older than `--max-base-age 0.25` seconds or ROS subscription errors during capture
→ stop the loop and mark session failed. Missing cmd_vel does not block odometry collection.
The collector attempts independent driver cleanup on all arms after a fault, without staged moves.
On normal completion it uses the existing staged/sleep disconnect motion.
It attempts to save accumulated frames after a Python exception, marks them failed, and never
treats them as successful demonstrations. Native process aborts or power loss may still lose the
in-memory episode. This cleanup is not a replacement for a hardware emergency stop.

## Training

The follower `observation.state` and `action` remain 14-dimensional to preserve the existing
arm-policy interface. Leaders, base and timing are separate features for logging and later
processing. **Pi0.5 will not automatically learn base control from these auxiliary fields.**
For mobile-manipulation training, explicitly select base inputs, include actual base action
targets, exclude/mask samples with invalid commands, and update the policy processors.
For stationary paper-bag manipulation, retain the arm policy and keep base data as a log.

## Offline smoke test

```bash
/home/midbar/ws/lerobot_trossen/.venv/bin/python -m capture_tools.smoke_mobile
```

Synthetic output is prefixed `SYNTHETIC_mobile_`; do not train on it.
