"""Find contributors whose sustained PR or review volume stopped or fell sharply.

Reads three sources into a local cache, each incrementally after the first run:

* GitHub TauCetiProject/TauCeti PRs through GraphQL only (ordered by update time, stopping at
  the previous run's high-water mark), with each PR's comments and reviews;
* Forgejo PRs (the issues listing with ``type=pulls``) and repository comments, both with
  ``since=``, read anonymously;
* the merge queue ledger (``inputs``), read-only over SSH, which is authoritative for landings.

Review volume is counted as comments and PR reviews an account posts on PRs it did not author:
the AI review scoreboards are PR comments, so GitHub PR reviews alone undercount them.

The report is markdown plus JSON. Nothing is posted anywhere.
"""

import argparse
import json
import math
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .store import FleetError, atomic, config_dir, data_dir, read

GITHUB_REPO = ("TauCetiProject", "TauCeti")
FORGEJO = "https://git.taucetiproject.org"
FORGEJO_REPO = "TauCetiProject/TauCeti"
QUEUE_HOST = "tauchonki2"
QUEUE_DB = "~/TauCetiQueue-data/queue.sqlite3"
TIMEZONE = "Australia/Sydney"
# Shared project, operator and automation accounts; never reported as contributors.
EXCLUDED = {
    "tauceti-ai-for-science", "kim-em", "tauceti-queue", "tauceti-redteam", "tauceti-review-bot",
    "github-actions", "dependabot", "ghost", "",
}

# Thresholds (see docs/contributors.md for the reasoning).
PEAK_DAYS = 28            # the sustained window
SUSTAINED_PER_WEEK = 10   # opened PRs + reviews per week across the peak window
SUSTAINED_WEEKS = 3       # weeks of the peak window with any activity
RECENT_DAYS = 14
LAPSED_FRACTION = 0.25    # recent 14-day rate at most this share of the peak rate
STREAM_FLOOR = 5          # per-stream peak (per week) below which a stream drop is not reported

GRAPHQL_PAGE = """
query($owner:String!,$name:String!,$after:String,$first:Int!){
  rateLimit{remaining resetAt}
  repository(owner:$owner,name:$name){
    pullRequests(first:$first,after:$after,orderBy:{field:UPDATED_AT,direction:DESC}){
      pageInfo{hasNextPage endCursor}
      nodes{
        number title state createdAt updatedAt closedAt mergedAt isDraft
        author{login}
        labels(first:20){nodes{name}}
        comments(first:60){totalCount nodes{author{login} createdAt}}
        reviews(first:40){totalCount nodes{author{login} submittedAt}}
      }
    }
  }
}"""


def log(message):
    print("contributors: " + message, file=sys.stderr, flush=True)


def stamp(text):
    return None if not text else datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def cache_dir():
    return data_dir() / "contributors"


def canonical(login, aliases):
    login = (login or "").strip()
    bare = re.sub(r"(\[bot\])$", "", login)
    if login in aliases:
        return aliases[login]
    if bare in aliases:
        return aliases[bare]
    if bare in EXCLUDED or login.endswith("[bot]") and bare.startswith("tauceti-"):
        return None
    # A contributor's own automation account (alice-bot) counts as alice.
    return re.sub(r"-bot$", "", bare) or None


# ---- GitHub ---------------------------------------------------------------------------------

def github_token():
    try:
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        token = ""
    if not token:
        raise FleetError("No GitHub token: log in with gh auth login")
    return token


def graphql(token, variables, *, attempts=4):
    body = json.dumps(dict(query=GRAPHQL_PAGE, variables=variables)).encode()
    for attempt in range(attempts):
        req = urllib.request.Request("https://api.github.com/graphql", data=body, method="POST", headers={
            "Authorization": "bearer " + token, "User-Agent": "TauCetiFleet-contributors"})
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                value = json.loads(response.read())
            if value.get("errors"):
                raise FleetError("GitHub GraphQL error: " + str(value["errors"])[:300])
            return value["data"]
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if attempt == attempts - 1:
                raise FleetError(f"GitHub GraphQL unavailable ({type(exc).__name__})") from None
            time.sleep(5 * (attempt + 1))


