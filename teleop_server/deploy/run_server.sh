#!/usr/bin/env bash
# teleop_server server (started by teleop-server.service). Arms connect only from the browser button.
set -eo pipefail
cd /home/midbar/ws/teleop_server
source /opt/ros/jazzy/setup.bash
export ROS_LOG_DIR=/tmp/lerobot-ros-logs
export HF_HOME=/home/midbar/ws/teleop_server/.cache/huggingface  # cache only; recordings go to dataset/
export PYTHONUNBUFFERED=1
exec /home/midbar/ws/lerobot_trossen/.venv/bin/python -m teleop_server.server --arms teleop "$@"
