"""Tell operators when a day's Cloudflare usage would make an expensive bill.

A systemd timer on the operator's control host runs `tauceti-fleet spend-alert` every hour. Once
per UTC day, after the day has had two hours to settle in Cloudflare's analytics, it reads the
previous day's Durable Object SQLite rows and R2 operations from the GraphQL Analytics API. When
rows written or R2 Class A operations exceed their thresholds it sends one message naming the
largest namespaces and buckets with an estimated cost at list prices. Each day is checked and
reported at most once; a failed read or delivery is retried the next hour.

The credential is a Cloudflare API token from `--token-file`, or else the wrangler OAuth login
on this account (its `account:read` scope covers GraphQL Analytics). Wrangler's access token
lasts an hour and other programs share the file, so this only reads it: when it has expired, or
Cloudflare rejects it, `wrangler whoami` is run once to refresh it.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from .store import FleetError, atomic, locked, read, state_dir

ACCOUNT = "ec2169bdf033f56b009956d4b64ba8ef"  # the TauCeti Cloudflare account
API = "https://api.cloudflare.com/client/v4"
ROWS_WRITTEN = 30_000_000
CLASS_A = 3_000_000
SETTLE = 2 * 60 * 60  # analytics for a day are complete a few minutes after it ends
KEEP_DAYS = 60  # days whose totals are kept
CATCH_UP = 3  # a day still unread or undelivered is retried this many days later
TOP = 5
UNIT = "tauceti-spend-alert"
WRANGLER_CONFIG = Path.home() / ".config/.wrangler/config/default.toml"
TOKEN_MARGIN = 120  # treat a token this close to expiry as expired

# List prices (Workers Paid), before the monthly free allowances.
PRICE_ROWS_WRITTEN = 1.00 / 1e6
PRICE_ROWS_READ = 1.00 / 1e9
PRICE_CLASS_A = 4.50 / 1e6
PRICE_CLASS_B = 0.36 / 1e6

# R2 operation classes (https://developers.cloudflare.com/r2/pricing/). Anything else is Class B.
CLASS_A_ACTIONS = {
    "ListBuckets", "PutBucket", "ListObjects", "PutObject", "CopyObject", "CompleteMultipartUpload",
    "CreateMultipartUpload", "LifecycleStorageTierTransition", "ListMultipartUploads", "UploadPart",
    "UploadPartCopy", "ListParts", "PutBucketEncryption", "PutBucketCors", "PutBucketLifecycleConfiguration",
}
FREE_ACTIONS = {"DeleteObject", "DeleteObjects", "DeleteBucket", "AbortMultipartUpload"}

LIMIT = 10000
QUERY = """query($a:String!,$d:Date!){viewer{accounts(filter:{accountTag:$a}){
 durableObjectsPeriodicGroups(limit:10000,filter:{date_geq:$d,date_leq:$d}){sum{rowsRead rowsWritten} dimensions{namespaceId}}
 r2OperationsAdaptiveGroups(limit:10000,filter:{date_geq:$d,date_leq:$d}){sum{requests} dimensions{bucketName actionType}}
}}}"""


class AuthError(FleetError):
    pass


def expiry(value):
    """Seconds since the epoch for wrangler's `2026-11-25T14:01:43.987Z`, or None."""
    try:
        moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo else None


def read_wrangler(path):
    """(token, expires_at) from wrangler's OAuth file; FleetError if it is unreadable."""
    try:
        config = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise FleetError(f"Cannot read the wrangler login ({type(error).__name__})") from None
    token = config.get("oauth_token")
    if not isinstance(token, str) or not token:
        raise FleetError("The wrangler login holds no OAuth token; run `wrangler login`")
    return token, expiry(config.get("expiration_time"))


