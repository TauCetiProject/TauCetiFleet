import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import prefill_guest


@unittest.skipUnless(shutil.which("zstd"), "zstd required")
class Payloads(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        original_environment = prefill_guest.environment
        self.original_environment = original_environment

        def test_environment():
            value = original_environment()
            value["PATH"] = os.environ["PATH"]
            return value

        environment = patch.object(prefill_guest, "environment", side_effect=test_environment)
        environment.start()
        self.addCleanup(environment.stop)
        self.project = self.root / "project"
        self.package = self.project / ".lake/packages/mathlib"
        self.package.mkdir(parents=True)
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://github.com/leanprover-community/mathlib4")
        (self.package / ".gitignore").write_text(".lake/\n")
        (self.package / "source").write_text("trusted source")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "fixture")
        self.revision = self.git("rev-parse", "HEAD")
        executable = self.package / ".lake/build/bin/cache"
        executable.parent.mkdir(parents=True)
        executable.write_text("fixture, not an executable-reuse proof")
        executable.chmod(0o755)
        (self.project / "lean-toolchain").write_text("leanprover/lean4:v4.36.0-rc2\n")
        (self.project / "lakefile.toml").write_text('name="TauCeti"\n')
        (self.project / "lake-manifest.json").write_text(
            json.dumps(
                dict(
                    packages=[
                        dict(name="mathlib", rev=self.revision, url="https://github.com/leanprover-community/mathlib4")
                    ]
                )
            )
        )
        self.destination = self.root / "payload"

    def git(self, *arguments):
        return subprocess.check_output(
            ["git", "-C", str(self.package), *arguments], text=True, stderr=subprocess.DEVNULL
        ).strip()

    def test_payload_format_binds_clean_packages_configuration_uid_and_path(self):
        result = prefill_guest.seal_packages(self.project, self.destination, "a" * 40)
        self.assertEqual(result["project_path"], str(self.project))
        self.assertEqual(result["uid"], os.getuid())
        self.assertEqual(result["packages"]["mathlib"]["rev"], self.revision)
        self.assertEqual(
            result["config_sha256"], {n: prefill_guest.digest(self.project / n) for n in prefill_guest.CONFIG}
        )
        self.assertEqual(result["payload_sha256"], prefill_guest.digest(self.destination / "packages.tar.zst"))
        self.assertFalse((self.destination / "packages.tar.zst").stat().st_mode & 0o222)
        with self.assertRaises(ValueError):
            prefill_guest.seal_packages(self.project, self.destination, "a" * 40)

    def test_external_git_alternates_and_dirty_sources_cannot_be_published(self):
        alternates = self.package / ".git/objects/info/alternates"
        alternates.write_text("/outside/objects\n")
        with self.assertRaisesRegex(ValueError, "self-contained"):
            prefill_guest.seal_packages(self.project, self.destination, "a" * 40)
        alternates.unlink()
        (self.package / "source").write_text("modified")
        with self.assertRaisesRegex(ValueError, "differs"):
            prefill_guest.seal_packages(self.project, self.destination, "a" * 40)
        self.assertFalse(self.destination.exists())

    def test_interrupted_archive_creation_never_publishes_a_partial_payload(self):
        run = prefill_guest.run

        def interrupted(arguments, **kwargs):
            if arguments[0] == "tar":
                raise subprocess.CalledProcessError(1, arguments)
            return run(arguments, **kwargs)

        with (
            patch.object(prefill_guest, "run", side_effect=interrupted),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            prefill_guest.seal_packages(self.project, self.destination, "a" * 40)
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob("prefill-publish-*")))

    def test_producer_environment_drops_credentials_git_configuration_and_wrappers(self):
        with patch.dict(os.environ, GH_TOKEN="secret", GIT_CONFIG_COUNT="1", LEAN="patched", BASH_ENV="untrusted"):
            result = self.original_environment()
        for name in ("GH_TOKEN", "GIT_CONFIG_COUNT", "LEAN", "BASH_ENV"):
            self.assertNotIn(name, result)
        self.assertEqual(result["RAYON_NUM_THREADS"], "16")


if __name__ == "__main__":
    unittest.main()
