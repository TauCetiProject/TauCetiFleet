import unittest
from unittest.mock import patch
from tauceti_fleet import github
from tauceti_fleet.store import FleetError

PREFIX = "TauCetiProject/TauCeti/.github/workflows/"
BUILD = PREFIX + "pr-build.yml@refs/heads/main"
NIGHTLY = PREFIX + "nightly-verify.yml@refs/heads/main"
SMOKE = PREFIX + "fleet-smoke.yml@refs/heads/main"
PICKER = PREFIX + "pick-runner.yml@refs/heads/main"
LABEL_ROUTER = PREFIX + "full-lint-label.yml@refs/heads/main"

HARNESS = "TauCetiProject/TauCetiTools/.github/workflows/"
# Revisions on TauCetiTools main in this fixture; anything else has diverged from it.
REVIEWED = {"8902fa12a4faafc7bb512ab0baa9a452249b93aa", "150b56c3ab743b1f2a120f0639d98586455d7f2a",
            "fe61c4f22f1e59c4393fe24ed32d22787f3d5e9f", "a7662ab81f900586db3586ec3a11c38a5f46962d",
            "abf810bf5064bf549ba76fac568da9d1dbb32362"}


def harness_api(group, reviewed=REVIEWED, calls=None):
    def request(path, **_):
        if calls is not None:
            calls.append(path)
        if path.startswith("repos/TauCetiProject/TauCetiTools/compare/"):
            revision, head = path.split("/compare/", 1)[1].split("?", 1)[0].split("...")
            assert head == "main"
            if revision not in reviewed:
                raise FleetError("GitHub GET returned HTTP 404")
            return dict(status="ahead")
        return group
    return request


class NightlyRunnerPolicy(unittest.TestCase):
    def setUp(self):
        github._REVIEWED_HARNESS.clear()

    def check(self, workflows, reviewed=REVIEWED, calls=None):
        group = dict(name="TauCetiFleet", restricted_to_workflows=True, selected_workflows=workflows)
        with patch.object(github, "request", side_effect=harness_api(group, reviewed, calls)):
            return github.runner_policy(dict(runner_group_id=3))

    def test_live_update_accepts_old_and_new_policy(self):
        for workflows in ([BUILD], [BUILD, SMOKE], [BUILD, NIGHTLY], [BUILD, NIGHTLY, SMOKE],
                          [BUILD, PICKER], [BUILD, LABEL_ROUTER],
                          [BUILD, NIGHTLY, SMOKE, PICKER, LABEL_ROUTER]):
            self.assertEqual(self.check(workflows), 3)

    def test_no_pr_refs_other_repositories_or_untrusted_workflows(self):
        for bad in (NIGHTLY.replace("refs/heads/main", "refs/pull/1/merge"),
                    PICKER.replace("refs/heads/main", "refs/pull/1/merge"),
                    LABEL_ROUTER.replace("refs/heads/main", "refs/pull/1/merge"),
                    NIGHTLY.replace("TauCetiProject/TauCeti/", "someone/Other/"),
                    PICKER.replace("TauCetiProject/TauCeti/", "someone/Other/"),
                    PREFIX + "untrusted.yml@refs/heads/main"):
            with self.assertRaises(FleetError): self.check([BUILD, bad])
        with self.assertRaises(FleetError): self.check([NIGHTLY])
        with self.assertRaises(FleetError): self.check([PICKER, LABEL_ROUTER])

    def test_exact_source_harness_pin_is_allowed_but_mutable_or_other_pins_are_not(self):
        source = "TauCetiProject/TauCetiTools/.github/workflows/pr-build.yml@8902fa12a4faafc7bb512ab0baa9a452249b93aa"
        self.assertEqual(self.check([BUILD, NIGHTLY, source]), 3)
        control_source = source.rsplit("@", 1)[0] + "@150b56c3ab743b1f2a120f0639d98586455d7f2a"
        self.assertEqual(self.check([BUILD, NIGHTLY, source, control_source]), 3)
        proof_source = source.rsplit("@", 1)[0] + "@fe61c4f22f1e59c4393fe24ed32d22787f3d5e9f"
        self.assertEqual(self.check([BUILD, NIGHTLY, source, control_source, proof_source]), 3)
        finalizer_source = source.rsplit("@", 1)[0] + "@a7662ab81f900586db3586ec3a11c38a5f46962d"
        self.assertEqual(self.check([BUILD, NIGHTLY, source, control_source, proof_source, finalizer_source]), 3)
        for bad in (source.rsplit("@", 1)[0] + "@refs/heads/main", source[:-1] + "0"):
            with self.assertRaises(FleetError): self.check([BUILD, bad])

    def test_any_harness_revision_on_tools_main_is_accepted_without_a_release(self):
        pinned = HARNESS + "pr-build.yml@abf810bf5064bf549ba76fac568da9d1dbb32362"
        calls = []
        self.assertEqual(self.check([BUILD, pinned], calls=calls), 3)
        self.assertEqual(self.check([BUILD, pinned], calls=calls), 3)
        # A reviewed revision is checked once; later policy reads use the cache.
        self.assertEqual(sum("/compare/" in c for c in calls), 1)

    def test_harness_entries_off_tools_main_or_for_other_workflows_are_rejected(self):
        for bad in (HARNESS + "pr-build.yml@" + "0" * 40,
                    HARNESS + "publish-lake-cache.yml@abf810bf5064bf549ba76fac568da9d1dbb32362",
                    HARNESS + "pr-build.yml@ABF810BF5064BF549BA76FAC568DA9D1DBB32362",
                    "someone/TauCetiTools/.github/workflows/pr-build.yml@abf810bf5064bf549ba76fac568da9d1dbb32362"):
            with self.assertRaises(FleetError): self.check([BUILD, bad])
        with self.assertRaises(FleetError):
            self.check([BUILD, HARNESS + "pr-build.yml@abf810bf5064bf549ba76fac568da9d1dbb32362"], reviewed=set())
