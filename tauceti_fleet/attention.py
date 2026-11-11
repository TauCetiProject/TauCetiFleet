"""What on a host is waiting for a person, gathered from a small set of sources.

A source lists items and may acknowledge one. The incident source reads the worker's incident
records (its docs/incidents.md): Fleet names the directory, the workers write into it. The other
two read what the host already reports: workers stopped by an identity halt, and Claude
credentials that are due or have lapsed.
"""

import json
import os
import re
import sqlite3
import time
from calendar import timegm
from contextlib import closing

from . import activity, claude_credentials, github
from .store import FleetError, atomic, host_config, read, state_dir

SCHEMA = "tauceti-incident/v1"
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
MAX_LOOKUPS = 100


def enabled(config):
    return config.get("incidents") is True


def validate(config):
    for name in ("incidents", "hold_declined"):
        if config.get(name) is not None and type(config[name]) is not bool:
            raise FleetError(f"{name} must be true or false")


def incidents_dir():
    return state_dir() / "incidents"


def worker_env(config):
    if not enabled(config):
        return {}
    env = {"TAUCETI_INCIDENTS_DIR": str(incidents_dir())}
    if config.get("hold_declined") is not False:
        env["TAUCETI_HOLD_DECLINED"] = "1"
    return env


def printable(value, limit):
    return "".join(c if c.isprintable() or c == "\n" else " " for c in str(value or "")[:limit])


def epoch(stamp):
    try:
        return timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def notes_path(record):
    return state_dir() / "attention/incidents" / record


def notes(record):
    """Fleet's own file beside a record. The worker rewrites a record whole, so nothing is added to it."""
    try:
        value = read(notes_path(record), {})
    except FleetError:
        return {}
    return value.get("prs", {}) if isinstance(value, dict) and isinstance(value.get("prs"), dict) else {}


def prepared_prs(keys):
    """Pull request state the worker's activity preparation already holds on this host."""
    path = activity.activity_root() / "activity.sqlite"
    found = {}
    if not keys or not path.exists():
        return found
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            for repository, number in keys:
                row = db.execute(
                    "SELECT state,head,observed,landed FROM prs WHERE repo=? AND number=?", (repository, number)
                ).fetchone()
                if row and row[0] in ("OPEN", "CLOSED", "MERGED") and isinstance(row[2], int | float):
                    found[(repository, number)] = dict(
                        state="MERGED" if row[3] is not None else row[0], head=row[1], observed=row[2]
                    )
    except sqlite3.Error:
        return {}
    return found


def overtaken(record, observation):
    """Why a record no longer needs anyone, from an observation made after it was written."""
    if not observation:
        return None
    if observation["state"] in ("MERGED", "CLOSED"):
        return observation["state"].lower()
    if observation.get("head") and record.get("head") and observation["head"] != record["head"]:
        return "new head"
    return None


def live_records():
    try:
        paths = sorted(incidents_dir().glob("*.json"))
    except OSError:
        return []
    found = []
    for path in paths:
        try:
            value = json.loads(path.read_text()) if path.stat().st_size <= 256 * 1024 else None
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            value = None
        readable = (isinstance(value, dict) and value.get("schema") == SCHEMA
                    and all(isinstance(value.get(k), str) and value[k] for k in ("kind", "key")))
        found.append((path.name, value if readable else None))
    return found


def incident_items(config):
    if not enabled(config):
        return []
    records = live_records()

    def pull_request(record):
        repository, number = record.get("repository"), record.get("pr")
        if type(number) is int and number > 0 and isinstance(repository, str) and REPOSITORY.fullmatch(repository):
            return repository, number
        return None

    prepared = prepared_prs({pull_request(r) for _, r in records if r and pull_request(r)})
    items = []
    for name, record in records:
        if record is None:
            items.append(dict(source="incident", id=name, kind="unreadable", record=name,
                              text=f"{incidents_dir() / name} is not an incident record this release reads"))
            continue
        target = pull_request(record)
        item = dict(
            source="incident",
            id=str(target[1]) if target else printable(record["key"], 100),
            kind=printable(record["kind"], 40),
            record=name,
            at=printable(record.get("last_at"), 20),
            first_at=printable(record.get("first_at"), 20),
            count=record["count"] if type(record.get("count")) is int else 1,
            worker=printable(record.get("worker"), 100),
            stage=printable(record.get("stage"), 40),
            head=printable(record.get("head"), 64),
            text=printable(record.get("summary") or record.get("detail") or record.get("reason"), 4000),
            transcript=printable(record.get("transcript"), 1000),
        )
        if target:
            cached = notes(name)
            written = epoch(record.get("last_at")) or float("inf")
            seen = [o for o in (prepared.get(target), cached.get(f"{target[0]}#{target[1]}"))
                    if isinstance(o, dict) and o.get("state") and isinstance(o.get("observed"), int | float)
                    and o["observed"] >= written]
            observation = max(seen, key=lambda o: o["observed"], default=None)
            item.update(repository=target[0], pr=target[1], cleared=overtaken(record, observation),
                        observed_at=observation["observed"] if observation else None, named=[])
            for relation in ("subsumed_by", "mentions"):
                numbers = record.get(relation) if isinstance(record.get(relation), list) else []
                for number in [n for n in numbers if type(n) is int and n > 0][:20]:
                    known = cached.get(f"{target[0]}#{number}")
                    item["named"].append(dict(known if isinstance(known, dict) else {}, pr=number, relation=relation))
        items.append(item)
    return items


