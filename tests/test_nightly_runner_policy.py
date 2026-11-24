import unittest
from unittest.mock import patch
from tauceti_fleet import github
from tauceti_fleet.store import FleetError

PREFIX = "TauCetiProject/TauCeti/.github/workflows/"
BUILD = PREFIX + "pr-build.yml@refs/heads/main"
NIGHTLY = PREFIX + "nightly-verify.yml@refs/heads/main"
SMOKE = PREFIX + "fleet-smoke.yml@refs/heads/main"  # deleted from TauCeti; no longer admitted
PICKER = PREFIX + "pick-runner.yml@refs/heads/main"
LABEL_ROUTER = PREFIX + "full-lint-label.yml@refs/heads/main"
API_DOCS = PREFIX + "api-docs.yml@refs/heads/main"

HARNESS = "TauCetiProject/TauCetiTools/.github/workflows/"


def harness_api(group, calls=None):
    def request(path, **_):
        if calls is not None:
            calls.append(path)
        return group
    return request


class NightlyRunnerPolicy(unittest.TestCase):
    def check(self, workflows, calls=None):
        group = dict(name="TauCetiFleet", restricted_to_workflows=True, selected_workflows=workflows)
        with patch.object(github, "request", side_effect=harness_api(group, calls)):
            return github.runner_policy(dict(runner_group_id=3))

    def test_live_update_accepts_old_and_new_policy(self):
        for workflows in ([BUILD], [BUILD, NIGHTLY], [BUILD, PICKER], [BUILD, LABEL_ROUTER], [BUILD, API_DOCS],
                          [BUILD, NIGHTLY, PICKER, LABEL_ROUTER, API_DOCS]):
            self.assertEqual(self.check(workflows), 3)

    def test_no_pr_refs_other_repositories_or_untrusted_workflows(self):
        for bad in (NIGHTLY.replace("refs/heads/main", "refs/pull/1/merge"),
                    PICKER.replace("refs/heads/main", "refs/pull/1/merge"),
                    LABEL_ROUTER.replace("refs/heads/main", "refs/pull/1/merge"),
                    API_DOCS.replace("refs/heads/main", "refs/pull/1/merge"),
                    API_DOCS.replace("refs/heads/main", "refs/heads/other"),
                    API_DOCS.replace("TauCetiProject/TauCeti/", "someone/Other/"),
                    NIGHTLY.replace("TauCetiProject/TauCeti/", "someone/Other/"),
                    PICKER.replace("TauCetiProject/TauCeti/", "someone/Other/"),
                    PREFIX + "untrusted.yml@refs/heads/main", SMOKE):
            with self.assertRaises(FleetError): self.check([BUILD, bad])
        with self.assertRaises(FleetError): self.check([NIGHTLY])
        with self.assertRaises(FleetError): self.check([PICKER, LABEL_ROUTER])
        with self.assertRaises(FleetError): self.check([API_DOCS])

    def test_shared_harness_at_tools_main_is_allowed_but_no_other_branch_or_fork(self):
        main = HARNESS + "pr-build.yml@refs/heads/main"
        self.assertEqual(self.check([BUILD, NIGHTLY, main]), 3)
        for bad in (HARNESS + "pr-build.yml@main", HARNESS + "pr-build.yml@refs/heads/other",
                    "someone/TauCetiTools/.github/workflows/pr-build.yml@refs/heads/main",
                    HARNESS + "publish-lake-cache.yml@refs/heads/main"):
            with self.assertRaises(FleetError): self.check([BUILD, bad])

    def test_api_docs_and_nightly_harness_at_tools_main_are_allowed(self):
        self.assertEqual(self.check([BUILD, HARNESS + "api-docs.yml@refs/heads/main",
                                     HARNESS + "nightly-verify.yml@refs/heads/main"]), 3)
        for bad in (HARNESS + "pick-runner.yml@refs/heads/main", HARNESS + "pages.yml@refs/heads/main",
                    HARNESS + "api-docs.yml@refs/heads/other"):
            with self.assertRaises(FleetError): self.check([BUILD, bad])
        with self.assertRaises(FleetError): self.check([HARNESS + "nightly-verify.yml@refs/heads/main"])

    def test_exact_harness_revisions_are_no_longer_admitted(self):
        # Even a revision on TauCetiTools main: the groups list only the `main` entry now.
        for pinned in (HARNESS + "pr-build.yml@abf810bf5064bf549ba76fac568da9d1dbb32362",
                       HARNESS + "pr-build.yml@8902fa12a4faafc7bb512ab0baa9a452249b93aa"):
            with self.assertRaises(FleetError): self.check([BUILD, HARNESS + "pr-build.yml@refs/heads/main", pinned])
