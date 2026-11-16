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

class NightlyRunnerPolicy(unittest.TestCase):
    def check(self, workflows):
        group = dict(name="TauCetiFleet", restricted_to_workflows=True, selected_workflows=workflows)
        with patch.object(github, "request", return_value=group):
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
