import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tauceti_fleet import dependency_prefill, store, vm


class Prefill(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, TAUCETI_FLEET_DATA=temp.name, TAUCETI_FLEET_STATE=temp.name)
        env.start()
        self.addCleanup(env.stop)
        self.root = store.data_dir() / "dependency-prefill"
        self.root.mkdir()
        self.sha = hashlib.sha256(b"iso fixture").hexdigest()
        self.iso = self.root / (self.sha + ".iso")
        self.iso.write_bytes(b"iso fixture")
        self.iso.chmod(0o444)
        self.ticket = self.root / "next-pr.json"
        self.ticket.write_text(json.dumps(dict(sha256=self.sha, expires_at=time.time() + 600)))

    def test_one_pr_claim_does_not_change_image_or_allow_a_second_claim(self):
        before = self.iso.read_bytes()
        self.assertIsNone(dependency_prefill.claim(dict(ci_kinds=["staging"]), "a" * 16))
        claim = dependency_prefill.claim(dict(ci_kinds=["pr", "staging"]), "a" * 16)
        self.assertEqual(claim, dict(path=str(self.iso), sha256=self.sha))
        self.assertFalse(self.ticket.exists())
        self.assertTrue((self.root / ("claimed-" + "a" * 16 + ".json")).exists())
        self.assertIsNone(dependency_prefill.claim(dict(ci_kinds=["pr"]), "b" * 16))
        self.assertEqual(self.iso.read_bytes(), before)

    def test_bad_optional_tickets_fall_back_and_are_quarantined_once(self):
        for change in ("expired", "writable", "corrupt", "symlink", "oversized", "malformed"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, TAUCETI_FLEET_DATA=directory), patch.object(dependency_prefill, "event"):
                root = store.data_dir() / "dependency-prefill"
                root.mkdir()
                sha = hashlib.sha256(b"x").hexdigest()
                image = root / (sha + ".iso")
                image.write_bytes(b"x")
                image.chmod(0o444)
                ticket = dict(sha256=sha, expires_at=time.time() + 600)
                if change == "expired": ticket["expires_at"] = time.time() - 1
                if change == "writable": image.chmod(0o644)
                if change == "corrupt":
                    image.chmod(0o644)
                    image.write_bytes(b"changed")
                    image.chmod(0o444)
                if change == "symlink":
                    image.rename(root / "other")
                    image.symlink_to(root / "other")
                path = root / "next-pr.json"
                path.write_text("x" * 4097 if change == "oversized" else "{" if change == "malformed" else json.dumps(ticket))
                self.assertIsNone(dependency_prefill.claim(dict(ci_kinds=["pr"]), "a" * 16))
                self.assertFalse(path.exists())
                self.assertTrue((root / ("rejected-" + "a" * 16 + ".json")).exists())

    def test_attachment_is_read_only_and_absent_from_smoke_guests(self):
        config = dict(ci_vcpus=16, ci_memory_gib=32)
        prefill = dict(path=str(self.iso), sha256=self.sha)
        with patch.object(vm, "executable", return_value="qemu"):
            normal = vm.qemu_args(config, self.root, "disk", "seed")
            canary = vm.qemu_args(config, self.root, "disk", "seed", prefill=prefill)
            smoke = vm.qemu_args(config, self.root, "disk", "seed", prefill=prefill, small=True)
        drive = f"file={self.iso},format=raw,media=cdrom,readonly=on"
        self.assertNotIn(drive, normal)
        self.assertIn(drive, canary)
        self.assertNotIn(drive, smoke)
        self.assertEqual(len(canary), len(normal) + 2)
        command = dependency_prefill.mount_commands()[0]
        self.assertIn("ro,nosuid,nodev,noexec", command)
        self.assertIn("timeout 5", command)
        self.assertNotIn("packages", command)


if __name__ == "__main__":
    unittest.main()
