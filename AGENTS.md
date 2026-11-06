# Production operations

- Prefer live deployment and repair. Leave worker and CI admissions enabled during routine production updates; draining confuses other agents monitoring the system.
- Do not use `tauceti-fleet drain` or pause admissions unless the user requests it or a concrete constraint prevents a safe live update. Explain the constraint first and look for a live update path rather than treating the CLI's drained-update requirement as permission to drain.
- Verify running revisions and useful work after deployment, and clean up any resulting interruption or stranded work within the authorized scope.
- On tauchonki, retain 40 ai-for-science workers and one subscription pool named `subscription`, limited to 16 workers. Switch its profile between qim, Gmail, and lean-fro; do not create separate account-specific pools. Gmail and lean-fro pool services were explicitly shut down by the operator.
