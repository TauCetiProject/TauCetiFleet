"""Find contributors whose sustained PR or review volume stopped or fell sharply.

Reads three sources into a local cache, each incrementally after the first run:

* GitHub TauCetiProject/TauCeti PRs through GraphQL only (ordered by update time, stopping at
  the previous run's high-water mark), with each PR's comments and reviews;
* Forgejo PRs (the issues listing with ``type=pulls``) and repository comments, both with
  ``since=``, read anonymously;
* the merge queue ledger (``inputs``), read-only over SSH, which is authoritative for landings.

A review is an account's comments and PR reviews on one PR it did not author, on one day: the AI
review scoreboards are PR comments, and every reply in a review thread is a separate PR review.

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
SUSTAINED_DAYS = 7        # active days within the peak window
RECENT_DAYS = 14
LAPSED_FRACTION = 0.25    # recent 14-day rate at most this share of the peak rate
STREAM_FLOOR = 5          # per-stream peak (per week) below which a stream drop is not reported
QUIET_DAYS = 5            # a sustained contributor silent this long has gone quiet, even if not yet lapsed
RECENT_LAPSE_DAYS = 42    # lapses whose last strong week ended within this many days are recent
# Labels saying who an open PR waits on; an author-side label wins over a project-side one.
AUTHOR_LABELS = {"awaiting-author", "ci-failed", "merge-conflict"}
PROJECT_LABELS = {"awaiting-review", "awaiting-CI", "ready-to-merge", "bors-staging"}
# The GitHub to Forgejo cutover; a drop inside it is flagged.
CUTOVER = ("2026-11-14", "2026-11-19")

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
        comments(first:100){totalCount pageInfo{hasNextPage endCursor} nodes{author{login} createdAt}}
        reviews(first:100){totalCount pageInfo{hasNextPage endCursor} nodes{author{login} submittedAt}}
      }
    }
  }
}"""
# The rest of one PR's comments or reviews, when a PR has more than one page of them.
GRAPHQL_REST = """
query($owner:String!,$name:String!,$number:Int!,$after:String){
  rateLimit{remaining resetAt}
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      %s(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{author{login} %s}}
    }
  }
}"""
# Comments and reviews fetched per PR before 2026-11-23 releases; such a list may be truncated.
LEGACY_CAPS = {"comments": 60, "reviews": 40}


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
    if bare in EXCLUDED or login.endswith("[bot]") or re.search(r"bors|copilot", bare, re.I):
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


def graphql(token, variables, *, query=GRAPHQL_PAGE, attempts=4):
    body = json.dumps(dict(query=query, variables=variables)).encode()
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
        # A cursor where a list continues; complete_github reads the rest.
        more={key: node[key]["pageInfo"]["endCursor"] for key in ("comments", "reviews")
              if node[key]["pageInfo"]["hasNextPage"]},
    )


TIME_FIELD = {"comments": "createdAt", "reviews": "submittedAt"}