def refresh_wrangler():
    """Let wrangler refresh its own login; its output (account details) is not kept."""
    try:
        subprocess.run(["wrangler", "whoami"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"spend alert: wrangler whoami failed ({type(error).__name__})", file=sys.stderr, flush=True)


class Credential:
    """A bearer token from a token file, or from wrangler's login with at most one refresh per run."""

    def __init__(self, token_file=None, wrangler_config=WRANGLER_CONFIG, refresh=refresh_wrangler, clock=time.time):
        self.token_file, self.wrangler_config, self.refresh, self.clock = token_file, wrangler_config, refresh, clock
        self.refreshed = False

    def token(self):
        token = self.read_token()
        # A token is sent in a header: never let a malformed one reach an error message.
        if not token.isascii() or not token.isprintable() or any(c.isspace() for c in token):
            raise AuthError("The Cloudflare token is malformed")
        return token

    def read_token(self):
        if self.token_file is not None:
            token = Path(self.token_file).read_text().strip()
            if not token:
                raise FleetError("The Cloudflare token file is empty")
            return token
        token, expires = read_wrangler(self.wrangler_config)
        if expires is not None and expires - self.clock() > TOKEN_MARGIN:
            return token
        if self.renew():
            token, expires = read_wrangler(self.wrangler_config)
            if expires is not None and expires - self.clock() > TOKEN_MARGIN:
                return token
        raise AuthError("The wrangler login has expired and `wrangler whoami` did not refresh it")

    def renew(self):
        """Refresh once per run; False when this credential cannot be refreshed or already was."""
        if self.token_file is not None or self.refreshed:
            return False
        self.refreshed = True
        self.refresh()
        return True


def call(credential, url, body=None, opener=None):
    """One Cloudflare API request; a rejected credential is refreshed once and the request repeated."""
    from .ci_overview import status_ssl_context

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None  # never carry the bearer token to another location

    opener = opener or urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=status_ssl_context()))
    while True:
        request = urllib.request.Request(
            url, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST",
            headers={"Authorization": "Bearer " + credential.token(), "Content-Type": "application/json",
                     "User-Agent": "TauCetiFleet/spend-alert"})
        try:
            with opener.open(request, timeout=60) as response:
                value = json.loads(response.read(16 * 1024 * 1024))
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (401, 403) and credential.renew():
                continue
            raise FleetError(f"Cloudflare answered HTTP {error.code}") from None
        errors = value.get("errors") if isinstance(value, dict) else None
        if errors and any("auth" in json.dumps(e).lower() for e in errors) and credential.renew():
            continue
        return value


def fetch(credential, day, account=ACCOUNT, opener=None):
    """The day's analytics groups and the account's Durable Object namespace names."""
    value = call(credential, f"{API}/graphql", dict(query=QUERY, variables=dict(a=account, d=day)), opener)
    if not isinstance(value, dict) or value.get("errors") or not isinstance(value.get("data"), dict):
        messages = [str(e.get("message"))[:200] for e in (value or {}).get("errors") or [] if isinstance(e, dict)]
        raise FleetError("Cloudflare analytics query failed: " + ("; ".join(messages) or "no data"))
    accounts = value["data"]["viewer"]["accounts"]
    if len(accounts) != 1:
        raise FleetError("Cloudflare analytics returned no data for the account")
    if any(len(accounts[0][key]) >= LIMIT for key in ("durableObjectsPeriodicGroups", "r2OperationsAdaptiveGroups")):
        raise FleetError("Cloudflare analytics returned a full page; the totals would be incomplete")
    names = {}
    try:
        listing = call(credential, f"{API}/accounts/{account}/workers/durable_objects/namespaces?per_page=1000",
                       opener=opener)
        for row in listing.get("result") or []:
            if row.get("script") and row.get("class"):
                names[row["id"]] = f"{row['script']}/{row['class']}"
    except (FleetError, OSError, ValueError, AttributeError, TypeError) as error:
        # Names only label the message; the identifiers still say which namespace it is.
        print(f"spend alert: namespace names unavailable ({type(error).__name__})", file=sys.stderr, flush=True)
    return accounts[0], names


def summarize(groups, names):
    """Daily totals and per-namespace and per-bucket counts."""
    written, read_, class_a, class_b = {}, {}, {}, {}
    for group in groups["durableObjectsPeriodicGroups"]:
        name = names.get(group["dimensions"]["namespaceId"], group["dimensions"]["namespaceId"])
        written[name] = written.get(name, 0) + int(group["sum"]["rowsWritten"])
        read_[name] = read_.get(name, 0) + int(group["sum"]["rowsRead"])
    for group in groups["r2OperationsAdaptiveGroups"]:
        action, bucket = group["dimensions"]["actionType"], group["dimensions"]["bucketName"] or "(account)"
        if action in FREE_ACTIONS:
            continue
        target = class_a if action in CLASS_A_ACTIONS else class_b
        target[bucket] = target.get(bucket, 0) + int(group["sum"]["requests"])
    totals = dict(rows_written=sum(written.values()), rows_read=sum(read_.values()),
                  class_a=sum(class_a.values()), class_b=sum(class_b.values()))
    totals["cost"] = round(totals["rows_written"] * PRICE_ROWS_WRITTEN + totals["rows_read"] * PRICE_ROWS_READ
                           + totals["class_a"] * PRICE_CLASS_A + totals["class_b"] * PRICE_CLASS_B, 2)
    return dict(totals=totals, written=written, read=read_, class_a=class_a, class_b=class_b)


