#!/usr/bin/env bash
set -eo pipefail
# Runs from edge_runtime (only the files the edge uses); the venv stays in the full copy.
cd /home/midbar/ws/teleop_server/edge_runtime
source /opt/ros/jazzy/setup.bash
export ROS_DOMAIN_ID=42 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/teleop-jetson-ros
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
exec /home/midbar/ws/teleop_jetson/.venv/bin/python -B -m teleop_server.distributed.edge \
  --dgx http://192.168.0.104:8081 --arms teleop --rgb-codec jpeg "$@"
