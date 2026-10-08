#!/usr/bin/env bash
# Install teleop_server + SLATE driver as always-on user services (no sudo needed).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.config/systemd/user
install -m 644 "$here/teleop-slate.service" ~/.config/systemd/user/teleop-slate.service
install -m 644 "$here/teleop-server.service" ~/.config/systemd/user/teleop-server.service
systemctl --user daemon-reload
systemctl --user enable teleop-slate.service teleop-server.service
# Start at boot even without a login session (needs polkit permission or sudo).
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  loginctl enable-linger "$USER" 2>/dev/null \
    || echo "NOTE: run 'sudo loginctl enable-linger $USER' once so the services start at boot without login."
fi
echo "Start now with: systemctl --user start teleop-slate teleop-server"
