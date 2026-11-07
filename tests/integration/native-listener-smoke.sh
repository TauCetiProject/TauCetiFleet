#!/bin/bash
# Exercise the real .NET runtime under the native unit's production limits.
# The offline harness runs this as the runner user without GitHub registration.
set -euo pipefail
cd /opt/actions-runner
version=$(./bin/Runner.Listener --version)
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
printf 'Stock Actions listener started: %s\n' "$version"
