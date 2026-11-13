import unittest
from unittest.mock import patch
from tauceti_fleet import github
from tauceti_fleet.store import FleetError

PREFIX = "TauCetiProject/TauCeti/.github/workflows/"
BUILD = PREFIX + "pr-build.yml@refs/heads/main"
NIGHTLY = PREFIX + "nightly-verify.yml@refs/heads/main"
SMOKE = PREFIX + "fleet-smoke.yml@refs/heads/main"

class NightlyRunnerPolicy(unittest.TestCase):
    def check(self, workflows):
        group = dict(name="TauCetiFleet", restricted_to_workflows=True, selected_workflows=workflows)
        with patch.object(github, "request", return_value=group):
            return github.runner_policy(dict(runner_group_id=3))

    def test_live_update_accepts_old_and_new_policy(self):
        for workflows in ([BUILD], [BUILD, SMOKE], [BUILD, NIGHTLY], [BUILD, NIGHTLY, SMOKE]):
            self.assertEqual(self.check(workflows), 3)

    def test_no_pr_refs_other_repositories_or_untrusted_workflows(self):
        for bad in (NIGHTLY.replace("refs/heads/main", "refs/pull/1/merge"),
                    NIGHTLY.replace("TauCetiProject/TauCeti/", "someone/Other/"),
                    PREFIX + "untrusted.yml@refs/heads/main"):
            with self.assertRaises(FleetError): self.check([BUILD, bad])
        with self.assertRaises(FleetError): self.check([NIGHTLY])
