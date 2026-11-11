"""What on a host is waiting for a person, gathered from a small set of sources.

A source lists items and may acknowledge one. The first source is the worker's incident
records (its docs/incidents.md): Fleet names the directory, the workers write into it.
"""

import json
import os
import re
import sqlite3
import time
from calendar import timegm
from contextlib import closing

from . import activity, forgejo, github
from .store import FleetError, atomic, host_config, read, state_dir

SCHEMA = "tauceti-incident/v1"
GITHUB = "https://github.com"
# Answers about one repository or number, not the forge: a request that gets one is not repeated in that call
# but the rest of its forge still is. GitHub's 403 is also its rate limit, so it stops the forge.
UNREADABLE_GITHUB = (404, 410, 451)
UNREADABLE_FORGEJO = (403, 404, 410, 451)
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
    """Path.glob hides a directory it cannot read, which would look like no records at all."""
    try:
        with os.scandir(incidents_dir()) as entries:
            paths = sorted(incidents_dir() / e.name for e in entries if e.name.endswith(".json"))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise FleetError(f"{incidents_dir()} could not be listed: {exc.strerror or exc}") from None
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


def target_of(record):
    """The pull request a record is about, on its own forge; None for a forge this release does not read.
    A record without `forge` predates the field and is GitHub's. Only a record of the worker's own GitHub
    repository has a key ending in its number, and only that one is acknowledged by the bare number."""
    repository, number, forge = record.get("repository"), record.get("pr"), record.get("forge")
    if not (type(number) is int and number > 0 and isinstance(repository, str) and REPOSITORY.fullmatch(repository)):
        return None
    if forge is None or isinstance(forge, dict) and forge.get("kind") == "github" and forge.get("url") == GITHUB:
        kind, origin = "github", GITHUB
    elif isinstance(forge, dict) and forge.get("kind") == "forgejo" and forgejo.origin(forge.get("url")):
        kind, origin = "forgejo", forge["url"]
    else:
        return None
    if kind == "github" and record["key"].endswith(f"-{number}"):
        ident = str(number)
    else:
        ident = f"{origin.split('://', 1)[1] + '/' if kind != 'github' else ''}{repository}#{number}"
    return dict(forge=kind, origin=origin, repository=repository, pr=number, id=ident)


def pr_url(item, number):
    if item["forge"] == "github":
        return f"{GITHUB}/{item['repository']}/pull/{number}"
    return f"{item['origin']}/{item['repository']}/pulls/{number}"


