"""Replay the sealed matched fixture using the unmodified supported disk backend."""
import hashlib
import importlib.util
import os
import shutil
import subprocess
import time
from pathlib import Path

repo = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('matched', repo / 'experiments/native-vm-matched-20261107/run.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
root = m.NORMAL_STATE / 'native-disk-implementation-20261107'
root.mkdir(mode=0o700, exist_ok=False)
m.ROOT, m.INPUT = root, root/'input'
m.INPUT.mkdir()
os.link(m.ARCHIVE, m.INPUT/'frozen-input.tar.gz')
for name in ('input-identity.py', 'cold-toolchain.py'):
    shutil.copyfile(m.SOURCE/name, m.INPUT/name)
m.call(['curl','--retry','3','-fLsS','https://archive.ubuntu.com/ubuntu/pool/main/b/bubblewrap/bubblewrap_0.9.0-1build1_amd64.deb','-o',m.INPUT/'bwrap.deb'])
assert m.digest(m.INPUT/'bwrap.deb') == 'dde30d1f24da50446d647ed504ec8dc5a714f171974d054c70a589b48ba38b48'
assert m.digest(m.ARCHIVE) == '2d1f9d4bda40c094f9a97fb946c81bf7f3917970adfa4aaa44b31ab8256c8dff'
before = m.CONFIG_PATH.read_bytes()
m.save(root/'inputs.json',dict(archive_sha256=m.digest(m.ARCHIVE),config_sha256=hashlib.sha256(before).hexdigest(),
    source_revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
    implementation_sha256={str(p.relative_to(repo)):m.digest(p) for p in (repo/'tauceti_fleet').glob('*.py')},
    native_image=m.native_runner.image(),started_at=time.time()))
os.environ['TAUCETI_FLEET_STATE'] = str(root/'native-state')
# Baseline identities are already sealed in the preceding evidence; no VM is launched.
(root/'results/vm-1').mkdir(parents=True)
for name in ('input-identity.json','built-modules.json'):
    shutil.copyfile(repo/'experiments/native-vm-matched-20261107/results/vm-2'/name,root/'results/vm-1'/name)
try:
    m.run('disk-supported','native')
finally:
    assert m.CONFIG_PATH.read_bytes() == before
    shutil.rmtree(m.INPUT)
    m.save(root/'final.json',dict(at=time.time(),config_unchanged=True,
           remaining_instances=[str(p) for p in (root/'vms').glob('*')]))