def pr_record(node):
    def people(nodes, key):
        return [[(n.get("author") or {}).get("login", ""), stamp(n[key])] for n in nodes if n.get(key)]
    return dict(
        title=node["title"], state=node["state"], author=(node.get("author") or {}).get("login", ""),
        created=stamp(node["createdAt"]), updated=node["updatedAt"], closed=stamp(node["closedAt"]),
        merged=stamp(node["mergedAt"]), draft=node["isDraft"],
        labels=[n["name"] for n in node["labels"]["nodes"]],
        comments=people(node["comments"]["nodes"], "createdAt"),
        reviews=people(node["reviews"]["nodes"], "submittedAt"),
    )


def sync_github(*, token=None, page=50, min_remaining=300):
    path = cache_dir() / "github.json"
    cache = read(path, {}) or {}
    prs = cache.get("prs", {})
    high_water = cache.get("high_water")
    token = token or github_token()
    after, newest, pages = cache.get("resume_cursor"), cache.get("resume_newest"), 0
    while True:
        data = graphql(token, dict(owner=GITHUB_REPO[0], name=GITHUB_REPO[1], after=after, first=page))
        connection = data["repository"]["pullRequests"]
        nodes = connection["nodes"]
        if nodes and newest is None:
            newest = nodes[0]["updatedAt"]
        for node in nodes:
            prs[str(node["number"])] = pr_record(node)
        pages += 1
        after = connection["pageInfo"]["endCursor"]
        done = not connection["pageInfo"]["hasNextPage"] or (
            high_water is not None and nodes and nodes[-1]["updatedAt"] < high_water)
        if done:
            cache = dict(prs=prs, high_water=newest or high_water, synced_at=time.time())
        else:
            # A long first crawl resumes where it stopped.
            cache = dict(prs=prs, high_water=high_water, resume_cursor=after, resume_newest=newest,
                         synced_at=cache.get("synced_at"))
        if done or pages % 20 == 0:
            atomic(path, cache)
        if pages % 20 == 0:
            log(f"GitHub: {pages} pages, {len(prs)} PRs cached, quota {data['rateLimit']['remaining']}")
        if done:
            break
        if data["rateLimit"]["remaining"] < min_remaining:
            atomic(path, cache)
            raise FleetError(f"GitHub GraphQL quota below {min_remaining}; rerun after {data['rateLimit']['resetAt']}")
    log(f"GitHub: {pages} pages read, {len(prs)} PRs cached")
    return cache


# ---- Forgejo --------------------------------------------------------------------------------

def forgejo_get(path, params):
    url = f"{FORGEJO}/api/v1/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "TauCetiFleet-contributors"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if attempt == 3:
                raise FleetError(f"Forgejo read unavailable ({type(exc).__name__})") from None
            time.sleep(3 * (attempt + 1))


def forgejo_pages(path, params, *, pause=0.2):
    page = 1
    while True:
        rows = forgejo_get(path, dict(params, limit=50, page=page))
        if not rows:
            return
        yield from rows
        if len(rows) < 50:
            return
        page += 1
        time.sleep(pause)


