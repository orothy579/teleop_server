#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source /home/midbar/ws/teleop_jetson/slate_ws/install/setup.bash
export ROS_DOMAIN_ID=42 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/teleop-jetson-ros
exec /home/midbar/ws/teleop_jetson/slate_ws/install/interbotix_slate_driver/lib/interbotix_slate_driver/slate_base_node --ros-args -p publish_tf:=true
