#!/usr/bin/env bash
# Run on Jetson. Installs dependencies only; does not start hardware control.
set -euo pipefail
cd /home/midbar/ws/teleop_jetson
mkdir -p artifacts third_party/ch341
if ! test -f /etc/apt/sources.list.d/ros2.sources && ! dpkg-query -W ros2-apt-source >/dev/null 2>&1; then
  python3 - <<'PY'
import json, urllib.request
release = json.load(urllib.request.urlopen('https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest'))
asset = next(a for a in release['assets'] if a['name'].endswith('.noble_all.deb') and a['name'].startswith('ros2-apt-source_'))
urllib.request.urlretrieve(asset['browser_download_url'], '/tmp/teleop-ros2-apt-source.deb')
print(asset['name'])
PY
  sudo -n dpkg -i /tmp/teleop-ros2-apt-source.deb
fi
sudo -n apt-get update
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ros-jazzy-ros-base ros-jazzy-tf2-geometry-msgs ros-jazzy-tf2-ros \
  python3-colcon-common-extensions python3-venv libudev-dev libusb-1.0-0-dev \
  build-essential cmake pkg-config ffmpeg libssl-dev
python3 -m venv --system-site-packages .venv
.venv/bin/pip install 'aiohttp==3.13.2' 'pyrealsense2==2.57.7.10387' 'trossen_arm==1.11.0' 'lz4==4.4.5'
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu 'torch==2.10.0' 'torchvision==0.25.0'
.venv/bin/pip install 'lerobot==0.6.0'
.venv/bin/pip install --no-deps ./packages/lerobot_robot_trossen ./packages/lerobot_teleoperator_trossen
.venv/bin/pip freeze > artifacts/jetson-pip-freeze.txt
sudo -n usermod -aG dialout,video midbar
echo 'Dependencies installed; hardware services have not been started.'