def sync_forgejo():
    path = cache_dir() / "forgejo.json"
    cache = read(path, {}) or {}
    prs, comments = cache.get("prs", {}), cache.get("comments", {})
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    since = cache.get("since")
    params = dict(type="pulls", state="all", **({"since": since} if since else {}))
    for row in forgejo_pages(f"repos/{FORGEJO_REPO}/issues", params):
        pull = row.get("pull_request") or {}
        prs[str(row["number"])] = dict(
            title=row["title"], state=row["state"], author=row["user"]["login"], created=stamp(row["created_at"]),
            closed=stamp(row.get("closed_at")), merged=stamp(pull.get("merged_at")),
            labels=[label["name"] for label in row.get("labels") or []], draft=bool(pull.get("draft")))
    for row in forgejo_pages(f"repos/{FORGEJO_REPO}/issues/comments", dict(since=since) if since else {}):
        match = re.search(r"/pulls/(\d+)", row.get("html_url") or "")
        if not match:
            continue
        comments[str(row["id"])] = [match.group(1), row["user"]["login"], stamp(row["created_at"])]
    cache = dict(prs=prs, comments=comments, since=started.isoformat().replace("+00:00", "Z"), synced_at=time.time())
    atomic(path, cache)
    log(f"Forgejo: {len(prs)} PRs and {len(comments)} comments cached")
    return cache


def forgejo_account_exists(login):
    try:
        forgejo_get(f"users/{urllib.parse.quote(login)}", {})
        return True
    except FleetError:
        return False


# ---- Merge queue ----------------------------------------------------------------------------

QUEUE_QUERY = ("select number, source_identity like '%forgejo%' as forgejo, author, state, approved_at, landed_at, "
               "title from inputs")


def sync_queue(host=QUEUE_HOST, db=QUEUE_DB):
    command = f"sqlite3 -readonly -json {db} {shlex.quote(QUEUE_QUERY)}"
    try:
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                                capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FleetError(f"Queue ledger read failed ({type(exc).__name__})") from None
    if result.returncode:
        raise FleetError("Queue ledger read failed: " + result.stderr.strip()[:200])
    rows = json.loads(result.stdout or "[]")
    cache = dict(inputs=rows, synced_at=time.time())
    atomic(cache_dir() / "queue.json", cache)
    log(f"Queue: {len(rows)} inputs")
    return cache


# ---- Analysis -------------------------------------------------------------------------------

def local_day(at, zone):
    return datetime.fromtimestamp(at, zone).date()


def collect_events(github, forgejo, queue, aliases):
    """Per identity: lists of (day-stamp, kind, forge, number) events."""
    events = defaultdict(list)
    accounts = defaultdict(set)
    open_prs = defaultdict(list)

    def add(login, kind, forge, number, at):
        who = canonical(login, aliases)
        if who and at:
            events[who].append((at, kind, forge, int(number)))
            accounts[who].add(f"{forge}:{login}")

    queue_landed = set()
    queue_since = min((r["approved_at"] for r in queue.get("inputs", [])), default=math.inf)
    for row in queue.get("inputs", []):
        forge = "forgejo" if row["forgejo"] else "github"
        if row["landed_at"]:
            queue_landed.add((forge, row["number"]))
            add(row["author"], "landed", forge, row["number"], row["landed_at"])
    for forge, source in (("github", github), ("forgejo", forgejo)):
        prs = source.get("prs", {})
        for number, pr in prs.items():
            add(pr["author"], "opened", forge, number, pr["created"])
            # Landings before the queue ledger begins come from the forge (Bors closes with a title prefix).
            landed_at = pr.get("merged") or (pr["closed"] if pr["title"].startswith("[Merged by Bors]") else None)
            if landed_at and (forge, int(number)) not in queue_landed and landed_at < queue_since:
                add(pr["author"], "landed", forge, number, landed_at)
            if pr["state"].lower() == "open":
                who = canonical(pr["author"], aliases)
                if who:
                    open_prs[who].append(dict(forge=forge, number=int(number), title=pr["title"],
                                              created=pr["created"], labels=pr.get("labels", []),
                                              draft=pr.get("draft", False)))
            for login, at in pr.get("comments", []) + pr.get("reviews", []):
                if login != pr["author"]:
                    add(login, "review", forge, number, at)
        if forge == "forgejo":
            for number, login, at in source.get("comments", {}).values():
                pr = prs.get(str(number))
                if pr and login != pr["author"]:
                    add(login, "review", forge, number, at)
    return events, accounts, open_prs


