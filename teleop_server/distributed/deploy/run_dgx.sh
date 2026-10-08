#!/usr/bin/env bash
set -eo pipefail
cd /home/midbar/ws/teleop_server
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
export HF_HOME=/home/midbar/ws/teleop_server/.cache/huggingface
exec /home/midbar/ws/lerobot_trossen/.venv/bin/python -B -m teleop_server.distributed.server "$@"