def complete_github(prs, token, *, min_remaining=300):
    """Read the remaining pages of comments and reviews for PRs with more than one page."""
    for pr in prs.values():
        for key, cap in LEGACY_CAPS.items():
            # A cache entry from before pagination: refetch from the start if it may be truncated.
            if "more" not in pr and len(pr.get(key, [])) >= cap:
                pr.setdefault("refetch", []).append(key)
    pending = [(n, pr) for n, pr in prs.items() if pr.get("more") or pr.get("refetch")]
    for number, pr in pending:
        for key in sorted(set(pr.get("more", {})) | set(pr.get("refetch", []))):
            after = pr.get("more", {}).get(key)
            # Collected apart and stored only once complete, so a failed read keeps its marker.
            found = list(pr.get(key, [])) if after is not None else []
            while True:
                data = graphql(token, dict(owner=GITHUB_REPO[0], name=GITHUB_REPO[1], number=int(number), after=after),
                               query=GRAPHQL_REST % (key, TIME_FIELD[key]))
                connection = (data["repository"]["pullRequest"] or {}).get(key) or {}
                found += [[(n.get("author") or {}).get("login", ""), stamp(n[TIME_FIELD[key]])]
                          for n in connection.get("nodes", []) if n.get(TIME_FIELD[key])]
                more = connection.get("pageInfo", {}).get("hasNextPage")
                after = connection.get("pageInfo", {}).get("endCursor")
                if data["rateLimit"]["remaining"] < min_remaining:
                    raise FleetError(f"GitHub GraphQL quota below {min_remaining}; rerun after "
                                     f"{data['rateLimit']['resetAt']}")
                if not more:
                    break
            pr[key] = found
            pr.get("more", {}).pop(key, None)
            if key in pr.get("refetch", []):
                pr["refetch"].remove(key)
        pr.pop("refetch", None)
        pr["more"] = {}
    if pending:
        log(f"GitHub: completed comments and reviews of {len(pending)} PRs with more than one page")
    return len(pending)


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
    try:
        complete_github(prs, token, min_remaining=min_remaining)
    finally:
        atomic(path, cache)
    log(f"GitHub: {pages} pages read, {len(prs)} PRs cached")
    return cache


# ---- Forgejo --------------------------------------------------------------------------------

