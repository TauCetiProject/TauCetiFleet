# One-guest dependency-prefill canary

This optional operator path attaches one local read-only payload ISO to the next
new general guest whose existing assignment classes include PR work. Reserved
staging-only slots are excluded, and guest classes/priorities remain unchanged.
The trusted PR workflow consumes the payload only for individual PRs, after
normalization and integrated configuration attestation. It retains compressed
archive restoration and all native cache validation. A mixed-class guest assigned
staging simply follows the ordinary staging setup.

Prepare the payload in a disposable, unregistered guest from trusted main inputs
at the production path. Publish `manifest.json` and `packages.tar.zst` on an ISO
labelled `TAUCETI_PREFILL`; include only dependency sources and cache-executable
outputs. Record exact config hashes, UID, platform, project path, payload digest
and bytes. Never use a worker checkout, candidate build or a registered job disk.
Mounting at boot exposes read-only data; it does not hydrate or execute it.

Place the checksum-verified ISO under the host's Fleet data root at
`dependency-prefill/<ISO-SHA256>.iso`, mode 0444. Atomically write
`dependency-prefill/next-pr.json` with `sha256` and an `expires_at` Unix timestamp
no more than an hour away. No new daemon, registration class or capacity change
is needed. The existing host-wide `ci-start.lock` serializes selection before
registration. A valid ticket is renamed `claimed-<instance>.json`; failure after
claim requires explicit operator re-arming. Invalid/expired tickets are renamed
`rejected-<instance>.json` and ordinary guest launch proceeds. Absent tickets add
no drive or mount command. Event receipts and VM state record the selected digest.

The guest mount is `ro,nosuid,nodev,noexec` with a five-second timeout; inability
to mount retains ordinary setup. The optional workflow consumer verifies exact
identity/digest, extracts into a private temporary directory and publishes the
complete package tree atomically. Mismatch leaves the usual cold path intact.
The ISO is never bound into the candidate sandbox.

Rollback: remove an unclaimed ticket; leave running guests and their ISO files
alone until they exit. A single consumed ticket cannot affect a subsequent guest.
Do not prune claimed ISO files automatically. After the canary, verify the exact
runner/job identity, a hydration hit, native cache-executable reuse, zero missing
Mathlib downloads, useful build completion and the next ordinary guest without
attachment. Compare matched host/pin setup timings and include producer/delivery
costs; one successful job alone is not a throughput measurement.

Live deployment must preserve production-local changes: copy the current release,
apply only the reviewed diff to `vm.py`, add the new module, and diff against the
current files before handover. Do not replace `vm.py` wholesale from another
checkout. Keep admissions and active VM services running during the handover.