def count(n):
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= size:
            return f"{n / size:.1f}{suffix}"
    return str(n)


def top(counts, price):
    rows = sorted(((n, k) for k, n in counts.items() if n), key=lambda row: (-row[0], row[1]))[:TOP]
    return "; ".join(f"{k} {count(n)} (${n * price:,.2f})" for n, k in rows) or "none"


def over(summary, rows_written, class_a):
    totals = summary["totals"]
    return totals["rows_written"] > rows_written or totals["class_a"] > class_a


def line(day, summary):
    t = summary["totals"]
    return (f"Cloudflare usage on {day}: DO rows written {count(t['rows_written'])}, read {count(t['rows_read'])}; "
            f"R2 Class A {count(t['class_a'])}, Class B {count(t['class_b'])}; about ${t['cost']:,.2f} at list prices")


def message(day, summary, rows_written, class_a):
    t = summary["totals"]
    above = [f"{label} {count(n)} (threshold {count(limit)})"
             for label, n, limit in (("Durable Object rows written", t["rows_written"], rows_written),
                                     ("R2 Class A operations", t["class_a"], class_a)) if n > limit]
    return (
        f":warning: **Cloudflare usage on {day} (UTC)** is above the daily alert threshold: {', '.join(above)}. "
        f"That day's Durable Object rows and R2 operations cost about ${t['cost']:,.2f} at list prices, "
        f"about ${t['cost'] * 30:,.0f} a month at this rate.\n"
        f"- DO rows written {count(t['rows_written'])}: {top(summary['written'], PRICE_ROWS_WRITTEN)}\n"
        f"- R2 Class A {count(t['class_a'])}: {top(summary['class_a'], PRICE_CLASS_A)}\n"
        f"- DO rows read {count(t['rows_read'])}: {top(summary['read'], PRICE_ROWS_READ)}\n"
        f"- R2 Class B {count(t['class_b'])}: {top(summary['class_b'], PRICE_CLASS_B)}"
    )


def due_day(now):
    """The last UTC day whose analytics have settled."""
    settled = datetime.datetime.fromtimestamp(now - SETTLE, datetime.UTC).date()
    return (settled - datetime.timedelta(days=1)).isoformat()


def pending_days(state, now, key="days"):
    """Settled days not yet checked, oldest first: the last `CATCH_UP` days, never before the first run."""
    due = datetime.date.fromisoformat(due_day(now))
    first = state.setdefault("since", due.isoformat())
    days = state.get(key) or {}
    candidates = (due - datetime.timedelta(days=back) for back in range(CATCH_UP - 1, -1, -1))
    return [d.isoformat() for d in candidates if d.isoformat() >= first and d.isoformat() not in days]


def check(state, day, now, load, send, rows_written=ROWS_WRITTEN, class_a=CLASS_A, key="days"):
    """Check `day` once: returns True when the day is settled (reported, or below the thresholds).
    `load(day)` returns the day's summary and may raise; `send(text)` returns whether it was delivered.
    A reply lost after delivery repeats the message next run: a message is never dropped."""
    days = state.setdefault(key, {})
    if day in days:
        return True
    summary = load(day)
    print(line(day, summary), flush=True)
    alerted = over(summary, rows_written, class_a)
    if alerted and not send(message(day, summary, rows_written, class_a)):
        return False
    days[day] = dict(checked_at=now, alerted=alerted, **summary["totals"])
    for old in sorted(days)[:-KEEP_DAYS]:
        days[old] = dict(alerted=days[old].get("alerted"))  # keep the record that the day was checked
    return True


