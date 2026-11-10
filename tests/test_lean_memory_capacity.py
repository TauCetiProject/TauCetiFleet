import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import hostos, lean_pressure

hostos.MACOS = False

GIB=1024**3


class MemoryCapacity(unittest.TestCase):
    def test_soft_reclaim_threshold_does_not_hide_available_host_ram(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);proc=root/'proc';(proc/'self').mkdir(parents=True)
            (proc/'meminfo').write_text('MemTotal: 131072000 kB\nMemAvailable: 36700160 kB\n')
            (proc/'self/cgroup').write_text('0::/user/workers\n')
            cg=root/'sys/fs/cgroup';(cg/'user/workers').mkdir(parents=True)
            for name,limit,used in (('',112,80),('user',112,80),('user/workers',112,12)):
                group=cg/name
                (group/'memory.max').write_text(str(limit*GIB))
                (group/'memory.current').write_text(str(used*GIB))
                (group/'memory.stat').write_text('file 0\nfile_dirty 0\nfile_writeback 0\n')
            (cg/'user/workers/memory.high').write_text(str(19*GIB))
            with patch.object(lean_pressure,'Path',side_effect=lambda p:root/str(p).lstrip('/')):
                total,available=lean_pressure.memory()
            self.assertEqual((total,available),(112*GIB,32*GIB))
            self.assertGreater(available,total*(1-lean_pressure.memory_limit(total)/100))
            # A real hard cap remains authoritative even with idle physical RAM.
            (cg/'user/workers/memory.max').write_text(str(14*GIB))
            with patch.object(lean_pressure,'Path',side_effect=lambda p:root/str(p).lstrip('/')):
                self.assertEqual(lean_pressure.memory(),(14*GIB,2*GIB))

    def test_native_readonly_counter_view_preserves_the_absolute_cgroup_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / 'proc/self'
            proc.mkdir(parents=True)
            (proc / 'cgroup').write_text('0::/\n')
            (root / 'proc/meminfo').write_text('MemTotal: 131072000 kB\nMemAvailable: 36700160 kB\n')
            view = root / 'usr/local/share/tauceti/native-memory'
            view.mkdir(parents=True)
            (view / 'memory.max').write_text(str(54*GIB))
            (view / 'memory.current').write_text(str(48*GIB))
            (view / 'memory.stat').write_text(f'file {8*GIB}\nshmem {6*GIB}\nfile_dirty {GIB}\nfile_writeback 0\n')
            with patch.object(lean_pressure,'Path',side_effect=lambda p:root/str(p).lstrip('/')):
                self.assertEqual(lean_pressure.memory(), (54*GIB,7*GIB))