def daily(events, zone, kinds):
    counts = defaultdict(int)
    for at, kind, _, _ in events:
        if kind in kinds:
            counts[local_day(at, zone)] += 1
    return counts


def window_sum(counts, end, days):
    return sum(counts.get(end - timedelta(days=i), 0) for i in range(days))


def peak_window(counts, first, today):
    """The 28-day window ending on or before today with the most volume; (end day, total, active weeks)."""
    best = (None, 0, 0)
    day = first
    while day <= today:
        total = window_sum(counts, day, PEAK_DAYS)
        if total > best[1]:
            weeks = sum(window_sum(counts, day - timedelta(days=7 * w), 7) > 0 for w in range(PEAK_DAYS // 7))
            best = (day, total, weeks)
        day += timedelta(days=1)
    return best


def analyse(events, accounts, open_prs, *, now=None, timezone=TIMEZONE):
    zone = ZoneInfo(timezone)
    now = time.time() if now is None else now
    today = local_day(now, zone)
    people = []
    for who, items in events.items():
        volume = daily(items, zone, {"opened", "review"})
        if not volume:
            continue
        first = min(volume)
        end, total, weeks = peak_window(volume, first, today)
        peak_rate = total / PEAK_DAYS * 7
        sustained = peak_rate >= SUSTAINED_PER_WEEK and weeks >= SUSTAINED_WEEKS
        recent7 = window_sum(volume, today, 7)
        recent14 = window_sum(volume, today, RECENT_DAYS)
        recent_rate = recent14 / RECENT_DAYS * 7
        last_active = max(volume)
        streams = {}
        for kind in ("opened", "review", "landed"):
            counts = daily(items, zone, {kind})
            k_end, k_total, _ = peak_window(counts, min(counts), today) if counts else (None, 0, 0)
            streams[kind] = dict(
                total=sum(counts.values()), peak_per_week=round(k_total / PEAK_DAYS * 7, 1),
                peak_window_end=k_end.isoformat() if k_end else None,
                last7=window_sum(counts, today, 7), last14=window_sum(counts, today, RECENT_DAYS),
                last=max(counts).isoformat() if counts else None)
        # The drop: the last active day of the last 7 days that still ran at half the peak rate or more.
        drop = None
        day = today
        while day >= first:
            if window_sum(volume, day, 7) >= 0.5 * peak_rate:
                drop = max(d for d in volume if d <= day)
                break
            day -= timedelta(days=1)
        lapsed = sustained and recent_rate <= LAPSED_FRACTION * peak_rate
        stream_drops = [k for k in ("opened", "review")
                        if streams[k]["peak_per_week"] >= STREAM_FLOOR
                        and streams[k]["last14"] / RECENT_DAYS * 7 <= LAPSED_FRACTION * streams[k]["peak_per_week"]]
        weekly = defaultdict(lambda: dict(opened=0, landed=0, review=0, active_days=set()))
        for at, kind, _, _ in items:
            day = local_day(at, zone)
            week = (day - timedelta(days=day.weekday())).isoformat()
            weekly[week][kind] += 1
            weekly[week]["active_days"].add(day)
        owned = sorted(open_prs.get(who, []), key=lambda p: p["created"])
        people.append(dict(
            identity=who, accounts=sorted(accounts[who]), sustained=sustained, lapsed=lapsed,
            stopped=lapsed and recent7 == 0, stream_drops=stream_drops,
            peak_per_week=round(peak_rate, 1), peak_window_end=end.isoformat() if end else None,
            peak_active_weeks=weeks, recent7=recent7, recent14=recent14, recent_per_week=round(recent_rate, 1),
            lost_per_week=round(max(0.0, peak_rate - recent_rate), 1),
            drop_after=drop.isoformat() if drop else None, last_active=last_active.isoformat(),
            first_active=first.isoformat(), streams=streams,
            open_prs=dict(total=len(owned), github=sum(p["forge"] == "github" for p in owned),
                          forgejo=sum(p["forge"] == "forgejo" for p in owned),
                          labels=dict(sorted(_label_counts(owned).items(), key=lambda kv: -kv[1])[:8]),
                          oldest=owned[:5]),
            weekly={w: dict(opened=v["opened"], landed=v["landed"], reviews=v["review"],
                            active_days=len(v["active_days"])) for w, v in sorted(weekly.items())},
        ))
    people.sort(key=lambda p: (not p["lapsed"], -p["lost_per_week"]))
    return dict(generated_at=now, today=today.isoformat(), timezone=timezone, people=people,
                thresholds=dict(peak_days=PEAK_DAYS, sustained_per_week=SUSTAINED_PER_WEEK,
                                sustained_weeks=SUSTAINED_WEEKS, recent_days=RECENT_DAYS,
                                lapsed_fraction=LAPSED_FRACTION, stream_floor=STREAM_FLOOR))


def _label_counts(prs):
    counts = defaultdict(int)
    for pr in prs:
        for label in pr["labels"]:
            counts[label] += 1
    return counts


# ---- Output ---------------------------------------------------------------------------------

def pr_url(forge, number):
    if forge == "forgejo":
        return f"{FORGEJO}/{FORGEJO_REPO}/pulls/{number}"
    return f"https://github.com/{'/'.join(GITHUB_REPO)}/pull/{number}"


def markdown(report, *, forgejo_accounts=None):
    t = report["thresholds"]
    zone = ZoneInfo(report["timezone"])
    generated = datetime.fromtimestamp(report["generated_at"], zone).strftime("%a %d %b %Y %H:%M %Z")
    lines = [
        f"# Lapsed contributors ({generated})", "",
        f"Volume is PRs opened plus reviews given (comments and reviews on other people's PRs), on both forges. "
        f"Sustained: at least {t['sustained_per_week']} per week over the best {t['peak_days']}-day window, with "
        f"activity in at least {t['sustained_weeks']} of its 4 weeks. Lapsed: the last {t['recent_days']} days ran "
        f"at {t['lapsed_fraction']:.0%} of that peak rate or less. Stopped: lapsed with nothing in the last 7 days. "
        f"Landings come from the merge queue ledger, and from Bors before the ledger begins.", "",
    ]
    lapsed = [p for p in report["people"] if p["lapsed"]]
    partial = [p for p in report["people"] if p["sustained"] and not p["lapsed"] and p["stream_drops"]]

    def row(p):
        s = p["streams"]
        status = "stopped" if p["stopped"] else "slowed"
        account = "" if forgejo_accounts is None else (
            "yes" if forgejo_accounts.get(p["identity"]) else "no")
        return (f"| {p['identity']} | {status} | {p['peak_per_week']} (to {p['peak_window_end']}) | "
                f"{p['recent_per_week']} | {p['lost_per_week']} | {s['opened']['peak_per_week']} / "
                f"{s['review']['peak_per_week']} / {s['landed']['peak_per_week']} | {p['drop_after']} | "
                f"{p['last_active']} | {p['open_prs']['github']} / {p['open_prs']['forgejo']} | {account} |")

    header = ("| who | status | peak volume/week | last 14 days/week | lost/week | peak opened / reviews / landed "
              "per week | last strong week ended | last active | open PRs GitHub / Forgejo | Forgejo account |")
    sep = "|---|---|---|---|---|---|---|---|---|---|"
    lines += [f"## Lapsed ({len(lapsed)})", "", header, sep, *map(row, lapsed), ""]
    if partial:
        lines += ["## Still active, but one stream lapsed", "", "| who | lapsed streams | peak volume/week | "
                  "last 14 days/week |", "|---|---|---|---|"]
        lines += [f"| {p['identity']} | {', '.join(p['stream_drops'])} | {p['peak_per_week']} | "
                  f"{p['recent_per_week']} |" for p in partial]
        lines.append("")
    lines += ["## Open PRs of lapsed contributors (oldest five)", ""]
    for p in lapsed:
        o = p["open_prs"]
        if not o["total"]:
            continue
        labels = ", ".join(f"{k} {v}" for k, v in o["labels"].items()) or "none"
        lines.append(f"- **{p['identity']}**: {o['total']} open (labels: {labels})")
        for pr in o["oldest"]:
            opened = local_day(pr["created"], zone).isoformat()
            lines.append(f"  - {pr_url(pr['forge'], pr['number'])} {pr['title']} (opened {opened})")
    lines += ["", "## Weekly series of lapsed contributors", "",
              "Weeks start on Monday; each cell is opened / landed / reviews / active days.", ""]
    weeks = sorted({w for p in lapsed for w in p["weekly"]})[-10:]
    if weeks:
        lines.append("| who | " + " | ".join(weeks) + " |")
        lines.append("|---|" + "---|" * len(weeks))
        for p in lapsed:
            cells = []
            for w in weeks:
                v = p["weekly"].get(w)
                cells.append(f"{v['opened']}/{v['landed']}/{v['reviews']}/{v['active_days']}" if v else "")
            lines.append(f"| {p['identity']} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def parser():
    p = argparse.ArgumentParser(prog="tauceti-fleet contributors",
                                description="Report contributors whose sustained PR or review volume lapsed")
    p.add_argument("--lapsed", action="store_true", help="print only lapsed contributors (the default report)")
    p.add_argument("--offline", action="store_true", help="use the cached sources without reading anything")
    p.add_argument("--skip", action="append", choices=("github", "forgejo", "queue"), default=[],
                   help="leave one source's cache as it is (repeatable)")
    p.add_argument("--queue-host", default=QUEUE_HOST, help="SSH host holding the queue ledger (read-only)")
    p.add_argument("--identities", help="JSON file: {\"aliases\": {login: identity}, \"exclude\": [logins]}")
    p.add_argument("--markdown", help="write the markdown report here instead of standard output")
    p.add_argument("--json", dest="json_path", help="also write the full JSON report here")
    p.add_argument("--timezone", default=TIMEZONE)
    p.add_argument("--no-account-check", action="store_true", help="skip the Forgejo account lookups")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        skip = set(args.skip) | ({"github", "forgejo", "queue"} if args.offline else set())
        github = read(cache_dir() / "github.json", {}) if "github" in skip else sync_github()
        forgejo = read(cache_dir() / "forgejo.json", {}) if "forgejo" in skip else sync_forgejo()
        queue = read(cache_dir() / "queue.json", {}) if "queue" in skip else sync_queue(args.queue_host)
        identities = read(args.identities or config_dir() / "contributors.json", {}) or {}
        aliases = dict(identities.get("aliases", {}))
        EXCLUDED.update(identities.get("exclude", []))
        events, accounts, open_prs = collect_events(github or {}, forgejo or {}, queue or {}, aliases)
        report = analyse(events, accounts, open_prs, timezone=args.timezone)
        accounts_on_forgejo = None
        if not args.offline and not args.no_account_check:
            accounts_on_forgejo = {}
            for p in report["people"]:
                if p["lapsed"]:
                    logins = {a.split(":", 1)[1] for a in p["accounts"]}
                    accounts_on_forgejo[p["identity"]] = any(map(forgejo_account_exists, sorted(logins)))
                    p["forgejo_account"] = accounts_on_forgejo[p["identity"]]
        text = markdown(report, forgejo_accounts=accounts_on_forgejo)
        if args.json_path:
            atomic(args.json_path, report)
        if args.markdown:
            with open(args.markdown, "w") as stream:
                stream.write(text)
        else:
            sys.stdout.write(text)
        return 0
    except FleetError as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
