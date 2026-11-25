#!/bin/bash
# The leased shared tree is read-only even to the guest's root, and an overlay over it keeps
# every write in the job's own upper layer.
set -euxo pipefail
tree=/opt/tauceti-shared-tree
test -f "$tree/ready.json"
findmnt -no OPTIONS "$tree" | grep -qw ro
! touch "$tree/lake/probe" 2>/dev/null
! sudo -n touch "$tree/lake/probe" 2>/dev/null
olean=$(cd "$tree/lake" && find build/lib/lean -name '*.olean' -print -quit)
before=$(sha256sum < "$tree/lake/$olean")
upper=/opt/actions-runner/_work/_temp/shared-upper
mkdir -p "$upper/u" "$upper/w" /opt/actions-runner/_work/TauCeti/TauCeti/pr/.lake
sudo -n mount -t overlay overlay -o "userxattr,lowerdir=$tree/lake,upperdir=$upper/u,workdir=$upper/w" /opt/actions-runner/_work/TauCeti/TauCeti/pr/.lake
merged=/opt/actions-runner/_work/TauCeti/TauCeti/pr/.lake
chmod u+w "$merged/$olean"
echo poisoned > "$merged/$olean"
test "$(cat "$merged/$olean")" = poisoned
test "$(sha256sum < "$tree/lake/$olean")" = "$before"
test -f "$upper/u/$olean"
rm "$merged/$(dirname "$olean")"/*.trace
test -n "$(cd "$tree/lake" && find build/lib/lean -name '*.trace' -print -quit)"
echo SHARED_TREE_OK
