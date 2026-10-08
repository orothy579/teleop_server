#!/usr/bin/env bash
# Rebuild edge_runtime from this Git checkout on the Jetson, with only the files in
# jetson_runtime_files.txt. Restart teleop-jetson after rebuilding.
set -euo pipefail
FULL=/home/midbar/ws/teleop_server
RUNTIME=$FULL/edge_runtime
LIST=$FULL/teleop_server/distributed/deploy/jetson_runtime_files.txt
NEW=$(mktemp -d "$FULL/.edge_runtime.XXXXXX")
grep -vE '^\s*(#|$)' "$LIST" | while read -r path; do
  install -D -m "$(stat -c %a "$FULL/$path")" "$FULL/$path" "$NEW/$path"
done
rm -rf "$RUNTIME.old"
[ -d "$RUNTIME" ] && mv "$RUNTIME" "$RUNTIME.old"
mv "$NEW" "$RUNTIME"
rm -rf "$RUNTIME.old"
echo "edge_runtime: $(find "$RUNTIME" -type f | wc -l) files"