def main(argv=None):
    from .fleet_alert import zulip

    parser = argparse.ArgumentParser(prog="tauceti-fleet spend-alert", description=__doc__)
    parser.add_argument("--zulip-config", type=Path,
                        help="mode-0600 bot configuration; without it, messages go only to standard output")
    parser.add_argument("--token-file", type=Path,
                        help="mode-0600 file holding a Cloudflare API token with Account Analytics read; "
                             "without it, wrangler's login is used")
    parser.add_argument("--account", default=ACCOUNT)
    parser.add_argument("--rows-written-threshold", type=int, default=ROWS_WRITTEN)
    parser.add_argument("--class-a-threshold", type=int, default=CLASS_A)
    parser.add_argument("--day", help="with --dry-run, check this UTC day (YYYY-MM-DD) instead of the last settled one")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the totals and any message; send nothing and record nothing")
    args = parser.parse_args(argv)
    for path in (args.zulip_config, args.token_file):
        if path is not None and path.stat().st_mode & 0o077:
            parser.error(f"{path} must be private (chmod 600)")
    if args.day is not None:
        if not args.dry_run:
            parser.error("--day needs --dry-run: only settled days are recorded")
        try:
            datetime.date.fromisoformat(args.day)
        except ValueError:
            parser.error("--day must be YYYY-MM-DD")
    config = None if args.zulip_config is None or args.dry_run else read(args.zulip_config)
    if args.zulip_config is not None and not args.dry_run and not isinstance(config, dict):
        parser.error("the Zulip configuration must be a JSON object")
    credential = Credential(args.token_file)
    now = time.time()

    def load(day):
        return summarize(*fetch(credential, day, args.account))

    if args.dry_run:
        day = args.day or due_day(now)
        summary = load(day)
        print(line(day, summary))
        if over(summary, args.rows_written_threshold, args.class_a_threshold):
            print(message(day, summary, args.rows_written_threshold, args.class_a_threshold))
        else:
            print("below the thresholds: no message")
        return 0

    def send(text):
        print(text, flush=True)
        if config is None:
            return True  # recorded under `logged`, so a later configured run still sends the day's message
        try:
            zulip(config, text)
            return True
        except (OSError, ValueError, KeyError) as error:
            print("spend alert: Zulip delivery failed (" + type(error).__name__ + "); retrying next run",
                  file=sys.stderr, flush=True)
            return False

    path = state_dir() / "spend-alert.json"
    settled = True
    with locked(path.with_suffix(".lock")):
        state = read(path, {})
        # Days checked without a delivery target are kept apart from delivered ones.
        key = "days" if config is not None else "logged"
        for day in pending_days(state, now, key):
            try:
                settled &= check(state, day, now, load, send, args.rows_written_threshold, args.class_a_threshold,
                                 key)
            except FleetError as error:
                print(f"spend alert: {day}: {error}; retrying next run", file=sys.stderr, flush=True)
                settled = False
            atomic(path, state)
        atomic(path, state)
    return 0 if settled else 1


def install(zulip_config=None, token_file=None, rows_written=None, class_a=None):
    """Write and start this account's hourly spend check; it runs the installed Fleet release."""
    from .host import unit_argument, units
    from .process import run

    args = [Path.home() / ".local/bin/tauceti-fleet", "spend-alert"]
    if zulip_config:
        args += ["--zulip-config", Path(zulip_config).expanduser().resolve()]
    if token_file:
        args += ["--token-file", Path(token_file).expanduser().resolve()]
    if rows_written is not None:
        args += ["--rows-written-threshold", str(rows_written)]
    if class_a is not None:
        args += ["--class-a-threshold", str(class_a)]
    service = (
        "[Unit]\nDescription=Alert operators when a day's Cloudflare usage is unusually expensive\n\n"
        "[Service]\nType=oneshot\nExecStart=" + " ".join(unit_argument(a) for a in args) + "\n"
    )
    # PATH must reach `wrangler` (and node) for the login refresh.
    for key in ("PATH", "TAUCETI_FLEET_CONFIG", "TAUCETI_FLEET_STATE", "TAUCETI_FLEET_DATA"):
        if key in os.environ:
            service += "Environment=" + unit_argument(key + "=" + os.environ[key]) + "\n"
    service += "TimeoutStartSec=600\nUMask=0077\nMemoryMax=512M\n"
    timer = ("[Unit]\nDescription=Check yesterday's Cloudflare usage every hour until it is reported\n\n"
             "[Timer]\nOnCalendar=hourly\nRandomizedDelaySec=5min\nPersistent=true\n\n"
             "[Install]\nWantedBy=timers.target\n")
    (units() / (UNIT + ".service")).write_text(service)
    (units() / (UNIT + ".timer")).write_text(timer)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", UNIT + ".timer"])
    return dict(installed=UNIT + ".timer", zulip=bool(zulip_config), token_file=bool(token_file))
