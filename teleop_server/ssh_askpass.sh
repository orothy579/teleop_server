#!/bin/sh
# OpenSSH askpass for the dataset transfer: the password comes from the environment of the one ssh
# process that opens the master connection (never from a file).
printf '%s\n' "$TELEOP_SSH_PASSWORD"
