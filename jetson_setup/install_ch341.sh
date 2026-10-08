#!/usr/bin/env bash
# Build only the missing USB serial module against the running Jetson kernel.
set -euo pipefail
cd /home/midbar/ws/teleop_jetson
mkdir -p third_party/ch341
cd third_party/ch341
curl --fail --location --retry 3 https://raw.githubusercontent.com/torvalds/linux/v6.8/drivers/usb/serial/ch341.c -o ch341.c
echo 'obj-m += ch341.o' > Makefile
make -C /lib/modules/"$(uname -r)"/build M="$PWD" modules -j2
sudo -n install -D -m 644 ch341.ko /lib/modules/"$(uname -r)"/extra/ch341.ko
sudo -n depmod -a
sudo -n modprobe ch341
sudo -n usermod -aG dialout midbar
ls -l /dev/serial/by-id/ /dev/ttyUSB*