def incident_items(config):
    if not enabled(config):
        return []
    records = live_records()
    # The worker's activity rows carry no forge and hold GitHub pull requests only.
    prepared = prepared_prs({(t["repository"], t["pr"]) for t in map(target_of, (r for _, r in records if r))
                             if t and t["forge"] == "github"})
    items = []
    for name, record in records:
        if record is None:
            items.append(dict(source="incident", id=name, kind="unreadable", record=name,
                              text=f"{incidents_dir() / name} is not an incident record this release reads"))
            continue
        target = target_of(record)
        item = dict(
            source="incident",
            id=target["id"] if target else printable(record["key"], 100),
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
            repository, number = target["repository"], target["pr"]
            cached = notes(name)
            # `last_at` is whole seconds, so an observation inside that second may predate the write.
            written = (epoch(record.get("last_at")) or float("inf")) + 1
            local = prepared.get((repository, number)) if target["forge"] == "github" else None
            own = cached.get(f"{repository}#{number}")
            seen = [o for o in (local, own)
                    if isinstance(o, dict) and o.get("state") in ("OPEN", "CLOSED", "MERGED")
                    and isinstance(o.get("observed"), int | float) and o["observed"] >= written]
            observation = max(seen, key=lambda o: o["observed"], default=None)
            item.update(repository=repository, pr=number, forge=target["forge"], origin=target["origin"],
                        cleared=overtaken(record, observation),
                        observed_at=observation["observed"] if observation else None, named=[])
            if isinstance(own, dict) and own.get("state") == "UNREADABLE":
                item["unreadable"] = own.get("status")
            for relation in ("subsumed_by", "mentions"):
                numbers = record.get(relation) if isinstance(record.get(relation), list) else []
                for number in [n for n in numbers if type(n) is int and n > 0][:20]:
                    known = cached.get(f"{repository}#{number}")
                    item["named"].append(dict(known if isinstance(known, dict) else {}, pr=number, relation=relation))
        items.append(item)
    return items


def incident_ack(config, item):
    """Move the live record to `acked/<name>`, the one name the worker's hold reads there. An earlier
    record of that name stays as `acked/<name>.<n>`, which the worker does not read."""
    source, acked = incidents_dir() / item["record"], incidents_dir() / "acked"
    target = acked / item["record"]
    acked.mkdir(mode=0o700, exist_ok=True)
    aside = None
    if target.exists() and source.exists():
        n = 1
        while True:
            try:
                os.link(target, acked / f"{item['record']}.{n}")
                aside = acked / f"{item['record']}.{n}"
                break
            except FileExistsError:
                n += 1
    try:
        os.replace(source, target)
    except OSError:
        if aside is not None:
            aside.unlink(missing_ok=True)
        raise


def ask(config, repository, number, *, named, forge="github", origin=GITHUB):
    """One read from the pull request's own forge, GitHub with the host's credential or Forgejo anonymously.
    The issue endpoint also answers for a number that is an issue, which an agent's `#N` may be."""
    path = f"repos/{repository}/{'issues' if named else 'pulls'}/{number}"
    try:
        if forge == "github":
            data = github.request(path, token_file=config.get("github_token_file"))
        else:
            data = forgejo.request(origin, path)
        meta = (data.get("pull_request") or {}) if named else data
        merged = meta.get("merged_at") or None
        value = dict(state="MERGED" if merged or meta.get("merged") is True else str(data["state"]).upper(),
                     merged_at=merged,
                     title=printable(data.get("title"), 200).replace("\n", " "),
                     author=printable((data.get("user") or {}).get("login"), 60))
        if not named:
            value["head"] = data["head"]["sha"]
    except FleetError as error:
        status = getattr(error, "status", None)
        if named and status == 404:
            value = dict(state="NOT FOUND")
        elif status in (UNREADABLE_GITHUB if forge == "github" else UNREADABLE_FORGEJO):
            value = dict(state="UNREADABLE", status=status)
        else:
            raise
    except (KeyError, TypeError, AttributeError):
        raise FleetError(f"{origin} returned an unexpected answer for {repository}#{number}") from None
    return dict(value, observed=time.time())


def incident_lookup(config, items):
    """Ask each listed item's forge about its pull request and the ones it names; answers go to Fleet's notes."""
    asked, answers, error = 0, {}, None
    deadline = time.monotonic() + 40

    def open_named(item):
        """A cleared item's own pull request may reopen at the record's head; what it names matters only once it does."""
        if item.get("cleared"):
            return []
        return [n for n in item["named"] if n.get("state") in (None, "OPEN", "UNREADABLE") and n["pr"] != item["pr"]]

    def observed(value):
        return value["observed"] if isinstance(value, dict) and isinstance(value.get("observed"), int | float) else 0

    # Each request is ordered by when it was last answered, so a bounded call resumes where the last one stopped,
    # inside an item as well as between items.
    requests = []
    for item in (i for i in items if "pr" in i and i.get("cleared") != "merged"):
        own = notes(item["record"]).get(f"{item['repository']}#{item['pr']}")
        requests.append((observed(own), item, item["pr"], False))
        requests += [(observed(n), item, n["pr"], True) for n in open_named(item)]
    requests.sort(key=lambda r: r[0])
    failed, unreadable = {}, []
    for _, item, number, named in requests:
        key = (item["origin"], item["repository"], number, named)
        if key in answers or item["origin"] in failed:
            continue
        if asked >= MAX_LOOKUPS or time.monotonic() >= deadline:
            error = f"stopped after {asked} requests; run --lookup again for the rest"
            break
        try:
            asked += 1
            answers[key] = ask(config, item["repository"], number, named=named,
                               forge=item["forge"], origin=item["origin"])
            if answers[key]["state"] == "UNREADABLE":
                unreadable.append(f"{item['origin']} answered HTTP {answers[key]['status']} "
                                  f"for {item['repository']}#{number}")
        except FleetError as exc:
            # One forge that fails leaves the other's requests to go ahead.
            failed[item["origin"]] = str(exc)
    error = "; ".join([*failed.values(), *unreadable, *([error] if error else [])]) or None
    fresh = {}
    for _, item, number, named in requests:
        if (key := (item["origin"], item["repository"], number, named)) in answers:
            fresh.setdefault(item["record"], {})[f"{item['repository']}#{number}"] = answers[key]
    for record, values in fresh.items():
        atomic(notes_path(record), dict(version=1, prs={**notes(record), **values}))
    return dict(asked=asked, error=error)


# name -> how to list its items, and optionally how to acknowledge one and how to ask the forges about them.
# An item is a dict with `source`, `id` (what --ack takes), `kind` and `text`; `cleared` names what
# overtook it. A source whose items only clear by themselves has no `ack`.
SOURCES = {
    "incident": dict(items=incident_items, ack=incident_ack, lookup=incident_lookup),
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


def prepare(config):
    """Write the count `status` shows. A failure is reported there and never reaches the controller."""
    try:
        value = summarize(listing(config))
    except Exception as exc:
        value = dict(at=time.time(), error=f"{type(exc).__name__}: {exc}"[:200])
    try:
        atomic(summary_path(), value)
    except OSError:
        pass


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
    try:
        atomic(summary_path(), summarize(value))
    except OSError:
        pass
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
        print(f"Looked up {looked['asked']} pull request{'s' if looked['asked'] != 1 else ''}"
              + (f"; {looked['error']}" if looked.get("error") else ""))
    if "acknowledged" in value or looked:
        print()
    for item in value["items"]:
        facts = [item["kind"], item.get("stage"), item.get("worker"), item.get("at"),
                 f"seen {item['count']}x" if item.get("count", 1) > 1 else None]
        subject = item["id"]
        if "pr" in item:
            on = "" if item["forge"] == "github" else f" on Forgejo {item['origin']}"
            subject = f"PR #{item['pr']}{on}  {pr_url(item, item['pr'])}"
        print(f"== {subject}  ({' · '.join(f for f in facts if f)})")
        if "pr" in item and item["id"] != str(item["pr"]):
            print(f"   ack id: {item['id']}")
        for line in item.get("text", "").splitlines():
            print("   " + line)
        for relation, label in (("subsumed_by", "subsumed by"), ("mentions", "mentions")):
            for named in (n for n in item.get("named", []) if n["relation"] == relation):
                if named.get("state"):
                    detail = "  ".join(str(v) for v in (
                        named["state"], (named.get("merged_at") or "")[:10], named.get("title"),
                        f"({named['author']})" if named.get("author") else "") if v)
                else:
                    detail = f"{pr_url(item, named['pr'])}  (--lookup for state and title)"
                print(f"   {label}: #{named['pr']}  {detail}")
        if item.get("unreadable"):
            print(f"   its forge answered HTTP {item['unreadable']} for this pull request"
                  + ("" if item["forge"] == "github" else "; --lookup reads Forgejo anonymously"))
        if item.get("transcript"):
            print("   transcript: " + item["transcript"])
        print()
    if not value["items"]:
        print("Nothing needs you.")
    if value.get("cleared"):
        print(f"Not listed: {len(value['cleared'])} record{'s' if len(value['cleared']) != 1 else ''} "
              "whose pull request merged, closed or moved to a new head.")
    unseen = sum("pr" in item and item.get("observed_at") is None for item in value["items"])
    if unseen and not looked:
        print(f"This host has no pull request state newer than the record for {unseen} of these; --lookup asks its forge.")
    if value["items"]:
        print(f"Acknowledge with: tauceti-fleet attention {name} --ack <PR number> | <ack id> | all")
    if not value.get("incidents"):
        print('Incident records are off for this host; the profile key "incidents" turns them on.')