def forgejo_get(path, params, *, with_next=False):
    url = f"{FORGEJO}/api/v1/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "TauCetiFleet-contributors"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                value = json.loads(response.read())
                # Pages can hold fewer rows than the limit (hidden pending reviews), so follow the Link header.
                return (value, 'rel="next"' in (response.headers.get("Link") or "")) if with_next else value
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return (None, False) if with_next else None
            if exc.code < 500 or attempt == 3:
                raise FleetError(f"Forgejo read returned HTTP {exc.code}") from None
            time.sleep(3 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if attempt == 3:
                raise FleetError(f"Forgejo read unavailable ({type(exc).__name__})") from None
            time.sleep(3 * (attempt + 1))


def forgejo_pages(path, params, *, pause=0.2):
    page = 1
    while True:
        rows, more = forgejo_get(path, dict(params, limit=50, page=page), with_next=True)
        yield from rows or []
        if not rows or not more:
            return
        page += 1
        time.sleep(pause)


def sync_forgejo(*, review_reads=300):
    """PRs and comments since the last run; PR reviews for at most review_reads PRs, newest first."""
    path = cache_dir() / "forgejo.json"
    cache = read(path, {}) or {}
    prs, comments = cache.get("prs", {}), cache.get("comments", {})
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    since = cache.get("since")
    params = dict(type="pulls", state="all", **({"since": since} if since else {}))
    for row in forgejo_pages(f"repos/{FORGEJO_REPO}/issues", params):
        pull = row.get("pull_request") or {}
        previous = prs.get(str(row["number"]), {})
        prs[str(row["number"])] = dict(
            title=row["title"], state=row["state"], author=(row.get("user") or {}).get("login", ""),
            created=stamp(row["created_at"]), closed=stamp(row.get("closed_at")), merged=stamp(pull.get("merged_at")),
            labels=[label["name"] for label in row.get("labels") or []], draft=bool(pull.get("draft")),
            # Reviews are reread whenever the PR changed.
            reviews=previous.get("reviews", []), reviews_read=previous.get("reviews_read")
            if previous.get("updated") == row.get("updated_at") else None, updated=row.get("updated_at"))
    for row in forgejo_pages(f"repos/{FORGEJO_REPO}/issues/comments", dict(since=since) if since else {}):
        match = re.search(r"/pulls/(\d+)", row.get("html_url") or "")
        if not match:
            continue
        comments[str(row["id"])] = [match.group(1), (row.get("user") or {}).get("login", ""), stamp(row["created_at"])]
    # Forgejo has no repository-wide review listing, so reviews are read per PR within a budget.
    due = sorted((n for n, pr in prs.items() if pr.get("reviews_read") is None), key=int, reverse=True)
    for number in due[:review_reads]:
        found = []
        for review in forgejo_pages(f"repos/{FORGEJO_REPO}/pulls/{number}/reviews", {}):
            # Review requests and pending drafts are not contributions.
            if review.get("state") in ("REQUEST_REVIEW", "PENDING") or not review.get("submitted_at"):
                continue
            found.append([(review.get("user") or {}).get("login", ""), stamp(review["submitted_at"])])
            # Replies in a review's threads belong to the review; their authors and days count too.
            if review.get("comments_count"):
                for reply in forgejo_pages(f"repos/{FORGEJO_REPO}/pulls/{number}/reviews/{review['id']}/comments", {}):
                    if reply.get("created_at"):
                        found.append([(reply.get("user") or {}).get("login", ""), stamp(reply["created_at"])])
        prs[number]["reviews"] = found
        prs[number]["reviews_read"] = time.time()
    if len(due) > review_reads:
        log(f"Forgejo: {len(due) - review_reads} PRs still need their reviews read; later runs continue")
    cache = dict(prs=prs, comments=comments, since=started.isoformat().replace("+00:00", "Z"), synced_at=time.time())
    atomic(path, cache)
    log(f"Forgejo: {len(prs)} PRs and {len(comments)} comments cached")
    return cache


def forgejo_account_exists(login):
    return forgejo_get(f"users/{urllib.parse.quote(login)}", {}) is not None


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

    reviewed = set()

    def add(login, kind, forge, number, at):
        who = canonical(login, aliases)
        if not (who and at):
            return
        if kind == "review":
            # Replies in review threads are each a review; count one per account, PR and (UTC) day.
            key = (login, forge, int(number), int(at // 86400))
            if key in reviewed:
                return
            reviewed.add(key)
        events[who].append((at, kind, forge, int(number)))
        accounts[who].add(f"{forge}:{login}")

    queue_landed = set()
    queue_since = min((r["approved_at"] for r in queue.get("inputs", [])), default=math.inf)
    for row in sorted(queue.get("inputs", []), key=lambda r: r["landed_at"] or math.inf):
        forge = "forgejo" if row["forgejo"] else "github"
        # A PR can have several ledger rows (a cancelled input, then a landed one); it lands once.
        if row["landed_at"] and (forge, row["number"]) not in queue_landed:
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
    """The 28-day window ending on or before today with the most volume; (end day, total, active days)."""
    best = (None, 0, 0)
    day = first
    while day <= today:
        total = window_sum(counts, day, PEAK_DAYS)
        if total and total >= best[1]:
            active = sum(counts.get(day - timedelta(days=i), 0) > 0 for i in range(PEAK_DAYS))
            # Among equal totals the window with more active days is the sustained one.
            if total > best[1] or active > best[2]:
                best = (day, total, active)
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
        end, total, active_days = peak_window(volume, first, today)
        peak_rate = total / PEAK_DAYS * 7
        sustained = peak_rate >= SUSTAINED_PER_WEEK and active_days >= SUSTAINED_DAYS
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
        # A stream that fell this week: its last 7 days ran at a quarter of its own peak or less.
        stream_drops = [k for k in ("opened", "review")
                        if streams[k]["peak_per_week"] >= STREAM_FLOOR
                        and streams[k]["last7"] <= LAPSED_FRACTION * streams[k]["peak_per_week"]]
        quiet_days = (today - last_active).days
        weekly = defaultdict(lambda: dict(opened=0, landed=0, review=0, active_days=set()))
        for at, kind, _, _ in items:
            day = local_day(at, zone)
            week = (day - timedelta(days=day.weekday())).isoformat()
            weekly[week][kind] += 1
            weekly[week]["active_days"].add(day)
        owned = sorted(open_prs.get(who, []), key=lambda p: p["created"])
        for pr in owned:
            labels = set(pr["labels"])
            pr["waits_on"] = ("author" if labels & AUTHOR_LABELS else "project" if labels & PROJECT_LABELS
                              else "unlabelled")
        waits = {k: sum(pr["waits_on"] == k for pr in owned) for k in ("project", "author", "unlabelled")}
        # Show the PRs we could move first.
        shown = [pr for pr in owned if pr["waits_on"] == "project"][:5]
        shown += [pr for pr in owned if pr not in shown][:max(0, 5 - len(shown))]
        people.append(dict(
            identity=who, accounts=sorted(accounts[who]), sustained=sustained, lapsed=lapsed,
            stopped=lapsed and recent7 == 0, stream_drops=stream_drops, quiet_days=quiet_days,
            quiet=sustained and not lapsed and quiet_days >= QUIET_DAYS,
            recent_lapse=lapsed and drop is not None and (today - drop).days <= RECENT_LAPSE_DAYS,
            peak_per_week=round(peak_rate, 1), peak_window_end=end.isoformat() if end else None,
            peak_active_days=active_days, recent7=recent7, recent14=recent14, recent_per_week=round(recent_rate, 1),
            # The current rate is the lower of the last 7 and 14 days', so a stop this week counts in full.
            lost_per_week=round(max(0.0, peak_rate - min(recent_rate, recent7)), 1),
            drop_after=drop.isoformat() if drop else None, last_active=last_active.isoformat(),
            first_active=first.isoformat(), streams=streams,
            open_prs=dict(total=len(owned), github=sum(p["forge"] == "github" for p in owned),
                          forgejo=sum(p["forge"] == "forgejo" for p in owned),
                          labels=dict(sorted(_label_counts(owned).items(), key=lambda kv: -kv[1])[:8]),
                          waits_on=waits, oldest=shown),
            dropped_at_cutover=bool(drop and CUTOVER[0] <= drop.isoformat() <= CUTOVER[1]),
            weekly={w: dict(opened=v["opened"], landed=v["landed"], reviews=v["review"],
                            active_days=len(v["active_days"])) for w, v in sorted(weekly.items())},
        ))
    people.sort(key=lambda p: (not p["lapsed"], -p["lost_per_week"]))
    return dict(generated_at=now, today=today.isoformat(), timezone=timezone, people=people,
                thresholds=dict(peak_days=PEAK_DAYS, sustained_per_week=SUSTAINED_PER_WEEK,
                                sustained_days=SUSTAINED_DAYS, recent_days=RECENT_DAYS,
                                lapsed_fraction=LAPSED_FRACTION, stream_floor=STREAM_FLOOR,
                                quiet_days=QUIET_DAYS, recent_lapse_days=RECENT_LAPSE_DAYS))


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


def markdown(report, *, forgejo_accounts=None, include_all=False):
    t = report["thresholds"]
    zone = ZoneInfo(report["timezone"])
    generated = datetime.fromtimestamp(report["generated_at"], zone).strftime("%a %d %b %Y %H:%M %Z")
    lines = [
        f"# Lapsed contributors ({generated})", "",
        f"Volume is PRs opened plus reviews given (comments and reviews on other people's PRs), on both forges. "
        f"Sustained: at least {t['sustained_per_week']} per week over the best {t['peak_days']}-day window, with "
        f"activity on at least {t['sustained_days']} of its days. Lapsed: the last {t['recent_days']} days ran "
        f"at {t['lapsed_fraction']:.0%} of that peak rate or less. Stopped: lapsed with nothing in the last 7 days. "
        f"Landings come from the merge queue ledger, and from Bors before the ledger begins.", "",
    ]
    lapsed = [p for p in report["people"] if p["lapsed"]]
    recent = [p for p in lapsed if p["recent_lapse"]]
    earlier = [p for p in lapsed if not p["recent_lapse"]]
    quiet = sorted((p for p in report["people"] if p["quiet"]), key=lambda p: -p["peak_per_week"])
    partial = [p for p in report["people"]
               if p["sustained"] and not p["lapsed"] and not p["quiet"] and p["stream_drops"]]

    def signals(p):
        found = []
        if p["dropped_at_cutover"]:
            found.append("dropped at the Forgejo cutover")
        if forgejo_accounts is not None and not forgejo_accounts.get(p["identity"]):
            found.append("no Forgejo account")
        if p["open_prs"]["waits_on"]["project"]:
            found.append(f"{p['open_prs']['waits_on']['project']} PRs waiting on the project")
        if p["streams"]["review"]["peak_per_week"] >= STREAM_FLOOR and "review" in p["stream_drops"]:
            found.append("stopped reviewing")
        return "; ".join(found) or "none"

    def row(p):
        s = p["streams"]
        o = p["open_prs"]
        status = ("stopped" if p["stopped"] else "slowed" if p["lapsed"]
                  else f"quiet {p['quiet_days']} days")
        return (f"| {p['identity']} | {status} | {p['peak_per_week']} (to {p['peak_window_end']}) | "
                f"{p['recent_per_week']} | {p['lost_per_week']} | {s['opened']['peak_per_week']} / "
                f"{s['review']['peak_per_week']} / {s['landed']['peak_per_week']} | {p['drop_after']} | "
                f"{p['last_active']} | {o['total']} ({o['waits_on']['project']} / {o['waits_on']['author']}) | "
                f"{signals(p)} |")

    header = ("| who | status | peak volume/week | last 14 days/week | lost/week | peak opened / reviews / landed "
              "per week | last strong week ended | last active | open PRs (waiting on project / on author) | "
              "signals |")
    sep = "|---|---|---|---|---|---|---|---|---|---|"
    lines += [f"## Recently lapsed ({len(recent)})", "",
              f"Last strong week ended within {t['recent_lapse_days']} days.", "", header, sep, *map(row, recent), ""]
    if quiet:
        lines += [f"## Gone quiet ({len(quiet)})", "",
                  f"Sustained, not yet lapsed on the 14-day measure, but nothing for {t['quiet_days']} days or more.",
                  "", header, sep, *map(row, quiet), ""]
    lines += [f"## Lapsed earlier ({len(earlier)})", "", header, sep, *map(row, earlier), ""]
    if include_all:
        sustained = [p for p in report["people"] if p["sustained"]]
        lines += [f"## Every sustained contributor ({len(sustained)})", "",
                  "| who | peak volume/week | last 14 days/week | last 7 days | lapsed |", "|---|---|---|---|---|"]
        lines += [f"| {p['identity']} | {p['peak_per_week']} | {p['recent_per_week']} | {p['recent7']} | "
                  f"{'yes' if p['lapsed'] else 'no'} |" for p in sorted(sustained, key=lambda p: -p["peak_per_week"])]
        lines.append("")
    if partial:
        lines += ["## Still active, but one stream fell this week", "",
                  "The stream's last 7 days ran at a quarter of its own peak weekly rate or less.", "",
                  "| who | fallen streams | stream peak/week | stream last 7 days | volume peak/week | "
                  "volume last 7 days |", "|---|---|---|---|---|---|"]
        lines += [f"| {p['identity']} | {', '.join(p['stream_drops'])} | "
                  f"{' / '.join(str(p['streams'][k]['peak_per_week']) for k in p['stream_drops'])} | "
                  f"{' / '.join(str(p['streams'][k]['last7']) for k in p['stream_drops'])} | "
                  f"{p['peak_per_week']} | {p['recent7']} |" for p in partial]
        lines.append("")
    flagged = recent + quiet + earlier
    lines += ["## Open PRs of lapsed and quiet contributors", "",
              "Up to five each, those waiting on the project first. Waiting on the author: awaiting-author, "
              "ci-failed or merge-conflict; on the project: awaiting-review, awaiting-CI, ready-to-merge "
              "or bors-staging.", ""]
    for p in flagged:
        o = p["open_prs"]
        if not o["total"]:
            continue
        labels = ", ".join(f"{k} {v}" for k, v in o["labels"].items()) or "none"
        w = o["waits_on"]
        lines.append(f"- **{p['identity']}**: {o['total']} open, {w['project']} waiting on the project, "
                     f"{w['author']} on the author, {w['unlabelled']} unlabelled (labels: {labels})")
        for pr in o["oldest"]:
            opened = local_day(pr["created"], zone).isoformat()
            lines.append(f"  - {pr_url(pr['forge'], pr['number'])} {pr['title']} (opened {opened}, "
                         f"waiting on {pr['waits_on']})")
    lines += ["", "## Weekly series of lapsed and quiet contributors", "",
              "Weeks start on Monday; each cell is opened / landed / reviews / active days.", ""]
    weeks = sorted({w for p in flagged for w in p["weekly"]})[-10:]
    if weeks:
        lines.append("| who | " + " | ".join(weeks) + " |")
        lines.append("|---|" + "---|" * len(weeks))
        for p in flagged:
            cells = []
            for w in weeks:
                v = p["weekly"].get(w)
                cells.append(f"{v['opened']}/{v['landed']}/{v['reviews']}/{v['active_days']}" if v else "")
            lines.append(f"| {p['identity']} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def parser():
    p = argparse.ArgumentParser(prog="tauceti-fleet contributors",
                                description="Report contributors whose sustained PR or review volume lapsed")
    p.add_argument("--lapsed", action="store_true", help="report only lapsed contributors (the default)")
    p.add_argument("--all", dest="include_all", action="store_true",
                   help="also list every contributor with sustained volume")
    p.add_argument("--offline", action="store_true", help="use the cached sources without reading anything")
    p.add_argument("--skip", action="append", choices=("github", "forgejo", "queue"), default=[],
                   help="leave one source's cache as it is (repeatable)")
    p.add_argument("--queue-host", default=QUEUE_HOST, help="SSH host holding the queue ledger (read-only)")
    p.add_argument("--identities", help="JSON file: {\"aliases\": {login: identity}, \"exclude\": [logins]}")
    p.add_argument("--markdown", help="write the markdown report here instead of standard output")
    p.add_argument("--json", dest="json_path", help="also write the full JSON report here")
    p.add_argument("--timezone", default=TIMEZONE)
    p.add_argument("--no-account-check", action="store_true", help="skip the Forgejo account lookups")
    p.add_argument("--forgejo-review-reads", type=int, default=300,
                   help="read the reviews of at most this many changed Forgejo PRs per run (default 300)")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        skip = set(args.skip) | ({"github", "forgejo", "queue"} if args.offline else set())
        github = read(cache_dir() / "github.json", {}) if "github" in skip else sync_github()
        forgejo = (read(cache_dir() / "forgejo.json", {}) if "forgejo" in skip
                   else sync_forgejo(review_reads=args.forgejo_review_reads))
        queue = read(cache_dir() / "queue.json", {}) if "queue" in skip else sync_queue(args.queue_host)
        identities = read(args.identities or config_dir() / "contributors.json", {}) or {}
        aliases = dict(identities.get("aliases", {}))
        EXCLUDED.update(identities.get("exclude", []))
        events, accounts, open_prs = collect_events(github or {}, forgejo or {}, queue or {}, aliases)
        report = analyse(events, accounts, open_prs, timezone=args.timezone)
        known_accounts = read(cache_dir() / "forgejo-accounts.json", {}) or {}
        accounts_on_forgejo = known_accounts if args.offline and known_accounts else None
        if not args.offline and not args.no_account_check:
            accounts_on_forgejo = {}
            for p in report["people"]:
                if p["lapsed"] or p["quiet"] or (p["sustained"] and p["stream_drops"]):
                    logins = {a.split(":", 1)[1] for a in p["accounts"]}
                    accounts_on_forgejo[p["identity"]] = any(map(forgejo_account_exists, sorted(logins)))
                    p["forgejo_account"] = accounts_on_forgejo[p["identity"]]
            atomic(cache_dir() / "forgejo-accounts.json", accounts_on_forgejo)
        text = markdown(report, forgejo_accounts=accounts_on_forgejo, include_all=args.include_all)
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
