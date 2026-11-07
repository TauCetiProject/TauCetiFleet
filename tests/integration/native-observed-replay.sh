#!/bin/bash
set -euo pipefail
python3 /fleet-link/input/native-observe.py &
observer=$!
trap 'kill "$observer" 2>/dev/null || true; wait "$observer" || true' EXIT
source /fleet-link/input/native-frozen-replay.sh
