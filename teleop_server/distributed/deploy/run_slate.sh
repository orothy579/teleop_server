#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source /home/midbar/ws/teleop_jetson/slate_ws/install/setup.bash
export ROS_DOMAIN_ID=42 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/teleop-jetson-ros
NODE=${SLATE_NODE:-/home/midbar/ws/teleop_jetson/slate_ws/install/interbotix_slate_driver/lib/interbotix_slate_driver/slate_base_node}
PORTS=${SLATE_PORT_PREFIX:-/dev/ttyUSB}

# The driver keeps running with a dead port when the SLATE is switched off (its USB serial chip is
# powered by the base), and never reopens it when the base comes back as a new /dev/ttyUSBn.
# Watch for that and exit non-zero: systemd (Restart=on-failure) starts a fresh driver, which retries
# every few seconds until the base is on and then finds the new port itself.
# A background job of a non-interactive shell ignores SIGINT, so stop the driver with SIGTERM
# (rclcpp shuts down cleanly on either).
"$NODE" --ros-args -p publish_tf:=true &
node=$!
trap 'kill -TERM "$node" 2>/dev/null || true' INT TERM
while kill -0 "$node" 2>/dev/null; do
  sleep 1
  for fd in /proc/"$node"/fd/*; do
    target=$(readlink "$fd" 2>/dev/null) || continue
    if [[ $target == "$PORTS"*" (deleted)" ]]; then
      echo "SLATE serial port ${target% (deleted)} disappeared (base power off or USB reset): restarting the driver" >&2
      kill -TERM "$node" 2>/dev/null || true
      for _ in 1 2 3 4 5; do kill -0 "$node" 2>/dev/null || break; sleep 1; done
      kill -KILL "$node" 2>/dev/null || true
      exit 75
    fi
  done
done
wait "$node"
