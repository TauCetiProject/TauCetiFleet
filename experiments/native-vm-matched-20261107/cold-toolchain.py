"""Discard only this disposable instance's disk-backed toolchain cache."""
import os
from pathlib import Path

seen = set()
for path in Path('/home/runner/.elan').rglob('*'):
    if path.is_file() and not path.is_symlink():
        st = path.stat()
        inode = st.st_dev, st.st_ino
        if inode in seen:
            continue
        seen.add(inode)
        with path.open('rb') as stream:
            os.fdatasync(stream.fileno())
            os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
print('TOOLCHAIN_FILES_DISCARDED', len(seen), flush=True)