def archive(name):
    """Move the live record to `acked/<name>`, the one name the worker's hold reads there, and return
    where an earlier record of that name was kept, as `acked/<name>.<n>`, which the worker does not read."""
    source, acked = incidents_dir() / name, incidents_dir() / "acked"
    target = acked / name
    acked.mkdir(mode=0o700, exist_ok=True)
    aside = None
    if target.exists() and source.exists():
        n = 1
        while True:
            try:
                os.link(target, acked / f"{name}.{n}")
                aside = acked / f"{name}.{n}"
                break
            except FileExistsError:
                n += 1
    try:
        os.replace(source, target)
    except OSError:
        if aside is not None:
            aside.unlink(missing_ok=True)
        raise
    return aside


def incident_ack(config, item):
    archive(item["record"])


def incident_sweep(config, items):
    """Archive the records whose pull request has merged, marked as overtaken. A closed pull request
    can reopen and a moved one can return to the record's head, so those stay live for `--lookup`."""
    for item in items:
        if item.get("cleared") != "merged":
            continue
        source, target = incidents_dir() / item["record"], incidents_dir() / "acked" / item["record"]
        try:
            aside = archive(item["record"])
        except OSError:
            continue
        try:
            record = json.loads(target.read_text())
            unchanged = (printable(record.get("last_at"), 20), printable(record.get("head"), 64)) == (item["at"], item["head"])
        except (OSError, ValueError):
            record, unchanged = None, False
        if unchanged:
            record["overtaken"] = dict(at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), by=item["cleared"])
            try:
                atomic(target, record)
            except (OSError, ValueError):
                pass
            notes_path(item["record"]).unlink(missing_ok=True)
            continue
        # The worker rewrote the record for a later round since it was judged: put it back unless an
        # even later write has taken the live name, which supersedes it as the worker's own replace would.
        try:
            try:
                os.link(target, source)
            except FileExistsError:
                pass
            if aside is not None:
                os.replace(aside, target)
            else:
                target.unlink()
        except OSError:
            continue


def halt_items(config):
    from . import host  # host imports this module

    items = []
    for worker in host.worker_statuses():
        if worker.get("state") == "halted":
            wid = printable(worker["id"], 100)
            reason = ": ".join(str(v) for v in (worker.get("halt_reason"), worker.get("failure_reason")) if v)
            items.append(dict(
                source="halt", id=wid, kind="halted", command=["clear-halt", wid],
                text=printable(reason or "reason not recorded", 1000)
                + "\nThe worker stays stopped; clear the halt once the account is in order.",
            ))
    return items


def credential_items(config):
    # A token without a usage login is a standing doctor note about the profile, never something due.
    return [
        dict(source="credential", id=entry["name"], kind="credential", text=entry["evidence"], next=entry["remedy"])
        for entry in claude_credentials.report(config, wait=False)
        if entry["status"] != "ok" and (entry["name"] != "claude-usage-login" or config.get(claude_credentials.USAGE))
    ]


