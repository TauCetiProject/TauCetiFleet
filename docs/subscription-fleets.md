# Subscription fleets

How to run a Fleet's workers on Claude and Codex subscriptions in place of API
billing: what to prepare, the settings in the order an operator meets them, the
commands for a running fleet, and the failures worth recognising. Installation,
roles, CI and the status tables are in the [README](../README.md), and each
section here links to the README section that is its reference.

## What it needs

| What | How | Lasts |
| --- | --- | --- |
| A GitHub account of its own | Sign `gh` in as it on the machine you run `tauceti-fleet` from: `gh auth login -h github.com -p https -s workflow`. Or name a token in the profile's `github_token_file` | Until revoked |
| A Claude setup-token | `claude setup-token`, saved in a private file | A year; it never rotates, so every worker shares it |
| A Claude usage sign-in, same account, one per host | `CLAUDE_CONFIG_DIR=~/.config/tauceti-fleet/claude-usage claude auth login` on Linux, which writes `.credentials.json` in that directory; a directory of its own for each host. On a Mac it writes no file ([macOS](#macos)) | About four weeks from the sign-in |
| A Codex sign-in, for `codex` and `auto` slots | `codex login` on the host, as the host account | Renewed by the workers, see below |

**The account.** The fleet acts as the profile's `github_account` and as nothing
else. It opens, reviews and repairs pull requests at a rate no person does, and
a rate limit or a suspension falls on the whole account, so use one that does
nothing else. `add` transfers that account's credential and no other, and every
worker asks GitHub before each round which account its credential belongs to
and [halts](#day-to-day) on any other answer. Workers fetch and push over HTTPS
with that token: the worker routes GitHub SSH URLs through HTTPS and disables
SSH transport, so the host needs no SSH key for GitHub. The `workflow` scope
lets a worker push a branch that carries a change to a workflow file, as a
branch merged with a newer `main` can.

**Two Claude credentials**, because neither does both jobs. The setup-token's
scope covers inference only: the usage endpoint answers it HTTP 403, and a
worker without a usage reading starts no Claude work. An ordinary sign-in can
read usage, but its access token is short-lived and its refresh token is
single-use, so one party alone may renew it. The workers do, which is why the
sign-in is made for the fleet in a directory of its own and is never a copy of
a login another `claude` uses. Each host's workers renew their own copy, so two
hosts given one login break each other at the first renewal: the other host's
refresh token no longer works, and once its access token expires it reads no
usage and starts no Claude work. Every host therefore needs a usage sign-in
made for it alone, and so does every install on a shared machine. `add` records
the host a usage login is for before sending it, keeps that record even if the
send fails, and refuses the login to any other host. A login is known by its
refresh token, so reformatting the file does not make it a new one. The
setup-token never rotates, and one token serves every host. A profile can also
leave both out: the host account is then signed in with `claude auth login`, and
each worker copies that login.

**Codex renewal.** A Codex access token lasts about ten days and the CLI renews
it only while it runs. A worker with `auto_refresh` renews the host's
`~/.codex/auth.json` itself, two days before expiry or on a 401, provided
Codex's `cli_auth_credentials_store` resolves to `file`, including in the
administrator files under `/etc/codex`. A workspace policy, which Codex fetches
for business, education and enterprise plans, and a `config_server` enrolled in
a requirements file under `/etc/codex`, for any plan, can choose another store,
so the worker renews by one of two routes:

- **Set the store explicitly (preferred).** With
  `cli_auth_credentials_store = "file"` in the `config.toml` of both the host's
  `~/.codex` and the worker's own Codex directory, which for a worker with an
  isolated home is the one in that home and is not copied from the host's, the
  worker renews on any plan and with a `config_server` enrolled.
- **Name the plan.** Otherwise the worker renews only a plan Codex fetches no
  policy for (free, go, plus, the pro plans, team and the self-serve business
  plans) or one named, as the token carries it, in `TAUCETI_CODEX_REFRESH_PLANS`
  (comma-separated, for example `edu_pro`), and nothing while a `config_server`
  is enrolled. Name a plan only after `codex doctor` on the host reports the
  file store for that login: a workspace policy can keep it in the keyring.

Fleet sets `auto_refresh` for a profile with a usage sign-in; another profile
sets `TAUCETI_AUTO_REFRESH` to `1` in [`env`](#billing-agent-sandbox-and-pace),
which also carries `TAUCETI_CODEX_REFRESH_PLANS`. TauCetiWorker's
[renewal notes](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/quota.md#keeping-the-claude-token-alive---auto-refresh)
are the reference. Once workers renew a login nothing else may use it: a
renewal logs out every other holder of the same file.

## A minimal profile

In `~/.config/tauceti-fleet/profiles.json` on the operator's machine
([Profiles](../README.md#profiles)), with both credential files private
(`chmod 600`):

```json
{
  "subscription": {
    "github_account": "fleet-bot",
    "claude_billing": "subscription",
    "agent": "auto",
    "claude_token_file": "~/.config/tauceti-fleet/claude-setup-token",
    "claude_usage_credentials_file": "~/.config/tauceti-fleet/claude-usage/.credentials.json"
  }
}
```

```sh
tauceti-fleet add fleet@big-server --no-ci --profile subscription
tauceti-fleet doctor big-server      # agent CLIs, days left on each credential
tauceti-fleet prepare big-server
tauceti-fleet enable big-server --workers
```

Give a host the two Claude keys before its workers first start: a worker slot
keeps the Claude credential directory it first ran with.

The profile serves one host, because its usage file does. For each further
host, copy the profile under another name with its own
`claude_usage_credentials_file`, signed in in its own directory, and `add` the
host with that profile. On a Linux host the profile may instead name a file the
operator's machine does not hold: `add` then transfers no usage login, and you
sign in on the host with `tauceti-fleet claude-usage-login`, which `doctor`
names there.

## Settings, one group at a time

| Group | Set with | Takes effect |
| --- | --- | --- |
| Billing, agent, sandbox, pace, `env` | Profile, then `add` | After a drain, like a model change |
| Claude credentials | Profile, then `add` | Adding or removing a key: after a drain. Replacing a credential: at once |
| Slot groups | Profile `slots`, or `slots HOST --group` | No drain; each changed worker when its round ends |
| Weekly schedule | `schedule HOST` | The controller's next pass |
| Target list | `targets HOST --file` | Next pass. Setting, moving or clearing a list restarts the workers; new contents at the same path restart nothing |
| Outside-list cap | Profile `outside_list_cap` | After a drain |
| Progress reports | Slot-group keys; profile `progress_repo`, `progress_ref` | Group keys restart that group's workers at once; profile keys after a drain |
| Incidents and decide | Profile | After a drain |
| Shared host directory | `shared_host_dir` in the host's `host.json` | The controller's next cycle |
| Status export | `status-export HOST --dir` | No restart |

### Billing, agent, sandbox and pace

| Key | Values | Default |
| --- | --- | --- |
| `claude_billing` | `api` or `subscription`. Under `subscription` no API key is read or transferred and workers follow the provider's usage windows, not [grants](../README.md#grants) | `api` |
| `agent` | `claude`, `codex`, or `auto` (the worker chooses per round). `codex` and `auto` need `subscription`; `auto` excludes `author_model` and `author_effort` | `claude` |
| `sandbox` | `host` or `bubble`; Bubble containers run outside the worker slice's limits, see [Hosts](#hosts) | `host` |
| `pace` | Pacing curve as `time%:budget%` points, for example `0:10,50:70,90:90` | the worker's curve |
| `env` | Extra environment for every worker, as printable ASCII strings. Values are written to `workers.toml`, so it holds no secret | none |

`env` carries the worker settings that have no profile key. Fleet refuses names
that it or the worker sets. The ones a subscription fleet uses, each described in
TauCetiWorker's
[reference](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/reference.md):

| Variable | Effect |
| --- | --- |
| `TAUCETI_PACE_CLAUDE`, `TAUCETI_PACE_CODEX` | A curve for one provider's windows; the other follows `pace`. For `auto` slots whose two subscriptions should be spent at different rates |
| `TAUCETI_AUTO_REFRESH` = `1` | Workers renew the sign-ins they were given |
| `TAUCETI_CODEX_REFRESH_PLANS` | Codex plans renewed beyond the worker's own list, for a login whose `config.toml` does not set the credential store ([Codex renewal](#what-it-needs)) |
| `TAUCETI_REACTIONS` = `0` | A reviewer's claim on an author contest is a file under `TAUCETI_GITHUB_SHARED_DIR`, not a reaction on GitHub |
| `TAUCETI_STUCK_ISSUES` = `0` | No "Review stuck" tracking issues are opened |
| `TAUCETI_SHARED_ATTEMPTS_DIR` | Repair attempts are counted once for all workers that name the directory |
| `TAUCETI_BUBBLE_TOOLCHAINS` = `1` | Bubble code-writing rounds mount the host's Lean toolchains read-only |
| `TAUCETI_PROGRESS_POLL` | Longest wait, in seconds, between two `threshold` progress plans |

### Claude credentials

`claude_token_file` and `claude_usage_credentials_file` are transferred like the
other credentials; `workers.toml` names only their paths on the host. The second
key requires the first. `doctor` and `status` report the days left on each, with
a note thirty days before the token expires and four days before the usage
sign-in stops renewing, each naming the command that replaces it. A lapsed
credential blocks worker activation. Once a worker has renewed the usage
sign-in the operator's copy of it no longer works, so a repeated `add` leaves
the host's sign-in in place unless the profile's file holds a different one.
When it is due, sign in again on the operator's machine and repeat `add`, or run
`tauceti-fleet claude-usage-login` on the host. That command runs
`claude auth login`, moves the new login into place under the workers' renewal
lock and records the time, from which `doctor` counts the four weeks again.
Either way the sign-in has to happen on Linux: on a Mac `claude auth login` writes no file, so the
operator's file stays as it was, and `claude-usage-login` on a Mac host reports
that the sign-in left no login and changes nothing.

### Slot groups

Without `slots` every slot runs the profile's agent through the worker's full
rotation. `slots` divides them ([Slot groups](../README.md#slot-groups)):

```json
"slots": [{"count": 6, "agent": "auto"}, {"count": 2, "agent": "claude", "roles": ["review"]}, {"count": 1, "agent": "codex", "roles": ["progress"]}]
```

`roles` takes the worker's phase names: `rebase`, `bump`, `lint-repair`,
`progress`, `fix-ci`, `fix`, `review`, `overlap-fix`, `overlap-scan` and
`roadmap`. A group without `roles` runs the full rotation and one without
`agent` uses the profile's. On a running host:

```sh
tauceti-fleet slots big-server            # the groups, their workers, how many are admitted
tauceti-fleet slots big-server --group 4:auto --group 4:claude:review --group 1:codex:progress
```

The groups given replace the saved list, so name every group on each edit: a
group left out admits no more rounds. An edit rewrites only the workers that
change group, and the worker manager
holds a changed agent or phase list until that worker's round ends. Fleet admits
a worker whose agent or phases were rewritten, including by `slots --clear`,
only once its status shows the new ones; one that has not started yet waits
for its first pass after it does. The
counts say how many slots exist; `workers --max` stays the admission ceiling,
and a ceiling below the total admits each group within one slot of its
proportional share.

### Weekly schedule

A subscription shared with other uses can be spent in chosen parts of the week
([Weekly schedule](../README.md#weekly-schedule)):

```sh
tauceti-fleet schedule big-server "Mon 23:00 -> Thu 07:00" "Sat 09:00 -> Sun 21:00" --timezone Europe/Berlin
tauceti-fleet schedule big-server          # the saved slots, the current slot, the next boundary
```

Outside every slot no new round starts and admitted rounds finish. Slots come
before any option, and times are host local time without `--timezone`.

### Target lists

A target list is a Markdown file of milestones on the way to one goal. Workers
given one author only from it: each roadmap round takes one open item whose
prerequisites have landed.

```markdown
# Law of large numbers
<!-- tauceti-targets:v1 -->
Goal and ground rules; the agent sees everything before the first `## ` heading.

## MeasureTheory
- [x] `borel-cantelli` — both Borel–Cantelli lemmas (serves: G1)
- [ ] `kolmogorov-zero-one` — tail events have probability 0 or 1 (needs: `borel-cantelli`)

## Probability
- [~] `truncation-bounds` — variance bounds for truncated variables (needs: none; in flight: #1234)
- [ ] `strong-law` — the strong law (needs: `borel-cantelli`, `truncation-bounds`)
```

- The marker comment is required. A `## ` heading that is a roadmap directory
  name opens that area; items under any other heading are not targets.
- An item is `- [ ]` open, `- [~]` in flight or `- [x]` done, then a slug in
  backticks, unique in the file, then ` — ` and a description. The slug names
  the claim and the pull request's marker, so keep it once work has started.
- A trailing parenthesis holds `key: value` clauses separated by `;`, with no
  parentheses inside. `needs:` lists slugs from any area, or `none`;
  `in flight: #N` and `partial: #N` name pull requests; other clauses go to the
  agent as written.

TauCetiWorker's
[target-lists.md](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/target-lists.md)
has the full format, how workers overlay the marks with open and merged pull
requests, and how they choose. Fleet's side is one command
([Target lists](../README.md#target-lists)):

```sh
tauceti-fleet targets big-server --file goal.md                       # transfer this file
tauceti-fleet targets big-server --file /srv/lists/goal.md --on-host  # use a file on the host
tauceti-fleet targets big-server                                      # the list and its counts
tauceti-fleet targets big-server --audit                              # compare with the host's files; --apply writes the corrections
tauceti-fleet targets big-server --clear
```

Workers read the file at the start of every roadmap round, and take no
[roadmap focus](../README.md#roadmap-focus) while they have a list. `--audit`
reports where the file disagrees with files on the host, without asking GitHub,
such as a `[~]` item whose `in flight:` pull request has closed. For one that
landed it proposes `[x]` unless the item's own `partial:` clause names that pull
request: the host cannot read a pull request's marker, so it cannot tell a
landing that completed the item from one that did only part of it. Check each
such line before `--apply`, and add `partial: #N` to an item that is not
finished, or `--apply` marks it done and the items that need it become
eligible. `--apply` writes into the file the workers read. It keeps an edit
someone made to the file while the audit ran, and writes nothing if that edit
touched a line it corrects; audit again then. For a transferred list the file
is the host's copy, so make the same edits in your own file. The
worker's `curate` phase corrects the file from GitHub instead, reading each pull
request's marker, on a worker whose `only` names it; slot groups do not name
that phase.

**Sharing a list.** Several installs on one machine each name the same absolute
path with `--on-host`; the file has to be readable by every fleet account, and
writable by one that runs `--audit --apply`. Across machines, transfer the same
file to each host, or keep a checkout of the list's repository on each and name
it with `--on-host`. A path that is a symlink is saved as the file it names when
the list is set; after repointing the link, set the list again. Workers that
share a claim namespace keep off each other's items through the claim each takes
on its item, and every worker sees an item as in flight or done from the pull
request that carries its marker, whichever host opened it. Each host's `status` counts only the file's marks and its own rounds.

### Authoring outside the list

A worker whose list offers it nothing ends its round. The profile key
`"outside_list_cap": {"floor": 0, "ceiling": 40, "start": 8}` lets such a round
author as a worker without a list does, and has the controller pace it
([Authoring outside the list](../README.md#authoring-outside-the-list)).

The cap is the number of the account's open pull requests outside the list
below which a worker may open another. Every 15 minutes the controller projects
the weekly usage window to its end, or to the end of the current schedule slot
if that comes first, reserves what list work needs, and steps the cap by a
tenth: up while outside-list work spends under plan, down while it spends over.
List work is served first and the window is spent when it ends. `ceiling` is
required. Set `start` near the number of such pull requests already open: a cap
below that count admits nothing until it has stepped past it. The key is
accepted on a subscription profile and acts once a target list is set.

### Progress reports

A group whose `roles` include `progress` can carry the worker's report settings:

```json
{"count": 1, "agent": "codex", "roles": ["progress"],
 "progress_strategy": "threshold", "progress_max_open": 3, "progress_skip": ["Sandbox"]}
```

`threshold` plans per roadmap on the worker's own timer, where the unset
strategy follows the project-wide cadence. Under it the worker plans no report
while `progress_max_open` of the account's reports are open (2 unless set).
`progress_skip` names roadmap directories never reported on. On a running host
the three follow the roles as `KEY=VALUE`, in a command that names every group,
since the groups given replace the saved list:

```sh
tauceti-fleet slots big-server --group 4:auto --group 4:claude:review \
  --group 1:codex:progress:progress_strategy=threshold:progress_skip=Sandbox,Drafts
```

The profile keys `progress_repo` (`OWNER/NAME`) and `progress_ref` (a full
commit id) choose the TauCetiProgress every worker runs; `threshold` needs a
revision whose `plan` has the strategy. Several installs acting as one account
may each run a progress slot, since the limit counts the account's open
reports, whoever opened them. The worker does nothing to a report once it is
open, so one that never lands holds its place until someone closes it.

### Incidents, attention and decide

| Profile key | Effect | Default |
| --- | --- | --- |
| `incidents` | Workers record each `rebase`, `fix` or `fix-ci` round that ended with nothing on GitHub (`declined`) and each stage that used up its attempts on a pull request head (`budget-spent`). Turned off with `false`, not by removing the key | off |
| `hold_declined` | A stage stays off a head that a `declined` record names until the head moves or the record is deleted | on with `incidents` |
| `decide_close` | Lets the worker's decide phase close one of the account's own pull requests that merged ones contain; needs `incidents` | off |
| `decide_max_closes_per_day` | 0 to 100 such closures a day; needs `incidents` | the worker's limit of 3 |

The records are what [`attention`](#day-to-day) lists
([Attention](../README.md#attention)). Fleet does not start the decide phase. It
runs on workers whose phases name `decide`, and where its ruling records exist
`attention` shows the newest ruling under each item
([Rulings from the decide phase](../README.md#rulings-from-the-decide-phase)).

### Several installs on one machine

Several Unix accounts can each run an install on one machine
([Several installs on one machine](../README.md#several-installs-on-one-machine)).
Each controller measures the whole machine, so name one directory in every
account's `host.json`, as `{"shared_host_dir": "/srv/tauceti-fleet-shared"}`.
The installs then divide spare RAM among their worker pools in proportion to
their worker ceilings, and a CI job in one raises the worker reserve in all.
The directory must exist, be writable by every fleet account, carry the sticky
bit, sit on local storage and be used for nothing else. Worker ceilings are not
divided: choose `workers --max` values that fit the machine together. Installs
that act as one GitHub account can also count repair attempts once: give each
profile the same `TAUCETI_SHARED_ATTEMPTS_DIR` in `env`, a directory their
common group can write (`install -d -m 2770 -g GROUP DIR`).

### Status export

`tauceti-fleet status-export big-server --dir /srv/fleet-status` has the
controller write the host's `status --json` object to `DIR/<machine_id>.json`,
mode 0644, at most every 30 seconds, for programs run by other accounts on the
machine ([Status export](../README.md#status-export)). Several fleet accounts
can export into one directory. Use a different directory from
`shared_host_dir`, which writes files of the same name. The object holds no
credential contents but does hold spending, the GitHub account name and paths
under the fleet account's home.

## Day to day

| Command | Shows or does |
| --- | --- |
| `status`, `status HOST`, `status -f` | The fleet or one host, once or live ([Status](../README.md#status-and-cost-estimates)) |
| `status --workers` | One row per worker: state, stage, the pull request or target it holds, agent, time in the round, last log line; then the workers' latest pull request events |
| `attention [HOST]` | What is waiting for a person; `--lookup` first asks each record's own forge about its pull request: GitHub with the host's credential, a Forgejo instance anonymously, so a private Forgejo repository cannot be looked up |
| `attention HOST --ack PR`, `--ack all` | Archive the incident records for one pull request, or all of them. A bare number names a pull request of the workers' own GitHub repository; any other, a Forgejo one included, takes the `ack id` its item shows, such as `git.example.org/OWNER/REPO#412` |
| `attention HOST --show-roadmap PR`, `--file-roadmap PR` | Print a roadmap change the decide phase drafted, or copy it to `./roadmap-PR.md` to file by hand |
| `clear-halt HOST [WORKER]` | Clear an identity halt and restart the worker, or every halted worker |
| `targets HOST`, `schedule HOST`, `slots HOST` | The list's counts per area; the saved schedule; the groups and how many of each are admitted |
| `workers HOST --max N` | The admission ceiling; `0` pauses new rounds |

`status` gains these lines on a subscription host:

| Line | Shown |
| --- | --- |
| `Claude usage: 5h 62% left, resets in 2h00m; 7d 41% left, resets in 3d08h (read 10m ago)` | One per provider, from the reading the workers' pacer last cached. Fleet sends no usage request, so the line ages while no worker is checking |
| `Weekly schedule (host local time): outside every slot; next slot starts Mon 23:00 CET (in 2d 3h)` | With a schedule |
| `Target list: 12 items in 3 areas: 4 done, 2 in flight, 3 eligible, 3 blocked` | With a list, then the items that rounds in progress hold |
| `Outside-list cap: 14 (bounds 0-40); 91% of the weekly window projected spent when the slot ends in 14h00m; ...` | With `outside_list_cap`, then the last step and its reason |
| `Needs you: 4 items (1 budget-spent, 2 declined, 1 halted)` | While anything is listed, with the `attention` command |
| `Rulings in the last 24h: 1 close, 2 escalate, 3 wait` | Where decide rulings exist |

**Attention.** The list has three sources: halted workers, a Claude credential
that is due, lapsed or missing, and incident records. The first two leave the
list once the halt is cleared or the credential replaced. An incident item
leaves it when its pull request merges, closes or moves to a new head. The
controller archives the record of a merged pull request into
`~/.local/state/tauceti-fleet/incidents/acked/`, and `--ack` archives one by
hand. The record of a closed pull request or a moved head is only hidden: it
stays live in `incidents/`, since the pull request can come back to that head,
and `--lookup` asks about it again. A `declined` record, live or archived, still
holds its head; to let the stage try that head again, delete the file that
exists, under `incidents/` or `incidents/acked/` on the host. When `acked/` already holds a record of the same name, `--ack`
keeps the earlier one as `<name>.1`, `<name>.2` and so on, which the worker does
not read, so only the file under the record's own name holds the head.

**Halts.** A worker halts when a forge says its credential belongs to another
account (`identity-mismatch`), rejects it (`invalid-credentials`) or refuses the
account (`account-halt`); the worker records the reason with its forge, as in
`github:identity-mismatch` or `forgejo:invalid-credentials`. `status` shows it as
`halted (<forge> <reason>)`, for example `halted (GitHub identity-mismatch)`,
and the worker manager does not start it again. It keeps its admission place,
so the host runs one fewer round until `clear-halt`. Put the account right
first: a worker whose account is still wrong halts again. A Forgejo halt needs
only `clear-halt` once the token file the worker names holds the new token,
since the worker reads that file at every check. A restarted worker takes its
GitHub token from the worker manager, which read the token file when it
started, so after a new GitHub token reaches the host `clear-halt` refuses for
a GitHub halt until the manager has been restarted with the command it names
(`systemctl --user restart tauceti-fleet-workers.service` on Linux). That
restart leaves workers in their own scopes running, with the token they started
with; a worker whose status shows degraded isolation runs inside the manager's
service, and its round ends with it.

## Hosts

### Bubble under a restricted Incus project

`"sandbox": "bubble"` runs code-writing and review rounds in
[Bubble](https://github.com/kim-em/bubble) containers, and `doctor` then also
checks for `bubble` and `incus`. The containers run under the Incus daemon,
outside the worker slice, so its memory ceiling and CPU weight do not apply to
them and host headroom at admission is their only bound. `doctor` warns of
this: set limits on the Incus project the containers use, or choose
`workers --max` for the host's free memory. Incus need not make the fleet account
root-equivalent: a member of group `incus`, as opposed to `incus-admin`, is
served by `incus-user` and gets a restricted project of its own with its own
bridge, which other fleet accounts on the machine cannot see into. What has to
be in place:

| What | Who | Otherwise |
| --- | --- | --- |
| The account is in group `incus`, and lingering is on (`loginctl enable-linger USER`) | administrator | No project; services stop at logout. A user manager that is already running keeps its old groups until restarted |
| The host firewall admits the project's bridge, which exists once the account has run `incus` once | administrator | Containers get no address, and rounds wait for the network until they time out |
| `/etc/subuid` and `/etc/subgid` delegate the account's own uid and gid to root (`root:UID:1`, `root:GID:1`), and Incus was restarted since | administrator | Incus refuses to map the account onto the container's `user`, and files mounted from the host belong to nobody inside |
| A Bubble release that accepts `--github-account`, uses the project's bridge and maps the host user onto the container's `user` | fleet account | Without the option the worker's preflight ends every round, since Fleet names an account for each worker; otherwise as the two rows above |
| Everything the worker mounts lies under the account's home: Fleet's configuration, state and data roots (the defaults do), `TAUCETI_GITHUB_SHARED_DIR`, a `TAUCETI_REVIEW_ENGINE_DIR` checkout, `ELAN_HOME` with `TAUCETI_BUBBLE_TOOLCHAINS`, and `TAUCETI_VERIFY_CACHE_DIR` with `TAUCETI_VERIFY_HELPER=1` | operator | Incus rejects the disk device and the round ends |

Restart Incus only while the host is drained: the restart rebuilds the bridge
under any round in progress. A container cannot download a Lean toolchain its
image lacks, so set `TAUCETI_BUBBLE_TOOLCHAINS` to `1` when `main` may move to a
newer one. TauCetiWorker's
[sandbox notes](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/sandbox.md#restricted-incus-projects)
are the reference.

### macOS

A Mac takes the workers role and nothing else
([macOS hosts](../README.md#macos-hosts)); add it with `--no-ci`. Its limits:

- launchd agents stand in for the systemd units and run only while the account
  is logged in at the Mac's desktop. With automatic login off, the fleet waits
  for a login after every restart.
- There are no CPU or memory limits, so admission counts each worker as 4 GiB,
  and rounds run in the `host` sandbox.
- The agents inherit the non-interactive PATH of the SSH session that installed
  them (zsh reads `~/.zshenv`), which must provide Python 3.11+, `uv` and the
  worker tools `doctor` lists.
- Claude Code keeps an ordinary sign-in in the login Keychain, also with
  `CLAUDE_CONFIG_DIR` set, and writes no `.credentials.json`. With the two
  credential keys of the [profile](#a-minimal-profile), the workers run their
  agents from the token file, renew the usage sign-in as a file, and read no
  Claude item from the Keychain. That file has to be made on Linux: sign in
  there as in [What it needs](#what-it-needs) and transfer it with `add`, both
  the first time and when it falls due. `tauceti-fleet claude-usage-login`,
  which `doctor` names, cannot sign a Mac host in: it reports that the sign-in
  left no login and changes nothing. `add` from a Mac finds no file to
  transfer. Nobody may sign in to `claude` with the token's directory as
  `CLAUDE_CONFIG_DIR`: Claude Code prefers a Keychain item for that directory to
  the file. Codex keeps a file and is renewed as on Linux.

## Troubleshooting

| Symptom | Cause | What to do |
| --- | --- | --- |
| Every worker `waiting-quota`; the usage line shows a window at 0% left | The subscription's session or weekly allowance is spent | Nothing: workers resume when the window resets, and `auto` slots keep working while the other provider has room |
| Workers wait with `claude usage HTTP 403` | The agents run on a setup-token and there is no usage sign-in to read the windows with | Add `claude_usage_credentials_file` to the profile and repeat `add` |
| Workers wait with `claude usage HTTP 401 (access token expired or rejected; log in again)` | The usage sign-in stopped renewing, about four weeks after it was made, or another holder of the same file spent its refresh token | Sign in again with the command `doctor` or `attention` prints, on Linux ([macOS](#macos)), and keep that directory for one host's fleet alone |
| `add` is refused: the usage login `was transferred to` another host | The profile's usage file holds a login already sent to that host, or reserved for it by an `add` whose send failed | Give this host its own profile and usage sign-in ([A minimal profile](#a-minimal-profile)) |
| `codex token expired; refresh left to the operator` | The Codex access token lapsed and the workers do not renew it: renewal is off, or the credential store does not resolve to `file`; or, where the two `config.toml` files do not set the store to `file` explicitly, the login's plan is outside the worker's list and not in `TAUCETI_CODEX_REFRESH_PLANS`, or a `config_server` is enrolled under `/etc/codex` | Run `codex` as the host account, signing in again if it asks. Then put right whichever condition of [Codex renewal](#what-it-needs) fails, preferably by setting the store in both `config.toml` files, or the next expiry stops the workers again |
| A worker's log repeats `keeps Claude creds first copied from ...` | The slot first ran before the host had `claude_token_file` and still uses the sign-in it copied then | With the worker stopped, remove `.credentials.json` and `.tauceti-creds-source` from the `.claude` directory of that worker's isolated home; its next start copies the token |
| `Starting new work: paused; outside the weekly schedule` in a slot you expected | Slot times are host local time unless saved with `--timezone`; a saved schedule the controller cannot read admits nothing | `schedule HOST` shows what is saved; `--clear` or a new set of slots replaces it |
| The target list line shows `0 eligible` | Every open item waits on another: often a `[~]` item whose pull request closed unmerged, or a landing too old for the workers' view of recently closed pull requests | `targets HOST --audit`; tick landed items `[x]` in the file, and give one that landed only in part `partial: #N` ([Target lists](#target-lists)). `outside_list_cap` keeps workers authoring meanwhile |
| The list has eligible items and no roadmap round starts | The account is at its open pull request limit, the profile's `max_open_prs` (8 unless set); `status` shows the authoring pressure | Nothing: repair and review continue, and authoring resumes as pull requests land |
| `Outside-list cap: not paced; ...` | No target list is set, or the installed TauCetiWorker does not report `targets_fallback` | Set a list, or `update` the host to a TauCetiWorker release that does |
| `targets --file` is refused | The installed TauCetiWorker does not report `roadmap_targets`, and a worker manager that does not know the key loads no worker at all | Install a TauCetiWorker release that does (`update`), and have the worker manager running on it before setting the list |
| Progress reports stop under `threshold` | `progress_max_open` reports of the account are open and none is landing | Close the stuck ones; the worker's suppression reason names them |
| Rounds in progress ended when a setting changed | The worker manager restarts a worker whose definition changes: setting, moving or clearing the list, or a progress key on a group | Nothing to repair. New contents at the same list path, and a slot group's agent or phases, end no round |
| A push is refused with a message about workflows | The account's token lacks the `workflow` scope | Give the credential the profile uses the scope. With `github_token_file`, `add` sends that file and never asks `gh`, so replace the file with a token that has the scope. Otherwise make the profile's `github_account` the active `gh` account (`gh auth switch -h github.com -u ACCOUNT`) and run `gh auth refresh -h github.com -s workflow`. Then repeat `add`. Workers hold the token they started with and the worker manager the one it read when it started, so restart both on the host. On Linux: `systemctl --user stop tauceti-fleet-workers.slice`, then `systemctl --user start tauceti-fleet-workers.service`. On a Mac, where the manager stops its workers when it stops: `launchctl bootout gui/$(id -u)/org.tauceti.fleet.workers`, then `launchctl bootstrap gui/$(id -u) ~/.config/tauceti-fleet/launchd/org.tauceti.fleet.workers.plist`. Either ends every round in progress |
| Every Bubble round ends at once, or waits for the network | A row of the [Bubble table](#bubble-under-a-restricted-incus-project) is missing: a mount outside the account's home, the bridge behind the firewall, or the uid and gid not delegated | Fix that row |
| `status` shows `Needs you: unavailable` | The fleet account cannot list its incident directory, `~/.local/state/tauceti-fleet/incidents/` | `attention HOST` prints the error; repair the directory's ownership or permissions |
| `status` or `doctor` reports the shared host directory unusable | The directory is missing or this account cannot write to it; until repaired the install sizes itself alone | Create it or fix its permissions |
