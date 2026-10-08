#!/usr/bin/env bash
# SLATE base ROS driver for teleop_server (started by teleop-slate.service). Publishes /odom,
# subscribes /cmd_vel. Exactly one copy may own /dev/ttyUSB0.
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source /home/midbar/interbotix_ws/install/setup.bash
export ROS_LOG_DIR=/tmp/lerobot-ros-logs
exec /home/midbar/interbotix_ws/install/interbotix_slate_driver/lib/interbotix_slate_driver/slate_base_node \
  --ros-args -p publish_tf:=true
