# Production operations

- Prefer live deployment and repair. Leave worker and CI admissions enabled during routine production updates; draining confuses other agents monitoring the system.
- Do not use `tauceti-fleet drain` or pause admissions unless the user requests it or a concrete constraint prevents a safe live update. Explain the constraint first and look for a live update path rather than treating the CLI's drained-update requirement as permission to drain.
- Verify running revisions and useful work after deployment, and clean up any resulting interruption or stranded work within the authorized scope.

# Known issues: do not report

- GitHub sometimes leaves TauCeti Actions runs reporting `queued` forever, with no jobs ("zombie runs"). They hold no runner, block no queue attempt and are harmless. `tauceti-fleet status` lists jobs queued for more than 24 hours on a separate "Stale queued (known GitHub zombies, nothing to do)" line, outside Run/Wait. Do not report them, investigate them or try to cancel them (cancel and force-cancel both return HTTP 409). TauCetiWorker `docs/queue/github-zombie-runs.md` records the known set and the human-approved cleanup.
