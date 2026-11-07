#!/bin/bash
set -euxo pipefail
# The archived snapshot comes from the frozen cold preparation, never production.
sudo -n tar -xzf /fleet-link/input/frozen-input.tar.gz -C / --transform='s@^_work@opt/actions-runner/_work@;s@^\.elan@home/runner/.elan@'
export PATH=/home/runner/.elan/bin:/usr/local/bin:/usr/bin:/bin
export LAKE_OVERRIDE_LEAN=true LEAN=/usr/local/bin/tauceti-lean TAUCETI_LEAN_WRAPPER=/usr/local/bin/tauceti-lean BASH_ENV=/opt/tauceti-lean-env
export RUNNER_TEMP=/opt/actions-runner/_work/_temp GITHUB_WORKSPACE=/opt/actions-runner/_work/TauCeti/TauCeti
cd "$GITHUB_WORKSPACE"
GITHUB_RUN_ID=123456 /opt/hook-start.sh
python3 - <<'IDENTITY'
import json
assert json.load(open('tested-integration.json'))['tested_tree']=='fdcc264d54a81b592314cf22ec2e6ff9c29f74ec'
IDENTITY
curl --retry 3 -fLsS https://archive.ubuntu.com/ubuntu/pool/main/b/bubblewrap/bubblewrap_0.9.0-1build1_amd64.deb -o "$RUNNER_TEMP/bwrap.deb"
echo "dde30d1f24da50446d647ed504ec8dc5a714f171974d054c70a589b48ba38b48 $RUNNER_TEMP/bwrap.deb" | sha256sum -c -
sudo -n dpkg -i "$RUNNER_TEMP/bwrap.deb"
echo '3e33f8255e1411ada346db27028c25ef5ba4fa3327754a2321ea4c8732a3bc10 /usr/bin/bwrap' | sha256sum -c -
cp /usr/local/bin/tauceti-lean "$PWD/watchdog/bin/lean"
export WATCHDOG_TOOLCHAIN=$PWD/watchdog LINT_DRIVER_DIR=$PWD/drivers LINT_SCOPE_DIR=$PWD/scope
export LINT_ONLY_MODULES=$PWD/scope/modules.txt AXIOMS_ONLY_MODULES=$PWD/scope/modules.txt
export TAUCETI_PROJECT_ROOT=$PWD/pr TAUCETI_TRUSTED_SCRIPTS=$PWD/gate/scripts
export LAKE_ARTIFACT_CACHE=true LAKE_RESTORE_ARTIFACTS=true LAKE_CACHE_DIR=$PWD/pr/.lake/cache LAKE_NO_CACHE=true
python3 gate/scripts/ci_host_resources.py --out "$RUNNER_TEMP/ci-host-resources.json" -- \
 env -i /usr/bin/bwrap --tmpfs / \
 --ro-bind /usr /usr --ro-bind /etc /etc \
 --symlink usr/bin /bin --symlink usr/sbin /sbin --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
 --dev /dev --proc /proc --tmpfs /tmp \
 --ro-bind "$HOME/.elan" "$HOME/.elan" \
 --ro-bind "$WATCHDOG_TOOLCHAIN" "$WATCHDOG_TOOLCHAIN" \
 --ro-bind "$LINT_DRIVER_DIR" "$LINT_DRIVER_DIR" --ro-bind "$LINT_SCOPE_DIR" "$LINT_SCOPE_DIR" \
 --ro-bind "$PWD/gate" "$PWD/gate" \
 --ro-bind "$PWD/pr-raw-head/.git/objects" "$PWD/pr-raw-head/.git/objects" \
 --ro-bind "$PWD/base/.git/objects" "$PWD/base/.git/objects" \
 --ro-bind "$PWD/pr" "$PWD/pr" --bind "$PWD/pr/.lake" "$PWD/pr/.lake" \
 --remount-ro / --setenv PATH "$HOME/.elan/bin:/usr/bin:/bin" --setenv HOME "$HOME" --setenv CI true \
 --setenv LAKE_NO_CACHE true --setenv LAKE_ARTIFACT_CACHE true --setenv LAKE_RESTORE_ARTIFACTS true \
 --setenv LAKE_CACHE_DIR "$LAKE_CACHE_DIR" --setenv WATCHDOG_TOOLCHAIN "$WATCHDOG_TOOLCHAIN" \
 --setenv LINT_DRIVER_EXE "$LINT_DRIVER_DIR/lint-env-driver" --setenv LINT_ONLY_MODULES "$LINT_ONLY_MODULES" \
 --setenv AXIOMS_ONLY_MODULES "$AXIOMS_ONLY_MODULES" --setenv STAGE_LAKE_CACHE '' --setenv IS_BATCH false \
 --setenv TAUCETI_PROJECT_ROOT "$TAUCETI_PROJECT_ROOT" --setenv TAUCETI_TRUSTED_SCRIPTS "$TAUCETI_TRUSTED_SCRIPTS" \
 --chdir "$PWD/pr" --unshare-user --unshare-ipc --unshare-pid --unshare-net --unshare-uts \
 --unshare-cgroup --disable-userns --die-with-parent --new-session \
 -- bash -euxo pipefail -c 'python3 ../gate/scripts/bulk-unpack-cache.py; exec bash ../gate/scripts/sandbox-build.sh'
GITHUB_RUN_ID=123456 /opt/hook-complete.sh
cp "$RUNNER_TEMP/ci-host-resources.json" /fleet-link/telemetry/ci-host-resources.json
