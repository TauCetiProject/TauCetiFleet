import unittest
from unittest.mock import patch
from tauceti_fleet import github
from tauceti_fleet.store import FleetError

PREFIX = "TauCetiProject/TauCeti/.github/workflows/"
BUILD = PREFIX + "pr-build.yml@refs/heads/main"
NIGHTLY = PREFIX + "nightly-verify.yml@refs/heads/main"
PINNED_TOOLS = "TauCetiProject/TauCetiTools/.github/workflows/pr-build.yml@8902fa12a4faafc7bb512ab0baa9a452249b93aa"
SMOKE = PREFIX + "fleet-smoke.yml@refs/heads/main"

class NightlyRunnerPolicy(unittest.TestCase):
    def check(self, workflows):
        group = dict(name="TauCetiFleet", restricted_to_workflows=True, selected_workflows=workflows)
        with patch.object(github, "request", return_value=group):
            return github.runner_policy(dict(runner_group_id=3))

    def test_live_update_accepts_old_and_new_policy(self):
        for workflows in ([BUILD], [BUILD, SMOKE], [BUILD, NIGHTLY], [BUILD, NIGHTLY, SMOKE], [BUILD, NIGHTLY, SMOKE, PINNED_TOOLS]):
            self.assertEqual(self.check(workflows), 3)

    def test_no_pr_refs_other_repositories_or_untrusted_workflows(self):
        for bad in (NIGHTLY.replace("refs/heads/main", "refs/pull/1/merge"),
                    NIGHTLY.replace("TauCetiProject/TauCeti/", "someone/Other/"),
                    PREFIX + "untrusted.yml@refs/heads/main",
                    PINNED_TOOLS.replace("8902fa12a4faafc7bb512ab0baa9a452249b93aa", "refs/heads/main"),
                    PINNED_TOOLS.replace("8902fa12a4faafc7bb512ab0baa9a452249b93aa", "0"*40)):
            with self.assertRaises(FleetError): self.check([BUILD, bad])
        with self.assertRaises(FleetError): self.check([NIGHTLY])
