#!/bin/bash
set -euxo pipefail
findmnt -n -o FSTYPE -T "$PWD" | grep -x tmpfs
test "$(id -u)" = 1000
sudo -n id -u | grep -x 0
sudo -n dpkg --audit
mkdir -p _actions/offline _temp TauCeti/TauCeti
# Hard links must share pages, scratch must use the same private quota.
dd if=/dev/zero of=_temp/population bs=1M count=64 status=none
ln _temp/population _actions/offline/shared
test "$(stat -c %i _temp/population)" = "$(stat -c %i _actions/offline/shared)"
env -i /usr/bin/bwrap --ro-bind /usr /usr --ro-bind /etc /etc \
  --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --proc /proc --dev /dev --tmpfs /tmp --unshare-user --unshare-net --unshare-pid \
  --die-with-parent -- /bin/bash -c 'test ! -e /fleet-link; test ! -e /home/kim; echo offline-boundary-ok'
GITHUB_RUN_ID=12345 /opt/hook-start.sh
GITHUB_RUN_ID=12345 /opt/hook-complete.sh

# Namespace root must not be able to expose the service's capped ancestor.
test ! -f /sys/fs/cgroup/cgroup.procs
sudo -n unshare --cgroup --mount sh -euxc '
 mkdir /tmp/cgroup-probe
 mount -t cgroup2 none /tmp/cgroup-probe
 test "$(cat /tmp/cgroup-probe/cgroup.procs | wc -l)" -ge 1
 test ! -e /tmp/cgroup-probe/memory.max || test "$(cat /tmp/cgroup-probe/memory.max)" != 57982058496
 umount /tmp/cgroup-probe
'

# A receiver restart must not kill the runner or reuse an old permit nonce.
sudo -n python3 - <<'PYRESTART'
from pathlib import Path
import os,signal,time
sock=Path('/usr/local/share/tauceti/admission.sock')
def receivers():
    result=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            if p.joinpath('cmdline').read_bytes().split(b'\0')[-2:]==[b'pressure',b'']:result.append(int(p.name))
        except OSError:pass
    return result
pid=receivers()[0]
os.kill(pid,signal.SIGKILL)
deadline=time.monotonic()+10
while time.monotonic()<deadline:
    if any(p!=pid for p in receivers()) and sock.exists():break
    time.sleep(.1)
else:raise RuntimeError('pressure receiver did not restart')
PYRESTART

# Exercise the actual host discovery/stream, not just a locally bound socket.
sudo -n python3 - <<'PYFRAME'
from pathlib import Path
import json,time
p=Path('/usr/local/share/tauceti/host-pressure.json')
deadline=time.monotonic()+10
while time.monotonic()<deadline:
    if p.exists():
        v=json.loads(p.read_text())
        if v.get('version')==1 and 0<=time.time()-v['at']<5:break
    time.sleep(.1)
else:raise RuntimeError('no fresh frame from the normal host pressure service')
view=Path('/usr/local/share/tauceti/native-memory')
assert int(view.joinpath('memory.max').read_text())==54*1024**3
print('existing pressure stream and absolute cgroup guard are present')
PYFRAME