def ask(config, repository, number, *, named):
    """One GitHub read. The issue endpoint also answers for a number that is an issue, which an agent's `#N` may be."""
    try:
        data = github.request(f"repos/{repository}/{'issues' if named else 'pulls'}/{number}",
                              token_file=config.get("github_token_file"))
        merged = (data.get("pull_request") or {}).get("merged_at") if named else data.get("merged_at")
        value = dict(state="MERGED" if merged else str(data["state"]).upper(), merged_at=merged,
                     title=printable(data.get("title"), 200).replace("\n", " "),
                     author=printable((data.get("user") or {}).get("login"), 60))
        if not named:
            value["head"] = data["head"]["sha"]
    except FleetError as error:
        if not named or getattr(error, "status", None) != 404:
            raise
        value = dict(state="NOT FOUND")
    except (KeyError, TypeError, AttributeError):
        raise FleetError(f"GitHub returned an unexpected answer for {repository}#{number}") from None
    return dict(value, observed=time.time())


def incident_lookup(config, items):
    """Ask GitHub about each listed item's pull request and the ones it names; answers go to Fleet's notes."""
    asked, answers, error = 0, {}, None
    deadline = time.monotonic() + 40

    def open_named(item):
        """A cleared item's own pull request may reopen at the record's head; what it names matters only once it does."""
        if item.get("cleared"):
            return []
        return [n for n in item["named"] if n.get("state") in (None, "OPEN") and n["pr"] != item["pr"]]

    def checked(item):
        """When this item's pull requests were last asked about, so a bounded call starts where the last one stopped."""
        own = notes(item["record"]).get(f"{item['repository']}#{item['pr']}")
        times = [own.get("observed") if isinstance(own, dict) else None] + [n.get("observed") for n in open_named(item)]
        return min(t if isinstance(t, int | float) else 0 for t in times)

    items = [i for i in items if "pr" in i and i.get("cleared") != "merged"]
    for item in sorted(items, key=checked):
        if error:
            break
        cached = notes(item["record"])
        fresh = {}
        wanted = [(item["pr"], False)] + [(n["pr"], True) for n in open_named(item)]
        for number, named in wanted:
            key = (item["repository"], number, named)
            if key not in answers:
                if asked >= MAX_LOOKUPS or time.monotonic() >= deadline:
                    error = f"stopped after {asked} requests; run --lookup again for the rest"
                    break
                try:
                    asked += 1
                    answers[key] = ask(config, item["repository"], number, named=named)
                except FleetError as exc:
                    error = str(exc)
                    break
            fresh[f"{item['repository']}#{number}"] = answers[key]
        if fresh:
            atomic(notes_path(item["record"]), dict(version=1, prs={**cached, **fresh}))
    return dict(asked=asked, error=error)


# name -> how to list its items, and optionally how to acknowledge one, how to ask GitHub about them,
# and how the controller puts away the ones that have cleared.
# An item is a dict with `source`, `id` (what --ack takes), `kind` and `text`; `cleared` names what
# overtook it. `next` is the step that settles it, or `command` the tauceti-fleet subcommand and the
# arguments that follow the host. A source whose items only clear by themselves has no `ack`.
SOURCES = {
    "halt": dict(items=halt_items),
    "credential": dict(items=credential_items),
    "incident": dict(items=incident_items, ack=incident_ack, lookup=incident_lookup, sweep=incident_sweep),
}


def gather(config):
    items = [item for source in SOURCES.values() for item in source["items"](config)]
    return sorted(items, key=lambda item: item.get("at") or "")


def listing(config):
    items = gather(config)
    return dict(
        incidents=enabled(config),
        hold_declined="TAUCETI_HOLD_DECLINED" in worker_env(config),
        items=[i for i in items if not i.get("cleared")],
        cleared=[{k: i.get(k) for k in ("source", "id", "kind", "record", "cleared")} for i in items if i.get("cleared")],
    )


def acknowledge(config, which):
    done = []
    for item in gather(config):
        ack = SOURCES[item["source"]].get("ack")
        if ack and which in ("all", item["id"]):
            try:
                ack(config, item)
            except FileNotFoundError:
                continue
            done.append({k: item.get(k) for k in ("source", "id", "kind", "record")})
    return done


def summary_path():
    return state_dir() / "attention/summary.json"


def summarize(value):
    kinds = {}
    for item in value["items"]:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    return dict(at=time.time(), count=len(value["items"]), kinds=kinds)


def publish(config, value):
    """Keep the summary only where there is something to show, so `status` on other hosts is unchanged."""
    try:
        if enabled(config) or value.get("count") or "error" in value:
            atomic(summary_path(), value)
        else:
            summary_path().unlink(missing_ok=True)
    except OSError:
        pass


