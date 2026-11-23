# Lapsed contributors

`tauceti-fleet contributors` finds people who used to open PRs or review at a sustained high
rate and then stopped or slowed sharply, so an operator can ask whether something is in their way.
It reads, posts nothing, and writes a markdown report (standard output or `--markdown PATH`) and,
with `--json PATH`, the full per-person data including weekly series.

```sh
tauceti-fleet contributors --markdown /tmp/lapsed.md --json /tmp/lapsed.json
tauceti-fleet contributors --offline          # recompute from the cache, no reads
tauceti-fleet contributors --skip github      # refresh Forgejo and the queue only
```

## Sources

The cache lives in `~/.local/share/tauceti-fleet/contributors/` (`TAUCETI_FLEET_DATA` moves it).
Every source is read incrementally after the first run.

- **GitHub** `TauCetiProject/TauCeti`: GraphQL only, never REST. PRs are read newest-updated first,
  50 per page with their labels, comments and reviews, until a page ends before the previous run's
  high-water mark. A first run reads all PRs (about 900 pages, about 2 points each); if the
  quota falls below 300 it stops, saves its cursor and resumes there next time. The token is
  `gh auth token`.
- **Forgejo** `git.taucetiproject.org`: the issues listing with `type=pulls&state=all` and the
  repository comment listing, both anonymous and with `since=` after the first run.
- **Merge queue ledger** `inputs` on `tauchonki2` (`--queue-host`), read with `sqlite3 -readonly`
  over SSH. It is authoritative for landings on both forges. Before the ledger begins
  (4 Nov 2026), landings are Bors's: closed PRs titled `[Merged by Bors]`, or merged PRs.
- For each lapsed person the report also asks Forgejo whether an account with the same login exists.

## Measures

Per identity, events are dated in `--timezone` (default Australia/Sydney):

- **opened**: PRs authored, on either forge;
- **reviews**: comments and PR reviews posted on PRs the account did not author. The AI review
  scoreboards are PR comments, so GitHub PR reviews alone miss nearly all review work;
- **landed**: PRs authored that landed;
- **active days**: distinct days with any of these.

**Volume** is opened plus reviews. Landings trail authoring by days and repeat it, so they are
reported but do not count towards volume.

## Thresholds

- **Sustained high volume:** at least 10 a week over the person's best 28-day window, with activity
  in at least 3 of its 4 weeks. Ten a week separates fleets and regular contributors from people
  who opened a handful of PRs; the weeks condition drops one-off bursts.
- **Lapsed:** sustained, and the last 14 days ran at 25% of that peak rate or less. Fourteen days
  is long enough that a weekend or a short outage does not trigger it, and short enough to catch
  a fleet that stopped this fortnight. A quarter of the peak is a dramatic drop, not ordinary
  variation.
- **Stopped:** lapsed and nothing at all in the last 7 days.
- **One stream lapsed:** still active overall, but opened or reviews (with a peak of at least 5 a
  week) fell to 25% of its own peak. This catches a fleet that stopped reviewing but still authors.
- **Last strong week ended:** the last active day of the latest 7 days that still ran at half the
  peak rate or more, a reading of when the drop happened.

People are ranked by lost volume: peak weekly rate minus the last 14 days' weekly rate.

## Identities

Shared project and operator accounts (`tauceti-ai-for-science`, `kim-em`, `tauceti-queue`,
`tauceti-redteam`, `tauceti-review-bot` and other bots) are left out. Contributor sessions that
push through a shared account are therefore not counted for their person. An `alice-bot` account
counts as `alice`. Other merges go in `~/.config/tauceti-fleet/contributors.json` (or `--identities`):

```json
{"aliases": {"roed314": "roed-math"}, "exclude": ["some-test-account"]}
```
