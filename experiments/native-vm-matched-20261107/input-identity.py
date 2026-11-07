"""Hash the consumed fixture after identical preparation, before timed work."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

work = Path('/opt/actions-runner/_work/TauCeti/TauCeti')
identity = json.loads((work / 'tested-integration.json').read_text())
assert identity['tested_tree'] == 'fdcc264d54a81b592314cf22ec2e6ff9c29f74ec'
tree = subprocess.check_output(['git', '-C', str(work / 'pr'), 'rev-parse', 'HEAD^{tree}'], text=True).strip()
assert tree == identity['tested_tree']
manifest, inodes = {}, {}
roots = [(work / name, name) for name in ('pr', 'gate', 'drivers', 'scope', 'watchdog')]
roots.append((Path('/home/runner/.elan'), '.elan'))
for root, label in roots:
    for directory, dirs, files in os.walk(root):
        for name in sorted(files + dirs):
            path = Path(directory) / name
            key = label + '/' + str(path.relative_to(root))
            if path.is_symlink():
                manifest[key] = dict(link=os.readlink(path))
            elif path.is_file():
                st = path.stat()
                inode = (st.st_dev, st.st_ino)
                if inode not in inodes:
                    with path.open('rb') as stream:
                        inodes[inode] = hashlib.file_digest(stream, 'sha256').hexdigest()
                manifest[key] = dict(bytes=st.st_size, sha256=inodes[inode])
encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
value = dict(identity=identity, tested_tree=tree, files=len(manifest), unique_files=len(inodes),
             manifest_sha256=hashlib.sha256(encoded).hexdigest(),
             bytes=sum(row.get('bytes', 0) for row in manifest.values()))
value['system_binaries'] = {}
for name in ('/usr/bin/bash', '/usr/bin/python3', '/usr/bin/bwrap',
             '/lib/x86_64-linux-gnu/libc.so.6', '/lib/x86_64-linux-gnu/libstdc++.so.6'):
    with open(name, 'rb') as stream:
        value['system_binaries'][name] = hashlib.file_digest(stream, 'sha256').hexdigest()
Path('/fleet-link/telemetry/input-identity.json').write_text(json.dumps(value, sort_keys=True))
print('MATCHED_INPUT', json.dumps(value), flush=True)