def prepare(config):
    """The controller's minute: put away what has cleared, then write the count `status` shows.

    Makes no GitHub request. A failure is reported in `status` and never reaches the controller.
    """
    try:
        for source in SOURCES.values():
            if source.get("sweep"):
                source["sweep"](config, [i for i in source["items"](config) if i.get("cleared")])
        value = summarize(listing(config))
    except Exception as exc:
        value = dict(at=time.time(), error=f"{type(exc).__name__}: {exc}"[:200])
    publish(config, value)


def prepared():
    try:
        return read(summary_path(), {})
    except FleetError as exc:
        return dict(error=str(exc))


def host_action(payload):
    config = host_config()
    result = {}
    which = payload.get("ack")
    if which is not None:
        if not isinstance(which, str) or not 0 < len(which) <= 200:
            raise FleetError("Acknowledge a pull request number, an item id, or all")
        result["acknowledged"] = acknowledge(config, which)
    if payload.get("lookup"):
        looked = dict(asked=0, error=None)
        for source in SOURCES.values():
            if source.get("lookup"):
                answer = source["lookup"](config, source["items"](config))
                looked = dict(asked=looked["asked"] + answer["asked"], error=looked["error"] or answer["error"])
        result["lookup"] = looked
    value = listing(config)
    publish(config, summarize(value))
    return dict(value, **result)


def status_lines(name, value):
    if value.get("error"):
        return ["Needs you: unavailable; " + value["error"]]
    count = value.get("count", 0)
    if not count:
        return []
    kinds = ", ".join(f"{n} {kind}" for kind, n in sorted(value.get("kinds", {}).items()))
    age = time.time() - value.get("at", 0)
    return [
        f"Needs you: {count} item{'s' if count != 1 else ''} ({kinds})" + (f", counted {age / 60:.0f}m ago" if age > 300 else ""),
        f"  tauceti-fleet attention {name}",
    ]


def display(name, value):
    if "acknowledged" in value:
        done = value["acknowledged"]
        print(f"Acknowledged {len(done)} item{'s' if len(done) != 1 else ''}"
              + (": " + ", ".join(d.get("record") or d["id"] for d in done) if done else ""))
    looked = value.get("lookup")
    if looked:
        print(f"Asked GitHub about {looked['asked']} pull request{'s' if looked['asked'] != 1 else ''}"
              + (f"; {looked['error']}" if looked.get("error") else ""))
    if "acknowledged" in value or looked:
        print()
    for item in value["items"]:
        facts = [item["kind"], item.get("stage"), item.get("worker"), item.get("at"),
                 f"seen {item['count']}x" if item.get("count", 1) > 1 else None]
        subject = item["id"]
        if "pr" in item:
            subject = f"PR #{item['pr']}  https://github.com/{item['repository']}/pull/{item['pr']}"
        print(f"== {subject}  ({' · '.join(f for f in facts if f)})")
        for line in item.get("text", "").splitlines():
            print("   " + line)
        for relation, label in (("subsumed_by", "subsumed by"), ("mentions", "mentions")):
            for named in (n for n in item.get("named", []) if n["relation"] == relation):
                if named.get("state"):
                    detail = "  ".join(str(v) for v in (
                        named["state"], (named.get("merged_at") or "")[:10], named.get("title"),
                        f"({named['author']})" if named.get("author") else "") if v)
                else:
                    detail = f"https://github.com/{item['repository']}/pull/{named['pr']}  (--lookup for state and title)"
                print(f"   {label}: #{named['pr']}  {detail}")
        if item.get("transcript"):
            print("   transcript: " + item["transcript"])
        command = item.get("command")
        step = item.get("next") or (command and " ".join(["tauceti-fleet", command[0], name, *command[1:]]))
        if step:
            print("   Next step: " + step)
        print()
    if not value["items"]:
        print("Nothing needs you.")
    if value.get("cleared"):
        print(f"Not listed: {len(value['cleared'])} record{'s' if len(value['cleared']) != 1 else ''} "
              "whose pull request merged, closed or moved to a new head.")
    unseen = sum("pr" in item and item.get("observed_at") is None for item in value["items"])
    if unseen and not looked:
        print(f"This host has no pull request state newer than the record for {unseen} of these; --lookup asks GitHub.")
    if any(SOURCES.get(item["source"], {}).get("ack") for item in value["items"]):
        print(f"Acknowledge with: tauceti-fleet attention {name} --ack <PR number> | all")
    if not value.get("incidents"):
        print('Incident records are off for this host; the profile key "incidents" turns them on.')
