#!/bin/bash
set -euxo pipefail
export PATH=/home/runner/.elan/bin:/usr/local/bin:/usr/bin:/bin
export LAKE_OVERRIDE_LEAN=true LEAN=/usr/local/bin/tauceti-lean TAUCETI_LEAN_WRAPPER=/usr/local/bin/tauceti-lean BASH_ENV=/opt/tauceti-lean-env
mkdir -p _actions/frozen-tools _temp TauCeti/TauCeti
export RUNNER_TEMP=/opt/actions-runner/_work/_temp GITHUB_WORKSPACE=/opt/actions-runner/_work/TauCeti/TauCeti
cd "$GITHUB_WORKSPACE"
GITHUB_RUN_ID=123456 /opt/hook-start.sh
curl --retry 3 -fLsS https://codeload.github.com/TauCetiProject/TauCetiTools/tar.gz/d0bc7f9619c0efbaca6bb88b28cb4e00ab626435 -o "$RUNNER_TEMP/tools.tar.gz"
git clone --no-checkout --branch paired-head /fleet-link/input/source.bundle pr
git -C pr checkout --detach 50604014370dfc4f4370dcb5fa0840e9b11c7177
git clone --no-checkout --branch paired-base /fleet-link/input/source.bundle base
git -C base checkout --detach afa08a3ed16fb167e35de371afb1eb03fbc5913a
mkdir gate tools
(git -C base archive HEAD) | tar -xf - -C gate
tar -xzf "$RUNNER_TEMP/tools.tar.gz" --strip-components=1 -C tools
PYTHONPATH=$PWD/tools python3 -m tauceti_tools materialize "$PWD/shared-scripts" --conf "$PWD/gate/tauceti.toml" --policy-dir "$PWD/gate/scripts"
cp -a shared-scripts/. gate/scripts/
curl --retry 3 -fLsS https://archive.ubuntu.com/ubuntu/pool/main/b/bubblewrap/bubblewrap_0.9.0-1build1_amd64.deb -o "$RUNNER_TEMP/bwrap.deb"
echo "dde30d1f24da50446d647ed504ec8dc5a714f171974d054c70a589b48ba38b48 $RUNNER_TEMP/bwrap.deb" | sha256sum -c -
sudo -n dpkg -i "$RUNNER_TEMP/bwrap.deb"
echo '3e33f8255e1411ada346db27028c25ef5ba4fa3327754a2321ea4c8732a3bc10 /usr/bin/bwrap' | sha256sum -c -
ulimit -n 65536
cd /opt/actions-runner/_work/TauCeti/TauCeti
curl --retry 3 -fLsS https://github.com/leanprover/elan/releases/download/v4.2.3/elan-x86_64-unknown-linux-gnu.tar.gz -o /tmp/elan.tar.gz
echo 'df0b2b3a439961ffcbb3985214365ffe40f49bc871df04dff268c7d8e21ca8b2 /tmp/elan.tar.gz' | sha256sum -c -
mkdir -p /tmp/elan-installer
tar -xf /tmp/elan.tar.gz -C /tmp/elan-installer
/tmp/elan-installer/elan-init -y --default-toolchain none
GITHUB_OUTPUT=/tmp/integration-output python3 gate/scripts/prepare_pr_integration.py prepare --head 50604014370dfc4f4370dcb5fa0840e9b11c7177 --base afa08a3ed16fb167e35de371afb1eb03fbc5913a --mode pr-integration --identity tested-integration.json
python3 - <<'PY'
import json
assert json.load(open('tested-integration.json'))['tested_tree']=='fdcc264d54a81b592314cf22ec2e6ff9c29f74ec'
PY
for pin in lakefile.toml lake-manifest.json lean-toolchain; do cmp "base/$pin" "pr/$pin"; done
bash gate/scripts/attest-pr-config.sh base pr "$(git -C pr rev-parse HEAD)" pull_request_target 1 0
(cd pr && lake exe cache get)
mkdir -p pr/.lake/tmp
export LAKE_CACHE_DIR=$PWD/pr/.lake/cache LAKE_ARTIFACT_CACHE=true LAKE_RESTORE_ARTIFACTS=true
export PUBLIC_ARTIFACT_ENDPOINT=https://cache.taucetiproject.org/artifacts PUBLIC_REVISION_ENDPOINT=https://cache.taucetiproject.org/revisions
bash gate/scripts/lake-cache-get.sh pr
mkdir -p scope
git -C pr diff --name-only afa08a3ed16fb167e35de371afb1eb03fbc5913a HEAD -- TauCeti/ | sed -n 's/\.lean$//p' | tr / . > scope/modules.txt
real_lean=$(cd pr && elan which lean)
bash gate/scripts/perf/prepare-watchdog-toolchain.sh "$PWD/watchdog" "$real_lean"
bash gate/scripts/build-lint-driver.sh pr gate/scripts/LintEnvDriver.lean "$PWD/drivers"
bash gate/scripts/build-audit-driver.sh pr gate/scripts "$PWD/drivers"
/usr/local/bin/tauceti-lean --install-watchdog "$PWD/watchdog/bin/lean-real"
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
