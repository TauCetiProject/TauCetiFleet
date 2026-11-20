# Production operations

- Prefer live deployment and repair. Leave worker and CI admissions enabled during routine production updates; draining confuses other agents monitoring the system.
- Do not use `tauceti-fleet drain` or pause admissions unless the user requests it or a concrete constraint prevents a safe live update. Explain the constraint first and look for a live update path rather than treating the CLI's drained-update requirement as permission to drain.
- Verify running revisions and useful work after deployment, and clean up any resulting interruption or stranded work within the authorized scope.

# Known issues: do not report

- GitHub zombie runs: about 750 TauCeti Actions runs (mostly created 2026-11-11), plus a few in TauCetiWorker and TauCetiFleet, report `queued` forever with no jobs. They hold no runner, block nothing and are harmless; cancel answers HTTP 409 and delete HTTP 403. We never file GitHub support requests about them (Kim, 2026-11-20): that option is off the table. `tauceti-fleet status` leaves them, and jobs queued over 24 hours, out of its text entirely; only `--json` carries `stale_waiting` counts. Do not report, investigate, cancel or delete them. TauCetiWorker `docs/queue/github-zombie-runs.md` has the policy and the record.
